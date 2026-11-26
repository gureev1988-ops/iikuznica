"""A persistent, synthetic mailbox shared by the local CLI and worker processes."""

from contextlib import contextmanager
from pathlib import Path
import os
import sqlite3

from backend.mailfaq.transport import InboxPage, Outbound, Receipt


class LocalMailbox:
    def __init__(self, path, max_bytes=2 * 1024 * 1024):
        self.path = Path(path)
        self.max_bytes = max_bytes

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(descriptor)
        connection = sqlite3.connect(self.path, timeout=15)
        try:
            with connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS sent (uid INTEGER PRIMARY KEY, mid TEXT, raw BLOB)"
                )
                connection.execute("CREATE TABLE IF NOT EXISTS inbox (uid INTEGER PRIMARY KEY, raw BLOB)")
                yield connection
        finally:
            connection.close()

    def send(self, outbound: Outbound) -> str:
        with self.connection() as conn:
            conn.execute("INSERT INTO sent (mid,raw) VALUES (?,?)", (outbound.message_id, outbound.bytes()))
        return outbound.message_id

    def inject(self, raw: bytes):
        with self.connection() as conn:
            return conn.execute("INSERT INTO inbox (raw) VALUES (?)", (raw,)).lastrowid

    def fetch(self, uidvalidity: str | None, last_uid: int, limit: int) -> InboxPage:
        # Rotation of the synthetic provider file is treated like a mailbox reset.
        with self.connection() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS mailbox_id (id INTEGER PRIMARY KEY CHECK(id=1), epoch TEXT)")
            conn.execute("INSERT OR IGNORE INTO mailbox_id VALUES (1, hex(randomblob(16)))")
            epoch, = conn.execute("SELECT epoch FROM mailbox_id WHERE id=1").fetchone()
            start = last_uid if uidvalidity == epoch else 0
            rows = conn.execute(
                "SELECT uid, length(raw), substr(raw,1,?) FROM inbox WHERE uid>? ORDER BY uid LIMIT ?",
                (self.max_bytes, start, limit),
            )
            return InboxPage(epoch, tuple(
                Receipt(uid, raw) if size <= self.max_bytes else Receipt(uid, b"", "message_too_large")
                for uid, size, raw in rows
            ))

    def sent(self):
        with self.connection() as conn:
            return conn.execute("SELECT mid, raw FROM sent ORDER BY uid").fetchall()
