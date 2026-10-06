import asyncio

import numpy as np
import soxr


class PCMResampler:
    def __init__(self, input_rate, output_rate):
        self.input_rate = input_rate
        self.output_rate = output_rate
        self.stream = soxr.ResampleStream(input_rate, output_rate, 1, dtype="int16", quality="LQ")

    def convert(self, pcm, last=False):
        if len(pcm) % 2:
            raise ValueError("PCM requires whole 16-bit samples")
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.int16, copy=False)
        return self.stream.resample_chunk(samples, last=last).astype("<i2", copy=False).tobytes()

    def delay_ms(self):
        return self.stream.delay() * 1000 / self.output_rate


class AudioBridge:
    def __init__(self, audio, provider):
        self.audio = audio
        self.provider = provider
        self.input = PCMResampler(audio.rate, provider.input_rate)
        self.output = PCMResampler(provider.output_rate, audio.rate)
        self.queue = asyncio.Queue(maxsize=8)
        self.pending_bytes = 0
        self.received_bytes = 0
        self.sent_bytes = 0

    async def input_loop(self):
        while True:
            pcm = self.input.convert(await self.audio.receive())
            if pcm:
                await self.provider.send_audio(pcm)
                self.received_bytes += len(pcm)

    async def enqueue(self, pcm):
        self.pending_bytes += len(pcm)
        try:
            await self.queue.put(pcm)
        except BaseException:
            self.pending_bytes -= len(pcm)
            raise

    async def finish_response(self):
        # Flush only at the API's output sequence boundary, never on local interruption policy.
        await self.queue.put(None)

    async def output_loop(self):
        while True:
            pcm = await self.queue.get()
            try:
                if pcm is None:
                    converted = self.output.convert(b"", last=True)
                    self.output = PCMResampler(self.provider.output_rate, self.audio.rate)
                    if converted:
                        await self.audio.send(converted)
                        self.sent_bytes += len(converted)
                    await self.audio.finish_output()
                else:
                    converted = self.output.convert(pcm)
                    if converted:
                        await self.audio.send(converted)
                        self.sent_bytes += len(converted)
                    self.pending_bytes -= len(pcm)
            finally:
                self.queue.task_done()

    async def drained(self):
        await self.queue.join()
        await self.audio.wait_playout()

    def status(self):
        return {
            "input": {
                "format": "s16le",
                "channels": 1,
                "from_rate": self.audio.rate,
                "to_rate": self.provider.input_rate,
                "resampler_pending_ms": self.input.delay_ms(),
            },
            "output": {
                "format": "s16le",
                "channels": 1,
                "from_rate": self.provider.output_rate,
                "to_rate": self.audio.rate,
                "resampler_pending_ms": self.output.delay_ms(),
            },
            "pending_ms": self.pending_bytes * 1000 / (self.provider.output_rate * 2),
            "model_input_bytes": self.received_bytes,
            "sco_output_bytes": self.sent_bytes,
            "sco": self.audio.status(),
        }
