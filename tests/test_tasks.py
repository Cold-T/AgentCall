import asyncio
import base64
import json
import socket
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
import pytest_asyncio
import websockets
from conftest import DEVICE, Phone

from agentcall.api.app import create_app
from agentcall.audio.transport import SCOAudio
from agentcall.bluetooth.hfp import HFPConnection
from agentcall.providers.gemini import GeminiLive
from agentcall.providers.openai import OpenAIRealtime
from agentcall.service.backend import Backend
from agentcall.service.config import Config
from agentcall.storage.store import Store
from agentcall.tasks.manager import TaskManager
from agentcall.tasks.models import DEFAULT_BACKGROUND, ProviderConfig, TaskInput, instructions


@pytest.mark.parametrize("background", [None, "", "The office reference is ABC."])
def test_background_defaults_only_when_omitted(background):
    body = {"device": DEVICE, "number": "12345", "goal": "Confirm office hours"}
    if background is not None:
        body["background"] = background
    task = TaskInput(**body)
    prompt = instructions({"input": task.model_dump(), "config": {"language": "English"}})
    assert (DEFAULT_BACKGROUND in prompt) == (background is None)
    assert "Confirm office hours" in prompt
    if background:
        assert background in prompt
    else:
        assert task.background == (DEFAULT_BACKGROUND if background is None else "")


def test_explicit_default_background_is_not_duplicated():
    task = TaskInput(
        device=DEVICE, number="12345", goal="Confirm hours", background=DEFAULT_BACKGROUND
    )
    prompt = instructions({"input": task.model_dump(), "config": {"language": "中文"}})
    assert prompt.count(DEFAULT_BACKGROUND) == 1


async def wait_for(predicate, seconds=4):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.005)


