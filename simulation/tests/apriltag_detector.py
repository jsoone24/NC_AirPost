#!/usr/bin/env python3
"""AprilTag/ArUco precision-landing detector (sim stand-in for Jetson Nano + Intel T265).

Subscribes to the drone's downward gz camera, detects the ArUco marker (DICT_4X4_50 id 0),
estimates the marker pose with solvePnP (real vision), transforms it to absolute local-NED
using the drone's pose from MAVLink, and streams MAVLINK LANDING_TARGET (position_valid,
MAV_FRAME_LOCAL_NED) to PX4 — which publishes landing_target_pose for PrecLand.
See https://mavlink.io/en/services/landing_target.html and PX4 precland docs.

Run with the detector venv (python 3.14) and the gz python path on PYTHONPATH:
    GZP=$(ls -d /opt/homebrew/Cellar/gz-transport13/*/lib/python3.14/site-packages)
    GZM=$(ls -d /opt/homebrew/Cellar/gz-msgs10/*/lib/python3.14/site-packages)
    PYTHONPATH=$GZP:$GZM simulation/.venv-detector/bin/python simulation/tests/apriltag_detector.py [--calib]
"""
import math
import os
import sys
import time
import threading

import numpy as np
import cv2
os.environ["MAVLINK20"] = "1"   # need MAVLink2 for the extended (NED) LANDING_TARGET fields
from pymavlink import mavutil
from gz.transport13 import Node
from gz.msgs10.image_pb2 import Image
from gz.msgs10.pose_v_pb2 import Pose_V

CALIB = "--calib" in sys.argv
WORLD = os.environ.get("PX4_GZ_WORLD", "airpost")
MODEL = os.environ.get("AIRPOST_MODEL", "airpost_delivery_drone_0")
CAM_TOPIC = f"/world/{WORLD}/model/{MODEL}/link/camera_link/sensor/imager/image"
POSE_TOPIC = f"/world/{WORLD}/dynamic_pose/info"
# send LANDING_TARGET to PX4's GCS-link receive port (no bind -> no port conflicts)
MAV_URL = os.environ.get("MAV_URL", "udpout:127.0.0.1:18570")
MARKER_LEN = 0.50           # marker side length [m] (airpost_landing_marker)
ARUCO_DICT = cv2.aruco.DICT_4X4_50
# PX4 local-NED origin = takeoff (home) world position. gz gives world coords; subtract HOME
# so LANDING_TARGET is in PX4 local NED. (HOME = takeoff station world N,E; 0,0 if spawn at origin.)
HOME_N = float(os.environ.get("HOME_N", "0")); HOME_E = float(os.environ.get("HOME_E", "0"))
# All pads share ArUco id 0, so when several are in view we must disambiguate by proximity to
# the KNOWN target-station world position; reject markers farther than GATE (rejects neighbours).
EXPECT_N = float(os.environ["EXPECT_N"]) if os.environ.get("EXPECT_N") else None
EXPECT_E = float(os.environ["EXPECT_E"]) if os.environ.get("EXPECT_E") else None
GATE = float(os.environ.get("GATE", "8.0"))
# the unique ArUco id of the station we intend to land on; ignore all other ids (other pads)
TARGET_ID = int(os.environ["TARGET_ID"]) if os.environ.get("TARGET_ID") else None
# The flight agent rewrites this file ("<world_N> <world_E> <station_id>") at the start of each
# sortie, so one long-lived detector retargets to DIFFERENT landing stations across repeated
# orders. Absent -> fall back to the launch-time env target (single-order / test runs).
LAND_TARGET_FILE = os.environ.get("LAND_TARGET_FILE", "/tmp/land_target")

