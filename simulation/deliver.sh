#!/usr/bin/env bash
# deliver.sh — the easy "just fly the drone delivery" harness.
#
# Spins up ONLY what's needed to watch drones deliver parcels in Gazebo — the world, the drone(s),
# the downward-camera AprilTag detectors and the winch — then flies each drone a full delivery
# (takeoff -> winch the parcel onto the red drop pad -> AprilTag precision landing) using built-in
# orders. NO backend, NO web UI, NO MySQL/Kafka, NO MQTT broker, NO Docker required.
#
#   ./deliver.sh            # 1 drone  (single-drone delivery)
#   ./deliver.sh 4          # 4 drones (multi-drone, with collision avoidance)
#   GUI=0 ./deliver.sh 4    # headless (no Gazebo window)
#
# It is just run_airpost_fleet.sh in DEMO mode. Need the full product (order from the web UI,
# live map, "delivered" email)? Use the backend stack + `SERVICE=1 ./run_airpost_fleet.sh N`.
set -uo pipefail
SIMDIR="$(cd "$(dirname "$0")" && pwd)"
N="${1:-1}"
echo ">> AirPost delivery demo: $N drone(s), standalone (no backend needed)."
GUI="${GUI:-1}" DEMO=1 "$SIMDIR/run_airpost_fleet.sh" "$N"
