#!/usr/bin/env python3
"""AprilTag/ArUco precision-landing detector (sim stand-in for Jetson Nano + Intel T265).

Subscribes to the drone's downward gz camera, detects the ArUco marker (DICT_4X4_50 id 0),
estimates camera bearing from the marker centre, and streams angular MAVLINK LANDING_TARGET
(position_valid=false) to PX4's landing_target_estimator / PrecLand path.
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

import numpy as np
import cv2
os.environ["MAVLINK20"] = "1"
from pymavlink import mavutil
from gz.transport13 import Node
from gz.msgs10.image_pb2 import Image

CALIB = "--calib" in sys.argv
WORLD = os.environ.get("PX4_GZ_WORLD", "airpost")
MODEL = os.environ.get("AIRPOST_MODEL", "airpost_delivery_drone_0")
CAM_TOPIC = f"/world/{WORLD}/model/{MODEL}/link/camera_link/sensor/imager/image"
# Send LANDING_TARGET to PX4's GCS-link receive port (no bind -> no port conflicts).
MAV_URL = os.environ.get("MAV_URL", "udpout:127.0.0.1:18570")
MARKER_LEN = 0.50           # marker side length [m] (airpost_landing_marker)
ARUCO_DICT = cv2.aruco.DICT_4X4_50
# the unique ArUco id of the station we intend to land on; ignore all other ids (other pads)
TARGET_ID = int(os.environ["TARGET_ID"]) if os.environ.get("TARGET_ID") else None
# The flight agent rewrites this file ("<world_N> <world_E> <station_id>") at the start of each
# sortie, so one long-lived detector retargets to DIFFERENT landing stations across repeated
# orders. Absent -> fall back to the launch-time env target (single-order / test runs).
LAND_TARGET_FILE = os.environ.get("LAND_TARGET_FILE", "/tmp/land_target")

# camera intrinsics derived from the ACTUAL frame size (set on the first image) + the camera's
# horizontal FOV, so the detector is correct at ANY resolution. (The fleet uses a 640x480 low-res
# camera to keep four concurrent precision landings light; deriving intrinsics from the frame means
# the bearing/angle maths stay correct without hardcoding a resolution.)
HFOV = float(os.environ.get("CAM_HFOV", "2.0"))   # radians, matches the gz camera model
MAX_DET_HZ = float(os.environ.get("MAX_DET_HZ", "12"))   # cap cv2 detection rate per detector (0=off)
# When set, only detect while this file exists (the flight service creates it during the drone's
# landing phase). Keeps idle detectors from burning CPU and starving the one that is landing.
LANDING_FLAG = os.environ.get("LANDING_FLAG")
DIST = np.zeros(5)
intr = {"w": 0, "h": 0, "fx": 1.0, "fy": 1.0, "cx": 0.0, "cy": 0.0, "K": np.eye(3, dtype=np.float64)}


def set_intrinsics(w, h):
    fx = (w / 2.0) / math.tan(HFOV / 2.0)
    fy = fx   # square pixels
    cx, cy = w / 2.0, h / 2.0
    intr.update(w=w, h=h, fx=fx, fy=fy, cx=cx, cy=cy,
                K=np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1]], dtype=np.float64))
OBJP = np.array([[-MARKER_LEN/2,  MARKER_LEN/2, 0],
                 [ MARKER_LEN/2,  MARKER_LEN/2, 0],
                 [ MARKER_LEN/2, -MARKER_LEN/2, 0],
                 [-MARKER_LEN/2, -MARKER_LEN/2, 0]], dtype=np.float64)


def detector():
    _params = cv2.aruco.DetectorParameters()
    _params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX  # sub-pixel corners -> accuracy
    det = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(ARUCO_DICT), _params)
    mav = mavutil.mavlink_connection(MAV_URL, source_system=1, source_component=196)
    last = [0.0]
    proc_t = [0.0]   # last time a frame was actually processed (for MAX_DET_HZ throttle)
    tgt = {"id": TARGET_ID}   # current landing target
    tgt_mtime = [-1.0]

    def refresh_target():
        """Reload the landing target from LAND_TARGET_FILE when the flight agent rewrites it, so the
        detector follows repeated orders to different stations. No-op if the file is absent/stale."""
        try:
            m = os.path.getmtime(LAND_TARGET_FILE)
            if m != tgt_mtime[0]:
                tgt_mtime[0] = m
                parts = open(LAND_TARGET_FILE).read().split()
                tgt["id"] = int(parts[2])
                print(f"detector: retargeted to station {tgt['id']}", flush=True)
        except Exception:
            pass

    def marker_distance(c):
        try:
            ok, _rvec, tvec = cv2.solvePnP(OBJP, c.astype(np.float64), intr["K"], DIST)
            if ok:
                d = float(np.linalg.norm(tvec))
                if math.isfinite(d) and d > 0.0:
                    return d
        except cv2.error:
            pass
        w1 = np.linalg.norm(c[1] - c[0]); w2 = np.linalg.norm(c[2] - c[3])
        h1 = np.linalg.norm(c[2] - c[1]); h2 = np.linalg.norm(c[3] - c[0])
        px = max(1.0, float((w1 + w2 + h1 + h2) / 4.0))
        return MARKER_LEN * intr["fx"] / px

    def marker_size(c):
        w = (np.linalg.norm(c[1] - c[0]) + np.linalg.norm(c[2] - c[3])) / 2.0
        h = (np.linalg.norm(c[2] - c[1]) + np.linalg.norm(c[3] - c[0])) / 2.0
        return float(w / intr["fx"]), float(h / intr["fy"])

    def on_image(msg: Image):
        # Throttle ArUco processing to MAX_DET_HZ. The camera publishes ~20-30 Hz, but running the
        # full-res cv2 detection on every frame in EACH of N per-drone detectors oversubscribes the
        # CPU under a multi-drone fleet -> every detector falls behind -> detection stutters with
        # multi-second gaps -> "Lost sight of Marker" -> PrecLand fallback -> off-pad touchdown.
        # Cheaply skipping frames down to a steady ~12 Hz keeps total cv2 load low so all detectors
        # track continuously; 12 Hz is ample for PrecLand. (MAX_DET_HZ=0 disables the throttle.)
        now_t = time.time()
        if MAX_DET_HZ > 0 and (now_t - proc_t[0]) < (1.0 / MAX_DET_HZ):
            return
        # Only run the (expensive) ArUco detection while THIS drone is actually in its landing phase.
        # The flight service creates LANDING_FLAG when the drone starts precision landing and removes
        # it afterwards. With landings serialised fleet-wide, this means at most one detector runs cv2
        # at a time, so it is never CPU-starved by the other drones' detectors (which caused the marker
        # to drop out -> PrecLand search/fallback -> off-pad touchdown). No flag configured => always on.
        if LANDING_FLAG and not os.path.exists(LANDING_FLAG):
            return
        proc_t[0] = now_t
        refresh_target()
        h, w = msg.height, msg.width
        if intr["w"] != w or intr["h"] != h:
            set_intrinsics(w, h)                 # derive intrinsics from the actual frame size
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
        best = None; best_d = 1e18
        idflat = ids.flatten()
        for k in range(len(ids)):
            if tgt["id"] is not None and int(idflat[k]) != tgt["id"]:
                continue                                  # not our station's tag -> ignore
            c = corners[k][0]
            cu = float(c[:, 0].mean()); cv = float(c[:, 1].mean())
            dd = math.hypot(cu - intr["cx"], cv - intr["cy"])
            if dd < best_d:
                best_d = dd; best = (int(idflat[k]), c, cu, cv)
        if best is None:
            return
        tag_id, c, cu, cv = best
        # Reject ONLY genuine close-up frames: when the tag nearly fills the camera (a few tens of
        # cm above the pad) its corner localisation degrades and the centre jumps, injecting a false
        # bearing right before touchdown. We do NOT reject a tag that merely sits near the image edge
        # at altitude — that is a VALID large-offset measurement the estimator needs to pull an
        # off-centre drone over the pad. Dropping those (the earlier border gate) starved the KF
        # under multi-drone load -> "measurement rejected"/"lost marker" -> PrecLand fallback ->
        # off-pad touchdown. So only the overflow case is gated.
        xs, ys = c[:, 0], c[:, 1]
        if max(xs.max() - xs.min(), ys.max() - ys.min()) > 0.85 * w:   # tag >85% of frame -> too close
            return
        angle_x = math.atan2(cu - intr["cx"], intr["fx"])
        angle_y = math.atan2(cv - intr["cy"], intr["fy"])
        ray_x = (cu - intr["cx"]) / intr["fx"]
        ray_y = (cv - intr["cy"]) / intr["fy"]
        distance = marker_distance(c)
        size_x, size_y = marker_size(c)
        now = time.time()
        if CALIB:
            print(f"  DETECT: id={tag_id} angle=({angle_x:+.3f},{angle_y:+.3f}) "
                  f"ray=({ray_x:+.3f},{ray_y:+.3f}) dist={distance:.1f}m",
                  flush=True)
            return
        # PX4's MAVLink receiver copies angle_x/angle_y straight into IrlockReport.pos_x/pos_y,
        # and that uORB message defines these fields as tan(theta), i.e. the camera ray x/y.
        mav.mav.landing_target_send(
            int(now * 1e6) & 0xFFFFFFFFFFFFFFFF, tag_id,
            mavutil.mavlink.MAV_FRAME_BODY_FRD,
            ray_x, ray_y, max(0.1, distance),
            size_x, size_y,
            0.0, 0.0, 0.0,
            (1.0, 0.0, 0.0, 0.0),
            mavutil.mavlink.LANDING_TARGET_TYPE_VISION_FIDUCIAL, 0)
        if now - last[0] > 1.0:
            last[0] = now
            print(f"LANDING_TARGET angular -> id={tag_id} angle=({angle_x:+.3f},{angle_y:+.3f}) dist={distance:.1f}m", flush=True)

    node = Node()
    if not node.subscribe(Image, CAM_TOPIC, on_image):
        print("FAILED to subscribe to", CAM_TOPIC); return 1
    print("detector subscribed | cam:", CAM_TOPIC, "| CALIB" if CALIB else "| sending LANDING_TARGET", flush=True)
    while True:
        time.sleep(1.0)


if __name__ == "__main__":
    sys.exit(detector())