def payload(**overrides):
    return {
        "device": "11:22:33:44:55:66",
        "number": "123",
        "goal": "Get the answer",
        "result_schema": {
            "type": "object",
            "properties": {"answer": {"type": "integer"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
        "max_call_seconds": 2,
        **overrides,
    }


class Rig:
    def __init__(self):
        self.mode = "complete"
        self.answer = True
        self.busy = False
        self.model_messages = []
        self.model_sockets = []
        self.output = bytearray()
        self.first_output_at = None
        self.jobs = []
        self.peers = []
        self.session_configs = []
        self.hangup_output_sizes = []
        self.response_count = 0
        self.closing_release = asyncio.Event()

    def tool_outputs(self):
        return [
            {"call_id": e["item"]["call_id"], "result": json.loads(e["item"]["output"])}
            for e in self.model_messages
            if e.get("item", {}).get("type") == "function_call_output"
        ]

    async def websocket(self, ws):
        self.model_sockets.append(ws)
        await ws.send(json.dumps({"type": "session.created", "session": {}}))
        local_responses = 0
        try:
            async for raw in ws:
                event = json.loads(raw)
                self.model_messages.append(event)
                if event["type"] == "session.update":
                    self.session_configs.append(event["session"])
                    await ws.send(
                        json.dumps({"type": "session.updated", "session": event["session"]})
                    )
                elif event["type"] == "response.create":
                    local_responses += 1
                    self.response_count += 1
                    if local_responses == 1:
                        await self.respond(ws)
                    elif local_responses == 2 and self.mode == "silent_hangup":
                        await self.closing_release.wait()
                        await ws.send(
                            json.dumps({"type": "response.created", "response": {"id": "r2"}})
                        )
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "response.output_audio.delta",
                                    "response_id": "r2",
                                    "delta": base64.b64encode(b"\x01\x02" * 2400).decode(),
                                }
                            )
                        )
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "response.output_item.done",
                                    "response_id": "r2",
                                    "item": {
                                        "type": "function_call",
                                        "call_id": "hangup2",
                                        "name": "hangup",
                                        "arguments": '{"reason":"Goodbye spoken"}',
                                    },
                                }
                            )
                        )
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "response.done",
                                    "response": {"id": "r2", "status": "completed"},
                                }
                            )
                        )
        except websockets.exceptions.ConnectionClosed:
            pass

    async def respond(self, ws):
        async def send(event):
            await ws.send(json.dumps(event))

        await send({"type": "response.created", "response": {"id": "r1"}})
        samples = (np.sin(np.arange(4800) * 2 * np.pi * 440 / 24000) * 9000).astype("<i2").tobytes()
        offset = 0
        for length in [100, 338, 2000, 416, 6746]:
            await send(
                {
                    "type": "response.output_audio.delta",
                    "response_id": "r1",
                    "delta": base64.b64encode(samples[offset : offset + length]).decode(),
                }
            )
            offset += length
        if self.mode == "disconnect":
            await ws.close()
            return
        await send(
            {"type": "response.output_audio_transcript.done", "transcript": "The answer is 42."}
        )

        async def tool(call_id, name, arguments):
            item = {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps(arguments),
            }
            await send({"type": "response.output_item.done", "response_id": "r1", "item": item})

        await tool("digits1", "send_dtmf", {"digits": "12*#"})
        await send(
            {
                "type": "response.function_call_arguments.done",
                "response_id": "r1",
                "call_id": "digits1",
                "name": "send_dtmf",
                "arguments": '{"digits":"12*#"}',
            }
        )
        if self.mode == "bad_tools":
            await tool("bad1", "send_dtmf", {"digits": "1\rATD999;"})
            await tool(
                "bad2", "finish_task", {"status": "completed", "result": {"answer": "wrong type"}}
            )
            await tool("bad3", "arbitrary_command", {})
        else:
            await tool(
                "finish1",
                "finish_task",
                {
                    "status": "partial" if self.mode == "partial" else "completed",
                    "result": {"answer": 42},
                },
            )
        if self.mode not in ("hold", "bad_tools"):
            if self.mode != "silent_hangup":
                await send(
                    {
                        "type": "response.output_audio.delta",
                        "response_id": "r1",
                        "delta": base64.b64encode(b"\x01\x02" * 2400).decode(),
                    }
                )
            await tool("hangup1", "hangup", {"reason": "Task finished"})
        await send({"type": "response.done", "response": {"id": "r1", "status": "completed"}})

    def setup_audio(self, device):
        if device in self.backend.audio:
            return
        host, peer = socket.socketpair(type=socket.SOCK_SEQPACKET)
        peer.setblocking(False)
        self.peers.append(peer)
        self.backend.audio[device] = SCOAudio(host, 1, mtu=48)

        async def feed():
            try:
                while True:
                    await asyncio.get_running_loop().sock_sendall(peer, b"\x10\x01" * 24)
                    await asyncio.sleep(0.003)
            except OSError:
                pass

        async def capture():
            try:
                while data := await asyncio.get_running_loop().sock_recv(peer, 4096):
                    if self.first_output_at is None:
                        self.first_output_at = asyncio.get_running_loop().time()
                    self.output.extend(data)
            except OSError:
                pass

        self.jobs.extend([asyncio.create_task(feed()), asyncio.create_task(capture())])

    async def drive_phone(self):
        position = 0
        while True:
            for command in self.phone.commands[position:]:
                position += 1
                if command.startswith("ATD") and command not in self.phone.rejected:
                    self.phone.initial = "0,2,0"
                    await self.phone.send("+CIEV: 2,2\r\n")
                    await asyncio.sleep(0.02)
                    if self.busy:
                        self.phone.initial = "0,0,0"
                        await self.phone.send("BUSY\r\n")
                    elif self.answer:
                        self.phone.initial = "1,0,0"
                        await self.phone.send("+CIEV: 1,1\r\n+CIEV: 2,0\r\n")
                elif command == "AT+CHUP":
                    self.hangup_output_sizes.append(len(self.output))
                    self.phone.initial = "0,0,0"
                    await self.phone.send("+CIEV: 1,0\r\n+CIEV: 2,0\r\n")
            await asyncio.sleep(0.002)


