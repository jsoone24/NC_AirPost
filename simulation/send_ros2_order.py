#!/usr/bin/env python3
"""Compute and publish a real AirPost delivery order to an airpost_drone ROS 2 node — the same mission
the backend dispatcher issues: take off from the drone's station, fly to a drop pad and lower the
parcel by winch at 10 m, then return and precision-land on the station's AprilTag pad.

Coordinates come from the shared `tests/airpost_sites.json` (the same file PX4 spawns drones from), so
the NED offsets the node flies and the gz-world centres the winch/precland use are all consistent.

Usage:  send_ros2_order.py <instance> [site_index]
        instance i  -> drone DRO<51+i>, spawned on station i+1; default delivers to site 0.
Env:    MQTT_BROKER_HOST (default 127.0.0.1)"""
import json
import os
import sys
import time

import paho.mqtt.client as mqtt

HERE = os.path.dirname(os.path.abspath(__file__))
DROP_PAD_H = 0.2      # red drop-box height (matches fleet_service.py)
PARCEL_HALF = 0.045   # half the parcel cube
WINCH_HEIGHT = 10.0   # metres above the pad while lowering — the AirPost 10 m winch delivery
CRUISE = 15.0


def main():
    inst = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    data = json.load(open(os.path.join(HERE, "tests", "airpost_sites.json")))
    stations = {s["id"]: s for s in data["stations"]}
    sites = data.get("sites", [])
    station = stations[inst + 1]                       # this drone's takeoff + landing station
    if sites:
        si = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        site = sites[si % len(sites)]
    else:                                              # fall back to a point 12 m east of the station
        site = {"N": station["N"], "E": station["E"] + 12.0, "Z": station.get("Z", 0.0)}

    rest_z = site.get("Z", station.get("Z", 0.0)) + DROP_PAD_H + PARCEL_HALF
    order = {
        "cruise": CRUISE,
        "winch_height": WINCH_HEIGHT,
        "precland_alt": 3.0,           # descend to 3 m over the station tag, then vision precision-land
        # NED offset (north, east) from the takeoff station to the drop pad — the PX4 local frame.
        "deliver_ned": [site["N"] - station["N"], site["E"] - station["E"]],
        # gz world drop-pad centre [E, N, parcel-rest-height] so the winch slides the parcel onto it.
        "deliver_world": [site["E"], site["N"], rest_z],
        "landing_ned": [0.0, 0.0],                     # return to the takeoff station
        # precland target the detector seeks: [N, E, station_id, marker_z].
        "landing_world": [station["N"], station["E"], station["id"],
                          station.get("marker_z", station.get("Z", 0.0))],
    }

    drone_id = f"DRO{51 + inst}"
    cl = mqtt.Client()
    cl.connect(os.environ.get("MQTT_BROKER_HOST", "127.0.0.1"), 1883)
    cl.loop_start()
    time.sleep(0.5)
    cl.publish(f"command/downlink/ActuatorReq/{drone_id}", json.dumps(order))
    time.sleep(1.0)
    cl.loop_stop()
    print(f"order -> {drone_id}: deliver_ned={order['deliver_ned']} winch={WINCH_HEIGHT}m -> land home")


if __name__ == "__main__":
    main()
