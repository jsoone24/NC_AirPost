#!/usr/bin/env bash
# Bring up the REAL integrated drone ROS 2 stack against PX4 v1.17 SITL — the code that ships on the
# aircraft, exercised end-to-end in simulation. Unlike run_airpost_fleet.sh (which flies the drones
# with a host-side MAVSDK service), this runs the actual airpost_drone ROS 2 node per drone, talking to
# PX4 over the native uXRCE-DDS bridge — exactly as it will on the Jetson+Pixhawk hardware.
#
# Pipeline per drone i (DDS namespace px4_<i+1>, id DRO<51+i>):
#   PX4 v1.17 SITL  --uXRCE-DDS-->  Micro-XRCE-DDS Agent  <--DDS-->  airpost_drone drone_node (ROS 2)
#   drone_node: subscribes the drone's OWN telemetry (/px4_k/fmu/out/*), flies OFFBOARD on MQTT
#   orders, drives the winch, and streams data/DRO<id> -> Sink -> Kafka. dummy_camera publishes the
#   realsense topic so the perception graph runs camera-less (swap in realsense-ros unchanged).
#   gcs_link heartbeats PX4 so autonomous DDS-offboard arming works without QGroundControl.
#
# Usage:   ./run_ros2_fleet.sh [N]            # N drones (default 1)
# Env (override for your machine; defaults match the documented dev setup):
#   ROS_ENV         micromamba env with ros-humble + px4_msgs + airpost_drone (default ros_env)
#   MAMBA_ROOT      micromamba root prefix                (default ~/mamba)
#   XRCE_AGENT_DIR  Micro-XRCE-DDS-Agent install prefix   (default ~/ws/Micro-XRCE-DDS-Agent/install)
#   PX4_DIR         PX4-Autopilot checkout                (default ~/ws/PX4-Autopilot)
#   COLCON_WS       colcon workspace with the overlay     (default ~/airpost_ros2_ws)
#   MQTT_BROKER_HOST telemetry broker                     (default 127.0.0.1)
set -u

N="${1:-1}"
SIMDIR="$(cd "$(dirname "$0")" && pwd)"
ROS_ENV="${ROS_ENV:-ros_env}"
export MAMBA_ROOT_PREFIX="${MAMBA_ROOT:-$HOME/mamba}"
XRCE_AGENT_DIR="${XRCE_AGENT_DIR:-$HOME/ws/Micro-XRCE-DDS-Agent/install}"
PX4_DIR="${PX4_DIR:-$HOME/ws/PX4-Autopilot}"
COLCON_WS="${COLCON_WS:-$HOME/airpost_ros2_ws}"
PROFILE="$SIMDIR/fastdds_localhost.xml"
export MQTT_BROKER_HOST="${MQTT_BROKER_HOST:-127.0.0.1}"
export ROS_DOMAIN_ID=0
export FASTRTPS_DEFAULT_PROFILES_FILE="$PROFILE" FASTDDS_DEFAULT_PROFILES_FILE="$PROFILE"

BUILD="$PX4_DIR/build/px4_sitl_default"
run() { micromamba run -n "$ROS_ENV" bash -c "$1"; }

cleanup() {
  pkill -f MicroXRCEAgent 2>/dev/null
  pkill -f "airpost_drone" 2>/dev/null; pkill -f dummy_camera 2>/dev/null; pkill -f drone_node 2>/dev/null
  pkill -f gcs_link.py 2>/dev/null
  pkill -x px4 2>/dev/null; pkill -f "gz sim" 2>/dev/null
}
trap cleanup EXIT INT TERM
cleanup; sleep 3

# 1) Micro-XRCE-DDS Agent (one agent serves every PX4 instance on :8888).
echo ">> starting Micro-XRCE-DDS Agent on udp:8888"
nohup micromamba run -n "$ROS_ENV" bash -c \
  "export DYLD_LIBRARY_PATH=$XRCE_AGENT_DIR/lib:\$DYLD_LIBRARY_PATH; $XRCE_AGENT_DIR/bin/MicroXRCEAgent udp4 -p 8888 -v 1" \
  >/tmp/airpost_agent.log 2>&1 &
