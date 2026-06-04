#!/usr/bin/env bash
# Boot ONLY gz + a single fresh PX4 instance 0 (no fleet_service / fleet_demo / detectors), so the
# native ROS 2 graph (airpost_drone drone_node) is the sole controller over the uXRCE-DDS bridge.
# The running MicroXRCEAgent + ROS 2 nodes are left untouched; PX4's uxrce_dds_client auto-connects
# to the agent on :8888 at boot. Used to get a clean, armable drone for the ROS2 offboard flight test.
set -u
PX4=/Users/js/ws/PX4-Autopilot
SIMDIR=/Users/js/ws/NC_AirPost/simulation
BUILD="$PX4/build/px4_sitl_default"
export PATH="$PX4/.venv/bin:/opt/homebrew/bin:$PATH"
export PX4_GZ_WORLD=airpost GST_REGISTRY_FORK=no
unset GZ_PARTITION
export GZ_IP=127.0.0.1
export GZ_SIM_RESOURCE_PATH="$SIMDIR/gz/models:$SIMDIR/gz/worlds:$PX4/Tools/simulation/gz/models:$PX4/Tools/simulation/gz/worlds:${GZ_SIM_RESOURCE_PATH:-}"
export GZ_SIM_SERVER_CONFIG_PATH="$PX4/src/modules/simulation/gz_bridge/server.config"
export HEADLESS=1

echo ">> killing existing px4 + gz (leaving agent + ros2 graph alive)"
pkill -x px4 2>/dev/null; pkill -f "gz sim" 2>/dev/null
sleep 4

echo ">> regenerating world (no drone) + gz server"
python3 "$SIMDIR/gen_world.py" 40 20 airpost 0 baylands >/dev/null
gz sim -r -s "$SIMDIR/gz/worlds/airpost.sdf" >/tmp/fleet_gz.log 2>&1 &
sleep 8

echo ">> launching fresh px4 instance 0"
pose=$(python3 -c "
import json
st={s['id']: s for s in json.load(open('$SIMDIR/tests/airpost_sites.json'))['stations']}
s=st[1]; print(f\"{s['E']},{s['N']},{round(s['Z']+0.55,2)},0,0,0\")")
wd="$BUILD/instance_0"; mkdir -p "$wd"
( cd "$wd" && PX4_GZ_STANDALONE=1 PX4_GZ_WORLD=airpost PX4_GZ_MODEL_POSE="$pose" \
    PX4_SIM_MODEL=gz_airpost_delivery_drone "$BUILD/bin/px4" -i 0 -d "$BUILD/etc" \
    >"$wd/out.log" 2>"$wd/err.log" & )
echo "   px4 instance 0 @ $pose -> uXRCE-DDS agent :8888, MAVSDK udp 14540"
echo ">> booted; PX4 reconnecting to agent. Done."
