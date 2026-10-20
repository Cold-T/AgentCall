import asyncio
import socket

import pytest

from agentcall.audio.transport import SCOAudio
from agentcall.vendor import msbc


async def test_cvsd_full_duplex_arbitrary_chunks_no_duration_accumulation():
    host, peer = socket.socketpair(type=socket.SOCK_SEQPACKET)
    peer.setblocking(False)
    audio = SCOAudio(host, 1, mtu=48)
    try:
        # Short input is forwarded immediately, not held until a fixed-duration block.
        await audio.send(b"\x01\x02")
        assert await asyncio.get_running_loop().sock_recv(peer, 100) == b"\x01\x02"
        incoming = asyncio.create_task(audio.receive())
        await asyncio.get_running_loop().sock_sendall(peer, b"\x03\x04" * 9)
        await audio.send(b"\x05\x06" * 35)
        assert await incoming == b"\x03\x04" * 9
        packets = [await asyncio.get_running_loop().sock_recv(peer, 100) for _ in range(2)]
        assert list(map(len, packets)) == [48, 22]
        assert b"".join(packets) == b"\x05\x06" * 35
        assert audio.status()["pending_ms"] == 0
        assert audio.status()["sample_rate"] == 8000
        with pytest.raises(ValueError):
            await audio.send(b"1")
    finally:
        audio.close()
        peer.close()


async def test_receive_eof_and_close_is_idempotent():
    host, peer = socket.socketpair(type=socket.SOCK_SEQPACKET)
    audio = SCOAudio(host, 1, mtu=48)
    peer.close()
    try:
        with pytest.raises(ConnectionError):
            await audio.receive()
    finally:
        audio.close()
        audio.close()


@pytest.mark.skipif(not msbc.AVAILABLE, reason="real libsbc not installed")
async def test_real_msbc_roundtrip_split_frames():
    host, peer = socket.socketpair(type=socket.SOCK_SEQPACKET)
    peer.setblocking(False)
    audio = SCOAudio(host, 2, mtu=24)
    decoder = msbc.MSBCCodec()
    try:
        await audio.send(bytes(100))
        assert audio.status()["pending_ms"] == 100 * 1000 / 32000
        await audio.send(bytes(140))
        packet = b"".join([await asyncio.get_running_loop().sock_recv(peer, 100) for _ in range(3)])
        assert len(packet) == 60
        assert len(decoder.decode(packet)) == 240
        await asyncio.get_running_loop().sock_sendall(peer, packet[:24])
        await asyncio.get_running_loop().sock_sendall(peer, packet[24:48])
        await asyncio.get_running_loop().sock_sendall(peer, packet[48:])
        assert len(await asyncio.wait_for(audio.receive(), 1)) == 240
    finally:
        decoder.close()
        audio.close()
        peer.close()


@pytest.mark.skipif(not msbc.AVAILABLE, reason="real libsbc not installed")
async def test_coalesced_msbc_frames_are_returned_without_waiting_for_next_packet():
    host, peer = socket.socketpair(type=socket.SOCK_SEQPACKET)
    peer.setblocking(False)
    audio = SCOAudio(host, 2, mtu=120)
    encoder = msbc.MSBCCodec()
    try:
        data = encoder.encode(bytes(240)) + encoder.encode(bytes(240))
        await asyncio.get_running_loop().sock_sendall(peer, data)
        assert len(await asyncio.wait_for(audio.receive(), 0.1)) == 240
        assert len(await asyncio.wait_for(audio.receive(), 0.1)) == 240
        with pytest.raises(ValueError):
            encoder.encode(bytes(238))
        encoder.close()
        encoder.close()
    finally:
        audio.close()
        encoder.close()
        peer.close()


async def test_audio_prime_retains_first_pcm_and_counts_it_once():
    from types import SimpleNamespace

    host, peer = socket.socketpair(type=socket.SOCK_SEQPACKET)
    audio = SCOAudio(host, 1, mtu=48)
    received = []
    try:
        peer.send(b"\x03\x04" * 9)
        await audio.prime()
        assert audio.rx_bytes == 18
        audio.recorder = SimpleNamespace(write=lambda channel, pcm: received.append((channel, pcm)))
        assert await audio.receive() == b"\x03\x04" * 9
        assert audio.rx_bytes == 18 and received == [(0, b"\x03\x04" * 9)]
    finally:
        audio.close()
        peer.close()


async def test_packet_pacing_does_not_accumulate_timer_overshoot(monkeypatch):
    from types import SimpleNamespace

    from agentcall.audio import transport

    clock = SimpleNamespace(now=100.0)

    async def sleep(delay):
        clock.now += delay + 0.001

    monkeypatch.setattr(
        transport,
        "asyncio",
        SimpleNamespace(
            get_running_loop=lambda: SimpleNamespace(time=lambda: clock.now),
            sleep=sleep,
        ),
    )
    audio = object.__new__(SCOAudio)
    audio.sock = SimpleNamespace(send=lambda packet: len(packet))
    audio.send_at = audio.playout_until = 0.0
    for _ in range(500):
        await audio.send_packet(bytes(160), 0.01)
    assert clock.now == pytest.approx(104.981, abs=0.011)
    assert audio.playout_until >= clock.now + 0.009
    assert audio.playout_until - clock.now < 0.021
    # Long scheduling stalls must not trigger an unlimited burst of old packets.
    clock.now += 10
    await audio.send_packet(bytes(160), 0.01)
    await audio.send_packet(bytes(160), 0.01)
    before = clock.now
    await audio.send_packet(bytes(160), 0.01)
    assert clock.now - before >= 0.009
