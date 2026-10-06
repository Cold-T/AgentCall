import asyncio
import socket
from types import SimpleNamespace

from conftest import DEVICE

from agentcall.audio.transport import SCOAudio
from agentcall.service.backend import Backend
from agentcall.service.config import Config
from agentcall.storage.store import Store


async def test_incoming_sco_accepts_native_mac_string(monkeypatch):
    store = Store(":memory:")
    backend = Backend(Config(), store)
    confirmed = asyncio.Event()
    confirmed.set()
    backend.connections[DEVICE] = SimpleNamespace(
        codec_confirmed=confirmed, codec=1, closed=False, state="incoming"
    )
    backend.current[DEVICE] = store.new_call(DEVICE, "123", "incoming", "incoming")
    host, peer = socket.socketpair(type=socket.SOCK_SEQPACKET)
    listener, listener_peer = socket.socketpair()

    async def address():
        return "AA:BB:CC:DD:EE:FF"

    async def accept(sock):
        return host, "11:22:33:44:55:66"  # native CPython SCO sockaddr representation

    backend.bluez.adapter_address = address
    monkeypatch.setattr("agentcall.service.backend.sco_listen", lambda *args: listener.detach())
    monkeypatch.setattr(
        "agentcall.service.backend.SCOAudio", lambda sock, codec: SCOAudio(sock, codec, mtu=48)
    )
    monkeypatch.setattr(asyncio.get_running_loop(), "sock_accept", accept)
    try:
        await backend.open_audio(DEVICE)
        assert backend.audio[DEVICE].status()["ready"]
        await backend.audio[DEVICE].send(b"\x01\x02")
        assert peer.recv(10) == b"\x01\x02"
    finally:
        backend.stop_audio(DEVICE)
        peer.close()
        listener.close()
        listener_peer.close()
        store.close()
