"""Exercise real D-Bus wire signatures and UNIX FD transfer on a private bus."""

import asyncio
import socket
import subprocess
from functools import partial

import pytest
from conftest import DEVICE, Phone, until
from dbus_next import Message, MessageType, Variant
from dbus_next.aio import MessageBus

from agentcall.bluetooth.dbus import AGENT_PATH, PROFILE_PATH
from agentcall.service.backend import Backend
from agentcall.service.config import Config
from agentcall.storage.store import Store


async def test_profile_registration_fd_handoff_and_pairing(monkeypatch):
    process = subprocess.Popen(
        ["dbus-daemon", "--session", "--nofork", "--print-address=1"],
        stdout=subprocess.PIPE,
        text=True,
    )
    address = process.stdout.readline().strip()
    monkeypatch.setenv("DBUS_SYSTEM_BUS_ADDRESS", address)
    fake = await MessageBus(bus_address=address, negotiate_unix_fd=True).connect()
    await fake.request_name("org.bluez")
    calls = []

    def handle(message):
        if message.message_type != MessageType.METHOD_CALL or message.destination != "org.bluez":
            return None
        calls.append(message)
        if message.member == "GetManagedObjects":
            return Message.new_method_return(
                message,
                "a{oa{sa{sv}}}",
                [
                    {
                        DEVICE: {
                            "org.bluez.Device1": {
                                "Address": Variant("s", "11:22:33:44:55:66"),
                                "Paired": Variant("b", True),
                                "UUIDs": Variant("as", ["0000111f-0000-1000-8000-00805f9b34fb"]),
                            }
                        }
                    }
                ],
            )
        return Message.new_method_return(message)

    fake.add_message_handler(handle)
    store = Store(":memory:")
    backend = Backend(Config(), store)
    monkeypatch.setattr(backend, "ensure_audio", lambda device: None)
    phone = None
    sent = None
    try:
        await backend.start()
        assert backend.running, backend.error
        registration = next(m for m in calls if m.member == "RegisterProfile")
        assert registration.signature == "osa{sv}"
        assert registration.body[0] == PROFILE_PATH
        assert registration.body[2]["RequireAuthentication"].value
        assert any(m.member == "RegisterAgent" for m in calls)
        assert (await backend.devices())[0]["hfp_ag"]
        sent, peer = socket.socketpair()
        phone = Phone(peer)
        reply = await fake.call(
            Message(
                destination=backend.bluez.bus.unique_name,
                path=PROFILE_PATH,
                interface="org.bluez.Profile1",
                member="NewConnection",
                signature="oha{sv}",
                body=[DEVICE, 0, {}],
                unix_fds=[sent.fileno()],
            )
        )
        assert reply.message_type == MessageType.METHOD_RETURN
        sent.close()
        await until(lambda: DEVICE in backend.connections and backend.connections[DEVICE].ready)
        await backend.dial(DEVICE, "123")
        assert phone.commands[-1] == "ATD123;"
        backend.pairing.add(DEVICE)
        confirmation = asyncio.create_task(
            fake.call(
                Message(
                    destination=backend.bluez.bus.unique_name,
                    path=AGENT_PATH,
                    interface="org.bluez.Agent1",
                    member="RequestConfirmation",
                    signature="ou",
                    body=[DEVICE, 123456],
                )
            )
        )
        await until(lambda: bool(backend.bluez.agent.pending))
        event = store.db.execute(
            "SELECT data FROM events WHERE kind='pairing.confirmation'"
        ).fetchone()
        assert "123456" in event[0]
        next(iter(backend.bluez.agent.pending.values())).set_result(True)
        assert (await confirmation).message_type == MessageType.METHOD_RETURN
        backend.pairing.clear()
        rejected = await fake.call(
            Message(
                destination=backend.bluez.bus.unique_name,
                path=AGENT_PATH,
                interface="org.bluez.Agent1",
                member="RequestConfirmation",
                signature="ou",
                body=[DEVICE, 1],
            )
        )
        assert rejected.message_type == MessageType.ERROR
        assert rejected.error_name == "org.bluez.Error.Rejected"
        reply = await fake.call(
            Message(
                destination=backend.bluez.bus.unique_name,
                path=PROFILE_PATH,
                interface="org.bluez.Profile1",
                member="RequestDisconnection",
                signature="o",
                body=[DEVICE],
            )
        )
        assert reply.message_type == MessageType.METHOD_RETURN
        await until(lambda: backend.connections[DEVICE].closed)
        assert backend.store.calls()[0]["end_reason"] == "bluetooth_disconnected"
    finally:
        await backend.close()
        if phone:
            await phone.close()
        if sent:
            sent.close()
        fake.disconnect()
        store.close()
        process.terminate()
        await asyncio.to_thread(process.wait, timeout=5)
        process.stdout.close()


@pytest.mark.parametrize("obex_bus", ["session", "system"])
async def test_obex_uses_only_configured_bus(monkeypatch, obex_bus):
    processes, buses, received = {}, {}, {"session": [], "system": []}
    backend = None
    store = Store(":memory:")
    try:
        for name in ("session", "system"):
            process = subprocess.Popen(
                ["dbus-daemon", "--session", "--nofork", "--print-address=1"],
                stdout=subprocess.PIPE,
                text=True,
            )
            processes[name] = process
            address = process.stdout.readline().strip()
            monkeypatch.setenv(f"DBUS_{name.upper()}_BUS_ADDRESS", address)
            bus = await MessageBus(bus_address=address).connect()
            buses[name] = bus
            await bus.request_name("org.bluez.obex")

            def handle(name, message):
                if (
                    message.message_type != MessageType.METHOD_CALL
                    or message.destination != "org.bluez.obex"
                ):
                    return None
                received[name].append(message)
                return Message.new_method_return(message, "o", [f"/obex/{name}/session1"])

            bus.add_message_handler(partial(handle, name))
        backend = Backend(Config(obex_bus=obex_bus), store)
        for _ in range(2):
            result = await backend.bluez.obex_call(
                "/org/bluez/obex",
                "org.bluez.obex.Client1",
                "CreateSession",
                "sa{sv}",
                ["11:22:33:44:55:66", {"Target": Variant("s", "PBAP")}],
            )
            assert result == [f"/obex/{obex_bus}/session1"]
        assert len(received[obex_bus]) == 2
        assert all(m.body[1]["Target"].value == "PBAP" for m in received[obex_bus])
        other = "system" if obex_bus == "session" else "session"
        assert received[other] == []
    finally:
        if backend:
            await backend.close()
        for bus in buses.values():
            bus.disconnect()
        store.close()
        for process in processes.values():
            process.terminate()
            await asyncio.to_thread(process.wait, timeout=5)
            process.stdout.close()


def test_obex_bus_configuration(tmp_path):
    path = tmp_path / "config.toml"
    assert Config.load().obex_bus == "session"
    path.write_text('[service]\nobex_bus="system"\n')
    assert Config.load(path).obex_bus == "system"
    path.write_text('[service]\nobex_bus="invalid"\n')
    with pytest.raises(ValueError, match="obex_bus"):
        Config.load(path)
