#!/usr/bin/env python3
"""Ground-truth acceptance check for the AirPost fleet.

Reads gz GROUND-TRUTH poses (not the drone's EKF — which can be biased) of every drone and every
delivered parcel, and reports, mapping-free:
  - each landed drone's horizontal distance to the NEAREST station AprilTag centre, and
  - each delivered parcel's horizontal distance to the NEAREST drop-pad (red box) centre + its
    height above that box top.
A precise landing is ~<=0.15 m to a tag; a parcel resting ON the box is ~<=0.10 m horizontally and
sits ~+0.045 m above the box top (parcel half-height).

Run:  PYTHONPATH=<gz-transport>:<gz-msgs> .venv-detector/bin/python tests/verify_truth.py
Env:  WORLD (default airpost)
"""
import json
import math
import os
import sys
import time

from gz.transport13 import Node
from gz.msgs10.pose_v_pb2 import Pose_V

SIMDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORLD = os.environ.get("WORLD", "airpost")
DROP_PAD_H = 0.2  # red box top above its ground (matches gen_world airpost_drop_pad)


def main():
    sites = json.load(open(os.path.join(SIMDIR, "tests", f"{WORLD}_sites.json")))
    # tag centres = station pad centres (E,N); box centres = site (E,N); box top z = site Z + DROP_PAD_H
    tags = [(s["E"], s["N"]) for s in sites["stations"]]
    boxes = [(s["E"], s["N"], s.get("Z", 0.0) + DROP_PAD_H) for s in sites["sites"]]

    drones, parcels = {}, {}

    def on_pose(m):
        for p in m.pose:
            n = p.name
            if n.startswith("airpost_delivery_drone_"):
                drones[n] = (p.position.x, p.position.y, p.position.z)   # x=E, y=N, z=up
            elif n.startswith("airpost_package_"):
                parcels[n] = (p.position.x, p.position.y, p.position.z)

    node = Node()
    node.subscribe(Pose_V, f"/world/{WORLD}/dynamic_pose/info", on_pose)
    node.subscribe(Pose_V, f"/world/{WORLD}/pose/info", on_pose)
    t0 = time.time()
    while time.time() - t0 < 6:
        time.sleep(0.2)

    if not drones and not parcels:
        print("NO POSES — is the sim running?")
        return 1

    print("=== DRONES vs nearest tag (ground truth) ===")
    land_ok = 0
    for name in sorted(drones):
        e, n, z = drones[name]
        d = min(math.hypot(e - te, n - tn) for te, tn in tags)
        ok = d <= 0.15 and abs(z) < 50  # reject physics-blowup z
        land_ok += ok
        print(f"  {name}: nearest tag {d:.2f} m  z={z:+.2f}  {'OK' if ok else 'OFF'}")

    print("=== PARCELS vs nearest drop box (ground truth) ===")
    drop_ok = 0
    for name in sorted(parcels):
        e, n, z = parcels[name]
        best = min(boxes, key=lambda b: math.hypot(e - b[0], n - b[1]))
        dxy = math.hypot(e - best[0], n - best[1])
        dz = z - best[2]            # +ve = resting on/above box top
        ok = dxy <= 0.12 and -0.02 <= dz <= 0.12
        drop_ok += ok
        print(f"  {name}: nearest box {dxy:.2f} m  height-above-box-top {dz:+.2f} m  {'ON BOX' if ok else 'OFF'}")

    print(f"=== landings on-tag: {land_ok}/{len(drones)} | parcels on-box: {drop_ok}/{len(parcels)} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
