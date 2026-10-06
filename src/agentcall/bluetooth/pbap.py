"""BlueZ obexd PBAP, derived from handsfree-linux's Select/PullAll/vCard flow."""

import asyncio
import hashlib
import tempfile
from pathlib import Path

import vobject
from dbus_next import Variant

from agentcall.storage.store import now


def parse_cards(raw, device, book):
    result = []
    for card in vobject.readComponents(raw):
        name = str(card.fn.value) if hasattr(card, "fn") else ""
        phones = [str(p.value).strip() for p in card.contents.get("tel", [])]
        uid = str(card.uid.value) if hasattr(card, "uid") else name
        serialized = card.serialize()
        stamp = ""
        for key, props in card.contents.items():
            if "call-datetime" in key:
                stamp = str(props[0].value)
        for number in phones:
            if not number:
                continue
            # Preserve timestamp exactly: a local time without Z is not UTC.
            identity = (
                f"{device}:{book}:{uid}:{number}"
                if book == "pb"
                else f"{device}:{book}:{number}:{stamp or serialized}"
            )
            result.append(
                {
                    "id": hashlib.sha256(identity.encode()).hexdigest(),
                    "name": name,
                    "number": number,
                    "raw": serialized,
                    "synced_at": now(),
                    "source": "pbap",
                    "book": book,
                    "started_at": stamp,
                    "direction": {"ich": "incoming", "och": "outgoing", "mch": "missed"}.get(book),
                }
            )
    return result


class PBAPClient:
    def __init__(self, rpc, timeout=60):
        self.rpc = rpc
        self.timeout = timeout
        self.lock = asyncio.Lock()

    async def sync(self, address, device):
        async with self.lock:
            session = await self.rpc(
                "/org/bluez/obex",
                "org.bluez.obex.Client1",
                "CreateSession",
                "sa{sv}",
                [address, {"Target": Variant("s", "PBAP")}],
            )
            session = session[0]
            contacts, history, errors = None, [], {}
            try:
                for book in ("pb", "ich", "och", "mch"):
                    try:
                        entries = await self.pull(session, device, book)
                        if book == "pb":
                            contacts = entries
                        else:
                            history.extend(entries)
                    except (RuntimeError, TimeoutError, ValueError, OSError) as exc:
                        errors[book] = str(exc)
                return {"contacts": contacts, "history": history, "errors": errors}
            finally:
                await self.rpc(
                    "/org/bluez/obex", "org.bluez.obex.Client1", "RemoveSession", "o", [session]
                )

    async def pull(self, session, device, book):
        await self.rpc(session, "org.bluez.obex.PhonebookAccess1", "Select", "ss", ["int", book])
        with tempfile.TemporaryDirectory(prefix="agentcall-pbap-") as temp:
            target = str(Path(temp) / "phonebook.vcf")
            # A system obexd writes as root. Precreate privately so the service user
            # retains ownership and can read the completed transfer without chmod.
            Path(target).touch(mode=0o600, exist_ok=False)
            transfer, props = await self.rpc(
                session,
                "org.bluez.obex.PhonebookAccess1",
                "PullAll",
                "sa{sv}",
                [target, {"Format": Variant("s", "vcard30")}],
            )
            deadline = asyncio.get_running_loop().time() + self.timeout
            while True:
                status = props.get("Status", Variant("s", "queued")).value
                if status == "complete":
                    break
                if status == "error":
                    raise RuntimeError(f"PBAP {book} transfer failed")
                if asyncio.get_running_loop().time() >= deadline:
                    try:
                        await self.rpc(transfer, "org.bluez.obex.Transfer1", "Cancel", "", [])
                    finally:
                        raise TimeoutError(f"PBAP {book} transfer timeout")
                await asyncio.sleep(0.1)
                props = (
                    await self.rpc(
                        transfer,
                        "org.freedesktop.DBus.Properties",
                        "GetAll",
                        "s",
                        ["org.bluez.obex.Transfer1"],
                    )
                )[0]
            filename = props.get("Filename", Variant("s", target)).value
            return parse_cards(Path(filename).read_text(encoding="utf-8"), device, book)