sleep 3

# 2) gz server + N PX4 instances, each with its OWN DDS namespace px4_<i+1>.
export PATH="$PX4_DIR/.venv/bin:/opt/homebrew/bin:$PATH"
export PX4_GZ_WORLD=airpost GST_REGISTRY_FORK=no GZ_IP=127.0.0.1 HEADLESS="${HEADLESS:-1}"; unset GZ_PARTITION
export GZ_SIM_RESOURCE_PATH="$SIMDIR/gz/models:$SIMDIR/gz/worlds:$PX4_DIR/Tools/simulation/gz/models:$PX4_DIR/Tools/simulation/gz/worlds:${GZ_SIM_RESOURCE_PATH:-}"
export GZ_SIM_SERVER_CONFIG_PATH="$PX4_DIR/src/modules/simulation/gz_bridge/server.config"
python3 "$SIMDIR/gen_world.py" 40 20 airpost 0 baylands >/dev/null
echo ">> starting gz server + $N PX4 instance(s)"
gz sim -r -s "$SIMDIR/gz/worlds/airpost.sdf" >/tmp/airpost_ros2_gz.log 2>&1 &
sleep 8
# Single drone uses the un-namespaced /fmu/... topics (one autopilot, no collision). Multiple drones
# need a per-instance DDS namespace (px4_<key>) so their topics don't overlap on the shared agent.
for i in $(seq 0 $((N - 1))); do
  pose=$(python3 -c "
import json
st={s['id']: s for s in json.load(open('$SIMDIR/tests/airpost_sites.json'))['stations']}
s=st[$i + 1]; print(f\"{s['E']},{s['N']},{round(s['Z']+0.55,2)},0,0,0\")")
  wd="$BUILD/instance_$i"; mkdir -p "$wd"
  [ "$N" -gt 1 ] && DDS_NS="px4_$((i + 1))" || DDS_NS=""
  ( cd "$wd" && PX4_GZ_STANDALONE=1 PX4_GZ_WORLD=airpost PX4_GZ_MODEL_POSE="$pose" \
      PX4_UXRCE_DDS_NS="$DDS_NS" PX4_SIM_MODEL=gz_airpost_delivery_drone \
      "$BUILD/bin/px4" -i "$i" -d "$BUILD/etc" >"$wd/out.log" 2>"$wd/err.log" & )
  echo "   px4 $i @ $pose  ns=${DDS_NS:-<root>}  id=DRO$((51 + i))"
  sleep 6
done
sleep 12

# 3) ground-station heartbeat (enables autonomous DDS-offboard arming without QGroundControl).
echo ">> starting gcs_link (ground-station heartbeat)"
nohup "$PX4_DIR/.venv/bin/python3" "$SIMDIR/gcs_link.py" "$N" >/tmp/airpost_gcs_link.log 2>&1 &

# 4) the REAL ROS 2 drone stack: one drone_node + dummy_camera per drone.
echo ">> launching $N airpost_drone ROS 2 node(s)"
for i in $(seq 0 $((N - 1))); do
  k=$((i + 1))
  [ "$N" -gt 1 ] && { NS="px4_$k"; CAM="/px4_$k/camera/color/image_raw"; } || { NS=""; CAM="/camera/color/image_raw"; }
  nohup micromamba run -n "$ROS_ENV" bash -c "
    source '$COLCON_WS/install/setup.bash'
    export PX4_NS=$NS DRONE_INSTANCE=$i DRONE_ID=DRO$((51 + i)) CAMERA_TOPIC=$CAM
    ros2 run airpost_drone dummy_camera &
    ros2 run airpost_drone drone_node" \
    >"/tmp/airpost_ros2_drone_$i.log" 2>&1 &
  echo "   ROS2 drone_node $i: PX4_NS=${NS:-<root>} DRO$((51 + i))"
done

echo ">> integrated ROS 2 stack up. Telemetry on data/DRO51.. ; send delivery orders to"
echo "   command/downlink/ActuatorReq/DRO<id> (the backend dispatcher does this in production)."
echo "   Ctrl-C to stop everything."
wait
