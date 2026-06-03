#!/usr/bin/env python3
"""Continuously submit delivery orders to the AirPost backend — a rolling demo load.

Instead of one batch, this keeps placing orders at a fixed interval so the fleet runs continuously:
each tick it picks a source station that currently has an idle drone and a destination tag, and POSTs
/regist/delivery. Stations with no free drone are simply skipped (the dispatcher would reject them),
so the load self-paces to the fleet's capacity.

Run:  PX4-Autopilot/.venv/bin/python demo_orders.py [interval_sec]
Env:  API=http://localhost:8081  ADMIN=admin@airpost.local  PASS=admin  STATIONS=8  TAGS=8
"""
import os
import sys
import time
import json
import urllib.request

API = os.environ.get("API", "http://localhost:8081")
ADMIN = os.environ.get("ADMIN", "admin@airpost.local")
PASSWORD = os.environ.get("PASS", "admin")
N_STATIONS = int(os.environ.get("STATIONS", "8"))
N_TAGS = int(os.environ.get("TAGS", "8"))
TAG_ID_BASE = 30  # tag i -> node id 30+i (seed.go)


def _post(path, body, token=None):
    data = json.dumps(body).encode()
    req = urllib.request.Request(API + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def login():
    return _post("/auth/login", {"email": ADMIN, "password": PASSWORD})["token"]


def main(interval):
    token = login()
    print(f"continuous demo: an order every {interval}s (Ctrl-C to stop)", flush=True)
    tick = 0
    while True:
        # Round-robin source station; deliver to the next station's drop tag (criss-cross routes).
        src = (tick % N_STATIONS) + 1
        tag = TAG_ID_BASE + ((tick + 1) % N_TAGS) + 1
        try:
            r = _post("/regist/delivery", {
                "src_station_id": src, "dest_tag_id": tag,
                "email": f"recipient{src}@airpost.local",
                "src_name": f"Sender-{src}", "dest_name": f"Recipient-{src}",
            }, token)
            print(f"  station {src} -> tag {tag}: order {r.get('order_num','?')[-6:]} drone {r.get('drone_id')}", flush=True)
        except urllib.error.HTTPError as e:
            # No idle drone at the source (dispatcher rejected) — skip this tick.
            print(f"  station {src} -> tag {tag}: skipped ({e.code})", flush=True)
        except Exception as e:
            print(f"  order error: {e!r}", flush=True)
        tick += 1
        time.sleep(interval)


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 20.0)
