"""Shared data shapes.

Two kinds of model live here. The Pydantic classes ending in `Read` are what
Claude returns through structured output, so every field is something the model
can observe in an email. Everything the business decides (prices, whether a
deal may be invoiced) lives in plain dataclasses filled by code, never by the
model.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class SenderRole(str, Enum):
    brand = "brand"                  # the company that owns the product writes directly
    agency = "agency"                # an agency writing for a named client
    link_broker = "link_broker"      # sells placements on to unnamed third parties
    automated_blast = "automated_blast"  # templated mail sent to many sites at once
    not_a_deal = "not_a_deal"        # newsletter, receipt, support mail, anything else
    unclear = "unclear"


class Deliverable(str, Enum):
    sponsored_post = "sponsored_post"
    link_insertion = "link_insertion"
    newsletter_slot = "newsletter_slot"
    dedicated_review = "dedicated_review"
    unknown = "unknown"


class Stage(str, Enum):
    new = "new"
    screened_out = "screened_out"   # blast, broker, blocked category
    quoted = "quoted"
    negotiating = "negotiating"
    agreed = "agreed"
    invoiced = "invoiced"
    paid = "paid"
    delivered = "delivered"
    declined = "declined"
    needs_human = "needs_human"


class InquiryRead(BaseModel):
    """What Claude reads off a first inbound email."""

    is_sponsorship_inquiry: bool = Field(description="True only if the sender wants paid or traded exposure on the creator's channel.")
    sender_role: SenderRole
    brand_name: Optional[str] = Field(description="The product or company being promoted, if named. Null when an agency or broker hides it.")
    brand_domain: Optional[str] = Field(description="Bare domain of the promoted product, e.g. 'example.com', if stated or obvious from the signature.")
    product_category: str = Field(description="Short category of the promoted product, e.g. 'video editing software', 'online casino', 'VPN'.")
    requested_deliverable: Deliverable
    budget_mentioned_usd: Optional[float] = Field(description="Any concrete price or budget the sender stated, in USD. Null if none.")
    wants_followed_link: Optional[bool] = Field(description="True if they ask for a dofollow / followed link, false if they accept nofollow or sponsored, null if unstated.")
    evidence_of_template: list[str] = Field(description="Exact short phrases suggesting mass-sent template mail. Empty if none.")
    summary: str = Field(description="One sentence describing what they want, in plain words.")


class ReplyRead(BaseModel):
    """What Claude reads off a counterparty's reply inside an open deal."""

    position: str = Field(description="One of: accepts, counters, asks_question, declines, unrelated.")
    counter_usd: Optional[float] = Field(description="The price they propose, in USD, when position is 'counters'. Null otherwise.")
    accepted_usd: Optional[float] = Field(description="The price they explicitly accept, in USD, when position is 'accepts'. Null otherwise.")
    billing_email: Optional[str] = Field(description="An email address they ask the invoice to go to, if they give one.")
    billing_name: Optional[str] = Field(description="Legal or company name for the invoice, if given.")
    question: Optional[str] = Field(description="Their question, when position is 'asks_question'.")
    summary: str


class DraftRead(BaseModel):
    subject: str
    body: str = Field(description="Plain-text email body. No markdown.")
