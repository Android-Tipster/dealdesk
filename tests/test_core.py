import json
from pathlib import Path

import pytest

from dealdesk import policy as pol
from dealdesk.desk import Desk
from dealdesk.llm import check_draft
from dealdesk.models import Deliverable, DraftRead, InquiryRead, ReplyRead, SenderRole
from dealdesk.paypal import MockPayPal, PayPalClient, paid_invoice_id
from dealdesk.store import Store
from dealdesk.taste import Entity, MockTaste, TasteProfile, score_fit, weighted_overlap

ROOT = Path(__file__).resolve().parents[1]
POLICY = pol.Policy.load(ROOT / "config" / "policy.example.json")


def inquiry(**kw):
    base = dict(is_sponsorship_inquiry=True, sender_role=SenderRole.brand, brand_name="Framewise",
                brand_domain="framewise.io", product_category="video editing software",
                requested_deliverable=Deliverable.sponsored_post, budget_mentioned_usd=None,
                wants_followed_link=False, evidence_of_template=[], summary="A sponsored tutorial for Framewise.")
    base.update(kw)
    return InquiryRead(**base)


class FakeLLM:
    """Scripted reads; drafts echo the instruction so price checks are real."""

    def __init__(self, inquiry_read, replies=()):
        self.inquiry_read, self.replies = inquiry_read, list(replies)

    def read_inquiry(self, email):
        return self.inquiry_read

    def read_reply(self, email, context):
        return self.replies.pop(0)

    def draft(self, instruction, facts):
        return DraftRead(subject="Re: sponsorship", body=instruction)


def make_desk(tmp_path, llm, **kw):
    return Desk(store=Store(str(tmp_path / "t.db")), llm=llm, policy=POLICY, paypal=MockPayPal(),
                taste=TasteProfile(MockTaste(), POLICY.taste_seeds), **kw)


# ------------------------------------------------------------------ policy
P = POLICY.products["sponsored_post"]   # target 450, floor 350, step 50


def test_counter_at_or_above_quote_is_accepted_at_quote():
    mv = pol.next_move(product=P, current_quote=450, counter=500, concessions_used=0, wants_followed=False, policy=POLICY)
    assert (mv.action, mv.price) == ("accept", 450)


def test_one_step_concession_never_below_floor():
    mv = pol.next_move(product=P, current_quote=450, counter=200, concessions_used=0, wants_followed=False, policy=POLICY)
    assert (mv.action, mv.price) == ("concede", 400)
    tight = pol.Product("x", "x", target=360, floor=350, step=50)
    mv = pol.next_move(product=tight, current_quote=360, counter=300, concessions_used=0, wants_followed=False, policy=POLICY)
    assert mv.price == 350


def test_counter_within_one_step_is_accepted_at_counter():
    mv = pol.next_move(product=P, current_quote=450, counter=420, concessions_used=0, wants_followed=False, policy=POLICY)
    assert (mv.action, mv.price) == ("accept", 420)


def test_after_concession_counter_above_floor_accepted_below_floor_held():
    mv = pol.next_move(product=P, current_quote=400, counter=360, concessions_used=1, wants_followed=False, policy=POLICY)
    assert (mv.action, mv.price) == ("accept", 360)
    mv = pol.next_move(product=P, current_quote=400, counter=300, concessions_used=1, wants_followed=False, policy=POLICY)
    assert (mv.action, mv.price) == ("hold", 400)
    mv = pol.next_move(product=P, current_quote=400, counter=100, concessions_used=1, wants_followed=False, policy=POLICY)
    assert mv.action == "decline"


def test_followed_link_raises_floor_and_quote():
    assert pol.opening_price(P, True, POLICY) == 600
    ok, why = pol.may_invoice(agreed_price=450, product=P, wants_followed=True, policy=POLICY, billing_email="a@b.co")
    assert not ok and "below the floor" in why


def test_invoice_gate_needs_price_floor_and_email():
    assert pol.may_invoice(agreed_price=None, product=P, wants_followed=False, policy=POLICY, billing_email="a@b.co")[0] is False
    assert pol.may_invoice(agreed_price=349.99, product=P, wants_followed=False, policy=POLICY, billing_email="a@b.co")[0] is False
    assert pol.may_invoice(agreed_price=350, product=P, wants_followed=False, policy=POLICY, billing_email="nope")[0] is False
    assert pol.may_invoice(agreed_price=350, product=P, wants_followed=False, policy=POLICY, billing_email="a@b.co")[0] is True


@pytest.mark.parametrize("kw,reason", [
    (dict(product_category="online casino"), "blocked category"),
    (dict(sender_role=SenderRole.link_broker), "link broker"),
    (dict(sender_role=SenderRole.automated_blast), "mass-sent"),
    (dict(sender_role=SenderRole.agency, brand_name=None), "agency has not named"),
    (dict(is_sponsorship_inquiry=False), "not a sponsorship"),
])
def test_screen_rejects(kw, reason):
    assert reason in pol.screen(inquiry(**kw), "hello", POLICY).stage_reason


