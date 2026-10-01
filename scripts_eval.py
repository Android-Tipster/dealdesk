"""Live consistency check: each sample N times through a fresh desk with real Claude.

    python scripts_eval.py [N]

Mock PayPal and mock taste data; only the model is live. Prints per-sample
agreement with the expected outcome and writes eval-results.json.
"""
import json, sys, tempfile, time
from pathlib import Path
from dealdesk.app import build_desk

EXPECT = {  # sample file -> (stage, deliverable, quote)
    "01-direct-brand.json": ("quoted", "sponsored_post", 450),
    "02-newsletter-slot.json": ("quoted", "newsletter_slot", 300),
    "03-guest-post-blast.json": ("screened_out", None, None),
    "04-casino.json": ("screened_out", None, None),
    "05-agency-no-client.json": ("needs_human", None, None),
    "06-review-followed.json": ("quoted", "dedicated_review", 800),
}
N = int(sys.argv[1]) if len(sys.argv) > 1 else 3
rows, t0 = [], time.time()
for run in range(N):
    desk = build_desk(db=str(Path(tempfile.mkdtemp()) / "e.db"))
    for f, (stage, deliv, quote) in EXPECT.items():
        s = json.loads((Path("samples") / f).read_text(encoding="utf-8"))
        d = desk.ingest(s["from"], s["subject"], s["body"])
        ok = d.stage == stage and (deliv is None or d.deliverable == deliv) and (quote is None or d.quote == quote)
        ok = ok and not d.draft_problems
        rows.append({"run": run, "sample": f, "ok": ok, "stage": d.stage, "deliverable": d.deliverable,
                     "quote": d.quote, "reason": d.screen_reason, "draft_problems": d.draft_problems,
                     "retries": sum("rejected by the price guard" in e["text"] for e in d.events)})
        print(("PASS" if ok else "FAIL"), run, f, d.stage, d.deliverable, d.quote, d.screen_reason, flush=True)
passed = sum(r["ok"] for r in rows)
summary = {"passed": passed, "total": len(rows), "runs": N, "seconds": round(time.time() - t0),
           "guard_retries": sum(r["retries"] for r in rows), "rows": rows}
Path("eval-results.json").write_text(json.dumps(summary, indent=1))
print(f"{passed}/{len(rows)} correct, {summary['guard_retries']} guard retries, {summary['seconds']}s")
