import asyncio
import base64
import json

import pytest
import websockets

from agentcall.providers.base import ProviderError
from agentcall.providers.gemini import GeminiLive
from agentcall.tasks.models import TOOLS, ProviderConfig


@pytest.fixture
async def gemini_server():
    messages, sockets = [], []

    async def handler(ws):
        sockets.append(ws)
        assert ws.request.headers["x-goog-api-key"] == "test-key"
        assert "key=" not in ws.request.path
        try:
            async for raw in ws:
                event = json.loads(raw)
                messages.append(event)
                if "setup" in event:
                    await ws.send(json.dumps({"setupComplete": {}}))
        except websockets.exceptions.ConnectionClosed:
            pass

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        yield f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/live", messages, sockets


async def test_setup_audio_tools_context_and_automatic_continuation(gemini_server):
    endpoint, messages, sockets = gemini_server
    config = ProviderConfig(
        provider="gemini", options={"inputAudioTranscription": {}, "outputAudioTranscription": {}}
    )
    provider = GeminiLive(config, "test-key", endpoint=endpoint)
    try:
        await provider.open("task in Chinese", TOOLS)
        setup = messages[0]["setup"]
        assert setup["model"] == "models/gemini-3.8-live"
        assert setup["generationConfig"]["responseModalities"] == ["AUDIO"]
        assert (
            setup["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"][
                "voiceName"
            ]
            == "Aoede"
        )
        declarations = setup["tools"][0]["functionDeclarations"]
        assert declarations[0]["parametersJsonSchema"] == TOOLS[0]["parameters"]
        assert all(t["behavior"] == "BLOCKING" for t in declarations)
        await provider.send_audio(bytes(94))
        await provider.start_response()
        await provider.update_context("additional fact")
        await provider.start_response()  # no second request after tool results
        await asyncio.sleep(0.01)
        assert messages[1]["realtimeInput"]["audio"]["mimeType"] == "audio/pcm;rate=16000"
        assert base64.b64decode(messages[1]["realtimeInput"]["audio"]["data"]) == bytes(94)
        assert "Begin" in messages[2]["realtimeInput"]["text"]
        assert "additional fact" in messages[3]["realtimeInput"]["text"]
        assert len(messages) == 4
        ws = sockets[0]
        await ws.send(
            json.dumps(
                {
                    "toolCall": {
                        "functionCalls": [
                            {"id": "t1", "name": "send_dtmf", "args": {"digits": "1"}},
                            {
                                "id": "t2",
                                "name": "finish_task",
                                "args": {"status": "partial", "result": {}},
                            },
                        ]
                    }
                }
            )
        )
        events = provider.events()
        assert (await anext(events))["kind"] == "response_started"
        first, second = await anext(events), await anext(events)
        assert json.loads(first["arguments"]) == {"digits": "1"}
        assert second["call_id"] == "t2"
        await provider.tool_result("t1", {"ok": True})
        await asyncio.sleep(0.01)
        assert messages[-1]["toolResponse"]["functionResponses"] == [
            {"id": "t1", "name": "send_dtmf", "response": {"ok": True}}
        ]
        await events.aclose()
    finally:
        await provider.close()


async def test_all_content_parts_turns_transcription_and_in_progress(gemini_server):
    endpoint, _, sockets = gemini_server
    provider = GeminiLive(ProviderConfig(provider="gemini"), "test-key", endpoint=endpoint)
    await provider.open("task", [])
    ws = sockets[0]
    blob = {
        "inlineData": {
            "mimeType": "audio/pcm;rate=24000",
            "data": base64.b64encode(bytes(26)).decode(),
        }
    }
    await ws.send(
        json.dumps(
            {
                "serverContent": {
                    "modelTurn": {
                        "parts": [blob, {"text": "text"}, blob, {"text": "hidden", "thought": True}]
                    },
                    "inputTranscription": {"text": "user words", "finished": True},
                    "outputTranscription": {"text": "model words"},
                    "generationComplete": True,
                    "turnComplete": True,
                    "interactionStatus": "IN_PROGRESS",
                }
            }
        )
    )
    events = provider.events()
    got = [await anext(events) for _ in range(8)]
    assert [e["kind"] for e in got] == [
        "response_started",
        "audio",
        "text",
        "audio",
        "transcript",
        "transcript",
        "turn",
        "turn",
    ]
    assert got[-1]["event"] == "turn_complete_in_progress"
    assert got[4]["role"] == "user" and got[5]["delta"]
    await ws.send(
        json.dumps({"serverContent": {"turnComplete": True, "interactionStatus": "IDLE"}})
    )
    assert (await anext(events))["kind"] == "response_done"
    await ws.send(
        json.dumps({"serverContent": {"modelTurn": {"parts": [blob]}, "turnComplete": True}})
    )
    assert (await anext(events))["response_id"] == "gemini-turn-2"
    assert (await anext(events))["kind"] == "audio"
    assert (await anext(events))["kind"] == "response_done"
    await events.aclose()
    await provider.close()


