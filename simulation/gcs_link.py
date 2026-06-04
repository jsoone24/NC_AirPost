#!/usr/bin/env python3
"""Minimal MAVLink GCS heartbeat for SITL — models the ground-station / telemetry-radio link that a
real AirPost drone always has.

PX4 treats the uXRCE-DDS link (used by the airpost_drone ROS 2 node) as the onboard control link, NOT
as a ground-station datalink. With no MAVLink GCS heartbeat, PX4 boots into a data-link-loss state and
refuses to arm ("GCS connection regained" appears the moment a heartbeat arrives, after which arming
succeeds). On real hardware the ground station / telemetry radio provides this; in SITL this tiny
sender stands in for it so autonomous DDS-offboard arming works without QGroundControl.

Sends a GCS heartbeat at 1 Hz to each PX4 instance i on its onboard MAVLink port 14540+i.
Usage: gcs_link.py <n_drones>   (default 1)"""
import sys
import time

from pymavlink import mavutil

GCS = mavutil.mavlink.MAV_TYPE_GCS
INVALID = mavutil.mavlink.MAV_AUTOPILOT_INVALID


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    links = [mavutil.mavlink_connection(f"udpout:127.0.0.1:{14540 + i}",
                                        source_system=255, source_component=190) for i in range(n)]
    print(f"gcs_link: heartbeating {n} PX4 instance(s) on udp 14540..{14540 + n - 1}", flush=True)
    while True:
        for link in links:
            link.mav.heartbeat_send(GCS, INVALID, 0, 0, 0)
        time.sleep(1.0)


if __name__ == "__main__":
    main()
