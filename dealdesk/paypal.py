"""PayPal Invoicing v2, sandbox first.

Why an invoice and not a payment link: in the four months of sponsorship mail
this tool was modelled on, every deal billed with a real PayPal invoice was paid
(3 of 3) and the one billed by pasting a PayPal address was not (0 of 1). Brands
route invoices through accounts payable; a bare address asks a marketer to do a
transfer by hand. So the agent's commerce step is: draft, send, then watch for
PAID, by webhook when the app is reachable and by polling when it is not.

`PayPalClient` talks to the real REST API. `MockPayPal` implements the same
methods in memory so the whole pipeline can be demonstrated, and tested,
without credentials. Pick with `make_client()`.
"""
from __future__ import annotations

import itertools
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

SANDBOX = "https://api-m.sandbox.paypal.com"
LIVE = "https://api-m.paypal.com"


@dataclass
class InvoiceRef:
    id: str
    status: str
    payer_url: str | None
    number: str | None = None


class InvoiceAPI(Protocol):
    mode: str
    def create_and_send(self, *, number: str, invoicer_email: str | None, invoicer_name: str,
                        recipient_email: str, recipient_name: str | None, item_name: str,
                        item_description: str, amount: float, currency: str, note: str,
                        terms_days: int) -> InvoiceRef: ...
    def get(self, invoice_id: str) -> InvoiceRef: ...
    def record_payment(self, invoice_id: str, amount: float, currency: str, method: str = "PAYPAL") -> InvoiceRef: ...
    def remind(self, invoice_id: str, note: str) -> None: ...
    def cancel(self, invoice_id: str, note: str) -> InvoiceRef: ...
    def verify_webhook(self, headers: dict[str, str], event: dict[str, Any], webhook_id: str) -> bool: ...


class PayPalError(RuntimeError):
    pass


def payment_term(days: int, today=None) -> dict[str, str]:
    """PayPal accepts NET_10/15/30/45/60/90; any other term is sent as an
    explicit due date so the invoice says what the emails promise."""
    import datetime
    if days <= 0:
        return {"term_type": "DUE_ON_RECEIPT"}
    if days in (10, 15, 30, 45, 60, 90):
        return {"term_type": f"NET_{days}"}
    due = (today or datetime.date.today()) + datetime.timedelta(days=days)
    return {"term_type": "DUE_ON_DATE_SPECIFIED", "due_date": due.isoformat()}


class PayPalClient:
    mode = "sandbox"

    def __init__(self, client_id: str, secret: str, base: str = SANDBOX, http: httpx.Client | None = None):
        self.client_id, self.secret, self.base = client_id, secret, base
        self.mode = "live" if base == LIVE else "sandbox"
        self.http = http or httpx.Client(timeout=30)
        self._token: str | None = None
        self._token_exp = 0.0

    # -- auth ------------------------------------------------------------
    def _bearer(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = self.http.post(f"{self.base}/v1/oauth2/token", auth=(self.client_id, self.secret),
                           data={"grant_type": "client_credentials"},
                           headers={"Accept": "application/json"})
        if r.status_code != 200:
            raise PayPalError(f"oauth failed {r.status_code}: {r.text[:300]}")
        j = r.json()
        self._token, self._token_exp = j["access_token"], time.time() + int(j.get("expires_in", 3000))
        return self._token

    def _req(self, method: str, path: str, **kw) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._bearer()}", "Content-Type": "application/json",
                   "Prefer": "return=representation", **kw.pop("headers", {})}
        r = self.http.request(method, f"{self.base}{path}", headers=headers, **kw)
        if r.status_code >= 400:
            raise PayPalError(f"{method} {path} -> {r.status_code}: {r.text[:500]}")
        return r

    # -- invoicing -------------------------------------------------------
    @staticmethod
    def _ref(j: dict[str, Any]) -> InvoiceRef:
        payer = None
        for link in j.get("links", []) or []:
            if link.get("rel") in ("payer-view", "payer_view"):
                payer = link.get("href")
        if not payer:
            payer = (j.get("detail", {}).get("metadata", {}) or {}).get("recipient_view_url")
        return InvoiceRef(id=j["id"], status=j.get("status", "UNKNOWN"), payer_url=payer,
                          number=j.get("detail", {}).get("invoice_number"))

    def create_and_send(self, *, number, invoicer_email, invoicer_name, recipient_email, recipient_name,
                        item_name, item_description, amount, currency, note, terms_days) -> InvoiceRef:
        given, _, surname = (invoicer_name or "").partition(" ")
        body: dict[str, Any] = {
            "detail": {
                "invoice_number": number,
                "currency_code": currency,
                "note": note,
                "payment_term": payment_term(terms_days),
            },
            "invoicer": {"name": {"given_name": given or invoicer_name, "surname": surname or ""}},
            "primary_recipients": [{"billing_info": {"email_address": recipient_email,
                                                     **({"name": {"full_name": recipient_name}} if recipient_name else {})}}],
            "items": [{"name": item_name[:200], "description": item_description[:1000], "quantity": "1",
                       "unit_amount": {"currency_code": currency, "value": f"{amount:.2f}"},
                       "unit_of_measure": "QUANTITY"}],
        }
        if invoicer_email:
            body["invoicer"]["email_address"] = invoicer_email
        draft = self._req("POST", "/v2/invoicing/invoices", json=body).json()
        inv_id = draft.get("id") or draft.get("href", "").rstrip("/").split("/")[-1]
        sent = self._req("POST", f"/v2/invoicing/invoices/{inv_id}/send",
                         json={"send_to_invoicer": True, "send_to_recipient": True}).json()
        ref = self.get(inv_id)
        if not ref.payer_url:
            ref.payer_url = sent.get("href")
        return ref

    def get(self, invoice_id: str) -> InvoiceRef:
        return self._ref(self._req("GET", f"/v2/invoicing/invoices/{invoice_id}").json())

    def record_payment(self, invoice_id, amount, currency, method="PAYPAL") -> InvoiceRef:
        self._req("POST", f"/v2/invoicing/invoices/{invoice_id}/payments",
                  json={"method": method, "amount": {"currency_code": currency, "value": f"{amount:.2f}"},
                        "payment_date": time.strftime("%Y-%m-%d")})
        return self.get(invoice_id)

    def remind(self, invoice_id, note) -> None:
        self._req("POST", f"/v2/invoicing/invoices/{invoice_id}/remind",
                  json={"subject": "Reminder: invoice due", "note": note,
                        "send_to_invoicer": False, "send_to_recipient": True})

    def cancel(self, invoice_id, note) -> InvoiceRef:
        self._req("POST", f"/v2/invoicing/invoices/{invoice_id}/cancel",
                  json={"subject": "Invoice cancelled", "note": note,
                        "send_to_invoicer": False, "send_to_recipient": True})
        return self.get(invoice_id)

    def verify_webhook(self, headers, event, webhook_id) -> bool:
        h = {k.lower(): v for k, v in headers.items()}
        body = {
            "auth_algo": h.get("paypal-auth-algo"), "cert_url": h.get("paypal-cert-url"),
            "transmission_id": h.get("paypal-transmission-id"), "transmission_sig": h.get("paypal-transmission-sig"),
            "transmission_time": h.get("paypal-transmission-time"), "webhook_id": webhook_id, "webhook_event": event,
        }
        if not all(body.values()):
            return False
        r = self._req("POST", "/v1/notifications/verify-webhook-signature", json=body)
        return r.json().get("verification_status") == "SUCCESS"


