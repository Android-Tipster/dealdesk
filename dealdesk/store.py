"""SQLite persistence. One table of deals, each carrying its own event log."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Deal:
    id: int | None = None
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    stage: str = "new"
    sender_email: str = ""
    sender_name: str = ""
    subject: str = ""
    brand: str | None = None
    brand_domain: str | None = None
    category: str = ""
    deliverable: str = "unknown"
    wants_followed: bool | None = None
    summary: str = ""
    screen_reason: str = ""
    marker_hits: list[str] = field(default_factory=list)
    quote: float | None = None
    concessions_used: int = 0
    agreed_price: float | None = None
    billing_email: str | None = None
    billing_name: str | None = None
    invoice_id: str | None = None
    invoice_number: str | None = None
    invoice_status: str | None = None
    payer_url: str | None = None
    fit_score: int | None = None
    fit_verdict: str | None = None
    fit_tags: list[str] = field(default_factory=list)
    fit_note: str = ""
    fit_source: str = ""
    draft_subject: str = ""
    draft_body: str = ""
    draft_problems: list[str] = field(default_factory=list)
    live_url: str | None = None
    reminders: int = 0
    nudged: bool = False
    thread: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)

    def log(self, kind: str, text: str, **data):
        self.events.append({"t": time.time(), "kind": kind, "text": text, **data})
        self.updated = time.time()


_JSON_FIELDS = {"marker_hits", "fit_tags", "draft_problems", "thread", "events"}


class Store:
    def __init__(self, path: str = "dealdesk.db"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute("CREATE TABLE IF NOT EXISTS deals (id INTEGER PRIMARY KEY AUTOINCREMENT, doc TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
        self.db.commit()

    def save(self, d: Deal) -> Deal:
        doc = asdict(d)
        doc.pop("id")
        with self.lock:
            if d.id is None:
                cur = self.db.execute("INSERT INTO deals (doc) VALUES (?)", (json.dumps(doc),))
                d.id = cur.lastrowid
            else:
                self.db.execute("UPDATE deals SET doc=? WHERE id=?", (json.dumps(doc), d.id))
            self.db.commit()
        return d

    def get(self, deal_id: int) -> Deal | None:
        row = self.db.execute("SELECT id, doc FROM deals WHERE id=?", (deal_id,)).fetchone()
        return self._load(row) if row else None

    def all(self) -> list[Deal]:
        return [self._load(r) for r in self.db.execute("SELECT id, doc FROM deals ORDER BY id DESC")]

    def by_invoice(self, invoice_id: str) -> Deal | None:
        return next((d for d in self.all() if d.invoice_id == invoice_id), None)

    def next_invoice_number(self, prefix: str) -> str:
        with self.lock:
            row = self.db.execute("SELECT v FROM kv WHERE k='inv_seq'").fetchone()
            n = int(row[0]) + 1 if row else 1
            self.db.execute("INSERT OR REPLACE INTO kv (k, v) VALUES ('inv_seq', ?)", (str(n),))
            self.db.commit()
        return f"{prefix}-{time.strftime('%Y%m')}-{n:04d}"

    @staticmethod
    def _load(row) -> Deal:
        doc = json.loads(row[1])
        return Deal(id=row[0], **doc)
