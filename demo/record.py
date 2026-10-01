"""Record the demo video end to end: real app, real model calls, narrated.

    python demo/record.py [--tts edge|eleven] [--out demo/dealdesk-demo.mp4]

Starts the app on a fresh database, drives the UI in headless Chromium with
video recording on, one clip per scene, narrates each scene with TTS, then
joins clips and audio with ffmpeg. Whatever PayPal and Qloo modes the config
selects are what the video shows, and the header badges say which.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from dealdesk.app import build_desk, create_app, load_config  # noqa: E402

PORT = 8050
BASE = f"http://127.0.0.1:{PORT}"
W, H = 1440, 900


def ffmpeg() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return shutil.which("ffmpeg") or "ffmpeg"


def duration(path: Path) -> float:
    out = subprocess.run([ffmpeg(), "-i", str(path)], capture_output=True, text=True).stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0


def tts(text: str, out: Path, engine: str) -> None:
    if engine == "eleven":
        import requests
        key = os.environ["ELEVENLABS_API_KEY"]
        voice = os.environ.get("ELEVENLABS_VOICE", "JBFqnCBsd6RMkjVDRZzb")
        r = requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
                          headers={"xi-api-key": key, "Accept": "audio/mpeg"},
                          json={"text": text, "model_id": "eleven_multilingual_v2",
                                "voice_settings": {"stability": 0.45, "similarity_boost": 0.8}}, timeout=120)
        r.raise_for_status()
        out.write_bytes(r.content)
    else:
        import edge_tts
        asyncio.run(edge_tts.Communicate(text, "en-US-AndrewNeural", rate="+4%").save(str(out)))


def start_server(db: str) -> None:
    cfg = load_config()
    app = create_app(build_desk(cfg, db=db), cfg)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    import httpx
    for _ in range(50):
        try:
            if httpx.get(f"{BASE}/api/status", timeout=1).status_code == 200:
                return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("server did not start")


def api(path: str, body: dict | None = None):
    import httpx
    r = httpx.post(f"{BASE}{path}", json=body, timeout=180) if body is not None or path.startswith("/api/samples") \
        else httpx.get(f"{BASE}{path}", timeout=60)
    r.raise_for_status()
    return r.json()


def deal_id(brand: str) -> int:
    return next(d["id"] for d in api("/api/deals") if (d.get("brand") or "") == brand)


# ---------------------------------------------------------------------------
# Scenes. Each has narration and an action(page) run while recording. Slow
# model work that is not the point of a scene runs in `prep`, before its clip
# starts, so the video does not show spinners.
# ---------------------------------------------------------------------------

def s_intro(page):
    page.goto(BASE)
    page.wait_for_timeout(1500)


def p_load(_):
    api("/api/samples", {})   # six live model reads, done before the clip so it shows results not a spinner


def s_load(page):
    page.goto(BASE)
    page.wait_for_function("document.querySelectorAll('.ag-center-cols-container .ag-row').length >= 6", timeout=60000)
    page.wait_for_timeout(1000)
    for row in page.locator(".ag-center-cols-container .ag-row").all()[:6]:
        row.hover()
        page.wait_for_timeout(450)


def s_blast(page):
    page.goto(BASE)
    page.wait_for_timeout(800)
    page.locator(".ag-row", has_text="Screened out").first.click()
    page.wait_for_timeout(2500)
    page.locator("#drawer .x").click()
    page.locator(".ag-row", has_text="Needs you").first.click()
    page.wait_for_timeout(2500)


def s_quote(page):
    page.goto(BASE)
    page.wait_for_timeout(800)
    page.locator(".ag-row", has_text="Cutline").first.click()
    page.wait_for_timeout(1500)
    page.locator("#drawer").evaluate("e => e.scrollTo({top: 260, behavior: 'smooth'})")
    page.wait_for_timeout(2500)


def p_counter(_):
    api(f"/api/deals/{deal_id('Cutline')}/approve", {})


def s_counter(page):
    page.goto(BASE)
    page.wait_for_timeout(600)
    page.locator(".ag-row", has_text="Cutline").first.click()
    page.wait_for_timeout(700)
    box = page.locator("#reply")
    box.scroll_into_view_if_needed()
    box.press_sequentially("Thanks Maya, love the idea. Budget is tight this quarter, could you do $300?", delay=25)
    page.get_by_role("button", name="Run the agent on this reply").click()
    page.wait_for_function("document.querySelector('#drawer .pill').innerText.includes('Negotiating')", timeout=180000)
    page.locator("#drawer").evaluate("e => e.scrollTo({top: 0})")
    page.wait_for_timeout(1500)
    page.locator("#drawer .draft").scroll_into_view_if_needed()
    page.wait_for_timeout(2500)


def p_accept(_):
    api(f"/api/deals/{deal_id('Cutline')}/approve", {})


def s_accept(page):
    page.goto(BASE)
    page.wait_for_timeout(600)
    page.locator(".ag-row", has_text="Cutline").first.click()
    page.wait_for_timeout(700)
    box = page.locator("#reply")
    box.scroll_into_view_if_needed()
    box.press_sequentially("Deal at $400. Please invoice accounts@cutline.example, billed to Cutline Labs Inc.", delay=25)
    page.get_by_role("button", name="Run the agent on this reply").click()
    page.wait_for_function("document.querySelector('#drawer .pill').innerText.includes('Invoiced')", timeout=180000)
    page.locator("#drawer").evaluate("e => e.scrollTo({top: 0})")
    page.wait_for_timeout(1200)
    page.get_by_text("Open PayPal invoice").scroll_into_view_if_needed()
    page.wait_for_timeout(2500)


def s_pay(page):
    d = next(x for x in api("/api/deals") if x.get("brand") == "Cutline")
    url = d["payer_url"] if d["payer_url"].startswith("http") else BASE + d["payer_url"]
    page.goto(url)
    page.wait_for_timeout(2500)
    btn = page.get_by_role("button", name=re.compile("Pay", re.I))
    if btn.count():
        btn.first.click()
        page.wait_for_timeout(1500)
    page.goto(BASE)
    page.wait_for_timeout(800)
    page.get_by_role("button", name="Check payments").click()
    page.wait_for_timeout(1500)
    page.locator(".ag-row", has_text="Cutline").first.click()
    page.wait_for_timeout(1000)
    page.locator("#drawer .tl").scroll_into_view_if_needed()
    page.wait_for_timeout(3500)


def s_pitch(page):
    page.goto(BASE)
    page.wait_for_timeout(1000)
    page.locator(".plink").first.click()
    page.wait_for_selector("#pitchout .draft", timeout=180000)
    page.locator("#pitchout").evaluate("e => e.scrollIntoView({block: 'center', behavior: 'smooth'})")
    page.wait_for_timeout(3500)


def s_close(page):
    page.goto(BASE)
    page.wait_for_timeout(2000)


SCENES = [
    ("intro", None, s_intro,
     "Independent creators get sponsorship email every week. We ran one of those inboxes for four months: "
     "two thousand nine hundred emails, ninety four sponsor threads, seven paid deals. The money was real. "
     "So was the noise around it. Deal Desk is an agent that runs that inbox, from the first email to a paid PayPal invoice."),
    ("load", p_load, s_load,
     "Here is a sample inbox of six emails. Claude reads each one: who is writing, what they want, and what they offered. "
     "Then plain code decides what happens next."),
    ("screen", None, s_blast,
     "A Dear Webmaster template and a casino are screened out without a reply. An agency that will not name its client "
     "gets a question, not a price, because the client decides whether the deal is allowed at all."),
    ("quote", None, s_quote,
     "A real brand gets a quote from the creator's rate card, and a taste fit score from Qloo: how closely the brand's "
     "customers match what this audience loves. Every draft is checked in code, and any dollar amount the policy did not "
     "authorise blocks it."),
    ("counter", p_counter, s_counter,
     "The brand counters at three hundred. Claude reads the number. The policy engine, not the model, picks the move: "
     "one concession of one step, never below the floor."),
    ("accept", p_accept, s_accept,
     "They accept in writing. The agent checks the floor and the billing address, then creates and sends a PayPal invoice "
     "through the Invoicing API, with a number and a line item a finance team can file. In our own inbox, every deal "
     "billed with a real invoice was paid."),
    ("pay", None, s_pay,
     "When PayPal reports the invoice paid, by webhook or by polling, the agent does not take the notification's word. "
     "It reads the invoice back from PayPal first. A forged paid event changes nothing. Then it drafts the receipt and "
     "the delivery checklist."),
    ("pitch", None, s_pitch,
     "It also works outbound. From the audience's taste profile, Qloo surfaces brands this audience over-indexes on "
     "that have never written in, and the agent drafts a pitch built on the measured overlap."),
    ("close", None, s_close,
     "Twenty six tests cover the rails. Across three live runs, the agent handled all eighteen sample emails correctly. "
     "Deal Desk: the AI reads and writes, the code decides, and PayPal gets you paid."),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tts", default="edge", choices=["edge", "eleven"])
    ap.add_argument("--out", default=str(ROOT / "demo" / "dealdesk-demo.mp4"))
    ap.add_argument("--only", default="")
    a = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="dd-demo-"))
    start_server(str(work / "demo.db"))
    print("server up, mode:", api("/api/status")["paypal"], api("/api/status")["taste"])

    # All narration first: edge-tts needs its own event loop, which cannot run
    # inside Playwright's sync API.
    durs = {}
    for name, _, _, text in SCENES:
        if a.only and name not in a.only.split(","):
            continue
        tts(text, work / f"{name}.mp3", a.tts)
        durs[name] = duration(work / f"{name}.mp3")
    print("narration total", round(sum(durs.values()), 1), "s")

    parts = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, prep, action, text in SCENES:
            if a.only and name not in a.only.split(","):
                continue
            audio = work / f"{name}.mp3"
            dur = durs[name]
            if prep:
                prep(None)
            ctx = browser.new_context(viewport={"width": W, "height": H}, record_video_dir=str(work / name),
                                      record_video_size={"width": W, "height": H}, device_scale_factor=1)
            page = ctx.new_page()
            t0 = time.time()
            action(page)
            spent = time.time() - t0
            if spent < dur + 0.6:
                page.wait_for_timeout(int((dur + 0.6 - spent) * 1000))
            vid = page.video.path()
            ctx.close()
            clip = work / f"{name}.mp4"
            total = max(dur + 0.6, time.time() - t0)
            # Trim the blank first frames Playwright records before the first paint.
            subprocess.run([ffmpeg(), "-y", "-loglevel", "error", "-ss", "0.4", "-i", str(vid), "-i", str(audio),
                            "-filter_complex", f"[1:a]apad=whole_dur={total:.2f}[a]", "-map", "0:v", "-map", "[a]",
                            "-t", f"{total:.2f}", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
                            "-c:a", "aac", "-b:a", "160k", str(clip)], check=True)
            parts.append(clip)
            print(f"scene {name}: narration {dur:.1f}s, clip {total:.1f}s")
        browser.close()

    lst = work / "list.txt"
    lst.write_text("".join(f"file '{c.as_posix()}'\n" for c in parts))
    subprocess.run([ffmpeg(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-c", "copy", a.out], check=True)
    print("wrote", a.out, f"{duration(Path(a.out)):.1f}s")


if __name__ == "__main__":
    main()