class GeminiRig(Rig):
    def tool_outputs(self):
        return [
            {"call_id": item["id"], "result": item["response"]}
            for e in self.model_messages
            for item in e.get("toolResponse", {}).get("functionResponses", [])
        ]

    async def websocket(self, ws):
        self.model_sockets.append(ws)
        tool_results = {}
        started = False
        try:
            async for raw in ws:
                event = json.loads(raw)
                self.model_messages.append(event)
                if "setup" in event:
                    self.session_configs.append(event["setup"])
                    await ws.send(json.dumps({"setupComplete": {}}))
                elif (
                    "Begin the task" in event.get("realtimeInput", {}).get("text", "")
                    and not started
                ):
                    started = True
                    self.response_count += 1
                    self.jobs.append(asyncio.create_task(self.respond_gemini(ws, tool_results)))
                elif "toolResponse" in event:
                    for result in event["toolResponse"]["functionResponses"]:
                        tool_results.setdefault(result["id"], asyncio.Event()).set()
        except websockets.exceptions.ConnectionClosed:
            pass

    async def respond_gemini(self, ws, results):
        async def send(event):
            await ws.send(json.dumps(event))

        async def tool(call_id, name, args, duplicate=False):
            call = {"id": call_id, "name": name, "args": args}
            await send({"toolCall": {"functionCalls": [call]}})
            if duplicate:
                await send({"toolCall": {"functionCalls": [call]}})
            await asyncio.wait_for(results.setdefault(call_id, asyncio.Event()).wait(), 1)

        samples = (np.sin(np.arange(4800) * 2 * np.pi * 440 / 24000) * 9000).astype("<i2").tobytes()
        offset = 0
        for length in [100, 338, 2000, 416, 6746]:
            await send(
                {
                    "serverContent": {
                        "modelTurn": {
                            "parts": [
                                {
                                    "inlineData": {
                                        "mimeType": "audio/pcm;rate=24000",
                                        "data": base64.b64encode(
                                            samples[offset : offset + length]
                                        ).decode(),
                                    }
                                }
                            ]
                        }
                    }
                }
            )
            offset += length
        if self.mode == "disconnect":
            await ws.close()
            return
        await send(
            {
                "serverContent": {
                    "inputTranscription": {"text": "Question"},
                    "outputTranscription": {"text": "The answer is 42."},
                }
            }
        )
        await tool("digits1", "send_dtmf", {"digits": "12*#"}, duplicate=True)
        if self.mode == "bad_tools":
            await tool("bad1", "send_dtmf", {"digits": "1\rATD999;"})
            await tool(
                "bad2", "finish_task", {"status": "completed", "result": {"answer": "wrong type"}}
            )
            await tool("bad3", "arbitrary_command", {})
        else:
            await tool(
                "finish1",
                "finish_task",
                {
                    "status": "partial" if self.mode == "partial" else "completed",
                    "result": {"answer": 42},
                },
            )
        if self.mode not in ("hold", "bad_tools"):
            if self.mode != "silent_hangup":
                await send(
                    {
                        "serverContent": {
                            "modelTurn": {
                                "parts": [
                                    {
                                        "inlineData": {
                                            "mimeType": "audio/pcm;rate=24000",
                                            "data": base64.b64encode(b"\x01\x02" * 2400).decode(),
                                        }
                                    }
                                ]
                            }
                        }
                    }
                )
            await tool("hangup1", "hangup", {"reason": "Task finished"})
            if self.mode == "silent_hangup":
                await self.closing_release.wait()
                await send(
                    {
                        "serverContent": {
                            "modelTurn": {
                                "parts": [
                                    {
                                        "inlineData": {
                                            "mimeType": "audio/pcm;rate=24000",
                                            "data": base64.b64encode(b"\x01\x02" * 2400).decode(),
                                        }
                                    }
                                ]
                            }
                        }
                    }
                )
                await tool("hangup2", "hangup", {"reason": "Goodbye spoken"})
        if self.mode == "cancel_hangup":
            await send({"toolCallCancellation": {"ids": ["hangup1"]}})
        await send({"serverContent": {"generationComplete": True}})
        await asyncio.sleep(0.01)
        await send({"serverContent": {"turnComplete": True, "interactionStatus": "IDLE"}})


