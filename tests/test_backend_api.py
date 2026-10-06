import asyncio
from types import SimpleNamespace

import pytest
from conftest import DEVICE, until

from agentcall.service.backend import Backend
from agentcall.service.config import Config
from agentcall.storage.store import Store


async def test_http_call_lifecycle_and_db_sources(service):
    backend, connection, phone, client, _ = service
    response = await client.post("/calls", json={"device": "11:22:33:44:55:66", "number": "123"})
    assert response.status_code == 201
    call_id = response.json()["id"]
    assert response.json()["state"] == "requested"
    await phone.send("+CIEV: 2,2\r\n+CIEV: 1,1\r\n+CIEV: 2,0\r\n")
    await until(lambda: connection.state == "active")
    assert (await client.get(f"/calls/{call_id}")).json()["answered_at"]
    assert (
        await client.post(f"/calls/{call_id}/dtmf", json={"digits": "*123#"})
    ).status_code == 200
    assert (await client.post(f"/calls/{call_id}/hangup")).status_code == 200
    assert (await client.get("/calls/current")).json()[0]["state"] == "active"
    await phone.send("+CIEV: 1,0\r\n")
    await until(lambda: DEVICE not in backend.current)
    row = (await client.get(f"/calls/{call_id}")).json()
    assert row["state"] == "idle" and row["end_reason"] == "phone_ended"
    assert row["source"] == "project" and row["ended_at"]
    assert (await client.post(f"/calls/{call_id}/hangup")).status_code == 409


async def test_concurrent_dial_never_sends_two_atd(service):
    _, _, phone, client, _ = service
    responses = await asyncio.gather(
        *[
            client.post("/calls", json={"device": DEVICE.rsplit("/", 1)[-1], "number": "123"})
            for _ in range(2)
        ]
    )
    assert sorted(r.status_code for r in responses) == [201, 409]
    assert sum(c.startswith("ATD") for c in phone.commands) == 1


async def test_dial_rejection_and_disconnect_causes(service):
    backend, connection, phone, client, _ = service
    phone.rejected.add("ATD123;")
    assert (
        await client.post("/calls", json={"device": "11:22:33:44:55:66", "number": "123"})
    ).status_code == 409
    assert backend.store.calls()[0]["end_reason"] == "dial_failed"
    phone.rejected.clear()
    call_id = (
        await client.post("/calls", json={"device": "11:22:33:44:55:66", "number": "123"})
    ).json()["id"]
    await phone.close()
    await until(lambda: connection.closed)
    assert backend.store.call(call_id)["end_reason"] == "bluetooth_disconnected"
    assert DEVICE not in backend.current


async def test_contact_resolution_and_pbap_denial_do_not_block_dial(service):
    backend, _, phone, client, _ = service

    async def failed_sync(address, device):
        return {"contacts": None, "history": [], "errors": {"pb": "Forbidden"}}

    backend.pbap.sync = failed_sync
    backend.store.save_phonebook(
        DEVICE,
        [{"id": "contact1", "name": "测试", "number": "+1 (555) 123-4567", "raw": "card"}],
        [],
    )
    contacts = (await client.get("/contacts", params={"q": "测试"})).json()
    assert len(contacts) == 1
    sync = await client.post("/devices/11:22:33:44:55:66/sync")
    assert sync.json()["errors"]["pb"] == "Forbidden"
    assert backend.store.contacts()[0]["id"] == "contact1"  # denied sync preserves old data
    dial = await client.post(
        "/calls", json={"device": "11:22:33:44:55:66", "contact_id": "contact1"}
    )
    assert dial.status_code == 201
    assert phone.commands[-1] == "ATD+15551234567;"


async def test_api_validation_and_auth(service, monkeypatch):
    backend, _, phone, client, _ = service
    for body in (
        {"device": "bad", "number": "123"},
        {"device": "11:22:33:44:55:66", "number": "123\rAT+CHUP"},
        {"device": "11:22:33:44:55:66", "number": "123", "contact_id": "x"},
    ):
        assert (await client.post("/calls", json=body)).status_code == 400
    assert not any(c.startswith("ATD") for c in phone.commands)
    monkeypatch.setenv("AGENTCALL_TOKEN", "secret")
    assert (await client.get("/health")).status_code == 401
    assert (
        await client.get("/health", headers={"Authorization": "Bearer secret"})
    ).status_code == 200
    assert (
        "secret"
        not in (await client.get("/health", headers={"Authorization": "Bearer secret"})).text
    )


