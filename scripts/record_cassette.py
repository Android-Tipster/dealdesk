"""Record model outputs for every one-click path in the demo.

    DEALDESK_CASSETTE=demo/cassette.json DEALDESK_RECORD=1 python scripts/record_cassette.py

Each branch runs on its own fresh database so the deal state, and therefore the
cache keys, match what a visitor clicking the same buttons will produce.
"""
import json, os, re, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dealdesk.app import build_desk  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = [json.loads(f.read_text(encoding="utf-8")) for f in sorted((ROOT / "samples").glob("*.json"))]


def fresh():
    desk = build_desk(db=str(Path(tempfile.mkdtemp()) / "r.db"))
    for s in SAMPLES:
        desk.ingest(s["from"], s["subject"], s["body"])
    return desk


def by_brand(desk, brand):
    return next(d for d in desk.store.all() if d.brand == brand)


def live_url(desk, d):
    slug = re.sub(r"[^a-z0-9]+", "-", (d.brand or "sponsor").lower()).strip("-")
    return desk.policy.channel_url.rstrip("/") + "/sponsored/" + slug


def finish(desk, d):
    """From invoiced: pay, mark paid, deliver."""
    d = desk.store.get(d.id)
    if d.stage != "invoiced":
        return d
    desk.approve_draft(d.id)
    desk.paypal.record_payment(d.invoice_id, d.agreed_price, "USD")
    desk.mark_paid(d.invoice_id, "webhook")
    desk.approve_draft(d.id)
    d = desk.store.get(d.id)
    return desk.deliver(d.id, live_url(desk, d))


def preset(desk, d, label_start):
    d = desk.store.get(d.id)
    if d.draft_body and not d.draft_problems:
        desk.approve_draft(d.id)
    p = next(x for x in desk.presets(d) if x["label"].startswith(label_start))
    return desk.on_reply(d.id, p["text"])


desk = fresh()   # also records the six inbound reads and first drafts
brands = [d.brand for d in desk.store.all() if d.stage == "quoted"]
print("quoted brands:", brands)
for brand in brands:
    for path in (["Accept"], ["Counter", "Accept"], ["Ask"], ["Counter", "Ask"]):
        desk = fresh()
        d = by_brand(desk, brand)
        for step in path:
            d = preset(desk, d, step)
        d = finish(desk, d)
        print(f"{brand:10} {' > '.join(path):18} -> {d.stage}")
desk = fresh()
for b in desk.taste.prospects({x.brand for x in desk.store.all() if x.brand}, 12):
    desk.pitch(b.name)
print("pitches recorded")
print("cassette entries:", len(json.loads(Path(os.environ["DEALDESK_CASSETTE"]).read_text())))