@pytest_asyncio.fixture(params=["openai", "gemini"])
async def rig(monkeypatch, request):
    rig = Rig() if request.param == "openai" else GeminiRig()
    rig.provider = request.param
    config = Config(
        answer_timeout_seconds=0.15,
        audio_timeout_seconds=0.15,
        hangup_timeout_seconds=0.3,
        model_connect_seconds=0.3,
        provider=ProviderConfig(provider=request.param),
    )
    store = Store(":memory:")
    backend = Backend(config, store)
    rig.backend = backend

    async def close():
        pass

    backend.bluez = SimpleNamespace(close=close)
    monkeypatch.setattr(backend, "ensure_audio", rig.setup_audio)
    host, peer = socket.socketpair()
    phone = Phone(peer)
    rig.phone = phone
    connection = HFPConnection(host, DEVICE, backend.emit, timeout=0.3)
    backend.connections[DEVICE] = connection
    await connection.start()
    backend.running = True
    rig.jobs.append(asyncio.create_task(rig.drive_phone()))
    async with websockets.serve(rig.websocket, "127.0.0.1", 0) as server:
        endpoint = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/realtime"

        def factory(c):
            provider = OpenAIRealtime if c.provider == "openai" else GeminiLive
            return provider(c, "test-key", endpoint=endpoint, timeout=0.3)

        manager = TaskManager(backend, config, factory)
        rig.manager = manager
        rig.store = store
        rig.connection = connection
        app = create_app(config, backend, manager)
        await manager.start()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            rig.client = client
            yield rig
        await manager.close()
    await backend.close()
    await phone.close()
    for job in rig.jobs:
        job.cancel()
    await asyncio.gather(*rig.jobs, return_exceptions=True)
    for peer in rig.peers:
        peer.close()
    store.close()


async def create_start(rig, body=None):
    response = await rig.client.post("/tasks", json=body or payload())
    assert response.status_code == 201, response.text
    task_id = response.json()["id"]
    response = await rig.client.post(
        f"/tasks/{task_id}/start", headers={"Idempotency-Key": task_id}
    )
    assert response.status_code == 202
    return task_id


async def completed(rig, task_id):
    await wait_for(lambda: rig.store.task(task_id)["state"] == "ended")
    return (await rig.client.get(f"/tasks/{task_id}")).json()


async def test_complete_http_task_model_audio_tools_hangup_and_records(rig, tmp_path):
    rig.store.recordings_dir = tmp_path / "recordings"
    task_id = await create_start(rig)
    await wait_for(lambda: rig.store.task(task_id)["state"] == "in_call")
    ready_at = asyncio.get_running_loop().time()
    await asyncio.sleep(0.2)
    assert not rig.output  # Even an immediately generated greeting cannot play early.
    result = await completed(rig, task_id)
    assert result["outcome"] == "completed", result
    assert result["downloads"] == {"transcript": True, "recording": True}
    recording = await rig.client.get(f"/tasks/{task_id}/recording")
    assert recording.status_code == 200 and recording.headers["content-type"] == "audio/wav"
    import io
    import wave

    with wave.open(io.BytesIO(recording.content)) as audio:
        assert audio.getnchannels() == 2 and audio.getframerate() == 8000
        assert audio.getnframes() >= 8000  # Includes the one-second opening silence.
    transcript = await rig.client.get(f"/tasks/{task_id}/transcript")
    assert (
        transcript.status_code == 200 and "attachment" in transcript.headers["content-disposition"]
    )
    assert "Get the answer" in transcript.text
    assert result["model_result"]["result"] == {"answer": 42}
    assert result["call"]["state"] == "idle" and result["call"]["end_reason"] == "phone_ended"
    assert result["call_id"] == result["call"]["id"]
    assert rig.phone.commands.count("ATD123;") == 1
    for digit in "12*#":
        assert rig.phone.commands.count("AT+VTS=" + digit) == 1
    assert rig.first_output_at - ready_at >= 0.98
    assert rig.hangup_output_sizes == [4800]  # greeting + closing audio reaches SCO before CHUP
    audio_inputs = [
        e
        for e in rig.model_messages
        if e.get("type") == "input_audio_buffer.append" or "audio" in e.get("realtimeInput", {})
    ]
    assert len(audio_inputs) > 1  # phone input continues while output plays
    tool_outputs = rig.tool_outputs()
    assert [e["call_id"] for e in tool_outputs].count("digits1") == 1
    events = (await rig.client.get(f"/tasks/{task_id}/events")).json()
    states = [e["data"]["state"] for e in events if e["kind"] == "task.state"]
    assert states == ["preparing", "dialing", "in_call", "finalizing", "ended"]
    assert any(e["kind"] == "model.transcript" for e in events)
    queried = (await rig.client.get(f"/tasks/{task_id}/result")).json()
    assert (
        queried["model_result"] == result["model_result"]
        and queried["call_id"] == result["call_id"]
    )
    tools = (await rig.client.get(f"/tasks/{task_id}/tools")).json()
    assert [tool["name"] for tool in tools] == ["send_dtmf", "finish_task", "hangup"]
    assert all(tool["state"] == "done" and tool["result"]["ok"] for tool in tools)
    history = (
        await rig.client.get(
            "/events/history",
            params={"task_id": task_id, "call_id": result["call_id"], "kind": "tool.result"},
        )
    ).json()
    assert [event["tool_call_id"] for event in history] == ["digits1", "finish1", "hangup1"]
    assert "test-key" not in json.dumps(result) + json.dumps(events) + json.dumps(
        tools
    ) + json.dumps(history)


