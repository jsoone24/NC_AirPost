#!/usr/bin/env bash
# Shared sim control: robust teardown (macOS pkill needs ONE pattern per call — no \| OR),
# lock-file cleanup, and a sim launcher. Source this from test scripts.
PX4=/Users/js/ws/PX4-Autopilot
SIMDIR=/Users/js/ws/NC_AirPost/simulation
export PATH="$PX4/.venv/bin:/opt/homebrew/bin:$PATH"
export PX4_GZ_WORLD="${PX4_GZ_WORLD:-airpost}" PX4_GZ_MODEL_POSE="${PX4_GZ_MODEL_POSE:-0,0}"
unset GZ_PARTITION
export GZ_IP=127.0.0.1
# make all project models (incl. the 40 unique-id tags) discoverable by gz (PX4's gz_env.sh appends to this)
export GZ_SIM_RESOURCE_PATH="$SIMDIR/gz/models:$SIMDIR/gz/worlds:${GZ_SIM_RESOURCE_PATH:-}"
GZP=$(ls -d /opt/homebrew/Cellar/gz-transport13/*/lib/python3.14/site-packages 2>/dev/null | head -1)
GZM=$(ls -d /opt/homebrew/Cellar/gz-msgs10/*/lib/python3.14/site-packages 2>/dev/null | head -1)
export DET_PYPATH="$GZP:$GZM"

kill_all() {
  for _ in 1 2 3; do
    for p in "apriltag_detector" "parcel_manager" "airpost_flight_agent" "full_mission" "mavsdk_server" \
             "gz sim" "build/px4_sitl_default" "bin/px4" "tail -f /dev/null" "make px4_sitl"; do
      pkill -9 -f "$p" 2>/dev/null
    done
    sleep 1
    # done when no px4/gz remain
    pgrep -f "build/px4_sitl_default/bin/px4" >/dev/null 2>&1 || pgrep -f "gz sim" >/dev/null 2>&1 || break
  done
  rm -f /tmp/px4_lock-* /tmp/px4-sock-* 2>/dev/null
}

start_sim() {   # $1 = optional log path
  local log="${1:-/tmp/airpost_sim.log}"
  kill_all; sleep 2
  ( cd "$PX4" && tail -f /dev/null | HEADLESS=1 make px4_sitl gz_airpost_delivery_drone >"$log" 2>&1 ) &
  local i
  for i in $(seq 1 90); do
    grep -aq "Ready for takeoff" "$log" 2>/dev/null && { echo "sim ready"; return 0; }
    grep -aqE "No rule|Error 255|already running" "$log" 2>/dev/null && { echo "SIM FAIL:"; tail -5 "$log"; return 1; }
    sleep 2
  done
  echo "SIM TIMEOUT"; tail -5 "$log"; return 1
}

det_py() { GZ_IP=127.0.0.1 PYTHONPATH="$DET_PYPATH" "$SIMDIR/.venv-detector/bin/python" "$@"; }
mav_py() { "$PX4/.venv/bin/python" "$@"; }
