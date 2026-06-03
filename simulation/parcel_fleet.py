#!/usr/bin/env python3
"""Fleet parcel + winch-cable manager for AirPost Gazebo Harmonic SITL.

One gz transport process owns every static parcel pose in the fleet. Each parcel rides under its
drone's forward winch while attached, lowers to the per-order ground height on a per-drone flag
file, then stays released on the pad until the flight service clears the done flag for the next
sortie.
"""
import argparse
import math
import os
import sys
import time
from dataclasses import dataclass, field

RATE = float(os.environ.get("LOWER_RATE", "1.0"))
HZ = 60.0
FWD = 0.11
DRUM_DZ = -0.02
HOOK_DZ = -0.075

STATE_ATTACHED = "ATTACHED"
STATE_LOWERING = "LOWERING"
STATE_RELEASED = "RELEASED"


def winch_go(inst):
    return f"/tmp/airpost_winch_go_{inst}"


def winch_done(inst):
    return f"/tmp/airpost_winch_done_{inst}"


def winch_ground(inst):
    return f"/tmp/airpost_winch_ground_{inst}"


def yaw_from_quat(q):
    return math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))


def package_sdf(name):
    return f"""<?xml version="1.0" ?>
<sdf version="1.9">
  <model name="{name}">
    <static>true</static>
    <link name="package_link">
      <visual name="package_visual">
        <geometry><box><size>0.09 0.09 0.09</size></box></geometry>
        <material>
          <ambient>0.55 0.35 0.15 1</ambient>
          <diffuse>0.72 0.45 0.20 1</diffuse>
          <specular>0.1 0.1 0.1 1</specular>
        </material>
      </visual>
    </link>
  </model>
</sdf>
"""


@dataclass
class ParcelState:
    inst: int
    package_name: str = field(init=False)
    drone_aliases: tuple = field(init=False)
    drone_seen_name: str = ""
    package_seen: bool = False
    spawn_requested: bool = False
    state: str = STATE_ATTACHED
    x: float | None = None
    y: float | None = None
    z: float | None = None
    yaw: float = 0.0
    ground: float = 0.05
    lower_z0: float = 0.0
    lower_t0: float = 0.0
    release_x: float = 0.0
    release_y: float = 0.0

    def __post_init__(self):
        self.package_name = f"airpost_package_{self.inst}"
        self.drone_aliases = (
            f"gz_airpost_delivery_drone_{self.inst}",
            f"airpost_delivery_drone_{self.inst}",
        )

    @property
    def has_drone_pose(self):
        return self.x is not None

    def body_to_world(self, bx, by, bz):
        c, s = math.cos(self.yaw), math.sin(self.yaw)
        return self.x + c * bx - s * by, self.y + s * bx + c * by, self.z + bz


