"""Local store-and-forward buffer (SQLite) for when the database is unreachable."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime

# rows are (tag name, timestamp, value, quality); ids are resolved when written
NamedRow = tuple[str, datetime, float, int]


class Outbox:
    def __init__(self, path: str):
        # used from the event loop and from the writer thread, so serialise access
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, tag TEXT, ts TEXT, value REAL, quality INTEGER)"
        )
        self.conn.commit()

    def put(self, rows: list[NamedRow]) -> None:
        with self.lock:
            self._put(rows)

    def _put(self, rows: list[NamedRow]) -> None:
        self.conn.executemany(
            "INSERT INTO outbox (tag, ts, value, quality) VALUES (?, ?, ?, ?)",
            [(t, ts.isoformat(), v, q) for t, ts, v, q in rows],
        )
        self.conn.commit()

    def peek(self, limit: int = 5000) -> tuple[list[int], list[NamedRow]]:
        with self.lock:
            cur = self.conn.execute("SELECT id, tag, ts, value, quality FROM outbox ORDER BY id LIMIT ?", (limit,))
            fetched = cur.fetchall()
        ids, rows = [], []
        for id_, tag, ts, value, quality in fetched:
            ids.append(id_)
            rows.append((tag, datetime.fromisoformat(ts), value, quality))
        return ids, rows

    def ack(self, ids: list[int]) -> None:
        if ids:
            with self.lock:
                self.conn.execute("DELETE FROM outbox WHERE id <= ?", (max(ids),))
                self.conn.commit()

    def __len__(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT count(*) FROM outbox").fetchone()[0]

    def close(self) -> None:
        with self.lock:
            self.conn.close()
