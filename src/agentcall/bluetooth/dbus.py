"""BlueZ profile/agent on the service's asyncio loop; no GLib or Qt dependencies."""

import asyncio
import os
import socket
from uuid import uuid4

from dbus_next import BusType, DBusError, Message, MessageType, Variant
from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, method

HF_UUID = "0000111e-0000-1000-8000-00805f9b34fb"
AG_UUID = "0000111f-0000-1000-8000-00805f9b34fb"
PROFILE_PATH = "/org/agentcall/hfp"
AGENT_PATH = "/org/agentcall/agent"


def unpack(value):
    if isinstance(value, Variant):
        return unpack(value.value)
    if isinstance(value, dict):
        return {str(k): unpack(v) for k, v in value.items()}
    if isinstance(value, list):
        return [unpack(v) for v in value]
    return value


async def rpc(bus, destination, path, interface, member, signature="", body=None, timeout=30):
    reply = await asyncio.wait_for(
        bus.call(
            Message(
                destination=destination,
                path=path,
                interface=interface,
                member=member,
                signature=signature,
                body=body or [],
            )
        ),
        timeout,
    )
    if reply.message_type == MessageType.ERROR:
        raise RuntimeError(f"{reply.error_name}: {' '.join(map(str, reply.body))}")
    return reply.body


class Profile(ServiceInterface):
    def __init__(self, backend):
        super().__init__("org.bluez.Profile1")
        self.backend = backend

    @method()
    def NewConnection(self, device: "o", fd: "h", properties: "a{sv}"):
        # dbus-next has already converted the message's FD index to the received raw fd.
        sock = socket.socket(fileno=os.dup(fd))
        os.close(fd)
        self.backend.new_connection(device, sock)

    @method()
    def RequestDisconnection(self, device: "o"):
        self.backend.disconnected(device)

    @method()
    def Release(self):
        self.backend.running = False
        self.backend.error = "BlueZ released HFP profile"
        for device in list(self.backend.connections):
            self.backend.disconnected(device)


class Agent(ServiceInterface):
    def __init__(self, backend):
        super().__init__("org.bluez.Agent1")
        self.backend = backend
        self.pending = {}

    async def confirmation(self, device, **data):
        if device not in self.backend.pairing:
            raise DBusError("org.bluez.Error.Rejected", "pairing was not requested via API")
        request_id = str(uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        self.backend.emit("pairing.confirmation", device=device, id=request_id, **data)
        try:
            if not await asyncio.wait_for(future, 60):
                raise DBusError("org.bluez.Error.Rejected", "user rejected pairing")
        except TimeoutError:
            raise DBusError("org.bluez.Error.Canceled", "pairing confirmation timeout") from None
        finally:
            self.pending.pop(request_id, None)

    @method()
    async def RequestConfirmation(self, device: "o", passkey: "u"):
        await self.confirmation(device, passkey=f"{passkey:06d}")

    @method()
    async def RequestAuthorization(self, device: "o"):
        await self.confirmation(device)

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):
        if uuid != AG_UUID and uuid != HF_UUID:
            raise DBusError("org.bluez.Error.Rejected", "unsupported service")

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):
        self.backend.emit(
            "pairing.passkey", device=device, passkey=f"{passkey:06d}", entered=entered
        )

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):
        self.backend.emit("pairing.pin", device=device, pin=pincode)

    @method()
    def RequestPinCode(self, device: "o") -> "s":
        raise DBusError("org.bluez.Error.Rejected", "legacy PIN pairing unsupported")

    @method()
    def RequestPasskey(self, device: "o") -> "u":
        raise DBusError("org.bluez.Error.Rejected", "passkey input unsupported")

    @method()
    def Cancel(self):
        for future in self.pending.values():
            if not future.done():
                future.set_result(False)

    @method()
    def Release(self):
        self.Cancel()


