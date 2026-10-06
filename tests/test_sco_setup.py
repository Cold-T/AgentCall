import asyncio
import socket
from types import SimpleNamespace

import pytest
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
        codec_selected=confirmed, codec_confirmed=confirmed, codec=1, closed=False, state="incoming"
    )
    backend.current[DEVICE] = store.new_call(DEVICE, "123", "incoming", "incoming")
    host, peer = socket.socketpair(type=socket.SOCK_SEQPACKET)
    listener, listener_peer = socket.socketpair()

    async def address():
        return "AA:BB:CC:DD:EE:FF"

    async def accept(sock):
        return host, "11:22:33:44:55:66"  # native CPython SCO sockaddr representation

    backend.bluez.adapter_address = address
    monkeypatch.setattr(
        "agentcall.service.backend.sco_listen", lambda *args, **kwargs: listener.detach()
    )
    monkeypatch.setattr(
        "agentcall.service.backend.SCOAudio",
        lambda sock, codec, mtu: SCOAudio(sock, codec, mtu=mtu),
    )
    monkeypatch.setattr("agentcall.service.backend.sco_authorize", lambda *args: None)

    async def connected(sock, timeout):
        return 48

    monkeypatch.setattr("agentcall.service.backend.sco_wait_connected", connected)
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
    connection = SimpleNamespace(
        codec_selected=confirmed, codec_confirmed=confirmed, codec=1, closed=False, state="alerting"
    )
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
    monkeypatch.setattr(
        "agentcall.service.backend.sco_listen", lambda *args, **kwargs: listener.detach()
    )
    monkeypatch.setattr("agentcall.service.backend._sco_connect", no_outbound)
    monkeypatch.setattr(
        "agentcall.service.backend.SCOAudio",
        lambda sock, codec, mtu: SCOAudio(sock, codec, mtu=mtu),
    )
    monkeypatch.setattr("agentcall.service.backend.sco_authorize", lambda *args: None)

    async def connected(sock, timeout):
        return 48

    monkeypatch.setattr("agentcall.service.backend.sco_wait_connected", connected)
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


@pytest.mark.parametrize("selected_codec, voice", [(1, 0x0060), (2, 0x0003)])
async def test_listens_before_codec_confirmation_and_authorizes_selected_codec(
    monkeypatch, selected_codec, voice
):
    store = Store(":memory:")
    backend = Backend(Config(), store)
    confirmed = asyncio.Event()
    listener, listener_peer = socket.socketpair()
    host, peer = socket.socketpair(type=socket.SOCK_SEQPACKET)
    steps = []
    connection = SimpleNamespace(
        codec_selected=confirmed, codec_confirmed=confirmed, codec=1, closed=False, state="alerting"
    )

    async def address():
        return "AA:BB:CC:DD:EE:FF"

    def listen(local, voice, *, defer_setup):
        assert defer_setup
        steps.append("listen")
        return listener.detach()

    async def command(value):
        assert steps == ["listen"]  # SCO may arrive while the BCS exchange is completing.
        connection.codec = selected_codec
        confirmed.set()
        steps.append("codec")

    async def accept(sock):
        return host, "11:22:33:44:55:66"

    def authorize(sock, setting):
        assert setting == voice
        steps.append("authorize")

    def audio(sock, codec, mtu):
        assert codec == selected_codec and steps == ["listen", "codec", "authorize"]
        return SCOAudio(sock, 1, mtu=48)  # Avoid requiring libsbc on CI.

    connection.command = command
    backend.connections[DEVICE] = connection
    backend.current[DEVICE] = store.new_call(DEVICE, "123", "outgoing", "alerting")
    backend.bluez.adapter_address = address
    monkeypatch.setattr("agentcall.service.backend.sco_listen", listen)
    monkeypatch.setattr("agentcall.service.backend.sco_authorize", authorize)
    monkeypatch.setattr("agentcall.service.backend.SCOAudio", audio)

    async def connected(sock, timeout):
        return 48

    monkeypatch.setattr("agentcall.service.backend.sco_wait_connected", connected)
    monkeypatch.setattr(asyncio.get_running_loop(), "sock_accept", accept)
    try:
        await backend.open_audio(DEVICE)
        assert backend.audio[DEVICE].status()["ready"]
    finally:
        backend.stop_audio(DEVICE)
        host.close()
        peer.close()
        listener.close()
        listener_peer.close()
        store.close()


def test_deferred_authorization_zero_read_is_not_audio_eof():
    from agentcall.audio.socket import sco_authorize

    class Socket:
        def setsockopt(self, level, option, value):
            assert (level, option, value) == (274, 11, b"\x03\x00")
            self.configured = True

        def recv(self, size):
            assert self.configured and size == 1
            return b""  # Linux BT_CONNECT2 authorizes the connection with a zero read.

    sco_authorize(Socket(), 0x0003)


