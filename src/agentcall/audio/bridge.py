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
        # Never wait for realtime playout in the provider event reader. Bound both bytes and items.
        self.queue = asyncio.Queue(maxsize=4096)
        self.max_pending_bytes = provider.output_rate * 2 * 60
        self.generation = 0
        self.items = {}
        self.playing = None
        self.pending_bytes = 0
        self.received_bytes = 0
        self.sent_bytes = 0

    async def input_loop(self):
        while True:
            pcm = self.input.convert(await self.audio.receive())
            if pcm:
                await self.provider.send_audio(pcm)
                self.received_bytes += len(pcm)

    async def enqueue(self, pcm, response_id=None, item_id=None, content_index=0):
        if self.pending_bytes + len(pcm) > self.max_pending_bytes:
            raise RuntimeError("model audio exceeded the bounded playback backlog")
        key = (response_id, item_id, content_index)
        item = self.items.setdefault(key, {"generated": 0, "tx_start": None, "tx_end": None})
        item["generated"] += len(pcm)
        self.pending_bytes += len(pcm)
        try:
            self.queue.put_nowait((self.generation, key, pcm))
        except BaseException:
            self.pending_bytes -= len(pcm)
            raise

    async def finish_response(self, response_id=None):
        self.queue.put_nowait((self.generation, response_id, None))

    async def interrupt(self):
        self.generation += 1
        while not self.queue.empty():
            _, _, pcm = self.queue.get_nowait()
            if pcm is not None:
                self.pending_bytes -= len(pcm)
            self.queue.task_done()
        await self.audio.interrupt_output()
        truncations = []
        responses = {key[0] for key in self.items if key[0] is not None}
        for key, item in self.items.items():
            played = 0
            if item["tx_start"] is not None:
                played = (item["tx_end"] - item["tx_start"]) * 1000 / (self.audio.rate * 2)
                if key == self.playing:
                    played -= (
                        max(0, self.audio.playout_until - asyncio.get_running_loop().time()) * 1000
                    )
            generated = item["generated"] * 1000 / (self.provider.output_rate * 2)
            played = max(0, min(played, generated))
            if key[1] and played < generated:
                truncations.append((key[1], key[2], int(played)))
        self.items.clear()
        self.playing = None
        self.output = PCMResampler(self.provider.output_rate, self.audio.rate)
        return responses, truncations

    async def write(self, pcm, item):
        before = self.audio.tx_bytes
        if item is not None and item["tx_start"] is None:
            item["tx_start"] = before
        await self.audio.send(pcm)
        self.sent_bytes += self.audio.tx_bytes - before
        if item is not None:
            item["tx_end"] = self.audio.tx_bytes

    async def output_loop(self, *, start_delay=0):
        # Gate playback once at call startup; input and provider events remain live.
        if start_delay:
            await asyncio.sleep(start_delay)
        while True:
            generation, key, pcm = await self.queue.get()
            try:
                if generation != self.generation:
                    continue
                if pcm is None:
                    item = self.items.get(self.playing)
                    converted = self.output.convert(b"", last=True)
                    self.output = PCMResampler(self.provider.output_rate, self.audio.rate)
                    if converted:
                        await self.write(converted, item)
                    if generation != self.generation:
                        continue
                    await self.audio.finish_output()
                    if item is not None:
                        item["tx_end"] = self.audio.tx_bytes
                    await self.audio.wait_playout()
                    if generation == self.generation:
                        self.items = {k: v for k, v in self.items.items() if k[0] != key}
                        self.playing = None
                else:
                    self.playing = key
                    item = self.items[key]
                    converted = self.output.convert(pcm)
                    if converted:
                        await self.write(converted, item)
            finally:
                if pcm is not None:
                    self.pending_bytes -= len(pcm)
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
