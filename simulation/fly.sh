#!/usr/bin/env bash
# Run AFTER ./run_airpost_sim.sh is up (drone spawned on the takeoff station).
# Flies ONE full delivery + AprilTag precision-landing; watch it in the GUI window.
#   ./fly.sh [deliver_N] [deliver_E] [landing_station_id] [cruise_alt]
#   TAKEOFF=<id> must match the station run_airpost_sim.sh launched on (default 0).
set -uo pipefail
source "$(dirname "$0")/tests/_simctl.sh"
TAKEOFF="${TAKEOFF:-0}"
DN="${1:-25}"; DE="${2:-25}"; LID="${3:-1}"; CR="${4:-25}"
JSON="$SIMDIR/tests/${PX4_GZ_WORLD}_sites.json"
# takeoff station = HOME (its world coords); landing station = precision-land target
read TN TE LN LE < <(python3 -c "
import json;d=json.load(open('$JSON'));s={x['id']:x for x in d['stations']}
T=s[$TAKEOFF];L=s[$LID];print(T['N'],T['E'],L['N'],L['E'])")
echo ">> takeoff st$TAKEOFF (N=$TN,E=$TE) -> deliver (N=$DN,E=$DE) -> precision-land st$LID (N=$LN,E=$LE), cruise ${CR}m"
HOME_N="$TN" HOME_E="$TE" EXPECT_N="$LN" EXPECT_E="$LE" TARGET_ID="$LID" \
  det_py "$SIMDIR/tests/apriltag_detector.py" >/tmp/det.log 2>&1 &
DET=$!
mav_py "$SIMDIR/tests/full_mission.py" "$TN" "$TE" "$DN" "$DE" "$LN" "$LE" "$CR" 2>&1 \
  | grep -aE "ready|-> delivery|hover .* delivery|DELIVER_ERR|-> landing|over station|AUTO.PRECLAND|LAND_ERR|RESULT"
kill $DET 2>/dev/null
