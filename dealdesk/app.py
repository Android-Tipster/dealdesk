"""HTTP surface: JSON API, PayPal webhook, the mock PayPal pay page, and the UI."""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from .desk import Desk
from .llm import BudgetExceeded, ClaudeLLM, ReplayLLM
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
    if llm is None:
        key = (cfg.get("anthropic") or {}).get("api_key") or os.environ.get("ANTHROPIC_API_KEY")
        llm = ClaudeLLM(key) if key else None
        cassette = os.environ.get("DEALDESK_CASSETTE")
        if not key and not cassette and (ROOT / "demo" / "cassette.json").exists():
            # No key: run the recorded demo instead of failing, and say so.
            cassette = str(ROOT / "demo" / "cassette.json")
            print("No ANTHROPIC_API_KEY set: running replay mode on the recorded sample flows.")
        if cassette:
            llm = ReplayLLM(llm, cassette, int(os.environ.get("DEALDESK_DAILY_LIVE_CALLS", "60")),
                            record=os.environ.get("DEALDESK_RECORD") == "1")
        if llm is None:
            raise RuntimeError("set ANTHROPIC_API_KEY, or DEALDESK_CASSETTE for replay-only mode")
    if db is None:
        db = os.environ.get("DEALDESK_DB") or ("/tmp/dealdesk.db" if os.environ.get("VERCEL") else str(ROOT / "dealdesk.db"))
    return Desk(
        store=Store(db),
        llm=llm,
        policy=policy,
        paypal=make_client(cfg),
        taste=TasteProfile(taste_api, policy.taste_seeds),
        autopilot_invoice=cfg.get("autopilot_invoice", True),
    )


class Inbound(BaseModel):
    sender: str = Field(max_length=300)
    subject: str = Field(max_length=300)
    body: str = Field(min_length=1, max_length=6000)


class Reply(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class Deliver(BaseModel):
    url: str = Field(max_length=500)


class BrandQ(BaseModel):
    brand: str = Field(min_length=1, max_length=120)


def create_app(desk: Desk | None = None, cfg: dict[str, Any] | None = None) -> FastAPI:
    cfg = cfg if cfg is not None else load_config()
    desk = desk or build_desk(cfg)
    app = FastAPI(title="Deal Desk")
    app.state.desk = desk

    from fastapi.responses import JSONResponse

    from .taste import QlooError

    @app.exception_handler(QlooError)
    async def _qloo(_, exc: QlooError):
        return JSONResponse(status_code=503, content={"detail": f"Taste data unavailable right now: {exc}"})

    @app.exception_handler(BudgetExceeded)
    async def _budget(_, exc: BudgetExceeded):
        return JSONResponse(status_code=429, content={"detail": str(exc)})

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
                "channel_url": p.channel_url, "replay": type(desk.llm).__name__ == "ReplayLLM",
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

    @app.get("/api/deals/{i}/presets")
    def presets(i: int):
        return desk.presets(_deal(i))

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

    @app.post("/api/follow-up")
    def follow_up(days_ahead: float = 0):
        """Run the follow-up pass. `days_ahead` lets the demo show what happens
        after a quiet week without waiting one."""
        import time as _t
        return {"actions": desk.follow_up(_t.time() + days_ahead * 86400)}

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

    @app.get("/media-kit", response_class=HTMLResponse)
    def media_kit():
        """A one-page, printable media kit built from the Qloo taste profile."""
        from html import escape as e
        p = desk.policy
        mock = desk.taste.api.mode != "qloo"
        tags = desk.taste.audience_tags()[:10]
        brands = desk.taste.audience_brands()[:8]
        top = max([t.affinity for t in tags] or [1]) or 1
        tag_rows = "".join(f'<div class="t"><span>{e(t.name)}</span><i style="width:{100 * t.affinity / top:.0f}%"></i></div>' for t in tags)
        brand_list = "".join(f"<li>{e(b.name)}</li>" for b in brands)
        rates = "".join(
            f"<tr><td>{e(v.label)}</td><td>${v.target:,.0f}</td>"
            f"<td>{'+$%s for a followed link' % format(v.followed_link_surcharge, ',.0f') if v.followed_link_surcharge and p.followed_links_allowed else ''}</td></tr>"
            for v in p.products.values())
        water = '<div class="mock">Sample data: connect a Qloo key for the real taste profile</div>' if mock else ""
        return f"""<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{e(p.channel_name)} media kit</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
<style>
body{{font:15px/1.5 Inter,system-ui,sans-serif;color:#1b1d22;background:#f6f5f2;margin:0}}
.page{{max-width:760px;margin:32px auto;background:#fff;border:1px solid #e6e3dc;border-radius:16px;padding:40px 44px}}
h1{{margin:0;font-size:30px;letter-spacing:-.02em}} h2{{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:#6b7080;margin:30px 0 10px}}
.sub{{color:#6b7080}} .t{{display:grid;grid-template-columns:180px 1fr;align-items:center;gap:12px;margin:6px 0}}
.t i{{display:block;height:8px;border-radius:4px;background:linear-gradient(90deg,#2f5bea,#11845b)}}
ul{{columns:2;padding-left:18px;margin:0}} table{{width:100%;border-collapse:collapse}} td{{padding:9px 0;border-bottom:1px solid #eee}}
td:nth-child(2){{font-weight:700;text-align:right;padding-right:16px}} td:nth-child(3){{color:#6b7080;font-size:13px}}
.mock{{background:#fdf1dc;color:#b26a00;padding:8px 12px;border-radius:8px;font-size:13px;margin-bottom:18px}}
footer{{margin-top:30px;color:#6b7080;font-size:12.5px}} @media print{{body{{background:#fff}}.page{{border:0;margin:0}}}}
@media (max-width:640px){{.page{{margin:0;border-radius:0;padding:24px 16px}}.t{{grid-template-columns:120px 1fr}}ul{{columns:1}}}}
</style><div class=page>{water}
<h1>{e(p.channel_name)}</h1><div class=sub>{e(p.creator_name)} · <a href="{e(p.channel_url)}">{e(p.channel_url)}</a></div>
<p>{e(p.audience)}</p>
<h2>What this audience loves</h2>{tag_rows or '<p class=sub>No taste data.</p>'}
<h2>Brands this audience over-indexes on</h2><ul>{brand_list}</ul>
<h2>Formats and rates</h2><table>{rates}</table>
<h2>How it works</h2><p>Every sponsorship is labelled as sponsored. Billing is by PayPal invoice, due within {p.invoice_terms_days} days, and work starts once it is paid. Not accepted: {e(", ".join(p.blocked_categories))}.</p>
<footer>{'Sample taste data for demonstration.' if mock else 'Audience taste profile by Qloo, built from what this audience talks about.'}</footer></div></html>"""

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
        i = desk.paypal.invoices.get(inv)
        if not i:
            raise HTTPException(404, "no such invoice")
        desk.paypal.record_payment(inv, i["amount"], i["currency"])
        # Deliver the same event a real PayPal webhook would.
        desk.mark_paid(inv, "webhook")
        return "<p style='font-family:system-ui;margin:60px'>Paid. You can close this tab.</p>"

    return app
