"""The creator's commercial rules, enforced in code.

Nothing in this module calls a model. Claude may read an email and say "they
counter at $90", but whether $90 is acceptable, what we say back, and whether an
invoice may be raised is decided here. An agent that can send invoices must not
be able to talk itself below the floor, so the floor is not in a prompt.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .models import Deliverable, InquiryRead, SenderRole


@dataclass
class Product:
    key: str
    label: str
    target: float          # first quote
    floor: float           # never invoice below this
    step: float            # size of one concession
    followed_link_surcharge: float = 0.0


@dataclass
class Policy:
    creator_name: str
    channel_name: str
    channel_url: str
    audience: str
    currency: str
    products: dict[str, Product]
    blocked_categories: list[str]
    max_concessions: int = 1
    followed_links_allowed: bool = True
    invoice_terms_days: int = 7
    signature: str = ""
    taste_seeds: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        prods = {k: Product(key=k, **v) for k, v in raw.pop("products").items()}
        return cls(products=prods, **raw)

    def product(self, d: Deliverable) -> Product | None:
        return self.products.get(d.value)


# Phrases that showed up again and again in four months of real inbound mail
# that turned out to be mass-sent. Each one alone proves little; two or more
# together almost always meant a blast.
TEMPLATE_MARKERS = [
    r"dear (webmaster|site owner|admin|editor)",
    r"\bguest post(ing)?\b.*\b(your|ur) (site|website|blog)\b",
    r"\bdo you accept (guest posts|sponsored posts)\b",
    r"\bwhat (is|are) your (price|prices|rates) for\b",
    r"\bhigh[- ]da\b|\bda ?\d{2}\+?",
    r"\bcasino|betting|gambling|crypto(currency)?\b",
    r"\bwe have (several|multiple|many) clients\b",
    r"\bbulk (order|deal|links)\b",
    r"\bniche edit",
]


@dataclass
class Screen:
    allowed: bool
    stage_reason: str
    marker_hits: list[str]


def screen(read: InquiryRead, raw_text: str, policy: Policy) -> Screen:
    """Decide whether an inquiry deserves a quote at all."""
    text = raw_text.lower()
    hits = [m for m in TEMPLATE_MARKERS if re.search(m, text)]
    # Claude's own template guesses are shown to the creator but never counted:
    # on a real brand email it flagged "Hello," and "newsletters your size",
    # which would have screened out a paying sponsor. Only the deterministic
    # markers, or Claude classifying the sender role itself, can screen.
    model_hints = [f"model: {p}" for p in read.evidence_of_template]

    if not read.is_sponsorship_inquiry or read.sender_role == SenderRole.not_a_deal:
        return Screen(False, "not a sponsorship inquiry", hits)

    cat = (read.product_category or "").lower()
    for blocked in policy.blocked_categories:
        if blocked.lower() in cat:
            return Screen(False, f"blocked category: {blocked}", hits)

    if read.sender_role == SenderRole.link_broker:
        return Screen(False, "link broker reselling to unnamed clients", hits)
    if read.sender_role == SenderRole.automated_blast or len(hits) >= 2:
        return Screen(False, "mass-sent template", hits + model_hints)
    if read.sender_role == SenderRole.agency and not read.brand_name:
        # Quote nothing until the agency names the client, because the client
        # decides whether the category is allowed at all.
        return Screen(False, "agency has not named the client", hits)
    return Screen(True, "qualified", hits + model_hints)


@dataclass
class Move:
    action: str            # quote | accept | concede | hold | escalate | decline
    price: float | None
    note: str


def opening_price(product: Product, wants_followed: bool | None, policy: Policy) -> float:
    extra = product.followed_link_surcharge if (wants_followed and policy.followed_links_allowed) else 0.0
    return product.target + extra


def floor_price(product: Product, wants_followed: bool | None, policy: Policy) -> float:
    extra = product.followed_link_surcharge if (wants_followed and policy.followed_links_allowed) else 0.0
    return product.floor + extra


def next_move(*, product: Product, current_quote: float, counter: float | None,
              concessions_used: int, wants_followed: bool | None, policy: Policy) -> Move:
    """Respond to a counteroffer. Pure function, fully unit-tested.

    The ladder is deliberately short: one concession of one step, never below
    the floor. In our own inbox the deals that closed closed at or near the
    first quote, and every extra round of haggling ended in silence.
    """
    floor = floor_price(product, wants_followed, policy)
    if counter is None:
        return Move("hold", current_quote, "no number on the table, restate the quote")
    if counter >= current_quote:
        return Move("accept", current_quote, "counter meets the quote")
    if counter >= floor and concessions_used >= policy.max_concessions:
        return Move("accept", counter, "counter is above the floor and concessions are spent")
    if concessions_used < policy.max_concessions:
        offer = max(floor, current_quote - product.step)
        if counter >= offer:
            return Move("accept", counter, "counter is within one step")
        return Move("concede", offer, f"one step down to {offer:.0f}, floor {floor:.0f}")
    if counter < floor * 0.5:
        return Move("decline", None, "counter is under half the floor")
    return Move("hold", current_quote, "concessions spent, holding at the last price")


def may_invoice(*, agreed_price: float | None, product: Product, wants_followed: bool | None,
                policy: Policy, billing_email: str | None) -> tuple[bool, str]:
    """The last gate before money is requested. Code, not prompt."""
    if agreed_price is None:
        return False, "no agreed price"
    floor = floor_price(product, wants_followed, policy)
    if agreed_price < floor:
        return False, f"agreed price {agreed_price:.2f} is below the floor {floor:.2f}"
    if not billing_email or "@" not in billing_email:
        return False, "no billing email"
    return True, "ok"