def test_screen_template_markers_need_two_hits():
    one = "Dear Webmaster, we love your content."
    two = "Dear Webmaster, do you accept guest posts? What is your price for a DA 50 link?"
    assert pol.screen(inquiry(), one, POLICY).allowed
    assert not pol.screen(inquiry(), two, POLICY).allowed


# ------------------------------------------------------------------ drafts
def test_check_draft_flags_unauthorised_amounts():
    d = DraftRead(subject="Quote", body="The price is $450, or $400 if you book two.")
    probs = check_draft(d, {450})
    assert len(probs) == 1 and "400" in probs[0]
    assert check_draft(DraftRead(subject="x", body="It is 450 USD."), {450}) == []


# ------------------------------------------------------------------ end to end
def test_full_deal_quote_counter_concede_accept_invoice_paid_delivered(tmp_path):
    llm = FakeLLM(inquiry(), replies=[
        ReplyRead(position="counters", counter_usd=300, accepted_usd=None, billing_email=None, billing_name=None, question=None, summary="Can you do 300?"),
        ReplyRead(position="accepts", accepted_usd=400, counter_usd=None, billing_email="ap@framewise.io", billing_name="Framewise Inc.", question=None, summary="400 works."),
    ])
    desk = make_desk(tmp_path, llm)
    d = desk.ingest('"Dana" <dana@framewise.io>', "Sponsorship", "Hi Maya, we'd love a sponsored tutorial.")
    assert d.stage == "quoted" and d.quote == 450 and d.draft_problems == []
    assert d.fit_source == "mock" and d.fit_score is not None

    d = desk.on_reply(d.id, "Can you do 300?")
    assert d.stage == "negotiating" and d.quote == 400 and d.concessions_used == 1

    d = desk.on_reply(d.id, "400 works, invoice ap@framewise.io")
    assert d.stage == "invoiced" and d.agreed_price == 400 and d.billing_email == "ap@framewise.io"
    assert d.invoice_id.startswith("INV2-MOCK")

    desk.paypal.record_payment(d.invoice_id, 400, "USD")
    paid = desk.poll_invoices()
    assert len(paid) == 1 and paid[0].stage == "paid"

    d = desk.deliver(d.id, "https://theframerate.example/framewise-tutorial")
    assert d.stage == "delivered"
    kinds = [e["kind"] for e in d.events]
    assert kinds.count("invoice") == 1 and "payment" in kinds and "deliver" in kinds


def test_accept_below_floor_never_invoices(tmp_path):
    llm = FakeLLM(inquiry(), replies=[
        ReplyRead(position="accepts", accepted_usd=200, counter_usd=None, billing_email=None, billing_name=None, question=None, summary="We accept at 200."),
    ])
    desk = make_desk(tmp_path, llm)
    d = desk.ingest("dana@framewise.io", "Sponsorship", "Hi")
    d = desk.on_reply(d.id, "We accept at 200")
    assert d.stage == "needs_human" and d.invoice_id is None
    assert desk.paypal.invoices == {}


def test_forged_paid_webhook_is_not_trusted(tmp_path):
    llm = FakeLLM(inquiry(), replies=[
        ReplyRead(position="accepts", accepted_usd=450, counter_usd=None, billing_email=None, billing_name=None, question=None, summary="Deal."),
    ])
    desk = make_desk(tmp_path, llm)
    d = desk.ingest("dana@framewise.io", "Sponsorship", "Hi")
    d = desk.on_reply(d.id, "Deal at 450")
    assert d.stage == "invoiced"
    # A notification arrives claiming payment, but PayPal still says SENT.
    d = desk.mark_paid(d.invoice_id, "webhook")
    assert d.stage == "invoiced"
    assert any("not marking paid" in e["text"] for e in d.events)


def test_blast_is_screened_without_reply(tmp_path):
    desk = make_desk(tmp_path, FakeLLM(inquiry(sender_role=SenderRole.automated_blast)))
    d = desk.ingest("seo@links.example", "Guest post", "Dear webmaster")
    assert d.stage == "screened_out" and d.draft_body == ""


def test_agency_without_client_gets_question_not_price(tmp_path):
    desk = make_desk(tmp_path, FakeLLM(inquiry(sender_role=SenderRole.agency, brand_name=None)))
    d = desk.ingest("amy@agency.example", "Partnership", "We represent a client")
    assert d.stage == "needs_human" and d.quote is None and d.draft_problems == []


