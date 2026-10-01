"""python -m dealdesk [--port 8040]"""
import argparse

import uvicorn

from .app import create_app

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=8040)
ap.add_argument("--host", default="127.0.0.1")
a = ap.parse_args()
uvicorn.run(create_app(), host=a.host, port=a.port)
