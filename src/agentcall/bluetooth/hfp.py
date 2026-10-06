"""Async RFCOMM SLC. Commands are serialized; only AG events set call state."""

import asyncio
import logging
import re
from contextlib import suppress

from agentcall.vendor import at

log = logging.getLogger(__name__)


class HFPError(RuntimeError):
    pass


class HFPConnection:
    def __init__(self, sock, device, emit, preferred_codec="cvsd", timeout=10):
        self.sock = sock
        self.sock.setblocking(False)
        self.device = device
        self.emit = emit
        self.preferred_codec = preferred_codec
        self.timeout = timeout
        self.ready = False
        self.codec = 1
        self.codec_confirmed = asyncio.Event()
        self.indicators = {}
        self.values = {}
        self.state = "unknown"
        self.number = ""
        self.pending = None
        self.lock = asyncio.Lock()
        self.reader = None
        self.keepalive = None
        self.closed = False
        self.features = 0

    def notify(self, kind, **data):
        self.emit(kind, device=self.device, **data)

    async def start(self):
        self.reader = asyncio.create_task(self.read_loop())
        try:
            await self.command(at.cmd_brsf(self.preferred_codec))
            # AG codec negotiation feature is bit 9; HF feature is bit 7.
            if self.preferred_codec == "msbc" and self.features & (1 << 9):
                await self.command(at.cmd_bac("msbc"))
            await self.command(at.cmd_cind_test())
            if not {"call", "callsetup"}.issubset(self.indicators.values()):
                raise HFPError("phone did not supply mandatory call indicators")
            await self.command(at.cmd_cind_read())
            await self.command(at.cmd_cmer())
            # Optional caller presentation must finish before accepting API commands.
            with suppress(HFPError):
                await self.command(at.cmd_clip_enable())
            if self.closed:
                raise HFPError("HFP closed during optional command")
            if not (self.preferred_codec == "msbc" and self.features & (1 << 9)):
                self.codec_confirmed.set()
            self.ready = True
            self.notify("hfp.ready", state=self.state, codec=self.codec)
            self.keepalive = asyncio.create_task(self.keepalive_loop())
        except BaseException:
            await self.close()
            raise

    async def command(self, command):
        if self.closed:
            raise HFPError("HFP disconnected")
        async with self.lock:
            future = asyncio.get_running_loop().create_future()
            self.pending = future
            try:
                await asyncio.get_running_loop().sock_sendall(
                    self.sock, (command + "\r").encode("ascii")
                )
                # Direct awaiting preserves cancellation when OK arrives in the same loop turn
                # (Python 3.11 wait_for can otherwise consume that cancellation).
                async with asyncio.timeout(self.timeout):
                    return await future
            except TimeoutError as exc:
                # Late OK must never be mistaken for the next command's response.
                await self.close()
                raise HFPError("AT response timeout; connection closed") from exc
            except asyncio.CancelledError:
                # A canceled in-flight command has the same late-response ambiguity.
                await self.close()
                raise
            except OSError as exc:
                await self.close()
                raise HFPError("RFCOMM write failed") from exc
            finally:
                self.pending = None

    async def read_loop(self):
        buffer = bytearray()
        try:
            while True:
                chunk = await asyncio.get_running_loop().sock_recv(self.sock, 4096)
                if not chunk:
                    break
                buffer.extend(chunk)
                while True:
                    match = re.search(b"[\r\n]", buffer)
                    if not match:
                        break
                    line = bytes(buffer[: match.start()]).decode("ascii", errors="replace").strip()
                    del buffer[: match.end()]
                    if line:
                        self.dispatch(line)
                if len(buffer) > 65536:
                    raise HFPError("oversized AT line")
        except (OSError, HFPError) as exc:
            self.notify("hfp.error", error=str(exc))
        finally:
            await self.close()

    def dispatch(self, line):
        event = at.parse_line(line)
        kind, data = event.kind, event.params
        if kind == "brsf":
            self.features = data["features"]
        elif kind == "cind_format":
            self.indicators = dict(enumerate(re.findall(r'"([^\"]+)"', line), 1))
        elif kind == "cind_values":
            self.values.update(
                {self.indicators.get(i, str(i)): v for i, v in enumerate(data["values"], 1)}
            )
            self.update_state()
        elif kind == "ciev":
            self.values[self.indicators.get(data["index"], str(data["index"]))] = data["value"]
            self.update_state()
        elif kind == "ring":
            self.values["callsetup"] = 1
            self.update_state()
        elif kind == "clip":
            self.number = data["number"]
            self.notify("call.number", number=self.number)
        elif kind == "bcs":
            codec = data["codec"]
            if codec not in (1, 2) or (codec == 2 and self.preferred_codec != "msbc"):
                self.notify("hfp.error", error="phone selected unsupported codec")
                asyncio.create_task(self.close())
            else:
                asyncio.create_task(self.confirm_codec(codec))
        elif kind in ("ok", "error", "cme_error"):
            if self.pending and not self.pending.done():
                if kind == "ok":
                    self.pending.set_result(None)
                else:
                    self.pending.set_exception(HFPError(line))
        elif kind in ("no_carrier", "busy", "no_answer"):
            self.values.update(call=0, callsetup=0, callheld=0)
            self.update_state(reason=kind)
            if self.pending and not self.pending.done():
                self.pending.set_exception(HFPError(line))

    async def confirm_codec(self, codec):
        try:
            await self.command(at.cmd_bcs_confirm(codec))
            self.codec = codec
            self.codec_confirmed.set()
            self.notify("audio.codec", codec=codec)
        except HFPError as exc:
            self.notify("hfp.error", error=str(exc))

    def update_state(self, reason=None):
        call, setup, held = (self.values.get(k, 0) for k in ("call", "callsetup", "callheld"))
        state = (
            "held"
            if held == 2
            else "active"
            if call
            else {1: "incoming", 2: "dialing", 3: "alerting"}.get(setup, "idle")
        )
        if state != self.state or reason:
            old = self.state
            self.state = state
            self.notify("call.state", state=state, previous=old, number=self.number, reason=reason)
            if state == "idle":
                self.number = ""

    async def dial(self, number):
        command = at.cmd_dial(number)  # validate before changing local state
        if not self.ready or self.state != "idle":
            raise HFPError("phone is not ready or already has a call")
        self.number = number
        # Dial command acceptance is not proof that the call connected.
        await self.command(command)

    async def dtmf(self, digits):
        if not re.fullmatch(r"[0-9*#]{1,64}", digits):
            raise ValueError("DTMF must be 1–64 digits, * or #")
        if self.state != "active":
            raise HFPError("DTMF requires an active call")
        for digit in digits:
            await self.command(at.cmd_dtmf(digit))

    async def answer(self):
        if self.state != "incoming":
            raise HFPError("no incoming call")
        await self.command(at.cmd_answer())

    async def hangup(self):
        if self.state in ("idle", "unknown"):
            raise HFPError("no current call")
        await self.command(at.cmd_hangup())

    async def keepalive_loop(self):
        try:
            while True:
                await asyncio.sleep(10)
                await self.command(at.cmd_cind_read())
        except HFPError:
            await self.close()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.ready = False
        self.sock.close()
        if self.pending and not self.pending.done():
            self.pending.set_exception(HFPError("HFP disconnected"))
        current = asyncio.current_task()
        for task in (self.reader, self.keepalive):
            if task and task is not current:
                task.cancel()
        self.state = "disconnected"
        self.notify("hfp.disconnected")
