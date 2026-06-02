#!/usr/bin/env bash
# Map landable clearings on the baylands VISUAL terrain by raycast (gpu_lidar), then write
# baylands_clearings.json. Raycast (not dropped boxes) so there is no collision tunnelling and we
# measure the surface the user actually sees. Needs the baylands fuel model downloaded once.
set -uo pipefail
SIMDIR="$(cd "$(dirname "$0")" && pwd)"
PX4=/Users/js/ws/PX4-Autopilot
export PATH="$PX4/.venv/bin:/opt/homebrew/bin:$PATH"
unset GZ_PARTITION; export GZ_IP=127.0.0.1
export GZ_SIM_RESOURCE_PATH="$SIMDIR/gz/models:$SIMDIR/gz/worlds"
GZ=$(which gz)
GZP=$(ls -d /opt/homebrew/Cellar/gz-transport13/*/lib/python3.14/site-packages 2>/dev/null | head -1)
GZM=$(ls -d /opt/homebrew/Cellar/gz-msgs10/*/lib/python3.14/site-packages 2>/dev/null | head -1)

cat > /tmp/probe_world.sdf <<'EOF'
<?xml version="1.0" ?>
<sdf version="1.9"><world name="probe">
  <plugin filename="gz-sim-physics-system" name="gz::sim::systems::Physics"/>
  <plugin filename="gz-sim-user-commands-system" name="gz::sim::systems::UserCommands"/>
  <plugin filename="gz-sim-scene-broadcaster-system" name="gz::sim::systems::SceneBroadcaster"/>
  <plugin filename="gz-sim-sensors-system" name="gz::sim::systems::Sensors"><render_engine>ogre2</render_engine></plugin>
  <scene><ambient>0.8 0.8 0.8 1</ambient><background>0.7 0.8 0.9 1</background></scene>
  <light name="s" type="directional"><pose>0 0 200 0 0 0</pose><direction>0 0 -1</direction><intensity>1</intensity></light>
  <include><uri>model://baylands_scenery</uri><name>park</name><pose>205 155 -1 0 0 0</pose></include>
  <include><uri>model://terrain_probe</uri><name>terrain_probe</name><pose>0 0 120 0 0 0</pose></include>
</world></sdf>
EOF

pkill -9 -f "gz sim" 2>/dev/null; sleep 1
echo ">> launching rendered baylands(visual) probe server..."
( "$GZ" sim -s -r -v1 /tmp/probe_world.sdf >/tmp/probe_gz.log 2>&1 ) &
GP=$!
for i in $(seq 1 50); do "$GZ" model --list 2>/dev/null | grep -q terrain_probe && { echo "   loaded @ ~$((i*2))s"; break; }; sleep 2; done
sleep 4
echo ">> raycasting terrain..."
PYTHONPATH="$GZP:$GZM" "$SIMDIR/.venv-detector/bin/python" "$SIMDIR/probe_terrain_lidar.py"
RC=$?
kill -9 $GP 2>/dev/null; pkill -9 -f "gz sim" 2>/dev/null
exit $RC