async def test_same_device_queued_tasks_and_idempotent_retries(rig):
    first = await create_start(rig)
    for _ in range(3):
        assert (
            await rig.client.post(f"/tasks/{first}/start", headers={"Idempotency-Key": first})
        ).status_code == 202
    second = await create_start(rig)
    assert rig.store.task(second)["state"] == "queued"
    await completed(rig, first)
    await completed(rig, second)
    assert rig.phone.commands.count("ATD123;") == 2
    assert len(rig.session_configs) == 2
    assert not rig.backend.claims
    await rig.client.post(f"/tasks/{first}/start", headers={"Idempotency-Key": first})
    await asyncio.sleep(0.03)
    assert rig.phone.commands.count("ATD123;") == 2  # terminal task never restarts


async def test_partial_result_and_per_task_config_override(rig):
    rig.mode = "partial"
    voice = "cedar" if rig.provider == "openai" else "Puck"
    task_id = await create_start(rig, payload(config={"voice": voice, "language": "English"}))
    result = await completed(rig, task_id)
    assert result["outcome"] == "partial", result
    session = rig.session_configs[0]
    if rig.provider == "openai":
        assert session["audio"]["output"]["voice"] == voice
        assert "Speak in English" in session["instructions"]
    else:
        assert (
            session["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"][
                "voiceName"
            ]
            == voice
        )
        assert "Speak in English" in session["systemInstruction"]["parts"][0]["text"]
    assert rig.manager.config.provider.voice == ("marin" if rig.provider == "openai" else "Aoede")


async def test_bad_tool_arguments_and_result_schema_do_not_execute(rig):
    rig.mode = "bad_tools"
    task_id = await create_start(rig, payload(max_call_seconds=0.35))
    result = await completed(rig, task_id)
    assert result["outcome"] == "timeout" and result["model_result"] is None
    outputs = [e["result"] for e in rig.tool_outputs()]
    assert sum(not e["ok"] for e in outputs) == 3
    assert "ATD999;" not in rig.phone.commands


async def test_bluetooth_failure_not_overridden_by_completed_model_result(rig):
    rig.mode = "hold"
    task_id = await create_start(rig)
    await wait_for(lambda: rig.store.task(task_id)["model_result"] is not None)
    await rig.phone.close()
    result = await completed(rig, task_id)
    assert result["outcome"] == "bluetooth_disconnected"
    assert result["model_result"]["status"] == "completed"
    assert result["call"]["end_reason"] == "bluetooth_disconnected"


async def test_model_disconnect_hangs_up_and_records_failure(rig):
    rig.mode = "disconnect"
    task_id = await create_start(rig)
    result = await completed(rig, task_id)
    assert result["outcome"] == "model_disconnected", result
    assert "AT+CHUP" in rig.phone.commands
    assert result["call"]["end_reason"] == "phone_ended"
    assert len(rig.session_configs) == 1  # no reconnect or automatic provider fallback
    assert result["config"]["provider"] == rig.provider


async def test_no_answer_and_busy_are_separate_outcomes(rig):
    rig.answer = False
    first = await create_start(rig)
    result = await completed(rig, first)
    assert result["outcome"] == "no_answer"
    assert rig.response_count == 0
    rig.busy = True
    second = await create_start(rig)
    result = await completed(rig, second)
    assert result["outcome"] == "busy"
    assert result["call"]["end_reason"] == "busy"
    assert rig.response_count == 0


async def test_active_cancel_and_saved_cancel(rig):
    rig.mode = "hold"
    active = await create_start(rig)
    await wait_for(lambda: rig.store.task(active)["state"] == "in_call")
    assert (await rig.client.post(f"/tasks/{active}/cancel")).status_code == 202
    result = await completed(rig, active)
    assert result["outcome"] == "user_cancelled"
    response = await rig.client.post("/tasks", json=payload())
    saved = response.json()["id"]
    assert (await rig.client.post(f"/tasks/{saved}/cancel")).json()["outcome"] == "user_cancelled"
    assert rig.phone.commands.count("ATD123;") == 1


