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


async def test_audio_listener_survives_long_ringing_before_answer(monkeypatch):
    store = Store(":memory:")
    backend = Backend(Config(), store)
    confirmed = asyncio.Event()
    confirmed.set()
    connection = SimpleNamespace(codec_confirmed=confirmed, codec=1, closed=False, state="alerting")
    backend.connections[DEVICE] = connection
    backend.current[DEVICE] = store.new_call(DEVICE, "123", "outgoing", "alerting")
    host, peer = socket.socketpair(type=socket.SOCK_SEQPACKET)
    listener, listener_peer = socket.socketpair()
    attempts = 0

    async def address():
        return "AA:BB:CC:DD:EE:FF"

    async def accept(sock):
        nonlocal attempts
        attempts += 1
        if attempts <= 25:
            raise TimeoutError
        connection.state = "active"
        return host, "11:22:33:44:55:66"

    def no_outbound(*args, **kwargs):
        raise AssertionError("outbound SCO must not run before answer")

    backend.bluez.adapter_address = address
    monkeypatch.setattr("agentcall.service.backend.sco_listen", lambda *args: listener.detach())
    monkeypatch.setattr("agentcall.service.backend._sco_connect", no_outbound)
    monkeypatch.setattr(
        "agentcall.service.backend.SCOAudio", lambda sock, codec: SCOAudio(sock, codec, mtu=48)
    )
    monkeypatch.setattr(asyncio.get_running_loop(), "sock_accept", accept)
    try:
        await backend.open_audio(DEVICE)
        assert attempts == 26 and backend.audio[DEVICE].status()["ready"]
        assert not store.db.execute("SELECT 1 FROM events WHERE kind='audio.error'").fetchone()
    finally:
        backend.stop_audio(DEVICE)
        host.close()
        peer.close()
        listener.close()
        listener_peer.close()
        store.close()


def test_sco_connector_preserves_errno_and_closes_failed_fd(monkeypatch):
    import ctypes
    import errno

    import pytest

    from agentcall.audio.socket import _sco_connect

    class Libc:
        closed = []

        def socket(self, *args):
            return 42

        def setsockopt(self, *args):
            return 0

        def bind(self, *args):
            return 0

        def connect(self, *args):
            ctypes.set_errno(errno.ECONNREFUSED)
            return -1

        def close(self, fd):
            self.closed.append(fd)

    libc = Libc()
    monkeypatch.setattr("agentcall.audio.socket._load_libc", lambda: libc)
    with pytest.raises(OSError, match="SCO connect failed") as error:
        _sco_connect("11:22:33:44:55:66", raise_errors=True)
    assert error.value.errno == errno.ECONNREFUSED
    assert libc.closed == [42]
    assert _sco_connect("11:22:33:44:55:66") == -1
    assert libc.closed == [42, 42]
