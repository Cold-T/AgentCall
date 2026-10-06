import asyncio
import time

import pytest
from conftest import DEVICE, until
from dbus_next import DBusError, Variant

from agentcall.bluetooth.dbus import Agent, unpack


async def test_incoming_pairing_requires_window_and_explicit_confirmation(service):
    backend, _, phone, client, calls = service
    agent = Agent(backend)
    backend.bluez.agent = agent
    with pytest.raises(DBusError, match="pairing was not requested"):
        await agent.confirmation(DEVICE, passkey="012345")
    response = await client.post("/discoverability/start")
    assert response.status_code == 200 and response.json()["timeout_seconds"] == 180
    assert backend.incoming_pairing_enabled()
    assert calls[-4][0][2] == "RequestDefaultAgent"
    assert [call[0][4][1] for call in calls[-3:]] == [
        "DiscoverableTimeout",
        "Pairable",
        "Discoverable",
    ]
    confirmation = asyncio.create_task(agent.confirmation(DEVICE, passkey="012345"))
    await until(lambda: bool(agent.pending))
    response = (await client.get("/pairing")).json()
    request = response["requests"][0]
    assert request["passkey"] == "012345" and request["device"] == DEVICE
    assert not confirmation.done()
    assert (
        await client.post(f"/pairing/{request['id']}", json={"accept": True})
    ).status_code == 200
    await confirmation
    assert not agent.pending and not agent.details
    assert not any(command.startswith("ATD") for command in phone.commands)
    assert (await client.post("/discoverability/stop")).status_code == 200
    assert not backend.incoming_pairing_enabled()
    with pytest.raises(DBusError):
        await agent.confirmation(DEVICE)


async def test_expired_window_and_rejected_confirmation(service):
    backend, _, _, _, _ = service
    agent = Agent(backend)
    backend.incoming_pairing_until = time.monotonic() - 1
    with pytest.raises(DBusError):
        await agent.confirmation(DEVICE)
    backend.incoming_pairing_until = time.monotonic() + 180
    confirmation = asyncio.create_task(agent.confirmation(DEVICE))
    await until(lambda: bool(agent.pending))
    next(iter(agent.pending.values())).set_result(False)
    with pytest.raises(DBusError, match="user rejected"):
        await confirmation
    assert not agent.pending and not agent.details


async def test_discoverability_start_failure_does_not_enable_incoming_pairing(service):
    backend, _, _, client, _ = service

    async def failed_call(*args):
        raise RuntimeError("BlueZ unavailable")

    backend.bluez.call = failed_call
    assert (await client.post("/discoverability/start")).status_code == 503
    assert not backend.incoming_pairing_enabled()
    assert (await client.post("/discoverability/invalid")).status_code == 422


def test_bluez_byte_properties_are_json_serializable():
    import json

    value = {"ManufacturerData": Variant("a{qv}", {76: Variant("ay", b"\x01\xff")})}
    assert unpack(value) == {"ManufacturerData": {"76": [1, 255]}}
    json.dumps(unpack(value))