async def test_reconnect_only_connects_never_redials(service):
    backend, connection, phone, _, calls = service
    await backend.device_action("11:22:33:44:55:66", "connect")
    assert backend.store.intents() == [DEVICE]
    await connection.close()
    backend.config.reconnect_seconds = 0.01
    retry = asyncio.create_task(backend.reconnect_loop())
    try:
        await until(lambda: len(calls) >= 3)
    finally:
        retry.cancel()
        await asyncio.gather(retry, return_exceptions=True)
    assert all(args[2] == "ConnectProfile" for args, _ in calls)
    assert not any(c.startswith("ATD") for c in phone.commands)
    await backend.device_action("11:22:33:44:55:66", "disconnect")
    assert backend.store.intents() == []


async def test_incoming_answer_api_and_pair_confirmation(service):
    backend, connection, phone, client, _ = service
    await phone.send("RING\r\n")
    await until(lambda: connection.state == "incoming")
    call_id = backend.current[DEVICE]
    assert (await client.post(f"/calls/{call_id}/answer")).status_code == 200
    assert phone.commands[-1] == "ATA"
    future = asyncio.get_running_loop().create_future()
    backend.bluez.agent.pending["confirmation"] = future
    assert (await client.post("/pairing/confirmation", json={"accept": True})).status_code == 200
    assert await future is True


def test_restart_records_unknown_and_retains_reconnect_intent(tmp_path):
    path = str(tmp_path / "calls.sqlite3")
    store = Store(path)
    store.reconnect(DEVICE, True)
    call_id = store.new_call(DEVICE, "123", "outgoing", "active")
    store.close()
    store = Store(path)
    assert store.call(call_id)["end_reason"] == "service_restart"
    assert store.call(call_id)["state"] == "unknown"
    assert store.intents() == [DEVICE]
    store.close()


def test_remote_listener_requires_token(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENTCALL_TOKEN", raising=False)
    config = tmp_path / "config.toml"
    config.write_text('[service]\nhost="0.0.0.0"\n')
    with pytest.raises(ValueError, match="Bearer"):
        Config.load(config)


async def test_no_bluez_reports_unavailable_without_crashing(service):
    backend, _, _, client, _ = service
    backend.running = False
    backend.error = "org.bluez service unavailable"
    assert not (await client.get("/health")).json()["ready"]
    assert (await client.get("/devices")).status_code == 503


async def test_bluez_startup_failure_recovers_without_dial(monkeypatch):
    store = Store(":memory:")
    backend = Backend(Config(reconnect_seconds=0.01), store)
    attempts = []

    async def start():
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("BlueZ not running")

    async def close():
        pass

    backend.bluez = SimpleNamespace(start=start, close=close)
    try:
        await backend.start()
        assert not backend.running
        await until(lambda: backend.running)
        assert backend.error is None
        assert len(attempts) == 2
        assert store.calls() == []
    finally:
        await backend.close()
        store.close()


async def test_unpair_removes_only_selected_bond_and_reconnect(service):
    backend, connection, phone, client, calls = service
    other = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
    backend.store.reconnect(DEVICE, True)
    backend.store.reconnect(other, True)
    backend.store.save_device(DEVICE, {"paired": True, "connected": True, "hfp_ready": True})
    response = await client.post("/devices/11:22:33:44:55:66/unpair")
    assert response.status_code == 200
    assert calls[-1][0][2:] == ("RemoveDevice", "o", [DEVICE])
    assert backend.store.intents() == [other]
    assert connection.closed
    snapshot = next(row for row in backend.store.saved_devices() if row["device"] == DEVICE)
    assert not snapshot["last_observed"]["paired"]
    assert not snapshot["last_observed"]["hfp_ready"]
    assert not any(command.startswith("ATD") for command in phone.commands)


async def test_unpair_rejects_active_phone_without_changing_reconnect(service):
    backend, _, _, client, calls = service
    backend.store.reconnect(DEVICE, True)
    backend.claims[DEVICE] = "active-task"
    before = len(calls)
    response = await client.post("/devices/11:22:33:44:55:66/unpair")
    assert response.status_code == 409
    assert backend.store.intents() == [DEVICE]
    assert len(calls) == before