# camera intrinsics from mono_cam: hfov=2.0 rad, square pixels
W, H = 1280, 960
FX = (W / 2.0) / math.tan(2.0 / 2.0)   # hfov = 2.0 rad (matches mono_cam)
FY = FX
CX, CY = W / 2.0, H / 2.0
K = np.array([[FX, 0, CX], [0, FY, CY], [0, 0, 1]], dtype=np.float64)
DIST = np.zeros(5)
OBJP = np.array([[-MARKER_LEN/2,  MARKER_LEN/2, 0],
                 [ MARKER_LEN/2,  MARKER_LEN/2, 0],
                 [ MARKER_LEN/2, -MARKER_LEN/2, 0],
                 [-MARKER_LEN/2, -MARKER_LEN/2, 0]], dtype=np.float64)

# --- shared drone state, from gz ground-truth pose (ENU world: x=E, y=N, z=up) ---
state = {"n": 0.0, "e": 0.0, "alt": 0.0, "yaw": 0.0, "ok": False}


def on_pose(msg: Pose_V):
    for p in msg.pose:
        if p.name == MODEL:
            state["e"] = p.position.x
            state["n"] = p.position.y
            state["alt"] = p.position.z
            q = p.orientation
            siny = 2.0 * (q.w * q.z + q.x * q.y)
            cosy = 1.0 - 2.0 * (q.y * q.y + q.z * q.z)
            enu_yaw = math.atan2(siny, cosy)         # ENU yaw (0 = +East, CCW)
            # convert to NED heading (0 = North, CW) used by the body->NED rotation
            state["yaw"] = math.atan2(math.cos(enu_yaw), math.sin(enu_yaw))  # = pi/2 - enu_yaw, wrapped
            state["ok"] = True
            return


