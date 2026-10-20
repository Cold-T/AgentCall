"""Offline continuous-audio playback and finite farewell boundaries."""

import asyncio
import socket
from contextlib import asynccontextmanager
from types import SimpleNamespace

import numpy as np
import pytest

from agentcall.audio.bridge import AudioBridge
from agentcall.audio.transport import SCOAudio


@asynccontextmanager
async def live_bridge(rate=24000):
    host, peer = socket.socketpair(type=socket.SOCK_SEQPACKET)
    peer.setblocking(False)
    audio = SCOAudio(host, 1, mtu=48)
    bridge = AudioBridge(
        audio,
        SimpleNamespace(
            input_rate=rate,
            output_rate=rate,
            continuous_audio=True,
        ),
    )
    output = bytearray()

    async def receive():
        while True:
            output.extend(await asyncio.get_running_loop().sock_recv(peer, 100))

    receiver = asyncio.create_task(receive())
    player = asyncio.create_task(bridge.output_loop())
    try:
        yield bridge, output
    finally:
        receiver.cancel()
        player.cancel()
        await asyncio.gather(receiver, player, return_exceptions=True)
        audio.close()
        peer.close()


async def test_continuous_audio_plays_without_response_events_or_item_growth():
    async with live_bridge() as (bridge, output):
        for index in range(30):
            await bridge.enqueue(bytes(480), response_id=str(index), item_id=str(index))
            await bridge.queue.join()
        assert len(output) > 0
        assert bridge.items == {}
        before = bridge.output
        await bridge.finish_response("obsolete-turn")
        assert bridge.queue.empty()
        assert bridge.output is before
        await bridge.finish_stream()
        await bridge.drained()
        assert bridge.audio.tx_bytes == 30 * 480 // 3
        assert bridge.status()["playback_high_water_ms"] > 0


async def test_live_backlog_fails_explicitly_instead_of_dropping_audio():
    async with live_bridge() as (bridge, output):
        await bridge.enqueue(bytes(24000 * 2 * 2))
        with pytest.raises(RuntimeError, match="2000ms"):
            await bridge.enqueue(bytes(2))
        assert bridge.pending_bytes == 96000
        assert bridge.status()["playback_latency_ms"] == 2000
        assert bridge.status()["playback_high_water_ms"] == 2000
        await bridge.interrupt()
        assert bridge.pending_bytes == 0
        assert not output


async def test_clip_preserves_resampling_tail_and_waits_until_played():
    async with live_bridge() as (bridge, output):
        pcm = np.full(2424, 2000, dtype="<i2").tobytes()
        started = asyncio.get_running_loop().time()
        assert await bridge.play_clip(pcm)
        assert asyncio.get_running_loop().time() - started >= 0.100
        assert bridge.audio.tx_bytes == 808 * 2
        assert len(output) == 808 * 2
        assert bridge.output.delay_ms() == 0
        assert bridge.status()["playback_latency_ms"] == 0


async def test_interrupt_cancels_farewell_without_playing_remaining_clip():
    async with live_bridge() as (bridge, output):
        clip = asyncio.create_task(bridge.play_clip(bytes(24000 * 2)))
        await asyncio.sleep(0.03)
        await asyncio.wait_for(bridge.interrupt(), 0.1)
        assert await asyncio.wait_for(clip, 0.1) is False
        assert len(output) < 8000 // 4
        assert bridge.pending_bytes == 0
        assert bridge.items == {}


async def test_latency_includes_transport_and_resampling_without_double_counting():
    async with live_bridge() as (bridge, _):
        bridge.pending_bytes = bridge.active_bytes = 4800
        bridge.audio.pending_bytes = 800
        bridge.audio.tx_buffer.extend(bytes(160))
        bridge.audio.playout_until = asyncio.get_running_loop().time() + 0.02
        assert 79 <= bridge.playback_latency_ms() <= 80
        bridge.pending_bytes = bridge.active_bytes = 0
        bridge.audio.pending_bytes = 0


async def test_malformed_clip_does_not_interrupt_current_playback():
    async with live_bridge() as (bridge, _):
        generation = bridge.generation
        with pytest.raises(ValueError, match="whole 16-bit"):
            await bridge.play_clip(b"x")
        assert bridge.generation == generation


async def test_farewell_does_not_discard_queued_answer():
    async with live_bridge() as (bridge, output):
        answer = np.full(2400, 1000, dtype="<i2").tobytes()
        farewell = np.full(2400, 2000, dtype="<i2").tobytes()
        await bridge.enqueue(answer)
        assert await bridge.play_clip(farewell)
        # Both complete clips must reach SCO, including each resampler tail.
        assert len(output) == 3200
        samples = np.frombuffer(output, dtype="<i2")
        assert 900 < np.mean(samples[:700]) < 1100
        assert 1900 < np.mean(samples[900:]) < 2100
