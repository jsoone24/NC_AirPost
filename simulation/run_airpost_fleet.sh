#!/usr/bin/env bash
# Multi-drone demo: N drones in ONE Gazebo world (PX4 multi-vehicle SITL on a standalone gz
# server), then fly them concurrently. Proves several drones simulate at once.
#
#   ./run_airpost_fleet.sh [N]          # N drones (default 2), Gazebo GUI window
#   GUI=0 ./run_airpost_fleet.sh 3      # headless (servers/CI)
#
# Mechanism (PX4 v1.17): one gz server holds the world; each px4 instance i is launched with
# PX4_GZ_STANDALONE=1 so it spawns its drone (airpost_delivery_drone_<i>) into that gz and
# exposes MAVSDK on udpin 14540+i. fleet_demo.py connects to all N and flies them together.
set -uo pipefail
PX4=/Users/js/ws/PX4-Autopilot
SIMDIR="$(cd "$(dirname "$0")" && pwd)"
N="${1:-2}"
BUILD="$PX4/build/px4_sitl_default"
export PATH="$PX4/.venv/bin:/opt/homebrew/bin:$PATH"
export PX4_GZ_WORLD=airpost GST_REGISTRY_FORK=no
unset GZ_PARTITION
# Include PX4's stock gz models/worlds: a manually-launched gz server (unlike PX4's own
# launch) must resolve x500/x500_base itself, and that base model carries the IMU/baro/mag
# sensors. Without it the server can't find x500/model.sdf and every instance boots with no
# gyro/accel/baro (Preflight Fail), so nothing arms.
export GZ_SIM_RESOURCE_PATH="$SIMDIR/gz/models:$SIMDIR/gz/worlds:$PX4/Tools/simulation/gz/models:$PX4/Tools/simulation/gz/worlds:${GZ_SIM_RESOURCE_PATH:-}"
# Load PX4's server config so the manual gz server runs the Sensors/Physics systems (the
# generated worlds carry no inline <plugin>; PX4 supplies these). Without this the px4
# instances see no IMU/accel/gyro and never arm.
export GZ_SIM_SERVER_CONFIG_PATH="$PX4/src/modules/simulation/gz_bridge/server.config"

cleanup() {
  pkill -x px4 2>/dev/null
  for p in "gz sim" "fleet_demo.py" "fleet_service.py" "mavsdk_server" "apriltag_detector.py"; do
    pkill -f "$p" 2>/dev/null
  done
  rm -f /tmp/airpost_land_target_* 2>/dev/null
}
trap cleanup EXIT INT TERM
cleanup; sleep 2

# 1) Regenerate the world WITHOUT a drone (each px4 instance spawns its own).
python3 "$SIMDIR/gen_world.py" 40 20 airpost 0 baylands >/dev/null

# 2) Spawn points: station ids 1..N "E,N,Z,0,0,0" (one drone per helipad). These ids match the
# backend seed (seed.go) so backend drone 51+i parks on station i+1 == fleet instance i.
poses=$(python3 -c "
import json
st={s['id']: s for s in json.load(open('$SIMDIR/tests/airpost_sites.json'))['stations']}
for i in range($N):
    s=st[i + 1]; print(f\"{s['E']},{s['N']},{round(s['Z']+0.55,2)},0,0,0\")
")

# 3) Start the gz server (standalone) holding the world.
echo ">> starting gz server (world=airpost), spawning $N drones"
gst-inspect-1.0 >/dev/null 2>&1 || true
if [ "${GUI:-1}" = "0" ]; then export HEADLESS=1; fi
gz sim -r -s "$SIMDIR/gz/worlds/airpost.sdf" >/tmp/fleet_gz.log 2>&1 &
sleep 8
[ "${GUI:-1}" = "1" ] && { gz sim -g >/tmp/fleet_gzgui.log 2>&1 & }

# 4) Launch N px4 instances; each spawns airpost_delivery_drone_<i> into the running gz.
FLEET_MODEL="${FLEET_MODEL:-gz_airpost_delivery_drone}"
i=0
while IFS= read -r pose; do
  [ -z "$pose" ] && continue
  wd="$BUILD/instance_$i"; mkdir -p "$wd"
  ( cd "$wd" && PX4_GZ_STANDALONE=1 PX4_GZ_WORLD=airpost PX4_GZ_MODEL_POSE="$pose" \
      PX4_SIM_MODEL="$FLEET_MODEL" "$BUILD/bin/px4" -i "$i" -d "$BUILD/etc" \
      >"$wd/out.log" 2>"$wd/err.log" & )
  echo "   px4 instance $i @ $pose  (MAVSDK udp 1454$i)"
  # Stagger generously: each instance inserts its model into the shared gz server, and two
  # inserts in the same physics cycle can race (a model occasionally never spawns). Spacing
  # them out makes 8-drone spawns reliable.
  i=$((i + 1)); sleep "${SPAWN_GAP:-8}"
done <<< "$poses"

# 5) Wait for boot, then run the flight program. SERVICE=1 runs the MQTT delivery service
# (fleet_service.py: waits for backend orders and flies them); default runs the self-contained
# fleet_demo.py that just proves all N drones take off, hover and land together.
echo "   waiting for $N drones to boot..."; sleep 20
if [ "${SERVICE:-0}" = "1" ]; then
  if [ "${PRECISION_LANDING:-1}" != "0" ] && [ "$FLEET_MODEL" = "gz_airpost_delivery_drone" ]; then
    GZP=$(ls -d /opt/homebrew/Cellar/gz-transport13/*/lib/python3.14/site-packages 2>/dev/null | head -1)
    GZM=$(ls -d /opt/homebrew/Cellar/gz-msgs10/*/lib/python3.14/site-packages 2>/dev/null | head -1)
    if [ -x "$SIMDIR/.venv-detector/bin/python" ] && [ -n "$GZP" ] && [ -n "$GZM" ]; then
      echo ">> starting $N per-drone precision landing detectors"
      det_rows=$(python3 -c "
import json
st={s['id']: s for s in json.load(open('$SIMDIR/tests/airpost_sites.json'))['stations']}
for i in range($N):
    s=st[i + 1]
    print(i, s['N'], s['E'], s['id'], s.get('marker_z', s.get('Z', 0.0)))
")
      while read -r di hn he sid mz; do
        [ -z "$di" ] && continue
        target="/tmp/airpost_land_target_$di"
        echo "$hn $he $sid $mz" > "$target"
        GZ_IP=127.0.0.1 PYTHONPATH="$GZP:$GZM" \
          AIRPOST_MODEL="airpost_delivery_drone_$di" \
          HOME_N="$hn" HOME_E="$he" EXPECT_N="$hn" EXPECT_E="$he" TARGET_ID="$sid" \
          LAND_TARGET_FILE="$target" MAV_URL="udpout:127.0.0.1:$((18570 + di))" \
          "$SIMDIR/.venv-detector/bin/python" "$SIMDIR/tests/apriltag_detector.py" \
          >"/tmp/airpost_det_$di.log" 2>&1 &
      done <<< "$det_rows"
    else
      echo ">> precision landing detectors not started (missing detector venv or gz python bindings)"
    fi
  fi
  echo ">> MQTT delivery service (broker ${MQTT_BROKER:-127.0.0.1}); register orders in the UI/API"
  MQTT_BROKER="${MQTT_BROKER:-127.0.0.1}" python3 "$SIMDIR/fleet_service.py" "$N"
else
  python3 "$SIMDIR/fleet_demo.py" "$N"
  echo "   fleet flight finished; Ctrl-C to stop the sim."
fi
wait