async def test_interrupted_turn_discards_stale_audio_and_allows_new_turn(gemini_server):
    endpoint, _, sockets = gemini_server
    provider = GeminiLive(ProviderConfig(provider="gemini"), "test-key", endpoint=endpoint)
    await provider.open("task", [])
    blob = {"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": "AAAAAA=="}}
    events = provider.events()
    try:
        await sockets[0].send(json.dumps({"serverContent": {"modelTurn": {"parts": [blob]}}}))
        old = await anext(events)
        assert old["kind"] == "response_started"
        assert (await anext(events))["kind"] == "audio"
        await sockets[0].send(
            json.dumps(
                {
                    "serverContent": {
                        "interrupted": True,
                        "modelTurn": {"parts": [blob]},
                        "turnComplete": True,
                    }
                }
            )
        )
        assert (await anext(events))["event"] == "interrupted"
        ended = await anext(events)
        assert ended["status"] == "cancelled" and ended["response_id"] == old["response_id"]
        await sockets[0].send(json.dumps({"serverContent": {"modelTurn": {"parts": [blob]}}}))
        new = await asyncio.wait_for(anext(events), 0.5)
        assert new["kind"] == "response_started" and new["response_id"] != old["response_id"]
        assert (await anext(events))["kind"] == "audio"
    finally:
        await events.aclose()
        await provider.close()


async def test_api_tool_cancellation_and_goaway_are_normalized(gemini_server):
    endpoint, messages, sockets = gemini_server
    provider = GeminiLive(ProviderConfig(provider="gemini"), "test-key", endpoint=endpoint)
    await provider.open("task", [])
    ws = sockets[0]
    await ws.send(json.dumps({"toolCallCancellation": {"ids": ["cancelled"]}}))
    await ws.send(
        json.dumps(
            {
                "toolCall": {
                    "functionCalls": [
                        {"id": "cancelled", "name": "send_dtmf", "args": {"digits": "1"}},
                        {"id": "valid", "name": "send_dtmf", "args": {"digits": "2"}},
                    ]
                }
            }
        )
    )
    events = provider.events()
    assert (await anext(events))["call_ids"] == ["cancelled"]
    assert (await anext(events))["kind"] == "response_started"
    assert (await anext(events))["call_id"] == "valid"
    await provider.tool_result("cancelled", {"ok": True})
    await provider.tool_result("valid", {"ok": True})
    await asyncio.sleep(0.01)
    assert len(messages) == 2  # only valid response, plus setup
    await ws.send(json.dumps({"goAway": {"timeLeft": "10s"}}))
    assert (await anext(events))["kind"] == "session_ending"
    await ws.close()
    with pytest.raises(ProviderError, match="connection ended"):
        await anext(events)
    await provider.close()


@pytest.mark.parametrize(
    "blob",
    [
        {"mimeType": "audio/pcm;rate=16000", "data": "AAA="},
        {"mimeType": "audio/pcm;rate=24000", "data": "bad"},
        {"mimeType": "audio/pcm;rate=24000", "data": "AA=="},
    ],
)
async def test_invalid_audio_format_or_payload_fails(gemini_server, blob):
    endpoint, _, sockets = gemini_server
    provider = GeminiLive(ProviderConfig(provider="gemini"), "test-key", endpoint=endpoint)
    await provider.open("task", [])
    await sockets[0].send(
        json.dumps({"serverContent": {"modelTurn": {"parts": [{"inlineData": blob}]}}})
    )
    events = provider.events()
    await anext(events)
    with pytest.raises(ProviderError):
        await anext(events)
    await provider.close()


@pytest.mark.parametrize(
    "options",
    [
        {"api_key": "secret"},
        {"endpoint": "ws://arbitrary"},
        {"realtimeInputConfig": {"automaticActivityDetection": {"disabled": True}}},
        {"realtimeInputConfig": {"activityHandling": "NO_INTERRUPTION"}},
        {"turn_detection": {}},
    ],
)
def test_gemini_rejects_credential_endpoint_and_local_turn_policy(options):
    with pytest.raises(ValueError):
        ProviderConfig(provider="gemini", options=options)


async def test_missing_key_and_setup_failure_never_expose_key():
    provider = GeminiLive(ProviderConfig(provider="gemini"), "", timeout=0.05)
    with pytest.raises(ProviderError, match="not configured"):
        await provider.open("task", [])

    async def reject(ws):
        await ws.recv()
        await ws.send(json.dumps({"error": {"message": "echo test-key"}}))

    async with websockets.serve(reject, "127.0.0.1", 0) as server:
        endpoint = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        provider = GeminiLive(ProviderConfig(provider="gemini"), "test-key", endpoint=endpoint)
        with pytest.raises(ProviderError) as error:
            await provider.open("task", [])
        assert "test-key" not in str(error.value)
        assert provider.closing


async def test_setup_timeout_closes_connection_and_runtime_error_is_redacted():
    async def stall(ws):
        await ws.recv()
        await ws.wait_closed()

    async with websockets.serve(stall, "127.0.0.1", 0) as server:
        endpoint = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        provider = GeminiLive(
            ProviderConfig(provider="gemini"), "test-key", endpoint=endpoint, timeout=0.03
        )
        with pytest.raises(ProviderError, match="timed out"):
            await provider.open("task", [])
        assert provider.closing

    async def runtime_error(ws):
        setup = json.loads(await ws.recv())["setup"]
        assert "tools" not in setup  # empty tool lists are omitted
        await ws.send(json.dumps({"setupComplete": {}}))
        await ws.send(
            json.dumps(
                {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "echo test-key"}}
            )
        )
        await ws.wait_closed()

    async with websockets.serve(runtime_error, "127.0.0.1", 0) as server:
        endpoint = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        provider = GeminiLive(ProviderConfig(provider="gemini"), "test-key", endpoint=endpoint)
        await provider.open("task", [])
        event = await anext(provider.events())
        assert event == {"kind": "error", "code": "400", "type": "INVALID_ARGUMENT"}
        assert "test-key" not in json.dumps(event)
        await provider.close()
