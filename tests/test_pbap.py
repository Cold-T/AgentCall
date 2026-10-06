from pathlib import Path

from conftest import DEVICE
from dbus_next import Variant

from agentcall.bluetooth.pbap import PBAPClient, parse_cards
from agentcall.storage.store import Store

CARD = (
    "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:测试用户\r\nUID:abc\r\nTEL:+123\r\nTEL:456\r\nEND:VCARD\r\n"
)
HISTORY = "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Test\r\nTEL:123\r\nX-IRMC-CALL-DATETIME;TYPE=DIALED:20261005T120000\r\nEND:VCARD\r\n"


def test_multiple_contact_numbers_device_scoping_and_history_source():
    contacts = parse_cards(CARD, DEVICE, "pb")
    assert [c["number"] for c in contacts] == ["+123", "456"]
    assert [c["id"] for c in contacts] == [c["id"] for c in parse_cards(CARD, DEVICE, "pb")]
    assert contacts[0]["id"] != parse_cards(CARD, "other", "pb")[0]["id"]
    history = parse_cards(HISTORY, DEVICE, "och")
    assert history[0]["started_at"] == "20261005T120000"  # no invented UTC suffix
    store = Store(":memory:")
    store.save_phonebook(DEVICE, contacts, history)
    store.save_phonebook(DEVICE, contacts, history)
    assert len(store.contacts()) == 2
    assert len(store.calls()) == 1
    assert store.calls()[0]["source"] == "pbap"
    store.close()


async def test_pbap_uses_select_transfer_status_and_always_removes_session():
    calls = []
    statuses = ["active", "complete"]

    async def rpc(path, interface, member, signature="", body=None):
        calls.append(member)
        if member == "CreateSession":
            return ["/session"]
        if member == "Select" and body[1] != "pb":
            raise RuntimeError("phone does not expose history")
        if member == "PullAll":
            assert Path(body[0]).stat().st_mode & 0o777 == 0o600
            Path(body[0]).write_text(CARD)
            return [
                "/transfer",
                {"Status": Variant("s", "queued"), "Filename": Variant("s", body[0])},
            ]
        if member == "GetAll":
            return [{"Status": Variant("s", statuses.pop(0))}]
        return []

    result = await PBAPClient(rpc).sync("11:22:33:44:55:66", DEVICE)
    assert len(result["contacts"]) == 2
    assert set(result["errors"]) == {"ich", "och", "mch"}
    assert calls[-1] == "RemoveSession"


async def test_partial_file_is_never_treated_as_success():
    calls = []

    async def rpc(path, interface, member, signature="", body=None):
        calls.append(member)
        if member == "CreateSession":
            return ["/session"]
        if member == "PullAll":
            Path(body[0]).write_text(CARD)
            return ["/transfer", {"Status": Variant("s", "error")}]
        return []

    result = await PBAPClient(rpc).sync("11:22:33:44:55:66", DEVICE)
    assert result["contacts"] is None and len(result["errors"]) == 4
    assert calls[-1] == "RemoveSession"


async def test_transfer_timeout_cancels():
    calls = []

    async def rpc(path, interface, member, signature="", body=None):
        calls.append(member)
        if member == "CreateSession":
            return ["/session"]
        if member == "PullAll":
            return ["/transfer", {"Status": Variant("s", "active")}]
        return []

    result = await PBAPClient(rpc, timeout=0).sync("11:22:33:44:55:66", DEVICE)
    assert len(result["errors"]) == 4
    assert calls.count("Cancel") == 4 and calls[-1] == "RemoveSession"


def test_phone_contact_id_survives_name_change_with_stable_uid():
    before = parse_cards(CARD, DEVICE, "pb")
    after = parse_cards(CARD.replace("测试用户", "New Name"), DEVICE, "pb")
    assert [c["id"] for c in before] == [c["id"] for c in after]


async def test_unreadable_transfer_is_recorded_and_session_removed(monkeypatch):
    calls = []

    async def rpc(path, interface, member, signature="", body=None):
        calls.append(member)
        if member == "CreateSession":
            return ["/session"]
        if member == "PullAll":
            return ["/transfer", {"Status": Variant("s", "complete")}]
        return []

    def unreadable(*args, **kwargs):
        raise PermissionError("unreadable PBAP file")

    monkeypatch.setattr(Path, "read_text", unreadable)
    result = await PBAPClient(rpc).sync("11:22:33:44:55:66", DEVICE)
    assert result["contacts"] is None
    assert len(result["errors"]) == 4
    assert all("unreadable PBAP file" in error for error in result["errors"].values())
    assert calls[-1] == "RemoveSession"