async def test_model_setup_failure_never_dials(rig):
    def failure(config):
        from agentcall.providers.base import ProviderError

        raise ProviderError("configuration failed")

    rig.manager.factory = failure
    task_id = await create_start(rig)
    result = await completed(rig, task_id)
    assert result["outcome"] == "model_connection_failed" and result["call_id"] is None
    assert not any(c.startswith("ATD") for c in rig.phone.commands)


async def test_contact_is_resolved_when_saved_and_client_exit_does_not_cancel(rig):
    rig.store.save_phonebook(
        DEVICE, [{"id": "c1", "name": "Caller", "number": "+1 (23)", "raw": "card"}], []
    )
    response = await rig.client.post(
        "/tasks", json=payload(number=None, contact_id="c1", start_immediately=True)
    )
    task_id = response.json()["id"]
    assert response.json()["input"]["number"] == "+123"
    rig.store.save_phonebook(DEVICE, [], [])
    await rig.client.aclose()
    await wait_for(lambda: rig.store.task(task_id)["state"] == "ended")
    assert rig.store.task(task_id)["outcome"] == "completed"
    assert "ATD+123;" in rig.phone.commands


async def test_reject_invalid_schemas_credentials_and_conflicting_start_keys(rig):
    for schema in ({"type": "invalid"}, {"$ref": "https://example.com/schema"}):
        response = await rig.client.post("/tasks", json=payload(result_schema=schema))
        assert response.status_code == 422
    response = await rig.client.post(
        "/tasks", json=payload(config={"options": {"api_key": "secret"}})
    )
    assert response.status_code == 400
    first = (await rig.client.post("/tasks", json=payload())).json()["id"]
    second = (await rig.client.post("/tasks", json=payload())).json()["id"]
    rig.backend.claims[DEVICE] = "reserved"
    assert (
        await rig.client.post(f"/tasks/{first}/start", headers={"Idempotency-Key": "same"})
    ).status_code == 202
    assert (
        await rig.client.post(f"/tasks/{second}/start", headers={"Idempotency-Key": "same"})
    ).status_code == 400
    assert (
        await rig.client.post("/calls", json={"device": DEVICE, "number": "123"})
    ).status_code == 409
    await rig.client.post(f"/tasks/{first}/cancel")
    assert rig.store.task(first)["outcome"] == "user_cancelled"
    assert not any(c.startswith("ATD") for c in rig.phone.commands)


async def test_audio_readiness_deadline_and_live_context(rig, monkeypatch):
    def no_audio(device):
        pass

    monkeypatch.setattr(rig.backend, "ensure_audio", no_audio)
    first = await create_start(rig, payload(max_call_seconds=0.05))
    assert (await completed(rig, first))["outcome"] == "timeout"
    assert rig.response_count == 0
    monkeypatch.setattr(rig.backend, "ensure_audio", rig.setup_audio)
    rig.mode = "hold"
    second = await create_start(rig)
    await wait_for(lambda: rig.store.task(second)["state"] == "in_call")
    assert (
        await rig.client.post(f"/tasks/{second}/context", json={"text": "extra fact"})
    ).status_code == 200
    await wait_for(
        lambda: any(
            e.get("item", {}).get("role") == "system"
            or "extra fact" in e.get("realtimeInput", {}).get("text", "")
            for e in rig.model_messages
        )
    )
    await rig.client.post(f"/tasks/{second}/cancel")
    await completed(rig, second)


def test_restart_never_requeues_interrupted_calls_and_preserves_dedup(tmp_path):
    path = str(tmp_path / "tasks.db")
    store = Store(path)
    store.init_tasks()
    ids = []
    for state in ("saved", "queued", "preparing", "dialing", "in_call", "finalizing"):
        task_id = store.create_task(DEVICE, payload(), {})
        store.update_task(task_id, state=state)
        ids.append(task_id)
    store.start_task(ids[1], "retry-key")
    assert store.claim_tool(ids[4], "tool-id", "send_dtmf", '{"digits":"1"}')
    store.close()
    reopened = Store(path)
    reopened.init_tasks()
    reopened.recover_tasks()
    assert reopened.task(ids[0])["state"] == "saved"
    assert reopened.task(ids[1])["state"] == "queued"
    for task_id in ids[2:]:
        assert reopened.task(task_id)["outcome"] == "service_restart"
        assert reopened.start_task(task_id)["state"] == "ended"
    assert not reopened.claim_tool(ids[4], "tool-id", "send_dtmf", '{"digits":"1"}')
    assert reopened.start_task(ids[1], "retry-key")["state"] == "queued"
    reopened.close()


