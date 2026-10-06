import asyncio
import io
import socket
import stat
import wave

import httpx
import numpy as np
import pytest
from conftest import DEVICE

from agentcall.api.app import create_app
from agentcall.audio.recording import CallRecording, recording_path
from agentcall.audio.transport import SCOAudio
from agentcall.tasks.manager import TaskManager
from agentcall.tasks.models import TaskInput


@pytest.mark.parametrize("codec,mtu", [(1, 48), (2, 24)])
async def test_recording_contains_received_and_only_transmitted_audio(tmp_path, codec, mtu):
    from agentcall.vendor import msbc

    if codec == 2 and not msbc.AVAILABLE:
        pytest.skip("libsbc unavailable")
    host, peer = socket.socketpair(type=socket.SOCK_SEQPACKET)
    peer.setblocking(False)
    audio = SCOAudio(host, codec, mtu=mtu)
    path = tmp_path / "recordings" / "test.wav"
    audio.recorder = recorder = CallRecording(path, audio.rate)
    incoming = np.full(120, 2000, dtype="<i2").tobytes()
    encoder = msbc.MSBCCodec() if codec == 2 else None
    packet = encoder.encode(incoming) if encoder else incoming
    await asyncio.get_running_loop().sock_sendall(peer, packet)
    received = await audio.receive()
    assert received
    sending = asyncio.create_task(audio.send(np.full(audio.rate * 4, 1000, dtype="<i2").tobytes()))
    try:
        await asyncio.sleep(0.025)
        await audio.interrupt_output()
        await sending
        assert recorder.finish() == path
        with wave.open(str(path)) as recording:
            assert recording.getnchannels() == 2
            samples = np.frombuffer(
                recording.readframes(recording.getnframes()), dtype="<i2"
            ).reshape(-1, 2)
        assert np.count_nonzero(samples[:, 1]) == audio.tx_bytes // 2
        assert 0 < audio.tx_bytes < audio.rate
        assert np.any(samples[:, 0])
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        sending.cancel()
        await asyncio.gather(sending, return_exceptions=True)
        audio.close()
        peer.close()
        if encoder:
            encoder.close()


async def test_authenticated_downloads_pagination_missing_and_active_recordings(
    service, monkeypatch, tmp_path
):
    backend = service[0]
    monkeypatch.setenv("AGENTCALL_TOKEN", "test-download-key")
    backend.store.recordings_dir = tmp_path / "recordings"
    manager = TaskManager(backend, backend.config)
    task = manager.create(TaskInput(device=DEVICE, number="123", goal="Export history"))
    task_id = task["id"]
    for _ in range(999):
        backend.store.task_event(task_id, "model.transcript", {"text": ""})
    for text, finished in [("Hello ", False), ("world", True)]:
        backend.store.task_event(
            task_id,
            "model.transcript",
            {
                "role": "assistant",
                "item_id": "i1",
                "text": text,
                "delta": True,
                "finished": finished,
            },
        )
    backend.store.task_event(task_id, "model.transcript", {"role": "user", "text": "你好"})
    app = create_app(backend.config, backend, manager)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for name in ("transcript", "recording"):
            assert (await client.get(f"/tasks/{task_id}/{name}")).status_code == 401
        client.headers["Authorization"] = "Bearer test-download-key"
        assert (await client.get(f"/tasks/{task_id}/recording")).status_code == 409
        backend.store.update_task(task_id, state="ended")
        assert (await client.get(f"/tasks/{task_id}/recording")).status_code == 404
        response = await client.get(f"/tasks/{task_id}/transcript")
        assert response.status_code == 200
        assert "Hello world" in response.text and "你好" in response.text
        assert "attachment;" in response.headers["content-disposition"]
        assert response.headers["cache-control"] == "no-store"
        assert "charset=utf-8" in response.headers["content-type"]
        path = recording_path(backend.store, task_id)
        recorder = CallRecording(path, 8000)
        recorder.write(0, bytes(160))
        recorder.write(1, bytes(160))
        recorder.finish()
        result = (await client.get(f"/tasks/{task_id}")).json()
        assert result["downloads"] == {"transcript": True, "recording": True}
        response = await client.get(f"/tasks/{task_id}/recording")
        assert response.status_code == 200 and response.headers["content-type"] == "audio/wav"
        with wave.open(io.BytesIO(response.content)) as recording:
            assert recording.getnchannels() == 2
        blank = manager.create(TaskInput(device=DEVICE, number="123", goal="No transcript"))
        assert (await client.get(f"/tasks/{blank['id']}/transcript")).status_code == 404
    await manager.close()


def test_recording_disk_failure_discards_file_and_closes_tracks(tmp_path):
    recorder = CallRecording(tmp_path / "rec.wav", 8000)
    recorder.tracks[0].close()
    recorder.write(0, bytes(80))
    assert recorder.failed and recorder.finish() is None
    assert all(track.closed for track in recorder.tracks)
    assert not recorder.path.exists()
