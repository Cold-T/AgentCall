import asyncio
import socket

import numpy as np
import pytest
import soxr

from agentcall.audio.bridge import PCMResampler
from agentcall.audio.transport import SCOAudio


@pytest.mark.parametrize("rate", [8000, 16000])
@pytest.mark.parametrize("outbound", [True, False])
def test_streaming_resampling_preserves_duration_signal_and_chunk_boundaries(rate, outbound):
    input_rate, output_rate = (24000, rate) if outbound else (rate, 24000)
    samples = (np.sin(np.arange(input_rate) * 2 * np.pi * 440 / input_rate) * 12000).astype(
        np.int16
    )
    resampler = PCMResampler(input_rate, output_rate)
    output = []
    position = 0
    for size in [1, 7, 57, 480, 1201, input_rate]:
        part = samples[position : position + size]
        position += len(part)
        output.append(resampler.convert(part.astype("<i2").tobytes()))
    output.append(resampler.convert(b"", last=True))
    actual = np.frombuffer(b"".join(output), dtype="<i2")
    expected = soxr.resample(samples, input_rate, output_rate, quality="LQ")
    assert len(actual) == output_rate
    # Independent int16 conversions use libsoxr dither: allow two least-significant bits.
    assert np.max(np.abs(actual.astype(int) - expected.astype(int))) <= 2
    assert resampler.delay_ms() == 0
    peak = np.argmax(abs(np.fft.rfft(actual)))
    assert peak == 440


async def test_transport_clock_and_playout_drain():
    host, peer = socket.socketpair(type=socket.SOCK_SEQPACKET)
    audio = SCOAudio(host, 1, mtu=48)
    try:
        before = asyncio.get_running_loop().time()
        await audio.send(bytes(480))  # 30ms speech, no fixed 30ms input accumulation
        await audio.wait_playout()
        assert asyncio.get_running_loop().time() - before >= 0.029
        assert audio.status()["pending_ms"] == 0
    finally:
        audio.close()
        peer.close()
