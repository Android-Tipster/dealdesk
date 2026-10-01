"""The deal state machine.

    inbound email
        -> read (Claude)  -> screen (code)  -> taste fit (Qloo)  -> quote (code)
        -> draft (Claude, checked by code)   -> creator approves -> sent
    reply
        -> read (Claude)  -> next move (code) -> draft ...
        -> on written agreement at or above the floor: PayPal invoice (code gate)
    INVOICING.INVOICE.PAID (webhook or poll)
        -> paid -> fulfilment checklist -> delivered with live URL -> receipt email

Every transition is logged on the deal, so the timeline in the UI is the audit
trail of what the agent did and why.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import policy as pol
from .llm import LLM, check_draft, clean
from .models import Deliverable, Stage
from .paypal import InvoiceAPI, PayPalError
from .store import Deal, Store
from .taste import TasteProfile

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


@dataclass
class Desk:
    store: Store
    llm: LLM
    policy: pol.Policy
    paypal: InvoiceAPI
    taste: TasteProfile | None = None
    autopilot_invoice: bool = True

    # ---------------------------------------------------------------- helpers
    def _product(self, d: Deal) -> pol.Product | None:
        try:
            return self.policy.product(Deliverable(d.deliverable))
        except ValueError:
            return None

    def _facts(self, d: Deal) -> str:
        p = self._product(d)
        lines = [
            f"Creator: {self.policy.creator_name}, {self.policy.channel_name} ({self.policy.channel_url})",
            f"Audience: {self.policy.audience}",
            f"Counterparty: {d.sender_name or d.sender_email}, brand: {d.brand or 'not named'}",
            f"What they asked for: {d.summary}",
        ]
        if p:
            lines.append(f"Product: {p.label}")
        if d.wants_followed is not None:
            lines.append("Link type: followed link" if d.wants_followed else "Link type: nofollow, marked sponsored")
        # Only measured taste data may reach a counterparty. Mock data stays in the UI.
        if d.fit_tags and d.fit_source == "qloo":
            lines.append("Audience interests that overlap with the brand: " + ", ".join(d.fit_tags[:4]))
        lines.append(f"Signature:\n{self.policy.signature}")
        return "\n".join(lines)

    def _draft(self, d: Deal, instruction: str, amounts: set[float], attempts: int = 2):
        """Draft, check in code, and retry once with the problems fed back.
        A draft that still fails is kept but cannot be approved."""
        allowed = ", ".join(f"${a:.0f}" for a in sorted(amounts)) or "none"
        task = f"{instruction}\nThe only dollar amounts you may write: {allowed}."
        for n in range(attempts):
            dr = clean(self.llm.draft(task, self._facts(d)))
            problems = check_draft(dr, amounts)
            if not problems:
                break
            d.log("draft", f"Draft {n + 1} rejected by the price guard: {'; '.join(problems)}")
            task += "\nYour previous draft was rejected: " + "; ".join(problems) + ". Fix that."
        d.draft_subject, d.draft_body, d.draft_problems = dr.subject, dr.body, problems
        d.log("draft", f"Drafted: {dr.subject}", problems=problems)

    # ---------------------------------------------------------------- inbound
    def ingest(self, sender: str, subject: str, body: str) -> Deal:
        m = EMAIL_RE.search(sender)
        d = Deal(sender_email=m.group(0) if m else sender, sender_name=sender.split("<")[0].strip().strip('"'),
                 subject=subject)
        d.thread.append({"from": "them", "subject": subject, "body": body})
        read = self.llm.read_inquiry(f"From: {sender}\nSubject: {subject}\n\n{body}")
        d.brand, d.brand_domain, d.category = read.brand_name, read.brand_domain, read.product_category
        d.deliverable, d.wants_followed, d.summary = read.requested_deliverable.value, read.wants_followed_link, read.summary
        d.log("read", f"Read as {read.sender_role.value}: {read.summary}")

        sc = pol.screen(read, f"{subject}\n{body}", self.policy)
        d.screen_reason, d.marker_hits = sc.stage_reason, sc.marker_hits
        if not sc.allowed:
            if sc.stage_reason == "agency has not named the client":
                d.stage = Stage.needs_human.value
                self._draft(d, "Thank them and ask which brand they represent before any pricing. "
                               "Say the creator reviews every sponsor before quoting. Mention no price.", set())
            else:
                d.stage = Stage.screened_out.value
            d.log("screen", f"Screened out: {sc.stage_reason}", hits=sc.marker_hits)
            return self.store.save(d)
        d.log("screen", "Qualified")

        if self.taste and d.brand:
            try:
                f = self.taste.fit(d.brand)
                d.fit_score, d.fit_verdict, d.fit_tags, d.fit_note, d.fit_source = f.score, f.verdict, f.shared_tags, f.note, f.source
                d.log("fit", f"Taste fit {f.score}/100 ({f.verdict}), {f.note}", source=f.source)
            except Exception as e:  # taste data is advice, never a blocker
                d.log("fit", f"Taste fit unavailable: {e}")

        product = self._product(d)
        if product is None:
            d.stage = Stage.needs_human.value
            self._draft(d, "Thank them, list the sponsorship formats available in one line each "
                           "without prices, and ask which they want.", set())
            return self.store.save(d)

        d.quote = pol.opening_price(product, d.wants_followed, self.policy)
        d.stage = Stage.quoted.value
        d.log("quote", f"Opening quote {d.quote:.0f} {self.policy.currency} for {product.label}")
        self._draft(d, f"Reply with a quote of ${d.quote:.0f} for {product.label}. "
                       "State what is included in one or two lines and that payment is by PayPal invoice "
                       f"due within {self.policy.invoice_terms_days} days. Ask if they want to go ahead.",
                    {d.quote})
        return self.store.save(d)

    # ---------------------------------------------------------------- replies
    def on_reply(self, deal_id: int, body: str) -> Deal:
        d = self.store.get(deal_id)
        if d is None:
            raise KeyError(deal_id)
        d.thread.append({"from": "them", "body": body})
        ctx = f"Our current quote: ${d.quote:.0f}\nWhat they asked for: {d.summary}" if d.quote else d.summary
        r = self.llm.read_reply(body, ctx)
        d.log("read", f"Reply read: {r.position}. {r.summary}")
        if r.billing_email:
            d.billing_email = r.billing_email
        if r.billing_name:
            d.billing_name = r.billing_name
        product = self._product(d)

        if r.position == "declines":
            d.stage = Stage.declined.value
            self._draft(d, "Thank them briefly and leave the door open. No price.", set())
        elif r.position == "asks_question" or product is None or d.quote is None:
            d.stage = Stage.needs_human.value
            self._draft(d, f"Answer their question if the facts allow, otherwise say you will check. Question: {r.question}",
                        {d.quote} if d.quote else set())
        elif r.position == "accepts":
            price = r.accepted_usd if r.accepted_usd is not None else d.quote
            self._agree(d, product, price)
        elif r.position == "counters":
            mv = pol.next_move(product=product, current_quote=d.quote, counter=r.counter_usd,
                               concessions_used=d.concessions_used, wants_followed=d.wants_followed, policy=self.policy)
            d.log("negotiate", f"Policy move: {mv.action} ({mv.note})", counter=r.counter_usd)
            if mv.action == "accept":
                self._agree(d, product, mv.price)
            elif mv.action == "concede":
                d.concessions_used += 1
                d.quote = mv.price
                d.stage = Stage.negotiating.value
                self._draft(d, f"Meet them partway: offer ${mv.price:.0f}, say this is the best available for this "
                               "format, and ask them to confirm in writing.", {mv.price})
            elif mv.action == "hold":
                d.stage = Stage.negotiating.value
                self._draft(d, f"Politely hold at ${d.quote:.0f}. Do not offer any other price.", {d.quote})
            else:
                d.stage = Stage.declined.value
                self._draft(d, "Thank them and say the budget does not work for this format. No price.", set())
        else:
            d.stage = Stage.needs_human.value
        return self.store.save(d)

    def _agree(self, d: Deal, product: pol.Product, price: float):
        d.agreed_price = price
        d.billing_email = d.billing_email or d.sender_email
        ok, why = pol.may_invoice(agreed_price=price, product=product, wants_followed=d.wants_followed,
                                  policy=self.policy, billing_email=d.billing_email)
        d.log("agree", f"Agreed at {price:.0f}. Invoice gate: {why}")
        if not ok:
            d.stage = Stage.needs_human.value
            return
        d.stage = Stage.agreed.value
        if self.autopilot_invoice:
            self.invoice(d, save=False)

    # ---------------------------------------------------------------- money
    def invoice(self, d: Deal, save: bool = True) -> Deal:
        product = self._product(d)
        ok, why = pol.may_invoice(agreed_price=d.agreed_price, product=product, wants_followed=d.wants_followed,
                                  policy=self.policy, billing_email=d.billing_email) if product else (False, "no product")
        if not ok:
            d.log("invoice", f"Refused to invoice: {why}")
            return self.store.save(d) if save else d
        number = self.store.next_invoice_number("DD")
        try:
            ref = self.paypal.create_and_send(
                number=number, invoicer_email=None, invoicer_name=self.policy.creator_name,
                recipient_email=d.billing_email, recipient_name=d.billing_name or d.brand,
                item_name=f"{product.label}, {self.policy.channel_name}",
                item_description=f"{d.summary} Brand: {d.brand or 'n/a'}.",
                amount=d.agreed_price, currency=self.policy.currency,
                note=f"Thank you. Work starts once this invoice is paid.", terms_days=self.policy.invoice_terms_days)
        except PayPalError as e:
            d.stage = Stage.needs_human.value
            d.log("invoice", f"PayPal error: {e}")
            return self.store.save(d) if save else d
        d.invoice_id, d.invoice_number, d.invoice_status, d.payer_url = ref.id, ref.number or number, ref.status, ref.payer_url
        d.stage = Stage.invoiced.value
        d.log("invoice", f"PayPal invoice {d.invoice_number} ({ref.id}) sent for {d.agreed_price:.2f}, status {ref.status}",
              mode=self.paypal.mode)
        self._draft(d, f"Confirm the deal at ${d.agreed_price:.0f} and say a PayPal invoice for that amount is on its way "
                       "from PayPal. Work starts as soon as it is paid.", {d.agreed_price})
        return self.store.save(d) if save else d

    def mark_paid(self, invoice_id: str, source: str) -> Deal | None:
        d = self.store.by_invoice(invoice_id)
        if d is None or d.stage in (Stage.paid.value, Stage.delivered.value):
            return d
        ref = self.paypal.get(invoice_id)   # never trust the notification alone
        d.invoice_status = ref.status
        if ref.status != "PAID":
            d.log("payment", f"{source} said paid but PayPal reports {ref.status}; not marking paid")
            return self.store.save(d)
        d.stage = Stage.paid.value
        d.log("payment", f"Invoice {d.invoice_number} PAID (confirmed by GET after {source})")
        self._draft(d, f"Thank them for the payment of ${d.agreed_price:.0f} and say when the placement will go live "
                       "(within 3 business days). Ask for any final brief, link or tracking URL.", {d.agreed_price})
        return self.store.save(d)

    def poll_invoices(self) -> list[Deal]:
        changed = []
        for d in self.store.all():
            if d.stage == Stage.invoiced.value and d.invoice_id:
                try:
                    if self.paypal.get(d.invoice_id).status == "PAID":
                        changed.append(self.mark_paid(d.invoice_id, "poll"))
                except PayPalError as e:
                    d.log("payment", f"Poll failed: {e}")
                    self.store.save(d)
        return changed

    def deliver(self, deal_id: int, live_url: str) -> Deal:
        d = self.store.get(deal_id)
        if d.stage != Stage.paid.value:
            raise ValueError("only a paid deal can be marked delivered")
        d.live_url = live_url
        d.stage = Stage.delivered.value
        d.log("deliver", f"Delivered: {live_url}")
        self._draft(d, f"Tell them the placement is live at {live_url} and thank them. No price.", set())
        return self.store.save(d)

    def approve_draft(self, deal_id: int) -> Deal:
        d = self.store.get(deal_id)
        if d.draft_problems:
            raise ValueError("draft has unresolved problems: " + "; ".join(d.draft_problems))
        d.thread.append({"from": "us", "subject": d.draft_subject, "body": d.draft_body})
        d.log("send", f"Sent: {d.draft_subject}")
        d.draft_subject = d.draft_body = ""
        return self.store.save(d)

    # ---------------------------------------------------------------- outbound
    def pitch(self, brand: str) -> dict:
        """Draft a cold pitch to a brand, grounded in measured taste overlap.

        The pitch states only facts the creator supplied plus the overlap Qloo
        measured. With mock taste data it is returned for preview only and
        flagged so it is never sent.
        """
        if not self.taste:
            raise ValueError("taste data is not configured")
        f = self.taste.fit(brand)
        product = self.policy.products.get("sponsored_post") or next(iter(self.policy.products.values()))
        measured = f.source == "qloo" and f.shared_tags
        facts = "\n".join([
            f"Creator: {self.policy.creator_name}, {self.policy.channel_name} ({self.policy.channel_url})",
            f"Audience: {self.policy.audience}",
            f"Brand being pitched: {brand}",
            ("Measured taste overlap between this audience and the brand's customers: " + ", ".join(f.shared_tags[:4]))
            if measured else "No measured overlap available; do not claim any.",
            f"Signature:\n{self.policy.signature}",
        ])
        task = ("Write a short first-contact pitch to the brand's partnerships team proposing a sponsorship. "
                "Lead with the specific audience overlap. Mention one format, the sponsored tutorial. "
                f"State the price as ${product.target:.0f}. Ask one question: who handles creator partnerships. "
                f"The only dollar amount you may write: ${product.target:.0f}.")
        dr = clean(self.llm.draft(task, facts))
        problems = check_draft(dr, {product.target})
        sendable = f.source == "qloo" and not problems and f.verdict in ("strong", "plausible")
        return {"brand": brand, "fit": {"score": f.score, "verdict": f.verdict, "shared_tags": f.shared_tags,
                "note": f.note, "source": f.source}, "subject": dr.subject, "body": dr.body,
                "problems": problems, "sendable": sendable,
                "why_not_sendable": None if sendable else ("mock taste data" if f.source != "qloo" else
                                                           "weak fit" if f.verdict not in ("strong", "plausible") else "; ".join(problems))}

    # ---------------------------------------------------------------- demo
    @staticmethod
    def presets(d: Deal) -> list[dict]:
        """One-click brand replies for the demo. Text depends only on deal state,
        so the hosted demo can replay recorded model output for every path."""
        if d.stage not in ("quoted", "negotiating") or not d.quote:
            return []
        domain = d.sender_email.split("@")[-1] if "@" in d.sender_email else "brand.example"
        low = int(round(d.quote * 0.65 / 10.0) * 10)
        out = []
        if d.stage == "quoted":
            out.append({"label": f"Counter at ${low}",
                        "text": f"Thanks, we like the idea. Budget is tight this quarter though. Could you do ${low}?"})
        out.append({"label": f"Accept ${d.quote:.0f}",
                    "text": f"That works for us at ${d.quote:.0f}. Please send the invoice to accounts@{domain}."})
        out.append({"label": "Ask a question",
                    "text": "Before we commit, could you share roughly how many clicks a sponsored post usually gets?"})
        return out