class MockPayPal:
    """In-memory stand-in with the same surface, for demos and tests."""

    mode = "mock"
    _seq = itertools.count(1)

    def __init__(self):
        self.invoices: dict[str, dict[str, Any]] = {}

    def create_and_send(self, *, number, invoicer_email, invoicer_name, recipient_email, recipient_name,
                        item_name, item_description, amount, currency, note, terms_days) -> InvoiceRef:
        if amount <= 0:
            raise PayPalError("amount must be positive")
        inv_id = f"INV2-MOCK-{next(self._seq):04d}-{uuid.uuid4().hex[:4].upper()}"
        self.invoices[inv_id] = {"status": "SENT", "amount": amount, "currency": currency, "number": number,
                                 "recipient": recipient_email, "item": item_name}
        return InvoiceRef(inv_id, "SENT", f"/mock-paypal/pay/{inv_id}", number)

    def get(self, invoice_id) -> InvoiceRef:
        inv = self.invoices.get(invoice_id)
        if not inv:
            raise PayPalError(f"no invoice {invoice_id}")
        return InvoiceRef(invoice_id, inv["status"], f"/mock-paypal/pay/{invoice_id}", inv["number"])

    def record_payment(self, invoice_id, amount, currency, method="PAYPAL") -> InvoiceRef:
        inv = self.invoices[invoice_id]
        if round(amount, 2) < round(inv["amount"], 2):
            inv["status"] = "PARTIALLY_PAID"
        else:
            inv["status"] = "PAID"
        return self.get(invoice_id)

    def remind(self, invoice_id, note) -> None:
        inv = self.invoices[invoice_id]
        if inv["status"] not in ("SENT", "UNPAID", "PARTIALLY_PAID"):
            raise PayPalError(f"cannot remind an invoice in status {inv['status']}")
        inv["reminders"] = inv.get("reminders", 0) + 1

    def cancel(self, invoice_id, note) -> InvoiceRef:
        inv = self.invoices[invoice_id]
        if inv["status"] == "PAID":
            raise PayPalError("cannot cancel a paid invoice")
        inv["status"] = "CANCELLED"
        return self.get(invoice_id)

    def verify_webhook(self, headers, event, webhook_id) -> bool:
        return headers.get("x-mock-signature") == "ok"


def make_client(cfg: dict[str, Any]) -> InvoiceAPI:
    pp = cfg.get("paypal") or {}
    if pp.get("client_id") and pp.get("secret"):
        return PayPalClient(pp["client_id"], pp["secret"], LIVE if pp.get("live") else SANDBOX)
    return MockPayPal()


def paid_invoice_id(event: dict[str, Any]) -> str | None:
    """Pull the invoice id out of an INVOICING.INVOICE.PAID webhook.

    PayPal has shipped this resource both as the invoice itself and wrapped in
    an `invoice` key, so accept either.
    """
    if event.get("event_type") not in ("INVOICING.INVOICE.PAID", "INVOICING.INVOICE.UPDATED"):
        return None
    res = event.get("resource") or {}
    inv = res.get("invoice", res)
    status = inv.get("status")
    if event["event_type"] == "INVOICING.INVOICE.UPDATED" and status != "PAID":
        return None
    return inv.get("id")
