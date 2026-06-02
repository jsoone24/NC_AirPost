#!/usr/bin/env bash
# Launch the AirPost delivery sim: PX4 v1.17 SITL + Gazebo Harmonic.
#
#   ./run_airpost_sim.sh                          # lite world + Gazebo GUI window
#   WORLD=airpost_baylands ./run_airpost_sim.sh   # full baylands scenery + GUI
#   GUI=0 ./run_airpost_sim.sh                     # headless, no window (CI/servers)
#
# This runs PX4's native launch. When GUI=1 (default) HEADLESS is left unset, so PX4's
# px4-rc.gzsim starts both the Gazebo server AND the GUI (`gz sim -g`) — same as on Linux.
# Run from a real Terminal (Terminal.app/iTerm) so the window can open. The terminal shows
# the interactive `pxh>` shell; type `shutdown` or Ctrl-C to stop.
set -uo pipefail
PX4=/Users/js/ws/PX4-Autopilot
SIMDIR="$(cd "$(dirname "$0")" && pwd)"
export PATH="$PX4/.venv/bin:/opt/homebrew/bin:$PATH"
export PX4_GZ_WORLD="${WORLD:-airpost}"
TAKEOFF="${TAKEOFF:-0}"            # which station the parcel + drone start on
unset GZ_PARTITION                 # gz transport must match the sim
# make project models (40 unique-id tags, delivery zone, drone, parcel) discoverable by gz
export GZ_SIM_RESOURCE_PATH="$SIMDIR/gz/models:$SIMDIR/gz/worlds:${GZ_SIM_RESOURCE_PATH:-}"

# (re)generate the baylands-scene world with stations/sites at random positions, parcel on the
# takeoff station; then spawn the drone exactly on that station (positions are randomised but
# seeded, so the layout is stable across runs).  SCENE=field for the plain flat field.
python3 "$SIMDIR/gen_world.py" 40 20 "$PX4_GZ_WORLD" "$TAKEOFF" "${SCENE:-baylands}" >/dev/null
read TE TN < <(python3 -c "import json;d=json.load(open('$SIMDIR/tests/${PX4_GZ_WORLD}_sites.json'));s={x['id']:x for x in d['stations']}[$TAKEOFF];print(s['E'],s['N'])")
export PX4_GZ_MODEL_POSE="$TE,$TN,0.30,0,0,0"
echo ">> world=$PX4_GZ_WORLD scene=${SCENE:-baylands}  takeoff station $TAKEOFF @ (E=$TE, N=$TN)"
export GST_REGISTRY_FORK=no        # fast/quiet GStreamer init for the GstCamera plugin (macOS)
[ "${GUI:-1}" = "0" ] && export HEADLESS=1   # GUI=0 -> server only, no window

# Pre-warm the GStreamer plugin registry so the first launch doesn't stall gz world-init.
gst-inspect-1.0 >/dev/null 2>&1 || true

cd "$PX4"
exec make px4_sitl gz_airpost_delivery_drone