def detector():
    _params = cv2.aruco.DetectorParameters()
    _params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX  # sub-pixel corners -> accuracy
    det = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(ARUCO_DICT), _params)
    ema = [None, None]   # exponential moving average of (tgt_n, tgt_e) to cut jitter
    mav = mavutil.mavlink_connection(MAV_URL, source_system=1, source_component=196)
    last = [0.0]
    # marker_z is the tag's world height (from gen_world). The bearing triangulation needs the drone's
    # height ABOVE THE MARKER, not its raw world z — baylands clearings sit at different terrain
    # heights, so using world z biased the target and the drone landed metres off at low-lying stations.
    tgt = {"N": EXPECT_N, "E": EXPECT_E, "id": TARGET_ID, "marker_z": 0.0}   # current landing target
    tgt_mtime = [-1.0]

    def refresh_target():
        """Reload the landing target from LAND_TARGET_FILE when the flight agent rewrites it, so the
        detector follows repeated orders to different stations. No-op if the file is absent/stale."""
        try:
            m = os.path.getmtime(LAND_TARGET_FILE)
            if m != tgt_mtime[0]:
                tgt_mtime[0] = m
                parts = open(LAND_TARGET_FILE).read().split()
                tgt["N"], tgt["E"], tgt["id"] = float(parts[0]), float(parts[1]), int(parts[2])
                tgt["marker_z"] = float(parts[3]) if len(parts) > 3 else 0.0
                ema[0] = ema[1] = None     # forget the previous station's smoothed target -> converge fresh
                print(f"detector: retargeted to station {tgt['id']} (N={tgt['N']:+.1f} E={tgt['E']:+.1f} mz={tgt['marker_z']:+.2f})", flush=True)
        except Exception:
            pass

    def on_image(msg: Image):
        refresh_target()
        h, w = msg.height, msg.width
        buf = np.frombuffer(msg.data, dtype=np.uint8)
        try:
            img = buf.reshape(h, w, 3)
        except ValueError:
            return
        gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
        corners, ids, _ = det.detectMarkers(gray)
        now0 = time.time()
        if CALIB and now0 - last[0] > 1.0:
            last[0] = now0
            found = [] if ids is None else ids.flatten().tolist()
            print(f"frame {w}x{h} mean={gray.mean():.0f} markers={found}", flush=True)
        if ids is None:
            return
        alt = max(0.3, state["alt"] - tgt["marker_z"])       # height ABOVE THE MARKER (gz pose minus tag z)
        yaw = state["yaw"]
        sx = float(os.environ.get("DET_SX", "1")); sy = float(os.environ.get("DET_SY", "1"))
        swap = os.environ.get("DET_SWAP", "0") == "1"

        def world_target(c):
            # ROBUST geometry: bearing (image-centre offset) x altitude -> marker world N,E.
            # (Avoids solvePnP planar pose ambiguity, which made the drone diverge.)
            cu = float(c[:, 0].mean()); cv = float(c[:, 1].mean())
            cx_ = ((cu - CX) / FX) * alt          # camera right offset on ground [m]
            cy_ = ((cv - CY) / FY) * alt          # camera down-image offset on ground [m]
            fwd = sx * (-cy_); right = sy * cx_
            if swap: fwd, right = right, fwd
            dn = fwd * math.cos(yaw) - right * math.sin(yaw)
            de = fwd * math.sin(yaw) + right * math.cos(yaw)
            return state["n"] + dn, state["e"] + de

        # all pads are id 0; evaluate every detected marker and pick the one nearest the
        # KNOWN target station (or, if unknown, nearest the drone's nadir). Reject far ones.
        refN = tgt["N"] if tgt["N"] is not None else state["n"]
        refE = tgt["E"] if tgt["E"] is not None else state["e"]
        best = None; best_d = 1e18
        idflat = ids.flatten()
        for k in range(len(ids)):
            if tgt["id"] is not None and int(idflat[k]) != tgt["id"]:
                continue                                  # not our station's tag -> ignore
            tn, te = world_target(corners[k][0])
            dd = math.hypot(tn - refN, te - refE)
            if dd < best_d:
                best_d = dd; best = (tn, te)
        if best is None:
            return
        if tgt["N"] is not None and best_d > GATE:   # not our station -> ignore (avoids neighbours)
            if CALIB and now0 - last[0] > 1.0:
                print(f"  (nearest marker {best_d:.1f} m from expected > gate {GATE}; ignoring)", flush=True)
            return
        tgt_n, tgt_e = best
        cz_ = alt
        tgt_d = 0.0  # ground
        # exponential moving average to suppress per-frame jitter
        a = 0.4
        ema[0] = tgt_n if ema[0] is None else (1 - a) * ema[0] + a * tgt_n
        ema[1] = tgt_e if ema[1] is None else (1 - a) * ema[1] + a * tgt_e
        tgt_n, tgt_e = ema[0], ema[1]
        now = time.time()
        if CALIB:
            print(f"  DETECT: alt={cz_:.1f} drone N={state['n']:+.1f} E={state['e']:+.1f} "
                  f"yaw={math.degrees(yaw):+.0f} | TARGET N={tgt_n:+.2f} E={tgt_e:+.2f} (nearest {best_d:.1f}m)",
                  flush=True)
            return
        # send LANDING_TARGET in PX4 local NED (subtract home/takeoff world position)
        mav.mav.landing_target_send(
            int(now * 1e6) & 0xFFFFFFFFFFFFFFFF, 0,
            mavutil.mavlink.MAV_FRAME_LOCAL_NED,
            0.0, 0.0, max(0.1, cz_),
            0.0, 0.0,
            tgt_n - HOME_N, tgt_e - HOME_E, tgt_d,
            (1.0, 0.0, 0.0, 0.0), 2, 1)
        if now - last[0] > 1.0:
            last[0] = now
            print(f"LANDING_TARGET -> N={tgt_n:+.2f} E={tgt_e:+.2f} (dist {cz_:.1f}m)", flush=True)

    node = Node()
    if not node.subscribe(Image, CAM_TOPIC, on_image):
        print("FAILED to subscribe to", CAM_TOPIC); return 1
    if not node.subscribe(Pose_V, POSE_TOPIC, on_pose):
        print("FAILED to subscribe to", POSE_TOPIC); return 1
    print("detector subscribed | cam:", CAM_TOPIC, "| CALIB" if CALIB else "| sending LANDING_TARGET", flush=True)
    while True:
        time.sleep(1.0)


if __name__ == "__main__":
    sys.exit(detector())
