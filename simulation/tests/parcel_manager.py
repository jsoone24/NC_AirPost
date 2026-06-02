#!/usr/bin/env python3
"""Parcel + winch-cable manager (gz Harmonic, in-process gz transport).

Owns the parcel pose and draws the visible winch cable, because the parcel cannot be physically
jointed here: its collision box shoves against the drone's frame collision box and tips the
airframe, and gz's DART engine ignores <collide_bitmask> so the collision can't be filtered, and
the DetachableJoint is contact-based so it won't hold a collision-less parcel. So the parcel is
STATIC and this process positions it:

  transport : rides snug under the FORWARD winch (tucked by the legs), following the drone
  on /tmp/winch_go : pays the cable out, lowering the parcel straight down at LOWER_RATE m/s
  on ground : sets it down at the delivery point, removes the cable, touches /tmp/winch_done

It then parks the parcel until the flight agent clears /tmp/winch_done at the next sortie's start,
at which point it re-arms (back to transport) — so one long-lived manager serves repeated sorties.

(/tmp/winch_{go,done,ground} is the sim seam for the winch actuator: in the real system the winch
is a servo the onboard computer commands locally, so there is no parcel_manager and no network hop.)

The cable is drawn as a thin cylinder /marker from the reel (drum) to the parcel - the visible
"string" from the motor to the package.

Env: PX4_GZ_WORLD, DRONE_MODEL, LOWER_RATE (default 1.0)
Run via the detector venv (gz python): see _simctl det_py.
"""
import math, os, sys, time

WORLD = os.environ.get("PX4_GZ_WORLD", "airpost")
DRONE = os.environ.get("DRONE_MODEL", "airpost_delivery_drone_0")
POSE_TOPIC = f"/world/{WORLD}/dynamic_pose/info"
RATE = float(os.environ.get("LOWER_RATE", "1.0"))
HZ = 60.0                      # high enough that the wound parcel tracks the drone tightly in cruise
FWD = 0.11                    # winch is forward of the drone centre (body x)
DRUM_DZ = -0.02               # reel height under base (body z)
HOOK_DZ = -0.075              # parcel wound up SNUG just under the reel in flight (short cable);
                              # the cable only pays out during the delivery lower
GO, DONE, GROUND_FILE = "/tmp/winch_go", "/tmp/winch_done", "/tmp/winch_ground"


def next_state(state, go_exists, done_exists, reached_ground):
    """The winch state machine, isolated as a pure function so it is testable without gz:
      transport -> lower      when the flight agent raises /tmp/winch_go
      lower     -> done       when the parcel has reached the ground
      done      -> transport  when the agent clears /tmp/winch_done for the next sortie (re-arm)
    Any other case stays in the current state.
    """
    if state == "transport":
        return "lower" if go_exists else "transport"
    if state == "lower":
        return "done" if reached_ground else "lower"
    return "done" if done_exists else "transport"


