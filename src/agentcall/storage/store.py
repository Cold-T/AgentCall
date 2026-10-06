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

    def init_tasks(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks(
                id TEXT PRIMARY KEY, device TEXT NOT NULL, input TEXT NOT NULL, config TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'saved', outcome TEXT, call_id TEXT, model_result TEXT,
                error TEXT, created_at TEXT NOT NULL, started_at TEXT, ended_at TEXT);
            CREATE TABLE IF NOT EXISTS task_starts(key TEXT PRIMARY KEY, task_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS task_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, time TEXT NOT NULL,
                kind TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tool_calls(
                task_id TEXT NOT NULL, call_id TEXT NOT NULL, name TEXT, arguments TEXT,
                result TEXT, state TEXT NOT NULL, PRIMARY KEY(task_id, call_id));
        """)

    def recover_tasks(self):
        with self.db:
            self.db.execute(
                "UPDATE tasks SET state='ended', outcome='service_restart', ended_at=? "
                "WHERE state IN ('preparing','dialing','in_call','finalizing')",
                (now(),),
            )

    def create_task(self, device, data, config):
        task_id = str(uuid4())
        with self.db:
            self.db.execute(
                "INSERT INTO tasks(id,device,input,config,created_at) VALUES (?,?,?,?,?)",
                (
                    task_id,
                    device,
                    json.dumps(data, ensure_ascii=False),
                    json.dumps(config, ensure_ascii=False),
                    now(),
                ),
            )
        return task_id

    def task(self, task_id):
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError("unknown task ID")
        task = dict(row)
        for key in ("input", "config", "model_result", "error"):
            task[key] = json.loads(task[key]) if task[key] else None
        task["call"] = self.call(task["call_id"]) if task["call_id"] else None
        return task

    def task_ids(self, state=None):
        return [
            r[0]
            for r in self.db.execute(
                "SELECT id FROM tasks WHERE (? IS NULL OR state=?) ORDER BY created_at,rowid",
                (state, state),
            )
        ]

    def update_task(self, task_id, **fields):
        allowed = {"state", "outcome", "call_id", "model_result", "error", "started_at", "ended_at"}
        if fields.keys() - allowed:
            raise ValueError("unsupported task field")
        for key in ("model_result", "error"):
            if key in fields:
                fields[key] = json.dumps(fields[key], ensure_ascii=False)
        with self.db:
            self.db.execute(
                "UPDATE tasks SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?",
                (*fields.values(), task_id),
            )

    def start_task(self, task_id, key=None):
        self.task(task_id)
        with self.db:
            if key:
                existing = self.db.execute(
                    "SELECT task_id FROM task_starts WHERE key=?", (key,)
                ).fetchone()
                if existing and existing[0] != task_id:
                    raise ValueError("idempotency key belongs to a different task")
                self.db.execute("INSERT OR IGNORE INTO task_starts VALUES (?,?)", (key, task_id))
            self.db.execute(
                "UPDATE tasks SET state='queued' WHERE id=? AND state='saved'", (task_id,)
            )
        return self.task(task_id)

    def task_event(self, task_id, kind, data):
        with self.db:
            self.db.execute(
                "INSERT INTO task_events(task_id,time,kind,data) VALUES (?,?,?,?)",
                (task_id, now(), kind, json.dumps(data, ensure_ascii=False)),
            )

    def task_events(self, task_id):
        self.task(task_id)
        return [
            {**dict(r), "data": json.loads(r["data"])}
            for r in self.db.execute(
                "SELECT * FROM task_events WHERE task_id=? ORDER BY id", (task_id,)
            )
        ]

    def claim_tool(self, task_id, call_id, name, arguments):
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO tool_calls VALUES (?,?,?,?,NULL,'executing')",
                (task_id, call_id, name, arguments),
            )
        return cursor.rowcount == 1

    def finish_tool(self, task_id, call_id, result):
        with self.db:
            self.db.execute(
                "UPDATE tool_calls SET result=?,state='done' WHERE task_id=? AND call_id=?",
                (json.dumps(result, ensure_ascii=False), task_id, call_id),
            )
