import asyncio
import json
import signal
import socket
import sys

import httpx
import pytest_asyncio
import uvicorn
from conftest import DEVICE, until
from typer.testing import CliRunner

from agentcall.api.app import create_app
from agentcall.cli.main import app as cli
from agentcall.cli.main import sse_values


@pytest_asyncio.fixture
async def cli_service(service, monkeypatch):
    backend, connection, phone, _, calls = service
    monkeypatch.setenv("AGENTCALL_TOKEN", "cli-test-token")

    async def start():
        backend.running = True

    monkeypatch.setattr(backend, "start", start)
    application = create_app(backend.config, backend)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(application, log_level="error", lifespan="on"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    await until(lambda: server.started)
    runner = CliRunner()

    async def invoke(*args, human=False):
        result = await asyncio.to_thread(
            runner.invoke, cli, ["--url", url, *([] if human else ["--json"]), *args]
        )
        assert result.exit_code == 0, result.output
        return result.stdout if human else json.loads(result.stdout)

    try:
        yield backend, connection, phone, calls, invoke, runner, url
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 5)
        listener.close()


async def test_cli_over_real_http_controls_queries_queue_and_json(cli_service, tmp_path):
    backend, connection, phone, calls, invoke, _, _ = cli_service
    assert (await invoke("health"))["ready"]
    assert len(await invoke("devices")) == 1
    assert (await invoke("device", "11:22:33:44:55:66"))["live"]
    assert await invoke("scan", "start") == {"action": "start", "accepted": True}
    await invoke("scan", "stop")
    await invoke("pair", "11:22:33:44:55:66")
    future = asyncio.get_running_loop().create_future()
    backend.bluez.agent.pending["request1"] = future
    assert (await invoke("pairing"))["pending_ids"] == ["request1"]
    await invoke("confirm", "request1")
    assert await future
    await invoke("connect", "11:22:33:44:55:66")
    assert backend.store.intents() == [DEVICE]

    async def pbap(address, device):
        return {
            "contacts": [
                {"id": "contact1", "name": "Alice [red]", "number": "123", "raw": "vcard"}
            ],
            "history": [
                {
                    "id": "history1",
                    "number": "456",
                    "raw": "history",
                    "synced_at": "2026-10-06T00:00:00+00:00",
                }
            ],
            "errors": {},
        }

    backend.pbap.sync = pbap
    assert (await invoke("sync", "11:22:33:44:55:66"))["contacts"] == 1
    assert (await invoke("contacts", "Alice", "--limit", "1"))[0]["id"] == "contact1"
    assert (await invoke("contact", "contact1"))["raw"] == "vcard"
    human = await invoke("contacts", human=True)
    assert "name" in human and "Alice [red]" in human  # remote text remains literal
    assert (await invoke("calls", "--source", "pbap"))[0]["id"] == "history1"
    assert (await invoke("call", "history1", "--source", "pbap"))["source"] == "pbap"
    call = await invoke("dial", "--device", "11:22:33:44:55:66", "--contact", "contact1")
    assert phone.commands[-1] == "ATD123;"
    await phone.send("+CIEV: 1,1\r\n")
    await until(lambda: connection.state == "active")
    assert (await invoke("call", call["id"]))["state"] == "active"
    await invoke("dtmf", call["id"], "12*#")
    assert phone.commands[-4:] == ["AT+VTS=" + digit for digit in "12*#"]
    file = tmp_path / "task.json"
    file.write_text(json.dumps({"device": "11:22:33:44:55:66", "number": "123", "goal": "Goal"}))
    task = await invoke("task", "create", "--file", str(file))
    task_id = task["id"]
    for _ in range(2):
        assert (await invoke("task", "start", task_id, "--key", "retry"))["state"] == "queued"
    assert len([c for c in phone.commands if c.startswith("ATD")]) == 1
    assert (await invoke("task", "list", "--state", "queued"))[0]["id"] == task_id
    assert (await invoke("task", "show", task_id))["input"]["goal"] == "Goal"
    assert (await invoke("task", "result", task_id))["model_result"] is None
    assert await invoke("task", "tools", task_id) == []
    assert (await invoke("task", "events", task_id))[0]["kind"] == "task.created"
    await invoke("task", "cancel", task_id)
    assert (await invoke("task", "result", task_id))["outcome"] == "user_cancelled"
    await invoke("hangup", call["id"])
    assert phone.commands[-1] == "AT+CHUP"
    await phone.send("+CIEV: 1,0\r\n")
    await until(lambda: DEVICE not in backend.current)
    assert await invoke("calls", "--current") == []
    await phone.send("RING\r\n")
    await until(lambda: connection.state == "incoming")
    incoming = backend.current[DEVICE]
    await invoke("answer", incoming)
    assert phone.commands[-1] == "ATA"
    await phone.send("+CIEV: 2,0\r\n")
    await until(lambda: DEVICE not in backend.current)
    numeric = await invoke("dial", "789", "--device", "11:22:33:44:55:66")
    assert numeric["number"] == "789" and phone.commands[-1] == "ATD789;"
    await phone.send("+CIEV: 2,2\r\n")
    await until(lambda: connection.state == "dialing")
    await invoke("hangup", numeric["id"])
    await phone.send("+CIEV: 2,0\r\n")
    await until(lambda: DEVICE not in backend.current)
    event = backend.emit("cli.probe", device=DEVICE, value=42)
    assert (await invoke("events", "--after", str(event["event_id"] - 1), "--kind", "cli.probe"))[
        0
    ]["value"] == 42
    assert len(await invoke("devices", "--saved")) == 1
    await invoke("disconnect", "11:22:33:44:55:66")
    assert backend.store.intents() == []
    assert any(args[2] == "Pair" for args, _ in calls)


