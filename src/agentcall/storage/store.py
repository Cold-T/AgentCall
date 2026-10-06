import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path):
        if path != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(Path(path).expanduser()) if path != ":memory:" else path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS intents(device TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS contacts(
                id TEXT PRIMARY KEY, device TEXT, name TEXT, number TEXT, raw TEXT, synced_at TEXT);
            CREATE TABLE IF NOT EXISTS phone_history(
                id TEXT PRIMARY KEY, device TEXT, data TEXT, source TEXT DEFAULT 'pbap');
            CREATE TABLE IF NOT EXISTS calls(
                id TEXT PRIMARY KEY, device TEXT, number TEXT, direction TEXT,
                state TEXT, started_at TEXT, answered_at TEXT, ended_at TEXT, end_reason TEXT,
                source TEXT DEFAULT 'project');
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, kind TEXT, data TEXT);
        """)
        # Crash/restart never implies redial or a confirmed phone hangup.
        self.db.execute(
            "UPDATE calls SET state='unknown', ended_at=?, end_reason='service_restart' "
            "WHERE ended_at IS NULL",
            (now(),),
        )
        self.db.commit()

    def reconnect(self, device, enabled):
        if enabled:
            self.db.execute("INSERT OR IGNORE INTO intents VALUES (?)", (device,))
        else:
            self.db.execute("DELETE FROM intents WHERE device=?", (device,))
        self.db.commit()

    def intents(self):
        return [r[0] for r in self.db.execute("SELECT device FROM intents")]

    def event(self, kind, data):
        stamp = now()
        cursor = self.db.execute(
            "INSERT INTO events(time,kind,data) VALUES (?,?,?)",
            (stamp, kind, json.dumps(data, ensure_ascii=False)),
        )
        self.db.commit()
        return {"id": cursor.lastrowid, "time": stamp, "kind": kind, **data}

    def new_call(self, device, number, direction, state):
        call_id = str(uuid4())
        self.db.execute(
            "INSERT INTO calls(id,device,number,direction,state,started_at) VALUES (?,?,?,?,?,?)",
            (call_id, device, number, direction, state, now()),
        )
        self.db.commit()
        return call_id

    def call(self, call_id):
        row = self.db.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
        return dict(row) if row else None

    def update_call(self, call_id, state, reason=None, number=None):
        self.db.execute(
            "UPDATE calls SET state=?, number=COALESCE(?,number) WHERE id=?",
            (state, number, call_id),
        )
        if state == "active":
            self.db.execute(
                "UPDATE calls SET answered_at=COALESCE(answered_at,?) WHERE id=?", (now(), call_id)
            )
        if reason:
            self.db.execute(
                "UPDATE calls SET ended_at=?, end_reason=? WHERE id=?", (now(), reason, call_id)
            )
        self.db.commit()

    def calls(self):
        project = [dict(r) for r in self.db.execute("SELECT * FROM calls ORDER BY started_at DESC")]
        history = [json.loads(r[0]) for r in self.db.execute("SELECT data FROM phone_history")]
        return project + history

    def contacts(self, query="", device=None):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM contacts WHERE (name LIKE ? OR number LIKE ?) "
                "AND (? IS NULL OR device=?) ORDER BY name",
                (f"%{query}%", f"%{query}%", device, device),
            )
        ]

    def save_phonebook(self, device, contacts, histories):
        with self.db:
            if contacts is not None:
                self.db.execute("DELETE FROM contacts WHERE device=?", (device,))
                for c in contacts:
                    self.db.execute(
                        "INSERT OR REPLACE INTO contacts VALUES (?,?,?,?,?,?)",
                        (c["id"], device, c["name"], c["number"], c["raw"], now()),
                    )
            for entry in histories:
                entry.update(device=device, source="pbap")
                self.db.execute(
                    "INSERT OR REPLACE INTO phone_history(id,device,data) VALUES (?,?,?)",
                    (entry["id"], device, json.dumps(entry, ensure_ascii=False)),
                )

    def close(self):
        self.db.close()