def main():
    from gz.transport13 import Node
    from gz.msgs10.pose_v_pb2 import Pose_V
    from gz.msgs10.pose_pb2 import Pose
    from gz.msgs10.boolean_pb2 import Boolean
    from gz.msgs10.marker_pb2 import Marker

    node = Node()
    st = {"x": None, "y": None, "z": None, "yaw": 0.0}

    def on_pose(msg):
        for p in msg.pose:
            if p.name == DRONE:
                o = p.orientation
                st["yaw"] = math.atan2(2 * (o.w * o.z + o.x * o.y), 1 - 2 * (o.y * o.y + o.z * o.z))
                st["x"], st["y"], st["z"] = p.position.x, p.position.y, p.position.z

    def b2w(bx, by, bz):                                   # body offset -> world (yaw only)
        c, s = math.cos(st["yaw"]), math.sin(st["yaw"])
        return (st["x"] + c * bx - s * by, st["y"] + s * bx + c * by, st["z"] + bz)

    def set_parcel(x, y, z):
        r = Pose(); r.name = "airpost_package"
        r.position.x, r.position.y, r.position.z = x, y, z; r.orientation.w = 1.0
        node.request(f"/world/{WORLD}/set_pose", r, Pose, Boolean, 80)

    def set_cable(x, y, z_top, z_bot):                     # vertical string from the reel to the parcel
        m = Marker(); m.ns = "winch_cable"; m.id = 1; m.action = Marker.ADD_MODIFY; m.type = Marker.CYLINDER
        m.pose.position.x = x; m.pose.position.y = y; m.pose.position.z = (z_top + z_bot) / 2.0
        m.pose.orientation.w = 1.0
        m.scale.x = m.scale.y = 0.01; m.scale.z = max(0.02, z_top - z_bot)
        m.material.ambient.r = m.material.ambient.g = m.material.ambient.b = 0.02; m.material.ambient.a = 1.0
        m.material.diffuse.r = m.material.diffuse.g = m.material.diffuse.b = 0.06; m.material.diffuse.a = 1.0
        node.request("/marker", m, Marker, Boolean, 80)

    def del_cable():
        m = Marker(); m.ns = "winch_cable"; m.id = 1; m.action = Marker.DELETE_MARKER
        node.request("/marker", m, Marker, Boolean, 80)

    if not node.subscribe(Pose_V, POSE_TOPIC, on_pose):
        print("parcel_manager: FAILED to subscribe", POSE_TOPIC, flush=True); sys.exit(1)
    t0 = time.time()
    while st["x"] is None and time.time() - t0 < 90:
        time.sleep(0.2)
    if st["x"] is None:
        print("parcel_manager: no drone pose; abort", flush=True); sys.exit(1)
    print("parcel_manager: tracking the winch", flush=True)

    try: os.remove(DONE)
    except OSError: pass
    ground = 0.05
    state = "transport"; dt = 1.0 / HZ; t_lower = lower_z0 = 0.0; gx = gy = 0.0; tick = 0
    while True:
        tick += 1
        draw = (tick % 3 == 0)                             # redraw the cable marker at ~13 Hz (smoother GUI)
        dx, dy, dz = b2w(FWD, 0.0, DRUM_DZ)                # reel (cable top) world pos
        dz = max(dz, ground + 0.04)                        # keep the reel above ground when landed
        if state == "transport":
            px, py, pz = b2w(FWD, 0.0, HOOK_DZ)            # parcel rides under the winch
            pz = max(ground, pz)                           # never below ground (drone base sits at z~0)
            set_parcel(px, py, pz)
            if draw: set_cable(dx, dy, max(dz, pz + 0.02), pz)
            if next_state(state, os.path.exists(GO), False, False) == "lower":
                # the flight agent writes the delivery clearing's real ground height here so the parcel
                # lands ON that spot (the terrain height varies across baylands); fall back to the env.
                try:
                    ground = float(open(GROUND_FILE).read().strip())
                except Exception:
                    pass
                state = "lower"; t_lower = time.time(); lower_z0 = max(b2w(FWD, 0.0, HOOK_DZ)[2], ground)
                print(f"parcel_manager: GO -> lowering to ground z={ground:.2f} at {RATE} m/s", flush=True)
        elif state == "lower":
            wx, wy, _ = b2w(FWD, 0.0, 0.0)                 # straight down below the winch
            z = max(ground, lower_z0 - RATE * (time.time() - t_lower))
            set_parcel(wx, wy, z)
            if draw: set_cable(dx, dy, dz, z)
            if next_state(state, False, False, z <= ground) == "done":
                gx, gy = wx, wy; state = "done"
                del_cable(); open(DONE, "w").write("done")
                print("parcel_manager: delivered (cable released)", flush=True)
        else:                                              # done: park the parcel until the flight agent
            if next_state(state, False, os.path.exists(DONE), False) == "done":
                set_parcel(gx, gy, ground)                 # clears /tmp/winch_done at the next sortie ->
            else:
                state = "transport"                        # re-arm and ride the winch again
                print("parcel_manager: re-armed for next sortie", flush=True)
        time.sleep(dt)


if __name__ == "__main__":
    main()
