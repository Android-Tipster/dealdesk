"""The three places Claude is used, each a single structured-output call.

Claude reads (an inquiry, a reply) and writes (a draft). It never chooses a
price: the draft prompt receives the number the policy engine already decided
and is told to use it verbatim, and `check_draft` rejects any draft that quotes
a different amount.
"""
from __future__ import annotations

import re
from typing import Protocol

import anthropic

from .models import DraftRead, InquiryRead, ReplyRead

MODEL = "claude-opus-5-5"

READ_INQUIRY = """You screen sponsorship email for an independent creator.
Read the email and report only what it shows. Do not guess a brand that is not named.
Treat an agency as `agency` only when it says it represents a client; a sender that
offers to buy placements for many unnamed clients is a `link_broker`. Mail that greets
no one by name, offers a fixed menu of guest-post prices, or could have been sent to
any website unchanged is an `automated_blast`."""

READ_REPLY = """You read a reply inside an open sponsorship negotiation and report the
counterparty's position. Only report a price if they state one. 'accepts' means they
agree to a specific price in writing; enthusiasm without a price is not acceptance."""

DRAFT = """You write short emails for an independent creator who sells sponsorships.
Voice: warm, plain, brief, like a person running a small business. No marketing
language, no em dashes or en dashes, no exclamation marks, at most 120 words.
Use exactly the price you are given and no other amount. Never invent audience
statistics; use only the facts provided. Sign with the signature provided."""


class LLM(Protocol):
    def read_inquiry(self, email: str) -> InquiryRead: ...
    def read_reply(self, email: str, context: str) -> ReplyRead: ...
    def draft(self, instruction: str, facts: str) -> DraftRead: ...


class ClaudeLLM:
    def __init__(self, api_key: str | None = None, model: str = MODEL):
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.model = model

    def _parse(self, system: str, user: str, schema, effort: str):
        resp = self.client.messages.parse(
            model=self.model,
            max_tokens=4000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=schema,
            output_config={"effort": effort},
        )
        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            raise RuntimeError(f"model returned no structured output (stop_reason={resp.stop_reason})")
        return resp.parsed_output

    def read_inquiry(self, email):
        return self._parse(READ_INQUIRY, f"<email>\n{email}\n</email>", InquiryRead, "low")

    def read_reply(self, email, context):
        return self._parse(READ_REPLY, f"<deal>\n{context}\n</deal>\n<reply>\n{email}\n</reply>", ReplyRead, "low")

    def draft(self, instruction, facts):
        return self._parse(DRAFT, f"<task>\n{instruction}\n</task>\n<facts>\n{facts}\n</facts>", DraftRead, "medium")


_MONEY = re.compile(r"(?:US)?\$\s?(\d[\d,]*(?:\.\d{1,2})?)|(\d[\d,]*(?:\.\d{1,2})?)\s?(?:USD|dollars)", re.I)
_DASHES = str.maketrans({"—": ",", "–": "-"})


def check_draft(d: DraftRead, allowed_amounts: set[float]) -> list[str]:
    """Return problems with a draft. Empty list means it may be shown for approval."""
    problems = []
    for m in _MONEY.finditer(d.body + " " + d.subject):
        val = float((m.group(1) or m.group(2)).replace(",", ""))
        if not any(abs(val - a) < 0.01 for a in allowed_amounts):
            problems.append(f"draft mentions ${val:,.2f}, which the policy did not authorise")
    if "—" in d.body or "–" in d.body:
        problems.append("draft contains an em or en dash")
    if len(d.body.split()) > 160:
        problems.append("draft is longer than 160 words")
    return problems


def clean(d: DraftRead) -> DraftRead:
    return DraftRead(subject=d.subject.translate(_DASHES), body=d.body.translate(_DASHES))


class BudgetExceeded(RuntimeError):
    pass


class ReplayLLM:
    """Serve recorded model outputs for known inputs; call the live model for new
    ones, up to a daily cap.

    Used by the public demo so that the sample flows cost nothing and a stranger
    pasting email cannot run up an unbounded bill. With `record=True` every live
    result is written back to the cassette file.
    """

    def __init__(self, inner: "LLM | None", path: str, daily_cap: int = 60, record: bool = False):
        import json
        import os
        self.inner, self.path, self.cap, self.record = inner, path, daily_cap, record
        self.model = getattr(inner, "model", "replay")
        self.cache: dict[str, dict] = json.loads(open(path, encoding="utf-8").read()) if os.path.exists(path) else {}
        self._day, self._used = "", 0

    @staticmethod
    def _key(kind: str, *parts: str) -> str:
        import hashlib
        return kind + ":" + hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]

    def _live(self, kind, schema, key, call):
        import datetime
        import json
        if key in self.cache:
            return schema.model_validate(self.cache[key])
        today = datetime.date.today().isoformat()
        if today != self._day:
            self._day, self._used = today, 0
        if self.inner is None or self._used >= self.cap:
            raise BudgetExceeded("The public demo has used today's live model budget. "
                                 "The sample inbox still works; run locally for unlimited use.")
        self._used += 1
        out = call()
        self.cache[key] = out.model_dump(mode="json")
        if self.record:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.cache, f, indent=1, sort_keys=True)
        return out

    def read_inquiry(self, email):
        return self._live("inquiry", InquiryRead, self._key("inquiry", email), lambda: self.inner.read_inquiry(email))

    def read_reply(self, email, context):
        return self._live("reply", ReplyRead, self._key("reply", email, context), lambda: self.inner.read_reply(email, context))

    def draft(self, instruction, facts):
        return self._live("draft", DraftRead, self._key("draft", instruction, facts), lambda: self.inner.draft(instruction, facts))
