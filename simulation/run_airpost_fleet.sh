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
export GZ_SIM_RESOURCE_PATH="$SIMDIR/gz/models:$SIMDIR/gz/worlds:${GZ_SIM_RESOURCE_PATH:-}"

cleanup() { pkill -x px4 2>/dev/null; pkill -f 'gz sim' 2>/dev/null; pkill -f fleet_demo.py 2>/dev/null; }
trap cleanup EXIT INT TERM
cleanup; sleep 2

# 1) Regenerate the world WITHOUT a drone (each px4 instance spawns its own).
python3 "$SIMDIR/gen_world.py" 40 20 airpost 0 baylands >/dev/null

# 2) First N station poses "E,N,Z,0,0,0" as spawn points (one drone per station helipad).
poses=$(python3 -c "
import json
st=json.load(open('$SIMDIR/tests/airpost_sites.json'))['stations']
for i in range($N):
    s=st[i]; print(f\"{s['E']},{s['N']},{round(s['Z']+0.4,2)},0,0,0\")
")

# 3) Start the gz server (standalone) holding the world.
echo ">> starting gz server (world=airpost), spawning $N drones"
gst-inspect-1.0 >/dev/null 2>&1 || true
if [ "${GUI:-1}" = "0" ]; then export HEADLESS=1; fi
gz sim -r -s "$SIMDIR/gz/worlds/airpost.sdf" >/tmp/fleet_gz.log 2>&1 &
sleep 8
[ "${GUI:-1}" = "1" ] && { gz sim -g >/tmp/fleet_gzgui.log 2>&1 & }

# 4) Launch N px4 instances; each spawns airpost_delivery_drone_<i> into the running gz.
i=0
while IFS= read -r pose; do
  [ -z "$pose" ] && continue
  wd="$BUILD/instance_$i"; mkdir -p "$wd"
  ( cd "$wd" && PX4_GZ_STANDALONE=1 PX4_GZ_WORLD=airpost PX4_GZ_MODEL_POSE="$pose" \
      PX4_SIM_MODEL=gz_airpost_delivery_drone "$BUILD/bin/px4" -i "$i" -d "$BUILD/etc" \
      >"$wd/out.log" 2>"$wd/err.log" & )
  echo "   px4 instance $i @ $pose  (MAVSDK udp 1454$i)"
  i=$((i + 1)); sleep 5
done <<< "$poses"

# 5) Wait for boot, then fly all N concurrently.
echo "   waiting for $N drones to boot..."; sleep 20
python3 "$SIMDIR/fleet_demo.py" "$N"
echo "   fleet flight finished; Ctrl-C to stop the sim."
wait
