from fastapi.testclient import TestClient

from dealdesk.app import create_app
from dealdesk.llm import ReplayLLM
from tests.test_core import FakeLLM, inquiry, make_desk


def client(tmp_path, llm=None):
    desk = make_desk(tmp_path, llm or FakeLLM(inquiry()))
    return TestClient(create_app(desk, cfg={})), desk


def test_inbound_quote_presets_and_status(tmp_path):
    c, _ = client(tmp_path)
    assert c.get("/api/status").json()["paypal"] == "mock"
    d = c.post("/api/inbound", json={"sender": "dana@cutline.example", "subject": "Hi", "body": "Sponsor?"}).json()
    assert d["stage"] == "quoted"
    labels = [p["label"] for p in c.get(f"/api/deals/{d['id']}/presets").json()]
    assert labels[0].startswith("Counter at $") and any(l.startswith("Accept") for l in labels)
    assert c.get("/").status_code == 200


def test_size_limits(tmp_path):
    c, _ = client(tmp_path)
    r = c.post("/api/inbound", json={"sender": "a@b.c", "subject": "x", "body": "x" * 6001})
    assert r.status_code == 422


def test_budget_exhausted_is_429_not_500(tmp_path):
    llm = ReplayLLM(None, str(tmp_path / "empty.json"), daily_cap=0)
    c, _ = client(tmp_path, llm)
    r = c.post("/api/inbound", json={"sender": "a@b.c", "subject": "x", "body": "new email"})
    assert r.status_code == 429 and "budget" in r.json()["detail"]


def test_unknown_deal_404_and_deliver_unpaid_409(tmp_path):
    c, _ = client(tmp_path)
    assert c.get("/api/deals/999").status_code == 404
    d = c.post("/api/inbound", json={"sender": "a@b.c", "subject": "x", "body": "y"}).json()
    assert c.post(f"/api/deals/{d['id']}/deliver", json={"url": "https://x"}).status_code == 409


def test_mock_pay_page_marks_paid_via_reread(tmp_path):
    from dealdesk.models import ReplyRead
    llm = FakeLLM(inquiry(), replies=[ReplyRead(position="accepts", accepted_usd=450, counter_usd=None,
                                                billing_email=None, billing_name=None, question=None, summary="ok")])
    c, desk = client(tmp_path, llm)
    d = c.post("/api/inbound", json={"sender": "a@b.co", "subject": "x", "body": "y"}).json()
    d = c.post(f"/api/deals/{d['id']}/reply", json={"body": "deal"}).json()
    assert d["stage"] == "invoiced"
    assert c.get(d["payer_url"]).status_code == 200
    c.post(d["payer_url"])
    assert c.get(f"/api/deals/{d['id']}").json()["stage"] == "paid"
