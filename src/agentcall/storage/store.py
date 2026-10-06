import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path):
        self.recordings_dir = (
            Path(path).expanduser().resolve().parent / "recordings" if path != ":memory:" else None
        )
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
            CREATE TABLE IF NOT EXISTS devices(
                device TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS calls_device_time ON calls(device,started_at);
            CREATE INDEX IF NOT EXISTS contacts_device_name ON contacts(device,name);
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, kind TEXT, data TEXT);
        """)
        self.db.execute(
            "INSERT OR IGNORE INTO devices SELECT device,'{}',? FROM ("
            "SELECT device FROM calls UNION SELECT device FROM contacts UNION "
            "SELECT device FROM phone_history UNION SELECT device FROM intents) WHERE device IS NOT NULL",
            (now(),),
        )
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
        return {
            "id": cursor.lastrowid,
            "time": stamp,
            "kind": kind,
            **data,
            "event_id": cursor.lastrowid,
        }

    def new_call(self, device, number, direction, state):
        call_id = str(uuid4())
        self.save_device(device, {})
        self.db.execute(
            "INSERT INTO calls(id,device,number,direction,state,started_at) VALUES (?,?,?,?,?,?)",
            (call_id, device, number, direction, state, now()),
        )
        self.db.commit()
        return call_id

    def call(self, call_id):
        row = self.db.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["duration_seconds"] = None
        if result["answered_at"]:
            end = datetime.fromisoformat(result["ended_at"] or now())
            result["duration_seconds"] = max(
                0, (end - datetime.fromisoformat(result["answered_at"])).total_seconds()
            )
        return result

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

    def calls(self, device=None, source=None, limit=None, offset=0):
        rows = self.db.execute(
            "SELECT * FROM (SELECT id,device,source,started_at AS recorded_at FROM calls "
            "UNION ALL SELECT id,device,source,json_extract(data,'$.synced_at') AS recorded_at "
            "FROM phone_history) WHERE (? IS NULL OR device=?) AND (? IS NULL OR source=?) "
            "ORDER BY recorded_at DESC,source,id LIMIT ? OFFSET ?",
            (device, device, source, source, limit if limit is not None else -1, offset),
        ).fetchall()
        return [self.call_record(r["id"], r["source"]) for r in rows]

    def call_record(self, call_id, source=None):
        if source != "pbap":
            if result := self.call(call_id):
                return result
        if source != "project":
            row = self.db.execute(
                "SELECT data FROM phone_history WHERE id=?", (call_id,)
            ).fetchone()
            if row:
                result = json.loads(row[0])
                result.setdefault("duration_seconds", None)
                return result
        return None

    def history(self, device=None, limit=50, offset=0):
        rows = self.db.execute(
            "SELECT * FROM ("
            "SELECT 'task' AS type,t.id,t.device,'project' AS source,"
            "COALESCE(c.started_at,t.started_at,t.created_at) AS recorded_at "
            "FROM tasks t LEFT JOIN calls c ON c.id=t.call_id "
            "UNION ALL SELECT 'call',c.id,c.device,c.source,c.started_at FROM calls c "
            "WHERE NOT EXISTS (SELECT 1 FROM tasks t WHERE t.call_id=c.id) "
            "UNION ALL SELECT 'call',id,device,'pbap',json_extract(data,'$.synced_at') "
            "FROM phone_history) WHERE (? IS NULL OR device=?) "
            "ORDER BY recorded_at DESC,type,id LIMIT ? OFFSET ?",
            (device, device, limit, offset),
        ).fetchall()
        result = []
        for row in rows:
            task = self.task(row["id"]) if row["type"] == "task" else None
            result.append(
                {
                    **dict(row),
                    "task": task,
                    "call": task["call"] if task else self.call_record(row["id"], row["source"]),
                }
            )
        return result

    def contacts(self, query="", device=None, limit=None, offset=0):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM contacts WHERE (name LIKE ? OR number LIKE ?) "
                "AND (? IS NULL OR device=?) ORDER BY name,id LIMIT ? OFFSET ?",
                (
                    f"%{query}%",
                    f"%{query}%",
                    device,
                    device,
                    limit if limit is not None else -1,
                    offset,
                ),
            )
        ]

    def contact(self, contact_id):
        row = self.db.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
        if row is None:
            raise ValueError("unknown contact ID")
        return dict(row)

    def save_device(self, device, data):
        row = self.db.execute("SELECT data FROM devices WHERE device=?", (device,)).fetchone()
        previous = json.loads(row[0]) if row else {}
        merged = {**previous, **data}
        self.db.execute(
            "INSERT INTO devices VALUES (?,?,?) ON CONFLICT(device) DO UPDATE "
            "SET data=excluded.data,updated_at=excluded.updated_at",
            (device, json.dumps(merged, ensure_ascii=False), now()),
        )
        self.db.commit()

    def saved_devices(self):
        return [
            {
                "device": r["device"],
                "id": r["device"].rsplit("/", 1)[-1],
                "last_observed": json.loads(r["data"]),
                "updated_at": r["updated_at"],
            }
            for r in self.db.execute("SELECT * FROM devices ORDER BY device")
        ]

    def event_history(
        self, after_id=0, limit=100, kind=None, device=None, call_id=None, task_id=None
    ):
        rows = self.db.execute(
            "SELECT * FROM events WHERE id>? AND (? IS NULL OR kind=?) "
            "AND (? IS NULL OR json_extract(data,'$.device')=?) "
            "AND (? IS NULL OR json_extract(data,'$.call_id')=?) "
            "AND (? IS NULL OR json_extract(data,'$.task_id')=?) ORDER BY id LIMIT ?",
            (after_id, kind, kind, device, device, call_id, call_id, task_id, task_id, limit),
        )
        return [
            {
                "id": r["id"],
                "time": r["time"],
                "kind": r["kind"],
                **json.loads(r["data"]),
                "event_id": r["id"],
            }
            for r in rows
        ]

    def save_phonebook(self, device, contacts, histories):
        self.save_device(device, {})
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

        self.db.execute(
            "INSERT OR IGNORE INTO devices SELECT DISTINCT device,'{}',? FROM tasks", (now(),)
        )
        self.db.commit()

    def recover_tasks(self):
        with self.db:
            self.db.execute(
                "UPDATE tasks SET state='ended', outcome='service_restart', ended_at=? "
                "WHERE state IN ('preparing','dialing','in_call','finalizing')",
                (now(),),
            )

    def create_task(self, device, data, config):
        task_id = str(uuid4())
        self.save_device(device, {})
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

    def task_ids(self, state=None, device=None, outcome=None, limit=None, offset=0):
        return [
            r[0]
            for r in self.db.execute(
                "SELECT id FROM tasks WHERE (? IS NULL OR state=?) AND (? IS NULL OR device=?) "
                "AND (? IS NULL OR outcome=?) ORDER BY created_at,rowid LIMIT ? OFFSET ?",
                (
                    state,
                    state,
                    device,
                    device,
                    outcome,
                    outcome,
                    limit if limit is not None else -1,
                    offset,
                ),
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

    def task_events(self, task_id, after_id=0, limit=None, kind=None):
        self.task(task_id)
        return [
            {**dict(r), "data": json.loads(r["data"])}
            for r in self.db.execute(
                "SELECT * FROM task_events WHERE task_id=? AND id>? AND (? IS NULL OR kind=?) "
                "ORDER BY id LIMIT ?",
                (task_id, after_id, kind, kind, limit if limit is not None else -1),
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

    def task_result(self, task_id):
        task = self.task(task_id)
        return {
            key: task[key]
            for key in (
                "id",
                "state",
                "outcome",
                "model_result",
                "error",
                "call_id",
                "call",
                "ended_at",
            )
        }

    def task_tools(self, task_id, limit=100, offset=0):
        self.task(task_id)
        result = []
        for row in self.db.execute(
            "SELECT * FROM tool_calls WHERE task_id=? ORDER BY rowid LIMIT ? OFFSET ?",
            (task_id, limit, offset),
        ):
            item = dict(row)
            item["result"] = json.loads(item["result"]) if item["result"] else None
            result.append(item)
        return result