async def test_switch_provider_resolves_own_defaults_and_factory_credentials(rig, monkeypatch):
    import agentcall.tasks.manager as module

    selected = "gemini" if rig.provider == "openai" else "openai"
    other = GeminiRig() if selected == "gemini" else Rig()
    rig.manager.config.provider.options = (
        {"transcription": {"model": "gpt-4o-mini-transcribe"}}
        if rig.provider == "openai"
        else {"inputAudioTranscription": {}}
    )
    monkeypatch.setenv(rig.manager.config.api_key_env, "openai-only-key")
    monkeypatch.setenv(rig.manager.config.gemini_api_key_env, "gemini-only-key")
    seen = []
    async with websockets.serve(other.websocket, "127.0.0.1", 0) as server:
        endpoint = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"

        def build(config, key, timeout):
            seen.append((config.provider, key))
            provider = GeminiLive if config.provider == "gemini" else OpenAIRealtime
            return provider(config, "test-key", endpoint=endpoint, timeout=timeout)

        monkeypatch.setattr(module, "GeminiLive", build)
        monkeypatch.setattr(module, "OpenAIRealtime", build)
        rig.manager.factory = rig.manager.make_provider
        response = await rig.client.post("/tasks", json=payload(config={"provider": selected}))
        assert response.status_code == 201
        task = response.json()
        defaults = ProviderConfig(provider=selected)
        assert task["config"]["model"] == defaults.model
        assert task["config"]["voice"] == defaults.voice
        assert task["config"]["options"] == defaults.options  # only the new provider defaults
        rig.manager.config.provider = ProviderConfig(provider=rig.provider, voice="changed")
        await rig.client.post(f"/tasks/{task['id']}/start")
        result = await completed(rig, task["id"])
        assert result["outcome"] == "completed", result
        assert result["config"]["voice"] == defaults.voice  # saved config is immutable
        assert seen == [
            (selected, "gemini-only-key" if selected == "gemini" else "openai-only-key")
        ]
        assert "only-key" not in json.dumps(result)
        await asyncio.gather(*other.jobs, return_exceptions=True)


@pytest.mark.parametrize("rig", ["gemini"], indirect=True)
async def test_api_cancelled_hangup_does_not_hang_up_or_clear_received_audio(rig):
    rig.mode = "cancel_hangup"
    task_id = await create_start(rig)
    await wait_for(
        lambda: any(e["kind"] == "model.tool_cancelled" for e in rig.store.task_events(task_id))
    )
    await wait_for(lambda: len(rig.output) == 4800)
    await asyncio.sleep(0.03)
    assert rig.store.task(task_id)["state"] == "in_call"
    assert len(rig.output) == 4800  # interruption does not clear audio buffers
    assert "AT+CHUP" not in rig.phone.commands
    assert rig.store.task(task_id)["model_result"]["status"] == "completed"
    await rig.client.post(f"/tasks/{task_id}/cancel")
    assert (await completed(rig, task_id))["outcome"] == "user_cancelled"


async def test_silent_hangup_is_deferred_until_closing_audio_is_sent(rig):
    rig.mode = "silent_hangup"
    task_id = await create_start(rig)
    await wait_for(lambda: any(e["call_id"] == "hangup1" for e in rig.tool_outputs()))
    rejected = next(e["result"] for e in rig.tool_outputs() if e["call_id"] == "hangup1")
    assert not rejected["ok"] and "closing statement" in rejected["error"]
    assert "AT+CHUP" not in rig.phone.commands
    assert rig.store.task(task_id)["state"] == "in_call"
    assert rig.store.task(task_id)["model_result"]["status"] == "completed"
    rig.closing_release.set()
    result = await completed(rig, task_id)
    assert result["outcome"] == "completed" and result["error"] is None
    assert rig.phone.commands.count("AT+CHUP") == 1
    assert rig.hangup_output_sizes == [4800]


