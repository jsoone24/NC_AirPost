#!/usr/bin/env bash
# Bring up the LIVE AirPost flight endpoint for the GUI order -> drone-flies demo:
#   GUI sim (PX4 SITL + Gazebo)  +  persistent winch/parcel manager  +  AprilTag detector
#   +  the persistent flight agent (one MAVSDK link, so an order lifts off in ~2 s).
#
# The backend stack (UI, application, broker, ...) runs separately via
#   cd AirPost && docker compose up -d
# Register a parcel in the UI (http://localhost:4173); the order flows over MQTT to the
# agent here and the drone in this Gazebo window takes off immediately.
#
#   ./run_airpost_live.sh          # takeoff/landing station 1 (matches the backend demo seed)
#   TAKEOFF=1 LANDING=1 ./run_airpost_live.sh
set -uo pipefail
source "$(dirname "$0")/_simctl.sh" 2>/dev/null || source "$(dirname "$0")/tests/_simctl.sh"
TAKEOFF="${TAKEOFF:-1}"; LANDING="${LANDING:-1}"      # station 1 == backend seed src/landing

# Regenerate the world FIRST, THEN read station coords from the freshly-written sites file —
# otherwise the spawn pose uses stale coords from a previous scene and the drone spawns/delivers
# at the wrong place (the agent reads the new sites, giving a large delivery error).
python3 "$SIMDIR/gen_world.py" 40 20 airpost "$TAKEOFF" baylands >/dev/null
# read station N,E AND the real terrain height Z (pads sit on the baylands ground, not z=0)
read TN TE TZ LN LE < <(python3 -c "
import json; d=json.load(open('$SIMDIR/tests/airpost_sites.json')); st={s['id']:s for s in d['stations']}
t=st[$TAKEOFF]; l=st[$LANDING]; print(t['N'], t['E'], t['Z'], l['N'], l['E'])")
# spawn the drone ON TOP of the raised helipad box (box top = terrain Z + 0.25) + a little clearance
SPAWN_Z=$(python3 -c "print($TZ + 0.55)")

echo ">> launching GUI sim at station $TAKEOFF (N=$TN E=$TE Z=$TZ), landing station $LANDING"
export PX4_GZ_WORLD=airpost PX4_GZ_MODEL_POSE="$TE,$TN,$SPAWN_Z,0,0,0" GST_REGISTRY_FORK=no
rm -f /tmp/winch_go /tmp/winch_done 2>/dev/null
kill_all; sleep 2
gst-inspect-1.0 >/dev/null 2>&1 || true
( cd "$PX4" && tail -f /dev/null | make px4_sitl gz_airpost_delivery_drone >/tmp/gui_sim.log 2>&1 ) &
trap 'kill_all; pkill -9 -f airpost_flight_agent 2>/dev/null' EXIT INT TERM

echo "   waiting for sim ready..."
for i in $(seq 1 90); do grep -aq "Ready for takeoff" /tmp/gui_sim.log 2>/dev/null && break; sleep 2; done
grep -aq "Ready for takeoff" /tmp/gui_sim.log || { echo "SIM not ready"; tail -5 /tmp/gui_sim.log; exit 1; }
echo "   sim ready; starting winch manager + detector + flight agent"

# persistent helpers: the winch/parcel manager (positions the parcel under the winch + lowers
# it on the cable) and the AprilTag detector (camera -> LANDING_TARGET for precision landing)
# GROUND = the takeoff clearing's terrain height, so the winch lowers the parcel onto the real
# baylands ground (not z=0); the seed delivery point is right next to the takeoff station.
DRONE_MODEL=airpost_delivery_drone_0 LOWER_RATE=1.0 GROUND="$TZ" det_py "$SIMDIR/tests/parcel_manager.py" >/tmp/parcel_mgr.log 2>&1 &
HOME_N="$TN" HOME_E="$TE" EXPECT_N="$LN" EXPECT_E="$LE" TARGET_ID="$LANDING" \
  det_py "$SIMDIR/tests/apriltag_detector.py" >/tmp/det.log 2>&1 &

# the persistent flight agent: pays the connect + GPS/home lock ONCE, then flies each MQTT order
echo "   flight agent connecting (one-time GPS/home lock ~30 s)..."
MQTT_BROKER="${MQTT_BROKER:-127.0.0.1}" mav_py "$SIMDIR/tests/airpost_flight_agent.py"