def test_listener_enables_deferred_setup_after_bind_before_listen(monkeypatch):
    import ctypes

    from agentcall.audio.socket import sco_listen

    steps = []

    class Libc:
        def socket(self, *args):
            return 42

        def setsockopt(self, fd, level, option, value, size):
            steps.append(option)
            if option == 7:
                assert steps == [11, "bind", 7]
                assert size == 4
                assert ctypes.cast(value, ctypes.POINTER(ctypes.c_uint32)).contents.value == 1
            return 0

        def bind(self, *args):
            steps.append("bind")
            return 0

        def listen(self, *args):
            steps.append("listen")
            return 0

    monkeypatch.setattr("agentcall.audio.socket._load_libc", Libc)
    assert sco_listen("AA:BB:CC:DD:EE:FF", 0x0060, defer_setup=True) == 42
    assert steps == [11, "bind", 7, "listen"]


async def test_wait_for_deferred_hci_completion_without_consuming_audio():
    import errno

    from agentcall.audio.socket import sco_wait_connected

    class Socket:
        attempts = 0

        def getsockopt(self, *args):
            self.attempts += 1
            if self.attempts < 3:
                raise OSError(errno.ENOTCONN, "HCI setup in progress")
            return b"\x3c\x00"

    sock = Socket()
    assert await sco_wait_connected(sock, 1) == 60
    assert sock.attempts == 3


async def test_deferred_hci_wait_is_bounded_and_preserves_real_failures():
    import errno

    from agentcall.audio.socket import sco_wait_connected

    class Socket:
        error = errno.ENOTCONN

        def getsockopt(self, *args):
            raise OSError(self.error, "SCO failure")

    sock = Socket()
    with pytest.raises(TimeoutError):
        await sco_wait_connected(sock, 0.01)
    sock.error = errno.ECONNRESET
    with pytest.raises(OSError) as error:
        await sco_wait_connected(sock, 1)
    assert error.value.errno == errno.ECONNRESET


async def test_iphone_bcs_ok_waits_for_sco_authorization(monkeypatch):
    """The final AT+BCS OK must not prevent us accepting the AG's SCO request."""
    from conftest import Phone

    from agentcall.bluetooth.hfp import HFPConnection

    store = Store(":memory:")
    backend = Backend(Config(codec="msbc"), store)
    rfcomm, phone_sock = socket.socketpair()
    phone = Phone(phone_sock, features=1 << 9)
    authorized = asyncio.Event()
    original_send = phone.send

    async def send(text):
        if phone.commands[-1] == "AT+BCS=2" and text == "OK\r\n":
            await authorized.wait()
        await original_send(text)

    phone.send = send
    connection = HFPConnection(rfcomm, DEVICE, backend.emit, "msbc", timeout=0.5)
    backend.connections[DEVICE] = connection
    listener, listener_peer = socket.socketpair()
    host, peer = socket.socketpair(type=socket.SOCK_SEQPACKET)
    replacement_host, replacement_audio_peer = socket.socketpair(type=socket.SOCK_SEQPACKET)

    async def address():
        return "AA:BB:CC:DD:EE:FF"

    async def accept(sock):
        assert connection.codec_selected.is_set()
        assert not connection.codec_confirmed.is_set()
        return (host if host.fileno() >= 0 else replacement_host), "11:22:33:44:55:66"

    def authorize(sock, setting):
        assert setting == 0x0003
        authorized.set()

    async def connected(sock, timeout):
        await asyncio.wait_for(connection.codec_confirmed.wait(), timeout)
        return 48

    def audio(sock, codec, mtu):
        assert codec == 2 and connection.codec_confirmed.is_set()
        return SCOAudio(sock, 1, mtu=mtu)

    backend.bluez.adapter_address = address
    monkeypatch.setattr("agentcall.service.backend.sco_listen", lambda *a, **k: listener.detach())
    monkeypatch.setattr("agentcall.service.backend.sco_authorize", authorize)
    monkeypatch.setattr("agentcall.service.backend.sco_wait_connected", connected)
    monkeypatch.setattr("agentcall.service.backend.SCOAudio", audio)
    monkeypatch.setattr(asyncio.get_running_loop(), "sock_accept", accept)
    try:
        await connection.start()
        backend.current[DEVICE] = store.new_call(DEVICE, "123", "outgoing", "alerting")
        connection.state = "alerting"
        backend.ensure_audio(DEVICE)
        await asyncio.wait_for(backend.audio_jobs[DEVICE], 0.8)
        assert authorized.is_set()
        assert DEVICE in backend.audio
        assert not connection.closed

        # iPhone replaces ringing SCO before reporting an active call. The next
        # BCS confirmation must wait for a fresh listener, not reuse the old socket.
        first_audio = backend.audio[DEVICE]
        replacement_listener, replacement_peer = socket.socketpair()
        try:
            authorized.clear()
            connection.prepare_codec = lambda: backend.prepare_audio_codec(DEVICE)
            monkeypatch.setattr(
                "agentcall.service.backend.sco_listen",
                lambda *a, **k: replacement_listener.detach(),
            )
            await phone.send("+BCS: 2\r\n")
            from conftest import until

            await until(
                lambda: DEVICE in backend.audio and backend.audio[DEVICE] is not first_audio
            )
            assert first_audio.closed
            assert authorized.is_set()
            assert not connection.closed
        finally:
            replacement_listener.close()
            replacement_peer.close()
    finally:
        backend.stop_audio(DEVICE)
        await connection.close()
        await phone.close()
        host.close()
        peer.close()
        replacement_host.close()
        replacement_audio_peer.close()
        listener.close()
        listener_peer.close()
        store.close()
