#!/usr/bin/env python3
"""Fast, gz-free regression guards for the simulation's pure logic.

The full mission harness (test_mission.py) needs PX4 + Gazebo + a GL context, so it only runs
on a sim-capable runner. These tests cover the deterministic logic the mission depends on —
world generation and the ENU<->NED frame conversion — with the standard library alone, so they
run in milliseconds anywhere (including CI).

Run:  python -m pytest simulation/tests/test_sim_logic.py
"""
import json
import math
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

SIMDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _gen(stations, sites, name, scene):
    """Run gen_world.py and return (parsed SDF root, sites dict); cleans up its outputs."""
    sdf = os.path.join(SIMDIR, f"gz/worlds/{name}.sdf")
    sj = os.path.join(SIMDIR, f"tests/{name}_sites.json")
    try:
        subprocess.run([sys.executable, "gen_world.py", str(stations), str(sites), name, "0", scene],
                       cwd=SIMDIR, check=True, capture_output=True, text=True)
        root = ET.parse(sdf).getroot()
        data = json.load(open(sj))
        return root, data
    finally:
        for f in (sdf, sj):
            if os.path.exists(f):
                os.remove(f)


def _models(root, prefix):
    """names of <include>d models whose <name> starts with prefix."""
    names = []
    for inc in root.iter("include"):
        n = inc.find("name")
        if n is not None and n.text and n.text.startswith(prefix):
            names.append(n.text)
    return names


def test_field_world_is_valid_and_counted():
    """The field scene must emit valid SDF with the requested station/site/package models."""
    root, data = _gen(6, 4, "_t_field", "field")
    assert len(data["stations"]) == 6
    assert len(data["sites"]) == 4
    # one pad + one tag per station, one model per site, exactly one parcel
    assert len(_models(root, "station_")) == 12          # 6 pads + 6 tags
    assert len(_models(root, "site_")) == 4
    assert len(_models(root, "airpost_package")) == 1


def test_stations_are_separated_and_headings_randomised():
    """Stations must keep the min separation and carry distinct random headings (only one tag in
    view at a time, and the precision-landing yaw alignment is meaningful)."""
    _, data = _gen(8, 4, "_t_sep", "field")
    st = data["stations"]
    for i, a in enumerate(st):
        assert 0.0 <= a["yaw_deg"] <= 360.0
        for b in st[i + 1:]:
            assert math.hypot(a["E"] - b["E"], a["N"] - b["N"]) >= 30.0 - 1e-6
    assert len({round(s["yaw_deg"]) for s in st}) > 1   # headings are not all identical


def test_baylands_places_pads_on_measured_terrain():
    """On the real baylands map the pads must sit at the lidar-measured clearing heights, not z=0,
    so they rest on the ground instead of floating or sinking."""
    clearings = os.path.join(SIMDIR, "baylands_clearings.json")
    if not os.path.exists(clearings):
        pytest.skip("baylands_clearings.json not present")
    _, data = _gen(20, 10, "_t_bay", "baylands")
    zs = [s["Z"] for s in data["stations"]]
    valid = {round(c["z"], 2) for c in json.load(open(clearings))["clearings"]}
    assert any(abs(z) > 1e-6 for z in zs)               # not all flattened to zero
    for z in zs:                                        # every pad height is a real measured clearing
        assert round(z, 2) in valid


def test_airpost_pad_has_collision_and_is_tag_sized():
    """The station pad must (a) have a collision box — baylands scenery has no terrain collision, so
    without it a landed drone falls through the world — and (b) stay ~2x the AprilTag side (tag is
    0.8 m, pad 1.6 m), NOT an oversized deck. Camera precision landing puts the drone on the tag
    centre to a few cm, so a tag-sized pad is the realistic, intended target (an enlarged pad would
    just mask landing error)."""
    root = ET.parse(os.path.join(SIMDIR, "gz/models/airpost_pad/model.sdf")).getroot()
    size = root.find(".//collision/geometry/box/size")
    assert size is not None, "pad must have a collision box so drones don't fall through baylands"
    w, d, h = (float(x) for x in size.text.split())
    assert (w, d) == (1.6, 1.6), f"pad should be 1.6 m (~2x the 0.8 m tag), got {(w, d)}"
    assert 0.2 <= h <= 0.5


def test_winch_state_machine_rearms_between_sorties():
    """The winch manager must not get stuck after one delivery: a second sortie has to lower the
    parcel too. This is the regression guard for the back-to-back-sortie fix."""
    from parcel_manager import next_state
    # one full sortie
    assert next_state("transport", go_exists=False, done_exists=False, reached_ground=False) == "transport"
    assert next_state("transport", go_exists=True, done_exists=False, reached_ground=False) == "lower"
    assert next_state("lower", go_exists=True, done_exists=False, reached_ground=False) == "lower"
    assert next_state("lower", go_exists=True, done_exists=False, reached_ground=True) == "done"
    # parked while the agent still holds the done flag, then RE-ARMS once it is cleared
    assert next_state("done", go_exists=False, done_exists=True, reached_ground=False) == "done"
    assert next_state("done", go_exists=False, done_exists=False, reached_ground=False) == "transport"


def test_land_target_wire_format_roundtrip():
    """The flight agent hands the detector each sortie's landing station via a '<N> <E> <id> <marker_z>'
    line (marker_z = the tag's world height, for height-above-marker). Guard that the writer's format
    and the reader's parse stay in sync, and that the old 3-field form still parses (marker_z=0)."""
    N, E, station_id, marker_z = -12.5, 34.0, 7, -0.79
    line = "{} {} {} {}".format(N, E, station_id, marker_z)  # airpost_flight_agent.fly writes this
    parts = line.split()                                     # apriltag_detector.refresh_target parses this
    mz = float(parts[3]) if len(parts) > 3 else 0.0
    assert (float(parts[0]), float(parts[1]), int(parts[2]), mz) == (N, E, station_id, marker_z)
    legacy = "1.0 2.0 3".split()                             # 3-field (no marker_z) still valid
    assert (float(legacy[3]) if len(legacy) > 3 else 0.0) == 0.0


@pytest.mark.parametrize("enu_deg", [0.0, 45.0, 90.0, 137.0, 180.0, 270.0, 359.0])
def test_enu_to_ned_yaw_conversion(enu_deg):
    """The drone aligns to the tag with NED_yaw = 90 - ENU_yaw (flight agent), and the detector
    derives the same heading with atan2(cos,sin). Both must agree (a sign flip here makes the
    drone land rotated)."""
    agent_ned = (90.0 - enu_deg) % 360.0
    enu_rad = math.radians(enu_deg)
    detector_ned = math.degrees(math.atan2(math.cos(enu_rad), math.sin(enu_rad))) % 360.0
    diff = abs((agent_ned - detector_ned + 180.0) % 360.0 - 180.0)
    assert diff < 1e-6
