#!/usr/bin/env bash
# 10 m WINCH delivery demo, with GUI. Relaunches the sim (upright rigid drone, fixed cable
# visual), then flies: take off -> hover at 10 m over the delivery point -> the winch pays the
# cable out and lowers the parcel to the ground on a visible cable -> release -> climb away.
#   ./fly10.sh [deliver_dN] [deliver_dE]      (delivery offset from the takeoff station)
#   TAKEOFF=<id>  start station (default 0)
set -uo pipefail
source "$(dirname "$0")/tests/_simctl.sh"
TAKEOFF="${TAKEOFF:-0}"; dN="${1:-22}"; dE="${2:-15}"
read TN TE < <(python3 -c "import json;d=json.load(open('$SIMDIR/tests/airpost_sites.json'));s={x['id']:x for x in d['stations']}[$TAKEOFF];print(s['N'],s['E'])")
DN=$(python3 -c "print($TN+$dN)"); DE=$(python3 -c "print($TE+$dE)")
echo ">> takeoff st$TAKEOFF (N=$TN,E=$TE) -> winch-deliver at (N=$DN,E=$DE), 10 m hover"

# relaunch sim WITH GUI. Regenerate the world (parcel spawns under the forward winch).
python3 "$SIMDIR/gen_world.py" 40 20 airpost "$TAKEOFF" field >/dev/null
rm -f /tmp/winch_go /tmp/winch_done 2>/dev/null
kill_all; sleep 1; for p in "px4" "gz sim" "gz-sim" "ruby" "make px4_sitl" "tail -f /dev/null" "parcel_manager"; do pkill -9 -f "$p" 2>/dev/null; done
rm -f /tmp/px4_lock-* /tmp/px4-sock-* 2>/dev/null; sleep 2
export PX4_GZ_WORLD=airpost PX4_GZ_MODEL_POSE="$TE,$TN,0.30,0,0,0" GST_REGISTRY_FORK=no
gst-inspect-1.0 >/dev/null 2>&1 || true
( cd "$PX4" && tail -f /dev/null | make px4_sitl gz_airpost_delivery_drone >/tmp/gui_sim.log 2>&1 ) &
echo "   waiting for sim ready..."
for i in $(seq 1 80); do grep -aq "Ready for takeoff" /tmp/gui_sim.log 2>/dev/null && break; sleep 2; done
grep -aq "Ready for takeoff" /tmp/gui_sim.log || { echo "SIM not ready"; tail -5 /tmp/gui_sim.log; exit 1; }
echo "   sim ready; parcel manager + winch delivery"

# persistent parcel+cable manager (positions the parcel under the winch + draws the string) +
# the MAVSDK flight (takeoff -> 10 m hover over delivery -> GO -> wait DONE -> leave)
DRONE_MODEL=airpost_delivery_drone_0 LOWER_RATE=1.0 det_py "$SIMDIR/tests/parcel_manager.py" >/tmp/parcel_mgr.log 2>&1 &
MGR=$!
HOVER_ALT=10 HOLD_S=45 mav_py "$SIMDIR/tests/winch10_kinematic.py" "$TN" "$TE" "$DN" "$DE" 2>&1 | \
  grep -aE "connected|-> delivery|hover|DELIVER_ERR|RESULT"
echo "--- parcel manager log ---"; grep -aE "tracking|GO|delivered|abort" /tmp/parcel_mgr.log | tail -6
kill $MGR 2>/dev/null
