#!/usr/bin/env bash
# Execute ONE headless delivery sortie (the pytest harness uses this; the live GUI demo uses
# tests/airpost_flight_agent.py instead). Launches a fresh sim + AprilTag detector + winch
# manager and runs full_mission, emitting STAGE:/DELIVER_ERR/LAND_ERR/RESULT lines on stdout.
#   args: <takeoff_id> <deliver_N> <deliver_E> <landing_id> [cruise]
set -uo pipefail
source "$(dirname "$0")/_simctl.sh"
trap kill_all EXIT
TID="$1"; DN="$2"; DE="$3"; LID="$4"; CR="${5:-30}"
JSON="$SIMDIR/tests/airpost_sites.json"
read TN TE LN LE < <(python3 -c "
import json; d=json.load(open('$JSON')); st={s['id']:s for s in d['stations']}
T=st[$TID]; L=st[$LID]; print(T['N'],T['E'],L['N'],L['E'])")
echo "STAGE:planning takeoff=st$TID($TN,$TE) deliver=($DN,$DE) landing=st$LID($LN,$LE) cruise=${CR}m"
rm -f /tmp/winch_go /tmp/winch_done 2>/dev/null
# headless pytest path uses the fast flat FIELD scene (no heavy baylands mesh, no terrain probe);
# the live GUI demo (run_airpost_live.sh) is the one that flies the real baylands terrain.
python3 "$SIMDIR/gen_world.py" 40 20 airpost "$TID" field >/dev/null
export PX4_GZ_WORLD=airpost PX4_GZ_MODEL_POSE="$TE,$TN,0.30,0,0,0"
echo "STAGE:launching"
start_sim || { echo "RESULT=FAIL (sim)"; exit 1; }
# detector (camera -> AprilTag -> LANDING_TARGET) + parcel/winch-cable manager. full_mission.py
# hovers over the delivery point and hands off to parcel_manager via /tmp/winch_go|done, so the
# manager MUST run or the winch lower never completes (DELIVER_ERR would stay 99).
HOME_N="$TN" HOME_E="$TE" EXPECT_N="$LN" EXPECT_E="$LE" TARGET_ID="$LID" det_py "$SIMDIR/tests/apriltag_detector.py" >/tmp/det.log 2>&1 &
DRONE_MODEL=airpost_delivery_drone_0 LOWER_RATE=1.0 det_py "$SIMDIR/tests/parcel_manager.py" >/tmp/parcel_mgr.log 2>&1 &
sleep 3
# the request carries deliver_N/E as METERS OFFSET from the takeoff station (the MQTT
# contract); full_mission wants absolute world coords, so add the takeoff station origin.
DNw=$(python3 -c "print($TN+$DN)"); DEw=$(python3 -c "print($TE+$DE)")
# full_mission args: HN HE DNw DEw LNw LEw cruise  (world coords)
mav_py "$SIMDIR/tests/full_mission.py" "$TN" "$TE" "$DNw" "$DEw" "$LN" "$LE" "$CR" 2>&1 | \
  grep -aE "ready|SPAWN_RPY|-> delivery|hover .* delivery|DELIVER_ERR|-> landing|over station|AUTO.PRECLAND|LAND_ERR|RESULT"
