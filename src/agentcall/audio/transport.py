"""Full duplex native s16le PCM. No VAD, duration batching, or buffer clearing."""

import asyncio
import logging
import struct

from agentcall.vendor import msbc

log = logging.getLogger(__name__)


class SCOAudio:
    def __init__(self, sock, codec, mtu=None):
        self.sock = sock
        sock.setblocking(False)
        self.codec = codec
        if codec == 2 and not msbc.AVAILABLE:
            sock.close()
            raise RuntimeError("mSBC negotiated but libsbc unavailable")
        self.rate = 16000 if codec == 2 else 8000
        # struct sco_options { uint16_t mtu; }; never hard-code controller MTU.
        self.mtu = mtu or struct.unpack("@H", sock.getsockopt(17, 1, 2))[0]
        if self.mtu < 2:
            sock.close()
            raise RuntimeError("invalid SCO MTU")
        self.decoder = msbc.MSBCCodec() if codec == 2 else None
        self.encoder = msbc.MSBCCodec() if codec == 2 else None
        self.rx_buffer = bytearray()
        self.tx_buffer = bytearray()
        self.pending_bytes = 0
        self.rx_bytes = self.tx_bytes = 0
        self.owner = False
        self.closed = False
        self.write_lock = asyncio.Lock()
        log.info(
            "SCO ready: input/output=s16le mono %sHz codec=%s mtu=%s", self.rate, codec, self.mtu
        )

    def status(self):
        return {
            "ready": not self.closed,
            "format": "s16le",
            "channels": 1,
            "sample_rate": self.rate,
            "codec": self.codec,
            "mtu": self.mtu,
            "pending_ms": (self.pending_bytes + len(self.tx_buffer)) * 1000 / (self.rate * 2),
            "rx_bytes": self.rx_bytes,
            "tx_bytes": self.tx_bytes,
        }

    async def receive(self):
        while not self.closed:
            if self.codec == 2:
                # Drain all complete buffered frames before waiting for another SCO packet.
                while len(self.rx_buffer) >= 60:
                    if self.rx_buffer[0] != 1 or self.rx_buffer[1] not in (8, 56, 200, 248):
                        del self.rx_buffer[0]
                        continue
                    packet = bytes(self.rx_buffer[:60])
                    del self.rx_buffer[:60]
                    pcm = self.decoder.decode(packet)
                    if pcm:
                        self.rx_bytes += len(pcm)
                        return pcm
                    log.warning("mSBC decode failed; preserving negotiated codec")
            try:
                data = await asyncio.get_running_loop().sock_recv(self.sock, max(self.mtu, 60))
            except OSError:
                self.close()
                raise
            if not data:
                self.close()
                raise ConnectionError("SCO EOF")
            if self.codec == 1:
                self.rx_bytes += len(data)
                return data
            self.rx_buffer.extend(data)
        raise ConnectionError("SCO closed")

    async def send_packet(self, packet):
        # sock_sendall may split a partial write, which would corrupt SCO packet boundaries.
        loop = asyncio.get_running_loop()
        while True:
            try:
                written = self.sock.send(packet)
                if written != len(packet):
                    raise ConnectionError("partial SCO packet write")
                return
            except BlockingIOError:
                ready = loop.create_future()

                def writable(ready=ready):
                    if not ready.done():
                        ready.set_result(None)

                loop.add_writer(self.sock.fileno(), writable)
                try:
                    await ready
                finally:
                    if self.sock.fileno() >= 0:
                        loop.remove_writer(self.sock.fileno())

    async def send(self, pcm):
        if len(pcm) % 2:
            raise ValueError("s16le requires whole 16-bit samples")
        if self.closed:
            raise ConnectionError("SCO closed")
        async with self.write_lock:
            self.pending_bytes = len(pcm)
            try:
                if self.codec == 1:
                    # Forward immediately; split only at the controller's MTU.
                    size = self.mtu - self.mtu % 2
                    for offset in range(0, len(pcm), size):
                        part = pcm[offset : offset + size]
                        await self.send_packet(part)
                        self.pending_bytes -= len(part)
                        self.tx_bytes += len(part)
                else:
                    self.tx_buffer.extend(pcm)
                    self.pending_bytes = 0
                    while len(self.tx_buffer) >= msbc.PCM_BYTES:
                        frame = bytes(self.tx_buffer[: msbc.PCM_BYTES])
                        del self.tx_buffer[: msbc.PCM_BYTES]
                        packet = self.encoder.encode(frame)
                        for offset in range(0, len(packet), self.mtu):
                            await self.send_packet(packet[offset : offset + self.mtu])
                        self.tx_bytes += len(frame)
            finally:
                self.pending_bytes = 0

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.sock.close()
        for codec in (self.decoder, self.encoder):
            if codec:
                codec.close()
