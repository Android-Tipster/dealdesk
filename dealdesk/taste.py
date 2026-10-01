"""Audience-to-brand taste fit through Qloo's Taste AI API.

A creator rarely knows who their audience is in the terms a brand buys on. They
know what their audience loves: the tools, films, creators and products that
come up in the comments. Qloo turns those into a cultural profile, which lets
Deal Desk answer two questions a sponsorship inbox otherwise answers by gut:

1. Inbound: does this brand actually fit my audience, and why?
2. Outbound: which brands that I have never heard from should I be pitching?

`QlooClient` calls the hackathon endpoint. `MockTaste` returns deterministic
fake data so the app runs without a key; the UI labels it as mock everywhere.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

HACKATHON_BASE = "https://hackathon.api.qloo.com"


@dataclass
class Entity:
    id: str
    name: str
    affinity: float = 0.0


@dataclass
class Fit:
    brand: str
    brand_id: str | None
    score: int                      # 0..100
    verdict: str                    # strong | plausible | weak | unknown
    shared_tags: list[str] = field(default_factory=list)
    brand_rank: int | None = None   # rank of the brand in the audience's brand affinities
    note: str = ""
    source: str = "qloo"


class TasteAPI(Protocol):
    mode: str
    def resolve(self, name: str, kind: str = "urn:entity:brand") -> Entity | None: ...
    def tags(self, seed_ids: list[str], take: int = 30) -> list[Entity]: ...
    def brands(self, seed_ids: list[str], take: int = 50) -> list[Entity]: ...


class QlooError(RuntimeError):
    pass


class QlooClient:
    mode = "qloo"

    def __init__(self, api_key: str, base: str = HACKATHON_BASE, http: httpx.Client | None = None):
        self.base = base.rstrip("/")
        self.http = http or httpx.Client(timeout=30, headers={"X-Api-Key": api_key, "Accept": "application/json"})

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        r = self.http.get(f"{self.base}{path}", params=params)
        if r.status_code >= 400:
            raise QlooError(f"GET {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    @staticmethod
    def _rows(j: dict[str, Any]) -> list[dict[str, Any]]:
        res = j.get("results", j)
        if isinstance(res, list):
            return res
        for key in ("entities", "tags", "results"):
            if isinstance(res.get(key), list):
                return res[key]
        return []

    @staticmethod
    def _entity(row: dict[str, Any]) -> Entity:
        q = row.get("query") or {}
        aff = q.get("affinity", row.get("affinity", row.get("popularity", 0.0))) or 0.0
        return Entity(id=row.get("entity_id") or row.get("tag_id") or row.get("id") or "",
                      name=row.get("name", ""), affinity=float(aff))

    def resolve(self, name, kind="urn:entity:brand"):
        rows = self._rows(self._get("/search", {"query": name, "types": kind, "take": 3}))
        if not rows and kind != "":
            rows = self._rows(self._get("/search", {"query": name, "take": 3}))
        return self._entity(rows[0]) if rows else None

    def tags(self, seed_ids, take=30):
        j = self._get("/v2/insights", {"filter.type": "urn:tag", "signal.interests.entities": ",".join(seed_ids), "take": take})
        return [self._entity(r) for r in self._rows(j)]

    def brands(self, seed_ids, take=50):
        j = self._get("/v2/insights", {"filter.type": "urn:entity:brand", "signal.interests.entities": ",".join(seed_ids), "take": take})
        return [self._entity(r) for r in self._rows(j)]


class MockTaste:
    """Deterministic stand-in. Same inputs, same outputs, clearly fake."""

    mode = "mock"
    _TAGS = ["video editing", "color grading", "indie film", "motion graphics", "filmmaking gear",
             "creator economy", "productivity apps", "photography", "gaming", "music production",
             "streetwear", "travel", "personal finance", "fitness", "cooking", "anime"]
    _BRANDS = ["Blackmagic Design", "Frame.io", "Epidemic Sound", "Artlist", "Descript", "Rode",
               "Elgato", "Notion", "Squarespace", "Skillshare", "Storyblocks", "Motion Array",
               "Envato", "Canva", "Audible", "NordVPN", "HelloFresh", "Raycast", "Wistia", "Loom"]

    @staticmethod
    def _h(s: str) -> int:
        return int(hashlib.sha256(s.lower().encode()).hexdigest(), 16)

    def resolve(self, name, kind="urn:entity:brand"):
        return Entity(id=f"mock-{self._h(name) % 10**8}", name=name)

    def _profile(self, key: str) -> list[Entity]:
        out = []
        for t in self._TAGS:
            a = (self._h(key + t) % 1000) / 1000
            out.append(Entity(id=f"tag-{t}", name=t, affinity=round(a, 3)))
        return sorted(out, key=lambda e: -e.affinity)

    def tags(self, seed_ids, take=30):
        return self._profile("|".join(sorted(seed_ids)))[:take]

    def brands(self, seed_ids, take=50):
        key = "|".join(sorted(seed_ids))
        rows = [Entity(id=f"mock-{self._h(b) % 10**8}", name=b, affinity=round((self._h(key + b) % 1000) / 1000, 3))
                for b in self._BRANDS]
        return sorted(rows, key=lambda e: -e.affinity)[:take]


def make_taste(cfg: dict[str, Any]) -> TasteAPI:
    key = (cfg.get("qloo") or {}).get("api_key")
    return QlooClient(key) if key else MockTaste()


# ---------------------------------------------------------------------------
# Scoring. Kept separate from the clients so it is tested on fixed numbers.
# ---------------------------------------------------------------------------

def weighted_overlap(a: list[Entity], b: list[Entity]) -> tuple[float, list[str]]:
    """Cosine similarity of two tag-affinity vectors, plus the shared tags
    ordered by joint weight. Returns (0..1, names)."""
    va = {e.name.lower(): e.affinity for e in a if e.name}
    vb = {e.name.lower(): e.affinity for e in b if e.name}
    common = set(va) & set(vb)
    if not common:
        return 0.0, []
    dot = sum(va[k] * vb[k] for k in common)
    na = math.sqrt(sum(v * v for v in va.values())) or 1.0
    nb = math.sqrt(sum(v * v for v in vb.values())) or 1.0
    shared = sorted(common, key=lambda k: -(va[k] * vb[k]))
    return dot / (na * nb), shared


def score_fit(*, brand_name: str, brand: Entity | None, audience_tags: list[Entity], brand_tags: list[Entity],
              audience_brands: list[Entity], source: str) -> Fit:
    if brand is None:
        return Fit(brand_name, None, 0, "unknown", note="brand not found in the taste graph", source=source)
    sim, shared = weighted_overlap(audience_tags, brand_tags)
    rank = next((i + 1 for i, e in enumerate(audience_brands)
                 if e.id == brand.id or e.name.lower() == brand.name.lower()), None)
    # Tag similarity carries the score; appearing among the audience's own
    # top brands is strong direct evidence and adds up to 30 points.
    rank_bonus = 0.0 if rank is None else 30.0 * (1 - (rank - 1) / max(len(audience_brands), 1))
    score = int(round(min(100.0, 70.0 * sim + rank_bonus)))
    verdict = "strong" if score >= 65 else "plausible" if score >= 40 else "weak"
    note = (f"ranks #{rank} among brands this audience over-indexes on" if rank
            else "not among the audience's top brands")
    return Fit(brand_name, brand.id, score, verdict, shared[:6], rank, note, source)


class TasteProfile:
    """Caches the audience side, which never changes between inquiries."""

    def __init__(self, api: TasteAPI, seeds: list[str]):
        self.api = api
        self.seeds = seeds
        self._seed_ids: list[str] | None = None
        self._tags: list[Entity] | None = None
        self._brands: list[Entity] | None = None

    def seed_ids(self) -> list[str]:
        if self._seed_ids is None:
            ids = []
            for s in self.seeds:
                e = self.api.resolve(s, "")
                if e and e.id:
                    ids.append(e.id)
            self._seed_ids = ids
        return self._seed_ids

    def audience_tags(self) -> list[Entity]:
        if self._tags is None:
            self._tags = self.api.tags(self.seed_ids()) if self.seed_ids() else []
        return self._tags

    def audience_brands(self) -> list[Entity]:
        if self._brands is None:
            self._brands = self.api.brands(self.seed_ids()) if self.seed_ids() else []
        return self._brands

    def fit(self, brand_name: str) -> Fit:
        brand = self.api.resolve(brand_name, "urn:entity:brand")
        brand_tags = self.api.tags([brand.id]) if brand else []
        return score_fit(brand_name=brand_name, brand=brand, audience_tags=self.audience_tags(),
                         brand_tags=brand_tags, audience_brands=self.audience_brands(), source=self.api.mode)

    def prospects(self, exclude: set[str], take: int = 10) -> list[Entity]:
        ex = {e.lower() for e in exclude}
        return [b for b in self.audience_brands() if b.name.lower() not in ex][:take]
