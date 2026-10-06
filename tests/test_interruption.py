import asyncio
import socket
from types import SimpleNamespace

import numpy as np
import pytest
from conftest import DEVICE

from agentcall.audio.bridge import AudioBridge
from agentcall.audio.transport import SCOAudio
from agentcall.tasks.manager import TaskManager, TaskRun
from agentcall.tasks.models import TaskInput


@pytest.mark.parametrize("event_name", ["input_audio_buffer.speech_started", "interrupted"])
@pytest.mark.parametrize("generation_completed", [False, True])
async def test_interrupt_during_playout_drains_backlog_drops_late_audio_and_resumes(
    service, event_name, generation_completed
):
    backend = service[0]
    manager = TaskManager(backend, backend.config)
    task = manager.create(TaskInput(device=DEVICE, number="123", goal="test"))
    run = TaskRun(manager, task)
    host, peer = socket.socketpair(type=socket.SOCK_SEQPACKET)
    peer.setblocking(False)
    audio = SCOAudio(host, 1, mtu=48)
    truncations = []
    output = bytearray()
    old = np.full(2400, 1000, dtype="<i2").tobytes()
    late = np.full(1200, 15000, dtype="<i2").tobytes()
    new = np.full(480, -2000, dtype="<i2").tobytes()

    async def truncate(*args):
        truncations.append(args)

    async def events():
        yield {"kind": "response_started", "response_id": "r1"}
        yield {"kind": "audio", "response_id": "r1", "item_id": "item1", "pcm": old}
        await asyncio.sleep(0.03)
        for _ in range(39):  # Four seconds queued, well beyond the former 8-chunk queue.
            yield {"kind": "audio", "response_id": "r1", "item_id": "item1", "pcm": old}
        if generation_completed:
            yield {"kind": "response_done", "response_id": "r1", "status": "completed"}
        started = asyncio.get_running_loop().time()
        yield {"kind": "turn", "event": event_name}
        assert asyncio.get_running_loop().time() - started < 0.2
        assert run.bridge.pending_bytes == 0
        yield {"kind": "audio", "response_id": "r1", "item_id": "item1", "pcm": late}
        yield {"kind": "tool", "response_id": "r1", "name": "hangup"}  # Stale tool ignored.
        if not generation_completed:
            yield {"kind": "response_done", "response_id": "r1", "status": "cancelled"}
        yield {"kind": "response_started", "response_id": "r2"}
        yield {"kind": "audio", "response_id": "r2", "item_id": "item2", "pcm": new}
        yield {"kind": "response_done", "response_id": "r2", "status": "completed"}
        await run.bridge.drained()
        run.done.set()

    async def receive():
        while True:
            output.extend(await asyncio.get_running_loop().sock_recv(peer, 100))

    run.provider = SimpleNamespace(
        input_rate=24000, output_rate=24000, events=events, truncate_audio=truncate
    )
    run.bridge = AudioBridge(audio, run.provider)
    playing = asyncio.create_task(run.bridge.output_loop())
    receiving = asyncio.create_task(receive())
    try:
        await asyncio.wait_for(run.read_provider(), 1)
        await asyncio.sleep(0.01)
        assert len(truncations) == 1
        assert truncations[0][:2] == ("item1", 0)
        assert 0 < truncations[0][2] < 200
        samples = np.frombuffer(output, dtype="<i2")
        assert len(samples) < 8000 // 4  # Did not continue playing the four-second backlog.
        assert np.max(samples) < 5000  # Late audio never reaches the phone.
        assert np.min(samples) < -1000  # New response plays after interruption.
        assert run.bridge.pending_bytes == 0
    finally:
        playing.cancel()
        receiving.cancel()
        await asyncio.gather(playing, receiving, return_exceptions=True)
        audio.close()
        peer.close()
        await manager.close()


async def test_nonblocking_backlog_has_a_byte_limit():
    host, peer = socket.socketpair(type=socket.SOCK_SEQPACKET)
    audio = SCOAudio(host, 1, mtu=48)
    bridge = AudioBridge(audio, SimpleNamespace(input_rate=24000, output_rate=24000))
    try:
        bridge.max_pending_bytes = 100
        await bridge.enqueue(bytes(100))
        with pytest.raises(RuntimeError, match="bounded playback backlog"):
            await bridge.enqueue(bytes(2))
        await bridge.interrupt()
        assert bridge.pending_bytes == 0
        await asyncio.wait_for(bridge.drained(), 0.1)
    finally:
        audio.close()
        peer.close()


@pytest.mark.parametrize("codec,mtu", [(1, 48), (2, 24)])
async def test_transport_interruption_preserves_input_and_frame_boundaries(codec, mtu):
    from agentcall.vendor import msbc

    if codec == 2 and not msbc.AVAILABLE:
        pytest.skip("libsbc unavailable")
    host, peer = socket.socketpair(type=socket.SOCK_SEQPACKET)
    peer.setblocking(False)
    audio = SCOAudio(host, codec, mtu=mtu)
    packets = []

    async def receive():
        while True:
            packets.append(await asyncio.get_running_loop().sock_recv(peer, 100))

    receiving = asyncio.create_task(receive())
    sending = asyncio.create_task(audio.send(bytes(audio.rate * 2)))
    try:
        await asyncio.sleep(0.03)
        await asyncio.wait_for(audio.interrupt_output(), 0.1)
        await sending
        await asyncio.sleep(0.01)
        assert sum(map(len, packets)) < audio.rate // 4
        assert not audio.tx_buffer
        if codec == 2:
            assert sum(map(len, packets)) % 60 == 0
        else:
            await asyncio.get_running_loop().sock_sendall(peer, b"\x01\x02")
            assert await audio.receive() == b"\x01\x02"
    finally:
        sending.cancel()
        receiving.cancel()
        await asyncio.gather(sending, receiving, return_exceptions=True)
        audio.close()
        peer.close()
