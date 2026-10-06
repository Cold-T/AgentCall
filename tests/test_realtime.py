import asyncio
import base64
import json

import pytest
import websockets

from agentcall.providers.base import ProviderError
from agentcall.providers.openai import OpenAIRealtime
from agentcall.tasks.models import TOOLS, ProviderConfig


@pytest.fixture
async def realtime_server():
    messages = []
    sockets = []

    async def handler(ws):
        sockets.append(ws)
        await ws.send(json.dumps({"type": "session.created", "session": {}}))
        try:
            async for raw in ws:
                event = json.loads(raw)
                messages.append(event)
                if event["type"] == "session.update":
                    await ws.send(
                        json.dumps({"type": "session.updated", "session": event["session"]})
                    )
        except websockets.exceptions.ConnectionClosed:
            pass

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}/realtime", messages, sockets


async def test_ga_session_audio_context_tools_and_event_normalization(realtime_server):
    endpoint, messages, sockets = realtime_server
    provider = OpenAIRealtime(ProviderConfig(), "test-key", endpoint=endpoint, timeout=0.5)
    try:
        await provider.open("task instructions", TOOLS)
        session = messages[0]["session"]
        assert session["type"] == "realtime" and session["output_modalities"] == ["audio"]
        assert session["audio"]["input"]["turn_detection"]["interrupt_response"]
        assert session["audio"]["input"]["transcription"] == {"model": "gpt-4o-mini-transcribe"}
        assert session["audio"]["output"]["format"] == {"type": "audio/pcm", "rate": 24000}
        assert len(session["tools"]) == 3
        await provider.send_audio(bytes(18))
        await provider.update_context("extra facts")
        await provider.tool_result("call1", {"ok": True})
        await provider.start_response()
        await asyncio.sleep(0.02)
        assert base64.b64decode(messages[1]["audio"]) == bytes(18)
        assert messages[2]["item"]["role"] == "system"
        assert messages[3]["item"]["call_id"] == "call1"
        assert messages[4]["type"] == "response.create"
        ws = sockets[0]
        await ws.send(
            json.dumps(
                {
                    "type": "response.output_audio.delta",
                    "response_id": "r1",
                    "item_id": "audio1",
                    "content_index": 0,
                    "delta": base64.b64encode(bytes(38)).decode(),
                }
            )
        )
        await ws.send(
            json.dumps(
                {
                    "type": "response.function_call_arguments.done",
                    "response_id": "r1",
                    "call_id": "tool1",
                    "name": "send_dtmf",
                    "arguments": '{"digits":"1"}',
                }
            )
        )
        await ws.send(
            json.dumps({"type": "response.output_audio_transcript.done", "transcript": "hello"})
        )
        events = provider.events()
        audio = await anext(events)
        assert audio["pcm"] == bytes(38) and audio["item_id"] == "audio1"
        assert (await anext(events))["name"] == "send_dtmf"
        assert (await anext(events))["text"] == "hello"
        await provider.truncate_audio("audio1", 0, 150)
        await asyncio.sleep(0.02)
        assert messages[-1] == {
            "type": "conversation.item.truncate",
            "item_id": "audio1",
            "content_index": 0,
            "audio_end_ms": 150,
        }
        await events.aclose()
    finally:
        await provider.close()


async def test_invalid_audio_or_disconnect_is_reported(realtime_server):
    endpoint, _, sockets = realtime_server
    provider = OpenAIRealtime(ProviderConfig(), "test-key", endpoint=endpoint)
    await provider.open("test", TOOLS)
    await sockets[0].send(json.dumps({"type": "response.output_audio.delta", "delta": "???"}))
    with pytest.raises(ProviderError, match="invalid PCM"):
        await anext(provider.events())
    await sockets[0].close()
    with pytest.raises(ProviderError):
        await anext(provider.events())
    await provider.close()


async def test_session_format_mismatch_prevents_dial():
    async def handler(ws):
        event = json.loads(await ws.recv())
        event["session"]["audio"]["output"]["format"]["rate"] = 16000
        await ws.send(json.dumps({"type": "session.updated", "session": event["session"]}))
        await ws.wait_closed()

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        endpoint = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        provider = OpenAIRealtime(ProviderConfig(), "test-key", endpoint=endpoint)
        with pytest.raises(ProviderError, match="unsupported audio"):
            await provider.open("test", TOOLS)
        assert provider.closing


async def test_no_key_is_rejected_before_connection():
    provider = OpenAIRealtime(ProviderConfig(), "")
    with pytest.raises(ProviderError, match="not configured"):
        await provider.open("test", TOOLS)


@pytest.mark.parametrize(
    "options",
    [
        {"turn_detection": None},
        {"turn_detection": {"type": "server_vad", "interrupt_response": False}},
        {"api_key": "hidden"},
    ],
)
def test_options_cannot_disable_api_interruption_or_supply_credentials(options):
    with pytest.raises(ValueError):
        ProviderConfig(options=options)