async def test_sco_reset_before_idle_after_local_hangup_is_normal(rig, monkeypatch):
    command = rig.connection.command
    observed = []

    async def reset_before_idle(value):
        if value == "AT+CHUP":
            # Reproduce the phone closing SCO before the HFP idle indication arrives.
            run = next(iter(rig.manager.runs.values()))
            rig.backend.stop_audio(DEVICE)
            await asyncio.sleep(0.02)
            observed.append(run.failure)
        return await command(value)

    monkeypatch.setattr(rig.connection, "command", reset_before_idle)
    task_id = await create_start(rig)
    result = await completed(rig, task_id)
    assert observed == [None]
    assert result["outcome"] == "completed" and result["error"] is None
    assert rig.phone.commands.count("AT+CHUP") == 1


async def test_unexpected_sco_loss_before_hangup_remains_failure(rig):
    rig.mode = "hold"
    task_id = await create_start(rig)
    await wait_for(lambda: rig.store.task(task_id)["model_result"] is not None)
    rig.backend.stop_audio(DEVICE)
    result = await completed(rig, task_id)
    assert result["outcome"] == "audio_failed"
    assert result["model_result"]["status"] == "completed"


@pytest.mark.parametrize("criteria", [None, "", "   "])
async def test_api_defaults_goal_completion_and_transcription_without_dialing(rig, criteria):
    body = {"device": DEVICE, "number": "123", "goal": "Confirm the appointment"}
    if criteria is not None:
        body["completion_criteria"] = criteria
    response = await rig.client.post("/tasks", json=body)
    assert response.status_code == 201
    task = response.json()
    assert task["input"]["completion_criteria"] == body["goal"]
    assert task["state"] == "saved" and not task["input"]["start_immediately"]
    assert task["input"]["background"] == DEFAULT_BACKGROUND
    assert task["config"]["options"] == (
        {"transcription": {"model": "gpt-4o-mini-transcribe"}}
        if rig.provider == "openai"
        else {"inputAudioTranscription": {}, "outputAudioTranscription": {}}
    )
    saved = (await rig.client.get("/tasks/" + task["id"])).json()
    assert saved["input"]["completion_criteria"] == body["goal"]
    assert saved["config"] == task["config"]
    assert not any(command.startswith("ATD") for command in rig.phone.commands)


async def test_explicit_completion_and_provider_options_remain_overrides(rig):
    options = {"transcription": None} if rig.provider == "openai" else {"temperature": 0.2}
    response = await rig.client.post(
        "/tasks",
        json=payload(
            completion_criteria="The recipient explicitly confirms the time",
            config={"options": options},
        ),
    )
    assert response.status_code == 201
    task = response.json()
    assert task["input"]["completion_criteria"] == "The recipient explicitly confirms the time"
    for name, value in options.items():
        assert task["config"]["options"][name] == value
    assert not any(command.startswith("ATD") for command in rig.phone.commands)


async def test_reset_ringing_sco_is_replaced_before_speaking_without_redial(rig, monkeypatch):
    setup = rig.setup_audio
    attempts = []

    def ringing_socket(device):
        if device in rig.backend.audio:
            return
        attempts.append(device)
        if len(attempts) == 1:
            host, peer = socket.socketpair(type=socket.SOCK_SEQPACKET)
            rig.backend.audio[device] = SCOAudio(host, 1, mtu=48)
            peer.close()
        else:
            setup(device)

    monkeypatch.setattr(rig.backend, "ensure_audio", ringing_socket)
    task_id = await create_start(rig)
    result = await completed(rig, task_id)
    assert result["outcome"] == "completed" and result["error"] is None
    assert len(attempts) == 2
    assert sum(command.startswith("ATD") for command in rig.phone.commands) == 1
    assert rig.phone.commands.count("AT+CHUP") == 1
    events = rig.store.task_events(task_id)
    assert any(event["kind"] == "task.audio_reconnecting" for event in events)
    assert rig.output


async def test_reset_audio_retry_is_bounded_and_does_not_redial(rig, monkeypatch):
    def dead_audio(device):
        if device in rig.backend.audio:
            return
        host, peer = socket.socketpair(type=socket.SOCK_SEQPACKET)
        rig.backend.audio[device] = SCOAudio(host, 1, mtu=48)
        peer.close()

    monkeypatch.setattr(rig.backend, "ensure_audio", dead_audio)
    task_id = await create_start(rig)
    result = await completed(rig, task_id)
    assert result["outcome"] == "audio_failed"
    assert sum(command.startswith("ATD") for command in rig.phone.commands) == 1
    assert not rig.output