async def test_cli_http_errors_have_json_stderr_and_nonzero_exit(cli_service, monkeypatch):
    import agentcall.cli.main as module

    _, _, _, _, _, runner, url = cli_service
    with monkeypatch.context() as patch:
        patch.setattr(module, "headers", lambda: {"Authorization": "Bearer wrong"})
        result = await asyncio.to_thread(runner.invoke, cli, ["--url", url, "--json", "devices"])
        assert result.exit_code == 1 and result.stdout == ""
        assert json.loads(result.stderr) == {"error": "Bearer token required", "status": 401}
        result = await asyncio.to_thread(runner.invoke, cli, ["--url", url, "--json", "watch"])
        assert result.exit_code == 1 and json.loads(result.stderr)["status"] == 401
    result = await asyncio.to_thread(
        runner.invoke, cli, ["--url", url, "--json", "contacts", "--limit", "0"]
    )
    assert result.exit_code == 1 and json.loads(result.stderr)["status"] == 422


async def test_actual_cli_watch_filters_sse_and_exits_cleanly(cli_service):
    backend, _, _, _, _, _, url = cli_service
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "from agentcall.cli.main import app; app()",
        "--url",
        url,
        "--json",
        "watch",
        "--device",
        "11:22:33:44:55:66",
        "--kind",
        "watch.probe",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        ready = json.loads(await asyncio.wait_for(process.stdout.readline(), 5))
        assert ready["kind"] == "events.ready"
        backend.emit("watch.probe", device="/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF", value=0)
        backend.emit("unrelated", device=DEVICE, value=0)
        wanted = backend.emit("watch.probe", device=DEVICE, value=42)
        value = json.loads(await asyncio.wait_for(process.stdout.readline(), 3))
        assert value == wanted
        async with httpx.AsyncClient(
            base_url=url, headers={"Authorization": "Bearer cli-test-token"}
        ) as client:
            history = (
                await client.get(
                    "/events/history", params={"device": "11:22:33:44:55:66", "kind": "watch.probe"}
                )
            ).json()
            assert history == [wanted]
        process.send_signal(signal.SIGINT)
        _, stderr = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == 0 and stderr == b""
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()


def test_sse_parser_joins_multiline_data_and_ignores_metadata():
    values = list(
        sse_values(
            [
                ":heartbeat",
                "event: probe",
                "id: 42",
                'data: {"kind":',
                'data: "probe"}',
                "",
                'data:{"kind":"tail"}',
            ]
        )
    )
    assert values == [{"kind": "probe"}, {"kind": "tail"}]
