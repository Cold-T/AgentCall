import asyncio
import socket
from types import SimpleNamespace

import httpx
import pytest_asyncio

from agentcall.api.app import create_app
from agentcall.bluetooth.hfp import HFPConnection
from agentcall.service.backend import Backend
from agentcall.service.config import Config
from agentcall.storage.store import Store

DEVICE = "/org/bluez/hci0/dev_11_22_33_44_55_66"


class Phone:
    """AG emulator on a real socket; no production simulation mode."""

    def __init__(self, sock, *, features=0, rejected=None, initial="0,0,0"):
        self.sock = sock
        self.sock.setblocking(False)
        self.features = features
        self.rejected = rejected or set()
        self.ignored = set()
        self.initial = initial
        self.commands = []
        self.task = asyncio.create_task(self.run())

    async def send(self, text):
        await asyncio.get_running_loop().sock_sendall(self.sock, text.encode())

    async def run(self):
        buffer = b""
        try:
            while True:
                data = await asyncio.get_running_loop().sock_recv(self.sock, 4096)
                if not data:
                    return
                buffer += data
                while b"\r" in buffer:
                    cmd, buffer = buffer.split(b"\r", 1)
                    cmd = cmd.decode()
                    self.commands.append(cmd)
                    if cmd in self.ignored:
                        continue
                    if cmd in self.rejected:
                        await self.send("\r\nERROR\r\n")
                        continue
                    if cmd.startswith("AT+BRSF"):
                        await self.send(f"\r\n+BRSF: {self.features}\r\n")
                    elif cmd == "AT+CIND=?":
                        await self.send(
                            '+CIND: ("call",(0,1)),("callsetup",(0-3)),("callheld",(0-2))\r\n'
                        )
                    elif cmd == "AT+CIND?":
                        await self.send(f"+CIND: {self.initial}\r\n")
                    elif cmd == "AT+BCC":
                        await self.send("OK\r\n+BCS: 2\r\n")
                        continue
                    await self.send("OK\r\n")
        except (OSError, asyncio.CancelledError):
            return

    async def close(self):
        self.sock.close()
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


@pytest_asyncio.fixture
async def link():
    host, peer = socket.socketpair()
    events = []
    phone = Phone(peer)
    connection = HFPConnection(
        host, DEVICE, lambda kind, **data: events.append((kind, data)), timeout=0.3
    )
    await connection.start()
    yield connection, phone, events
    await connection.close()
    await phone.close()


async def until(predicate):
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.005)
    assert predicate()


@pytest_asyncio.fixture
async def service(monkeypatch):
    store = Store(":memory:")
    config = Config()
    backend = Backend(config, store)
    host, peer = socket.socketpair()
    phone = Phone(peer)
    calls = []

    async def dbus_call(*args, **kwargs):
        calls.append((args, kwargs))
        if args[2] == "Disconnect":
            await backend.connections[DEVICE].close()
        return []

    async def devices():
        return [{"id": DEVICE.rsplit("/", 1)[-1], "path": DEVICE}]

    async def close():
        pass

    backend.bluez = SimpleNamespace(
        call=dbus_call, devices=devices, close=close, agent=SimpleNamespace(pending={})
    )
    monkeypatch.setattr(backend, "ensure_audio", lambda device: None)
    connection = HFPConnection(host, DEVICE, backend.emit, timeout=0.3)
    backend.connections[DEVICE] = connection
    await connection.start()
    backend.running = True
    app = create_app(config, backend)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield backend, connection, phone, client, calls
    await backend.close()
    await phone.close()
    store.close()
