import asyncio
import base64
import json
import socket
from types import SimpleNamespace

import httpx
import numpy as np
import pytest_asyncio
import websockets
from conftest import DEVICE, Phone

from agentcall.api.app import create_app
from agentcall.audio.transport import SCOAudio
from agentcall.bluetooth.hfp import HFPConnection
from agentcall.providers.openai import OpenAIRealtime
from agentcall.service.backend import Backend
from agentcall.service.config import Config
from agentcall.storage.store import Store
from agentcall.tasks.manager import TaskManager


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
        self.jobs = []
        self.peers = []
        self.session_configs = []
        self.hangup_output_sizes = []
        self.response_count = 0

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


@pytest_asyncio.fixture
async def rig(monkeypatch):
    rig = Rig()
    config = Config(
        answer_timeout_seconds=0.15,
        audio_timeout_seconds=0.15,
        hangup_timeout_seconds=0.3,
        model_connect_seconds=0.3,
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
            return OpenAIRealtime(c, "test-key", endpoint=endpoint, timeout=0.3)

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


async def test_complete_http_task_model_audio_tools_hangup_and_records(rig):
    task_id = await create_start(rig)
    result = await completed(rig, task_id)
    assert result["outcome"] == "completed", result
    assert result["model_result"]["result"] == {"answer": 42}
    assert result["call"]["state"] == "idle" and result["call"]["end_reason"] == "phone_ended"
    assert result["call_id"] == result["call"]["id"]
    assert rig.phone.commands.count("ATD123;") == 1
    for digit in "12*#":
        assert rig.phone.commands.count("AT+VTS=" + digit) == 1
    assert rig.hangup_output_sizes == [3200]  # all 200ms closing audio reaches SCO before CHUP
    audio_inputs = [e for e in rig.model_messages if e["type"] == "input_audio_buffer.append"]
    assert len(audio_inputs) > 1  # phone input continues while output plays
    tool_outputs = [
        e for e in rig.model_messages if e.get("item", {}).get("type") == "function_call_output"
    ]
    assert [e["item"]["call_id"] for e in tool_outputs].count("digits1") == 1
    events = (await rig.client.get(f"/tasks/{task_id}/events")).json()
    states = [e["data"]["state"] for e in events if e["kind"] == "task.state"]
    assert states == ["preparing", "dialing", "in_call", "finalizing", "ended"]
    assert any(e["kind"] == "model.transcript" for e in events)
    assert "test-key" not in json.dumps(result) + json.dumps(events)


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
    task_id = await create_start(rig, payload(config={"voice": "cedar", "language": "English"}))
    result = await completed(rig, task_id)
    assert result["outcome"] == "partial", result
    assert rig.session_configs[0]["audio"]["output"]["voice"] == "cedar"
    assert "Speak in English" in rig.session_configs[0]["instructions"]
    assert rig.manager.config.provider.voice == "marin"


async def test_bad_tool_arguments_and_result_schema_do_not_execute(rig):
    rig.mode = "bad_tools"
    task_id = await create_start(rig, payload(max_call_seconds=0.35))
    result = await completed(rig, task_id)
    assert result["outcome"] == "timeout" and result["model_result"] is None
    outputs = [
        json.loads(e["item"]["output"])
        for e in rig.model_messages
        if e.get("item", {}).get("type") == "function_call_output"
    ]
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
        lambda: any(e.get("item", {}).get("role") == "system" for e in rig.model_messages)
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
