"""Vercel entrypoint: the public demo.

Runs in replay mode: recorded model output serves the sample inbox and every
one-click reply for free, and anything new goes to the live model under a
small daily cap (DEALDESK_DAILY_LIVE_CALLS). PayPal and Qloo run in whatever
mode the environment configures, and the header says which.
"""
import os
from pathlib import Path

os.environ.setdefault("DEALDESK_CASSETTE", str(Path(__file__).parent / "demo" / "cassette.json"))
os.environ.setdefault("DEALDESK_DAILY_LIVE_CALLS", "40")

from dealdesk.app import create_app  # noqa: E402

app = create_app()
