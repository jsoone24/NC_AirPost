#!/usr/bin/env python3
"""Headless pytest harness for the full AirPost mission.

Shells out to run_one_delivery.sh (which launches PX4 SITL + Gazebo, the AprilTag detector, and
the parcel/winch-cable manager, then flies the full sortie) and asserts the mission's own metrics:
  - the drone spawns LEVEL  (|roll|, |pitch| < SPAWN_LEVEL_DEG)
  - RESULT=PASS
  - DELIVER_ERR < MAX_DELIVER_ERR_M
  - LAND_ERR    < MAX_LAND_ERR_M
  - the whole sortie completes under MISSION_TIMEOUT_S

CI / Linux note: the detector reads a Gazebo camera, so the sim needs a GL context. On a headless
Linux runner export the software rasteriser before running:
    export LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=llvmpipe   # mesa llvmpipe
This file only shells out to the existing scripts, so it runs anywhere they do.

Run:  PX4-Autopilot/.venv/bin/python -m pytest simulation/tests/test_mission.py -s
"""
import os
import re
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
DELIVERY_SCRIPT = os.path.join(HERE, "run_one_delivery.sh")

# one full sortie: takeoff station -> deliver near it -> land at a different station
TAKEOFF_ID = int(os.environ.get("TEST_TAKEOFF_ID", "9"))
LANDING_ID = int(os.environ.get("TEST_LANDING_ID", "7"))
CRUISE_M = int(os.environ.get("TEST_CRUISE_M", "30"))

SPAWN_LEVEL_DEG = 5.0      # spawn must be within this of level (roll/pitch)
MAX_DELIVER_ERR_M = 2.0    # parcel inside the 2 m delivery circle
MAX_LAND_ERR_M = 0.30      # vision precision-landing accuracy
MISSION_TIMEOUT_S = 600    # one sortie incl. sim boot; full_mission has its own inner guard


def _station(station_id):
    """World (N, E) of a station from airpost_sites.json."""
    import json
    sites = json.load(open(os.path.join(HERE, "airpost_sites.json")))
    station = {s["id"]: s for s in sites["stations"]}[station_id]
    return station["N"], station["E"]


def _run_sortie():
    """Run one full delivery sortie and return its captured stdout."""
    take_n, take_e = _station(TAKEOFF_ID)
    deliver_n, deliver_e = take_n + 22, take_e + 15   # a point well inside the field, near takeoff
    cmd = ["bash", DELIVERY_SCRIPT, str(TAKEOFF_ID),
           str(deliver_n), str(deliver_e), str(LANDING_ID), str(CRUISE_M)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=MISSION_TIMEOUT_S)
    return proc.stdout + proc.stderr


def _find_float(pattern, text):
    """First capture group of `pattern` in `text` as a float, or None."""
    m = re.search(pattern, text)
    return float(m.group(1)) if m else None


@pytest.fixture(scope="module")
def mission_output():
    return _run_sortie()


def test_spawn_is_level(mission_output):
    m = re.search(r"SPAWN_RPY=([-\d.]+),([-\d.]+),", mission_output)
    assert m, f"no SPAWN_RPY line in output:\n{mission_output}"
    roll, pitch = abs(float(m.group(1))), abs(float(m.group(2)))
    assert roll < SPAWN_LEVEL_DEG and pitch < SPAWN_LEVEL_DEG, \
        f"drone did not spawn level: roll={roll:.2f} pitch={pitch:.2f} deg"


def test_mission_passes(mission_output):
    assert re.search(r"RESULT=PASS", mission_output), \
        f"mission did not PASS:\n{mission_output}"


def test_delivery_accuracy(mission_output):
    deliver_err = _find_float(r"DELIVER_ERR=([\d.]+)", mission_output)
    assert deliver_err is not None, f"no DELIVER_ERR in output:\n{mission_output}"
    assert deliver_err < MAX_DELIVER_ERR_M, \
        f"delivery error {deliver_err:.2f} m exceeds {MAX_DELIVER_ERR_M} m"


def test_landing_accuracy(mission_output):
    land_err = _find_float(r"LAND_ERR=([\d.]+)", mission_output)
    assert land_err is not None, f"no LAND_ERR in output:\n{mission_output}"
    assert land_err < MAX_LAND_ERR_M, \
        f"landing error {land_err:.2f} m exceeds {MAX_LAND_ERR_M} m"


if __name__ == "__main__":
    # allow running without pytest: print metrics and a verdict
    out = _run_sortie()
    print(out)
    for line in out.splitlines():
        if any(k in line for k in ("SPAWN_RPY", "DELIVER_ERR", "LAND_ERR", "RESULT")):
            print("METRIC:", line.strip())