# ------------------------------------------------------------------ paypal + taste
def test_paid_invoice_id_both_shapes():
    assert paid_invoice_id({"event_type": "INVOICING.INVOICE.PAID", "resource": {"invoice": {"id": "INV2-A"}}}) == "INV2-A"
    assert paid_invoice_id({"event_type": "INVOICING.INVOICE.PAID", "resource": {"id": "INV2-B"}}) == "INV2-B"
    assert paid_invoice_id({"event_type": "INVOICING.INVOICE.UPDATED", "resource": {"invoice": {"id": "X", "status": "SENT"}}}) is None
    assert paid_invoice_id({"event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {"id": "C"}}) is None


def test_paypal_client_builds_expected_requests():
    import httpx
    seen = []

    def handler(req: httpx.Request):
        is_json = req.headers.get("content-type", "").startswith("application/json") and req.content
        seen.append((req.method, req.url.path, json.loads(req.content) if is_json else None))
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if req.url.path == "/v2/invoicing/invoices" and req.method == "POST":
            return httpx.Response(201, json={"id": "INV2-TEST", "status": "DRAFT"})
        if req.url.path.endswith("/send"):
            return httpx.Response(200, json={"href": "https://www.sandbox.paypal.com/invoice/p/#INV2-TEST"})
        if req.method == "GET":
            return httpx.Response(200, json={"id": "INV2-TEST", "status": "SENT", "detail": {"invoice_number": "DD-1",
                                  "metadata": {"recipient_view_url": "https://www.sandbox.paypal.com/invoice/p/#INV2-TEST"}}})
        return httpx.Response(404)

    c = PayPalClient("id", "secret", http=httpx.Client(transport=httpx.MockTransport(handler)))
    ref = c.create_and_send(number="DD-1", invoicer_email=None, invoicer_name="Maya Chen", recipient_email="ap@x.io",
                            recipient_name="X Inc", item_name="Sponsored tutorial", item_description="d", amount=400,
                            currency="USD", note="n", terms_days=7)
    assert ref.id == "INV2-TEST" and ref.status == "SENT" and "INV2-TEST" in ref.payer_url
    create = next(s for s in seen if s[1] == "/v2/invoicing/invoices" and s[0] == "POST")[2]
    assert create["items"][0]["unit_amount"] == {"currency_code": "USD", "value": "400.00"}
    assert create["primary_recipients"][0]["billing_info"]["email_address"] == "ap@x.io"
    assert create["detail"]["payment_term"]["term_type"] == "DUE_ON_RECEIPT"


def test_weighted_overlap_and_score():
    a = [Entity("1", "video editing", 0.9), Entity("2", "indie film", 0.6), Entity("3", "fitness", 0.1)]
    b = [Entity("x", "video editing", 0.8), Entity("y", "gaming", 0.7)]
    sim, shared = weighted_overlap(a, b)
    assert 0 < sim < 1 and shared == ["video editing"]
    assert weighted_overlap(a, [Entity("z", "cooking", 1)])[0] == 0
    f = score_fit(brand_name="Acme", brand=Entity("b1", "Acme"), audience_tags=a, brand_tags=a,
                  audience_brands=[Entity("b1", "Acme", .9), Entity("b2", "Other", .5)], source="t")
    assert f.score == 100 and f.brand_rank == 1 and f.verdict == "strong"
    assert score_fit(brand_name="Nope", brand=None, audience_tags=a, brand_tags=[], audience_brands=[], source="t").verdict == "unknown"


class OnceBadLLM(FakeLLM):
    """First draft quotes a wrong price, second is clean."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k); self.calls = 0
    def draft(self, instruction, facts):
        self.calls += 1
        if self.calls == 1:
            return DraftRead(subject="Quote", body="We could also do $199 if that helps.")
        assert "rejected" in instruction and "$450" in instruction   # feedback reached the model
        return DraftRead(subject="Quote", body="The price is $450.")


def test_bad_draft_is_retried_with_feedback(tmp_path):
    llm = OnceBadLLM(inquiry())
    desk = make_desk(tmp_path, llm)
    d = desk.ingest("dana@framewise.io", "Sponsorship", "Hi")
    assert llm.calls == 2 and d.draft_problems == []
    assert any("rejected by the price guard" in e["text"] for e in d.events)


class AlwaysBadLLM(FakeLLM):
    def draft(self, instruction, facts):
        return DraftRead(subject="Quote", body="Special price $1.")


def test_persistently_bad_draft_cannot_be_approved(tmp_path):
    desk = make_desk(tmp_path, AlwaysBadLLM(inquiry()))
    d = desk.ingest("dana@framewise.io", "Sponsorship", "Hi")
    assert d.draft_problems
    with pytest.raises(ValueError):
        desk.approve_draft(d.id)


class FactsSpy(FakeLLM):
    def draft(self, instruction, facts):
        self.facts = facts
        return DraftRead(subject="x", body=instruction)


def test_mock_taste_never_reaches_a_counterparty(tmp_path):
    llm = FactsSpy(inquiry())
    desk = make_desk(tmp_path, llm)
    d = desk.ingest("dana@framewise.io", "Sponsorship", "Hi")
    assert d.fit_source == "mock" and d.fit_tags
    assert "Audience interests" not in llm.facts


def test_model_template_guesses_alone_never_screen_out_a_brand():
    r = inquiry(evidence_of_template=["Hello,", "newsletters your size", "we'd like to book"])
    sc = pol.screen(r, "Hello, we'd like to book a sponsor slot.", POLICY)
    assert sc.allowed and len(sc.marker_hits) == 3


def test_pitch_with_mock_taste_is_never_sendable(tmp_path):
    desk = make_desk(tmp_path, FakeLLM(inquiry()))
    out = desk.pitch("Epidemic Sound")
    assert out["fit"]["source"] == "mock" and out["sendable"] is False and out["why_not_sendable"] == "mock taste data"
