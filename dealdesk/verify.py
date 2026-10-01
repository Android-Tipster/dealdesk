"""Live smoke test for the two external APIs.

    python -m dealdesk.verify            # both
    python -m dealdesk.verify paypal     # sandbox invoice round trip
    python -m dealdesk.verify qloo       # search + both insight calls

Each check prints what it sent and what came back, and exits non-zero on the
first mismatch, so a wrong field name surfaces here instead of in a demo.
"""
from __future__ import annotations

import sys

from .app import load_config
from .paypal import PayPalClient, SANDBOX
from .taste import QlooClient


def check_paypal(cfg) -> None:
    pp = cfg.get("paypal") or {}
    if not (pp.get("client_id") and pp.get("secret")):
        sys.exit("paypal: no sandbox client_id/secret in config/config.json or env")
    c = PayPalClient(pp["client_id"], pp["secret"], SANDBOX)
    recipient = pp.get("test_recipient", "sb-buyer@personal.example.com")
    ref = c.create_and_send(number=f"DD-VERIFY-{__import__('time').strftime('%H%M%S')}", invoicer_email=None,
                            invoicer_name="Deal Desk Verify", recipient_email=recipient, recipient_name="Verify Buyer",
                            item_name="Verification placement", item_description="Smoke test", amount=1.00,
                            currency="USD", note="Smoke test, ignore.", terms_days=7)
    print(f"paypal: created and sent {ref.id} status={ref.status} payer_url={ref.payer_url}")
    assert ref.status in ("SENT", "UNPAID", "SCHEDULED"), ref.status
    assert ref.payer_url, "no payer url in the invoice response"
    paid = c.record_payment(ref.id, 1.00, "USD")
    print(f"paypal: recorded payment, status={paid.status}")
    assert paid.status in ("PAID", "MARKED_AS_PAID"), paid.status
    second = c.create_and_send(number=f"DD-VERIFY-{__import__('time').strftime('%H%M%S')}-B", invoicer_email=None,
                               invoicer_name="Deal Desk Verify", recipient_email=recipient, recipient_name="Verify Buyer",
                               item_name="Verification placement", item_description="Smoke test", amount=1.00,
                               currency="USD", note="Smoke test, ignore.", terms_days=7)
    c.remind(second.id, "Smoke test reminder, ignore.")
    print(f"paypal: reminded {second.id}")
    cancelled = c.cancel(second.id, "Smoke test, ignore.")
    print(f"paypal: cancelled, status={cancelled.status}")
    assert cancelled.status == "CANCELLED", cancelled.status
    print("paypal: OK")


def check_qloo(cfg) -> None:
    key = (cfg.get("qloo") or {}).get("api_key")
    if not key:
        sys.exit("qloo: no api_key")
    q = QlooClient(key)
    seeds = []
    for name in ["DaVinci Resolve", "Corridor Crew", "Wes Anderson"]:
        e = q.resolve(name, "")
        print(f"qloo: resolve {name!r} -> {e}")
        if e:
            seeds.append(e.id)
    assert seeds, "search returned nothing for any seed"
    tags = q.tags(seeds, 10)
    print(f"qloo: {len(tags)} tags, first: {[t.name for t in tags[:5]]}")
    assert tags and tags[0].name, "tag insights empty or unnamed"
    brands = q.brands(seeds, 10)
    print(f"qloo: {len(brands)} brands, first: {[(b.name, b.affinity) for b in brands[:5]]}")
    assert brands and brands[0].name, "brand insights empty or unnamed"
    assert any(b.affinity for b in brands), "no affinity values parsed; check the response shape"
    b = q.resolve("Adobe", "urn:entity:brand")
    print(f"qloo: brand resolve Adobe -> {b}")
    print("qloo: OK")


if __name__ == "__main__":
    cfg = load_config()
    which = sys.argv[1:] or ["paypal", "qloo"]
    if "paypal" in which:
        check_paypal(cfg)
    if "qloo" in which:
        check_qloo(cfg)
