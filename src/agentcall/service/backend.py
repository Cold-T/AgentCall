import asyncio
import logging
import os
import re
import socket

from agentcall.audio.socket import _sco_connect, sco_listen
from agentcall.audio.transport import SCOAudio
from agentcall.bluetooth.dbus import AG_UUID, BlueZ
from agentcall.bluetooth.hfp import HFPConnection, HFPError
from agentcall.bluetooth.pbap import PBAPClient
from agentcall.vendor import at, msbc

log = logging.getLogger(__name__)


class Backend:
    def __init__(self, config, store):
        self.config = config
        self.store = store
        self.bluez = BlueZ(self)
        self.connections = {}
        self.audio = {}
        self.current = {}
        self.jobs = set()
        self.subscribers = set()
        self.listeners = set()
        self.claims = {}
        self.pairing = set()
        self.error = None
        self.running = False
        self.device_locks = {}
        self.audio_jobs = {}
        self.audio_setup_lock = asyncio.Lock()
        self.pbap = PBAPClient(self.bluez.obex_call)
        self.retry = None

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.jobs.add(task)

        def done(t):
            self.jobs.discard(t)
            if not t.cancelled() and t.exception():
                self.emit("service.error", error=str(t.exception()))

        task.add_done_callback(done)
        return task

    def path(self, device):
        if re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", device):
            device = "dev_" + device.upper().replace(":", "_")
        if re.fullmatch(r"dev_(?:[0-9A-Fa-f]{2}_){5}[0-9A-Fa-f]{2}", device):
            return f"/org/bluez/{self.config.adapter}/{device}"
        if re.fullmatch(
            rf"/org/bluez/{re.escape(self.config.adapter)}/dev_(?:[0-9A-Fa-f]{{2}}_){{5}}[0-9A-Fa-f]{{2}}",
            device,
        ):
            return device
        raise ValueError("device must be a Bluetooth MAC or dev_XX_XX_XX_XX_XX_XX")

    def emit(self, kind, **data):
        device = data.get("device")
        if kind == "call.state":
            state = data["state"]
            call_id = self.current.get(device)
            if state not in ("idle", "unknown") and not call_id:
                direction = (
                    "incoming"
                    if state == "incoming"
                    else "outgoing"
                    if state in ("dialing", "alerting")
                    else "unknown"
                )
                call_id = self.store.new_call(device, data.get("number", ""), direction, state)
                self.current[device] = call_id
            if call_id:
                reason = data.get("reason") or ("phone_ended" if state == "idle" else None)
                self.store.update_call(call_id, state, reason, data.get("number") or None)
                data["call_id"] = call_id
                if state == "idle":
                    self.current.pop(device, None)
                    self.stop_audio(device)
            if state in ("incoming", "dialing", "alerting", "active"):
                self.ensure_audio(device)
        elif kind == "call.number" and device in self.current:
            row = self.store.call(self.current[device])
            self.store.update_call(row["id"], row["state"], number=data["number"])
        elif kind == "hfp.ready":
            if device in self.current:
                self.ensure_audio(device)
        elif kind == "hfp.disconnected":
            if device in self.current:
                self.store.update_call(
                    self.current.pop(device), "disconnected", "bluetooth_disconnected"
                )
            self.stop_audio(device)
        elif kind == "audio.codec":
            # Codec confirmation occurred before audio is established. Never silently fall back.
            if device in self.current:
                self.ensure_audio(device)
        event = self.store.event(kind, data)
        for queue in tuple(self.subscribers):
            if queue.full():
                self.subscribers.remove(queue)
                # Event streams are bounded. Report the gap instead of silently hiding state changes.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({"kind": "events.overflow", "error": "reconnect and query state"})
            else:
                queue.put_nowait(event)
        for listener in tuple(self.listeners):
            listener(event)
        log.info("%s %s", kind, data)
        return event

    async def start(self):
        if self.config.codec == "msbc" and not msbc.AVAILABLE:
            self.error = "msbc configured but libsbc is not installed"
            return
        await self.register_bluetooth()
        self.retry = self.spawn(self.reconnect_loop())

    async def register_bluetooth(self):
        try:
            await self.bluez.start()
            self.running = True
            self.error = None
            self.emit("service.bluetooth_ready")
        except (RuntimeError, OSError, TimeoutError) as exc:
            self.running = False
            self.error = str(exc)
            await self.bluez.close()
            self.emit("service.unavailable", error=self.error)

    def new_connection(self, device, sock):
        async def establish():
            old = self.connections.get(device)
            if old:
                await old.close()
            connection = HFPConnection(sock, device, self.emit, self.config.codec)
            self.connections[device] = connection
            try:
                await connection.start()
            except Exception:
                if self.connections.get(device) is connection:
                    self.connections.pop(device, None)
                raise

        self.spawn(establish())

    def disconnected(self, device):
        connection = self.connections.get(device)
        if connection:
            self.spawn(connection.close())

    def slc(self, device):
        connection = self.connections.get(self.path(device))
        if not connection or not connection.ready or connection.closed:
            raise HFPError("phone HFP service-level connection is not ready")
        return connection

    async def devices(self):
        if not self.running:
            raise RuntimeError(self.error or "BlueZ unavailable")
        devices = await self.bluez.devices()
        for device in devices:
            slc = self.connections.get(device["path"])
            device.update(
                hfp_ready=bool(slc and slc.ready and not slc.closed),
                call_state=slc.state if slc else "unknown",
                audio=self.audio[device["path"]].status() if device["path"] in self.audio else None,
                reconnect=device["path"] in self.store.intents(),
            )
        return devices

    async def device_action(self, device, action):
        path = self.path(device)
        if not self.running:
            raise RuntimeError(self.error or "BlueZ unavailable")
        if action == "pair":
            self.pairing.add(path)
            try:
                await self.bluez.call(path, "org.bluez.Device1", "Pair", timeout=90)
            finally:
                self.pairing.discard(path)
        elif action == "connect":
            self.store.reconnect(path, True)
            await self.bluez.call(path, "org.bluez.Device1", "ConnectProfile", "s", [AG_UUID])
        elif action == "disconnect":
            self.store.reconnect(path, False)
            await self.bluez.call(path, "org.bluez.Device1", "Disconnect")
            connection = self.connections.get(path)
            if connection:
                await connection.close()
        else:
            raise ValueError("unknown device action")
        return {"device": path, "action": action, "accepted": True}

    async def reconnect_loop(self):
        while True:
            await asyncio.sleep(self.config.reconnect_seconds)
            if not self.running:
                await self.register_bluetooth()
                if not self.running:
                    continue
            for device in self.store.intents():
                slc = self.connections.get(device)
                if slc and not slc.closed:
                    continue
                try:
                    await self.bluez.call(
                        device, "org.bluez.Device1", "ConnectProfile", "s", [AG_UUID]
                    )
                    self.emit("device.reconnect_requested", device=device)
                except (RuntimeError, TimeoutError) as exc:
                    self.emit("device.reconnect_failed", device=device, error=str(exc))
                # No ATD/redial call is present in this path.

    async def dial(self, device, number=None, contact_id=None, owner=None):
        path = self.path(device)
        lock = self.device_locks.setdefault(path, asyncio.Lock())
        async with lock:
            if path in self.claims and self.claims[path] != owner:
                raise HFPError("device is reserved by an AI task")
            if contact_id:
                contacts = [c for c in self.store.contacts(device=path) if c["id"] == contact_id]
                if not contacts:
                    raise ValueError("contact does not belong to this device")
                number = re.sub(r"[\s().-]", "", contacts[0]["number"])
            at.cmd_dial(number or "")
            connection = self.slc(path)
            if path in self.current or connection.state != "idle":
                raise HFPError("device already has a call")
            call_id = self.store.new_call(path, number, "outgoing", "requested")
            self.current[path] = call_id
            if owner:
                self.store.update_task(owner, call_id=call_id)
            try:
                await connection.dial(number)
            except (HFPError, OSError):
                # Disconnect callback may already have recorded a more precise cause.
                if self.current.get(path) == call_id:
                    self.current.pop(path)
                    self.store.update_call(call_id, "idle", "dial_failed")
                raise
            return self.store.call(call_id)

    def call_device(self, call_id):
        call = self.store.call(call_id)
        if not call:
            raise ValueError("unknown call ID")
        if self.current.get(call["device"]) != call_id:
            raise HFPError("call is no longer current")
        return call["device"]

    async def sync(self, device):
        path = self.path(device)
        address = path.rsplit("dev_", 1)[1].replace("_", ":")
        result = await self.pbap.sync(address, path)
        self.store.save_phonebook(path, result["contacts"], result["history"])
        return {
            "device": path,
            "contacts": len(result["contacts"] or []),
            "history": len(result["history"]),
            "errors": result["errors"],
        }

    def ensure_audio(self, device):
        connection = self.connections.get(device)
        if not connection or not connection.ready:
            return
        existing = self.audio_jobs.get(device)
        if device in self.audio or (existing and not existing.done()):
            return
        self.audio_jobs[device] = self.spawn(self.open_audio(device))

    async def open_audio(self, device):
        # Only socket establishment owns the listener, not the full lifetime of a call.
        async with self.audio_setup_lock:
            await self._open_audio(device)

    async def _open_audio(self, device):
        listener = None
        try:
            connection = self.connections.get(device)
            if not connection:
                return
            if not connection.codec_confirmed.is_set():
                await connection.command(at.cmd_bcc())
                # Both CVSD and mSBC require successful BCS confirmation when negotiated.
                await asyncio.wait_for(connection.codec_confirmed.wait(), 5)
            codec = connection.codec
            voice = 0x0003 if codec == 2 else 0x0060
            local = await self.bluez.adapter_address()
            fd = sco_listen(local, voice)
            listener = socket.socket(fileno=fd)
            listener.setblocking(False)
            remote = device.rsplit("dev_", 1)[1].replace("_", ":")
            sock = None
            # Accept phone-initiated audio first; retry outbound SCO for an active call.
            for _ in range(20):
                if connection.closed or device not in self.current:
                    return
                try:
                    accepted, peer = await asyncio.wait_for(
                        asyncio.get_running_loop().sock_accept(listener), 0.15
                    )
                    # CPython returns a bare MAC string for BTPROTO_SCO, unlike RFCOMM.
                    peer_mac = peer if isinstance(peer, str) else peer[0]
                    if peer_mac.upper() == remote.upper():
                        sock = accepted
                        break
                    accepted.close()
                except TimeoutError:
                    if connection.state == "active":
                        # Wait for the finite worker even on cancellation so its fd cannot leak.
                        task = asyncio.create_task(
                            asyncio.to_thread(_sco_connect, remote, 1, voice, local)
                        )
                        try:
                            fd = await asyncio.shield(task)
                        except asyncio.CancelledError:
                            fd = await task
                            if fd >= 0:
                                os.close(fd)
                            raise
                        if fd >= 0:
                            sock = socket.socket(fileno=fd)
                            break
            if sock is None:
                raise RuntimeError("SCO connection unavailable; check controller and HFP owner")
            audio = SCOAudio(sock, codec)
            self.audio[device] = audio
            self.emit("audio.ready", device=device, **audio.status())
        except (OSError, RuntimeError, HFPError, TimeoutError) as exc:
            self.emit("audio.error", device=device, error=str(exc))
        finally:
            if listener:
                listener.close()

    def stop_audio(self, device):
        task = self.audio_jobs.pop(device, None)
        if task and not task.done():
            task.cancel()
        audio = self.audio.pop(device, None)
        if audio:
            audio.close()

    async def close(self):
        self.running = False
        for connection in list(self.connections.values()):
            await connection.close()
        for device in list(self.audio):
            self.stop_audio(device)
        for task in list(self.jobs):
            task.cancel()
        await asyncio.gather(*list(self.jobs), return_exceptions=True)
        await self.bluez.close()