class BlueZ:
    def __init__(self, backend):
        self.backend = backend
        self.bus = None
        self.obex = None
        self.agent = Agent(backend)

    async def start(self):
        if self.bus:
            self.bus.disconnect()
        self.bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()
        self.bus.export(PROFILE_PATH, Profile(self.backend))
        self.bus.export(AGENT_PATH, self.agent)
        await self.call(
            "/org/bluez",
            "org.bluez.ProfileManager1",
            "RegisterProfile",
            "osa{sv}",
            [
                PROFILE_PATH,
                HF_UUID,
                {
                    "Name": Variant("s", "AgentCall"),
                    "Role": Variant("s", "client"),
                    "AutoConnect": Variant("b", True),
                    "RequireAuthentication": Variant("b", True),
                    "Version": Variant("q", 0x0107),
                    "Features": Variant("q", 0x20 if self.backend.config.codec == "msbc" else 0),
                },
            ],
        )
        await self.call(
            "/org/bluez",
            "org.bluez.AgentManager1",
            "RegisterAgent",
            "os",
            [AGENT_PATH, "DisplayYesNo"],
        )
        # Do not replace the user's desktop default agent. Pair on this same bus connection.
        self.bus.add_message_handler(self.properties_changed)
        await self.call(
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "AddMatch",
            "s",
            [
                "type='signal',sender='org.freedesktop.DBus',interface='org.freedesktop.DBus',member='NameOwnerChanged',arg0='org.bluez'"
            ],
            destination="org.freedesktop.DBus",
        )
        await self.call(
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "AddMatch",
            "s",
            [
                "type='signal',sender='org.bluez',interface='org.freedesktop.DBus.Properties',member='PropertiesChanged'"
            ],
            destination="org.freedesktop.DBus",
        )

    def properties_changed(self, message):
        if (
            message.message_type == MessageType.SIGNAL
            and message.interface == "org.freedesktop.DBus"
            and message.member == "NameOwnerChanged"
            and message.body[0] == "org.bluez"
        ):
            if message.body[1]:
                self.backend.running = False
                self.backend.error = "BlueZ restarted or disconnected"
                for device in list(self.backend.connections):
                    self.backend.disconnected(device)
        if message.message_type == MessageType.SIGNAL and message.member == "PropertiesChanged":
            self.backend.emit(
                "device.properties", device=message.path, values=unpack(message.body[1])
            )

    async def call(
        self, path, interface, member, signature="", body=None, destination="org.bluez", timeout=30
    ):
        return await rpc(self.bus, destination, path, interface, member, signature, body, timeout)

    async def obex_call(self, path, interface, member, signature="", body=None):
        if self.obex is None:
            self.obex = await MessageBus(bus_type=BusType.SESSION).connect()
        return await rpc(self.obex, "org.bluez.obex", path, interface, member, signature, body)

    async def devices(self):
        objects = (await self.call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"))[
            0
        ]
        result = []
        for path, interfaces in objects.items():
            if "org.bluez.Device1" in interfaces and path.startswith(
                f"/org/bluez/{self.backend.config.adapter}/"
            ):
                p = unpack(interfaces["org.bluez.Device1"])
                result.append(
                    {
                        "id": path.rsplit("/", 1)[-1],
                        "path": path,
                        "address": p["Address"],
                        "name": p.get("Alias", p.get("Name", "")),
                        "paired": p.get("Paired", False),
                        "connected": p.get("Connected", False),
                        "hfp_ag": AG_UUID in p.get("UUIDs", []),
                    }
                )
        return result

    async def adapter_address(self):
        result = await self.call(
            f"/org/bluez/{self.backend.config.adapter}",
            "org.freedesktop.DBus.Properties",
            "Get",
            "ss",
            ["org.bluez.Adapter1", "Address"],
        )
        return result[0].value

    async def close(self):
        if self.obex:
            self.obex.disconnect()
        if self.bus:
            self.bus.disconnect()  # BlueZ unregisters profiles/agents on bus disconnect.
