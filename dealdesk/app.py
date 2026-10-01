"""HTTP surface: JSON API, PayPal webhook, the mock PayPal pay page, and the UI."""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from .desk import Desk
from .llm import ClaudeLLM
from .paypal import MockPayPal, make_client, paid_invoice_id
from .policy import Policy
from .store import Store
from .taste import TasteProfile, make_taste

ROOT = Path(__file__).resolve().parents[1]


def load_config() -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    p = ROOT / "config" / "config.json"
    if p.exists():
        cfg = json.loads(p.read_text(encoding="utf-8"))
    env = os.environ
    if env.get("PAYPAL_CLIENT_ID"):
        cfg.setdefault("paypal", {}).update(client_id=env["PAYPAL_CLIENT_ID"], secret=env.get("PAYPAL_SECRET", ""),
                                            webhook_id=env.get("PAYPAL_WEBHOOK_ID", ""))
    if env.get("QLOO_API_KEY"):
        cfg.setdefault("qloo", {})["api_key"] = env["QLOO_API_KEY"]
    if env.get("ANTHROPIC_API_KEY"):
        cfg.setdefault("anthropic", {})["api_key"] = env["ANTHROPIC_API_KEY"]
    return cfg


def build_desk(cfg: dict[str, Any] | None = None, llm=None, db: str | None = None) -> Desk:
    cfg = cfg if cfg is not None else load_config()
    policy_path = ROOT / "config" / ("policy.json" if (ROOT / "config" / "policy.json").exists() else "policy.example.json")
    policy = Policy.load(policy_path)
    taste_api = make_taste(cfg)
    return Desk(
        store=Store(db or str(ROOT / "dealdesk.db")),
        llm=llm or ClaudeLLM((cfg.get("anthropic") or {}).get("api_key")),
        policy=policy,
        paypal=make_client(cfg),
        taste=TasteProfile(taste_api, policy.taste_seeds),
        autopilot_invoice=cfg.get("autopilot_invoice", True),
    )


class Inbound(BaseModel):
    sender: str
    subject: str
    body: str


class Reply(BaseModel):
    body: str


class Deliver(BaseModel):
    url: str


class BrandQ(BaseModel):
    brand: str


def create_app(desk: Desk | None = None, cfg: dict[str, Any] | None = None) -> FastAPI:
    cfg = cfg if cfg is not None else load_config()
    desk = desk or build_desk(cfg)
    app = FastAPI(title="Deal Desk")
    app.state.desk = desk

    def _deal(i: int):
        d = desk.store.get(i)
        if d is None:
            raise HTTPException(404, "no such deal")
        return d

    @app.get("/")
    def index():
        return FileResponse(ROOT / "static" / "index.html")

    @app.get("/api/status")
    def status():
        p = desk.policy
        return {"paypal": desk.paypal.mode, "taste": desk.taste.api.mode if desk.taste else "off",
                "model": getattr(desk.llm, "model", "fake"), "creator": p.creator_name, "channel": p.channel_name,
                "audience": p.audience, "currency": p.currency, "autopilot_invoice": desk.autopilot_invoice,
                "products": {k: {"label": v.label, "target": v.target, "floor": v.floor} for k, v in p.products.items()},
                "blocked": p.blocked_categories, "seeds": p.taste_seeds}

    @app.get("/api/deals")
    def deals():
        return [asdict(d) for d in desk.store.all()]

    @app.get("/api/deals/{i}")
    def deal(i: int):
        return asdict(_deal(i))

    @app.post("/api/inbound")
    def inbound(m: Inbound):
        return asdict(desk.ingest(m.sender, m.subject, m.body))

    @app.post("/api/deals/{i}/reply")
    def reply(i: int, m: Reply):
        _deal(i)
        return asdict(desk.on_reply(i, m.body))

    @app.post("/api/deals/{i}/approve")
    def approve(i: int):
        _deal(i)
        try:
            return asdict(desk.approve_draft(i))
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.post("/api/deals/{i}/invoice")
    def invoice(i: int):
        return asdict(desk.invoice(_deal(i)))

    @app.post("/api/deals/{i}/deliver")
    def deliver(i: int, m: Deliver):
        _deal(i)
        try:
            return asdict(desk.deliver(i, m.url))
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.post("/api/poll")
    def poll():
        return {"paid": [d.id for d in desk.poll_invoices() if d]}

    @app.post("/api/taste/fit")
    def fit(m: BrandQ):
        return asdict(desk.taste.fit(m.brand))

    @app.post("/api/taste/pitch")
    def pitch(m: BrandQ):
        try:
            return desk.pitch(m.brand)
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.get("/api/taste/prospects")
    def prospects():
        known = {d.brand for d in desk.store.all() if d.brand}
        return {"source": desk.taste.api.mode,
                "audience_tags": [asdict(t) for t in desk.taste.audience_tags()[:12]],
                "prospects": [asdict(b) for b in desk.taste.prospects(known, 12)]}

    @app.post("/api/samples")
    def samples():
        out = []
        for f in sorted((ROOT / "samples").glob("*.json")):
            s = json.loads(f.read_text(encoding="utf-8"))
            out.append(desk.ingest(s["from"], s["subject"], s["body"]).id)
        return {"created": out}

    @app.post("/webhooks/paypal")
    async def paypal_webhook(request: Request):
        event = await request.json()
        headers = dict(request.headers)
        webhook_id = (cfg.get("paypal") or {}).get("webhook_id", "")
        if not isinstance(desk.paypal, MockPayPal) and not desk.paypal.verify_webhook(headers, event, webhook_id):
            raise HTTPException(400, "signature verification failed")
        inv = paid_invoice_id(event)
        if inv:
            d = desk.mark_paid(inv, "webhook")
            return {"ok": True, "deal": d.id if d else None}
        return {"ok": True, "ignored": event.get("event_type")}

    # A stand-in for PayPal's hosted invoice page, only in mock mode.
    @app.get("/mock-paypal/pay/{inv}", response_class=HTMLResponse)
    def mock_pay_page(inv: str):
        if not isinstance(desk.paypal, MockPayPal):
            raise HTTPException(404)
        i = desk.paypal.invoices.get(inv)
        if not i:
            raise HTTPException(404)
        return f"""<!doctype html><meta charset=utf-8><title>Mock PayPal invoice</title>
<body style="font-family:system-ui;max-width:420px;margin:60px auto;color:#142c8e">
<p style="color:#888">MOCK PAYPAL, no real money moves. Configure sandbox keys for the real flow.</p>
<h2>Invoice {i['number']}</h2><p>{i['item']}</p><h1>${i['amount']:.2f} {i['currency']}</h1>
<p>Status: <b>{i['status']}</b></p>
<form method=post><button style="background:#ffc439;border:0;border-radius:20px;padding:12px 40px;font-size:16px">Pay invoice</button></form>"""

    @app.post("/mock-paypal/pay/{inv}", response_class=HTMLResponse)
    def mock_pay(inv: str):
        if not isinstance(desk.paypal, MockPayPal):
            raise HTTPException(404)
        i = desk.paypal.invoices[inv]
        desk.paypal.record_payment(inv, i["amount"], i["currency"])
        # Deliver the same event a real PayPal webhook would.
        desk.mark_paid(inv, "webhook")
        return "<p style='font-family:system-ui;margin:60px'>Paid. You can close this tab.</p>"

    return app