def request_result(value):
    """Normalize gz.transport Node.request return shapes across bindings."""
    if isinstance(value, tuple):
        ok = bool(value[0])
        msg = value[1] if len(value) > 1 else None
        return ok, bool(getattr(msg, "data", ok))
    return bool(value), bool(value)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n-drones", type=int, required=True)
    parser.add_argument("--world", default="airpost")
    args = parser.parse_args()

    from gz.transport13 import Node
    from gz.msgs10.boolean_pb2 import Boolean
    from gz.msgs10.entity_factory_pb2 import EntityFactory
    from gz.msgs10.marker_pb2 import Marker
    from gz.msgs10.pose_pb2 import Pose
    from gz.msgs10.pose_v_pb2 import Pose_V

    world = args.world
    pose_topic = f"/world/{world}/dynamic_pose/info"
    node = Node()
    states = [ParcelState(i) for i in range(args.n_drones)]
    by_drone_name = {name: st for st in states for name in st.drone_aliases}
    by_package_name = {st.package_name: st for st in states}

    def on_pose(msg):
        for p in msg.pose:
            st = by_drone_name.get(p.name)
            if st is not None:
                st.drone_seen_name = p.name
                st.yaw = yaw_from_quat(p.orientation)
                st.x, st.y, st.z = p.position.x, p.position.y, p.position.z
                continue
            st = by_package_name.get(p.name)
            if st is not None:
                st.package_seen = True

    def set_parcel(st, x, y, z):
        req = Pose()
        req.name = st.package_name
        req.position.x, req.position.y, req.position.z = x, y, z
        req.orientation.w = 1.0
        node.request(f"/world/{world}/set_pose", req, Pose, Boolean, 80)

    def set_cable(st, x, y, z_top, z_bot):
        marker = Marker()
        marker.ns = f"winch_cable_{st.inst}"
        marker.id = st.inst + 1
        marker.action = Marker.ADD_MODIFY
        marker.type = Marker.CYLINDER
        marker.pose.position.x = x
        marker.pose.position.y = y
        marker.pose.position.z = (z_top + z_bot) / 2.0
        marker.pose.orientation.w = 1.0
        marker.scale.x = marker.scale.y = 0.01
        marker.scale.z = max(0.02, z_top - z_bot)
        marker.material.ambient.r = marker.material.ambient.g = marker.material.ambient.b = 0.02
        marker.material.ambient.a = 1.0
        marker.material.diffuse.r = marker.material.diffuse.g = marker.material.diffuse.b = 0.06
        marker.material.diffuse.a = 1.0
        node.request("/marker", marker, Marker, Boolean, 80)

    def del_cable(st):
        marker = Marker()
        marker.ns = f"winch_cable_{st.inst}"
        marker.id = st.inst + 1
        marker.action = Marker.DELETE_MARKER
        node.request("/marker", marker, Marker, Boolean, 80)

    def create_package(st):
        if st.spawn_requested or st.package_seen or not st.has_drone_pose:
            return
        x, y, z = st.body_to_world(FWD, 0.0, HOOK_DZ)
        req = EntityFactory()
        req.name = st.package_name
        req.sdf = package_sdf(st.package_name)
        req.allow_renaming = False
        req.pose.position.x = x
        req.pose.position.y = y
        req.pose.position.z = max(st.ground, z)
        req.pose.orientation.w = 1.0
        # Spawning a model into the running sim can take >1 s; an 800 ms timeout returned false
        # (timeout), so the parcels never appeared. Give it a real budget.
        ok, created = request_result(node.request(f"/world/{world}/create", req, EntityFactory, Boolean, 5000))
        st.spawn_requested = True
        print(f"parcel_fleet: package {st.package_name} create requested ok={ok} result={created}", flush=True)

    if not node.subscribe(Pose_V, pose_topic, on_pose):
        print(f"parcel_fleet: FAILED to subscribe {pose_topic}", flush=True)
        sys.exit(1)

    for st in states:
        for path in (winch_go(st.inst), winch_done(st.inst)):
            try:
                os.remove(path)
            except OSError:
                pass

    t0 = time.time()
    while time.time() - t0 < 90 and not all(st.has_drone_pose for st in states):
        time.sleep(0.2)
    missing = [st.inst for st in states if not st.has_drone_pose]
    if missing:
        print(f"parcel_fleet: no drone pose for instances {missing}; continuing", flush=True)
    else:
        print(f"parcel_fleet: tracking {len(states)} drones on {pose_topic}", flush=True)

    dt = 1.0 / HZ
    tick = 0
    while True:
        tick += 1
        draw = tick % 3 == 0
        for st in states:
            if not st.has_drone_pose:
                continue
            create_package(st)
            dx, dy, dz = st.body_to_world(FWD, 0.0, DRUM_DZ)
            dz = max(dz, st.ground + 0.04)

            if st.state == STATE_ATTACHED:
                px, py, pz = st.body_to_world(FWD, 0.0, HOOK_DZ)
                pz = max(st.ground, pz)
                set_parcel(st, px, py, pz)
                if draw:
                    set_cable(st, dx, dy, max(dz, pz + 0.02), pz)
                if os.path.exists(winch_go(st.inst)):
                    try:
                        st.ground = float(open(winch_ground(st.inst)).read().strip())
                    except Exception:
                        pass
                    st.lower_t0 = time.time()
                    st.lower_z0 = max(st.body_to_world(FWD, 0.0, HOOK_DZ)[2], st.ground)
                    st.state = STATE_LOWERING
                    print(
                        f"parcel_fleet: drone {st.inst} lowering {st.package_name} "
                        f"to ground z={st.ground:.2f} at {RATE} m/s",
                        flush=True,
                    )

            elif st.state == STATE_LOWERING:
                px, py, _ = st.body_to_world(FWD, 0.0, 0.0)
                pz = max(st.ground, st.lower_z0 - RATE * (time.time() - st.lower_t0))
                set_parcel(st, px, py, pz)
                if draw:
                    set_cable(st, dx, dy, dz, pz)
                if pz <= st.ground:
                    st.release_x, st.release_y = px, py
                    set_parcel(st, st.release_x, st.release_y, st.ground)
                    del_cable(st)
                    open(winch_done(st.inst), "w").write("done")
                    st.state = STATE_RELEASED
                    print(f"parcel_fleet: drone {st.inst} delivered {st.package_name}", flush=True)

            else:
                if not os.path.exists(winch_done(st.inst)) and not os.path.exists(winch_go(st.inst)):
                    st.state = STATE_ATTACHED
                    print(f"parcel_fleet: drone {st.inst} re-armed", flush=True)

        time.sleep(dt)


if __name__ == "__main__":
    main()
