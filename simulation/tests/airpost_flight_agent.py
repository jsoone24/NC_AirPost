#!/usr/bin/env python3
"""Persistent AirPost flight agent — the live sim endpoint of the delivery stack.

One long-lived MAVSDK link flies a sortie per MQTT order, so the connect + EKF lock is paid
once at startup and an order lifts off in ~2 s. A sortie has three phases (one method each):
  _takeoff  -> vertical AUTO.TAKEOFF climb, then hand to OFFBOARD at cruise altitude
  _deliver  -> cruise to the drop point, hover, signal the winch to lower the parcel
  _land     -> cruise to the landing station, align to the tag, precision-land
fly() is the orchestrator: it resolves the order into a Plan of world/NED targets and runs the
three phases against it.

Order  (airpost/delivery/request): {order_id, takeoff_id, deliver_N, deliver_E, landing_id, cruise}
  deliver_N/E are METERS OFFSET (local NED) from the takeoff station.
Status (airpost/delivery/status):  {order_id, state, deliver_err, land_err, result}
Run:  MQTT_BROKER=127.0.0.1 PX4-Autopilot/.venv/bin/python tests/airpost_flight_agent.py
"""
import asyncio, json, math, os, re, subprocess
from collections import namedtuple
import paho.mqtt.client as mqtt
from mavsdk import System
from mavsdk.offboard import OffboardError, PositionNedYaw
from pymavlink import mavutil

HERE = os.path.dirname(os.path.abspath(__file__))
BROKER = os.environ.get("MQTT_BROKER", "127.0.0.1")
SITES = os.path.join(HERE, "airpost_sites.json")
REQ_TOPIC, STATUS_TOPIC = "airpost/delivery/request", "airpost/delivery/status"
DELIV_ALT = float(os.environ.get("DELIV_ALT", "10"))   # winch-hover height above the drop point
HOLD_S = float(os.environ.get("HOLD_S", "45"))         # max seconds to wait for the winch lower
# sim-internal winch seam: the agent signals the lower and reads the result via these files; the
# detector reads the per-sortie landing target from /tmp/land_target (see apriltag_detector.py).
WINCH_GO, WINCH_DONE, WINCH_GROUND = "/tmp/winch_go", "/tmp/winch_done", "/tmp/winch_ground"
LAND_TARGET = "/tmp/land_target"
GZ = {**{k: v for k, v in os.environ.items() if k != "GZ_PARTITION"}, "GZ_IP": "127.0.0.1"}
# raw-MAVLink side channel: the one thing MAVSDK can't do cleanly — switch PX4 to AUTO.PRECLAND
_mav = mavutil.mavlink_connection("udpout:127.0.0.1:18570", source_system=255, source_component=190)

# one sortie's resolved targets. dn/de/ln/le are local NED (origin = spawn); dnw/dew/deliv_gz are
# gz world coords used only for measuring the drop error; land_yaw_deg is the tag's ENU heading.
Plan = namedtuple("Plan", "cruise dn de dnw dew deliv_gz ln le land_yaw_deg")


def stations():
    return {s["id"]: s for s in json.load(open(SITES))["stations"]}


def parcel_xy():
    """world (E, N) of the static parcel, positioned/lowered by parcel_manager.py (gz x=E, y=N)."""
    try:
        out = subprocess.run(["gz", "model", "-m", "airpost_package", "-p"], env=GZ,
                             timeout=4, capture_output=True, text=True).stdout
        m = re.search(r"\[\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*\]", out)
        return (float(m.group(1)), float(m.group(2))) if m else None
    except Exception:
        return None


def request_precland():
    _mav.mav.command_long_send(1, 1, mavutil.mavlink.MAV_CMD_DO_SET_MODE, 0, 1, 4, 9, 0, 0, 0, 0)


class FlightAgent:
    """Holds the persistent drone link and flies one sortie at a time.

    The link is opened once (connect) and reused for every order, so the slow GPS/EKF/home lock is
    paid a single time. Each sortie is driven by a live OFFBOARD position setpoint (self._sp) that a
    background _offboard_stream task republishes at 10 Hz; self._flying gates that task, and
    self._cruise_yaw is the heading captured at takeoff and held through cruise.
    """

    def __init__(self):
        self.d = System()
        self.lock = asyncio.Lock()
        self._sp = PositionNedYaw(0.0, 0.0, 0.0, 0.0)   # live OFFBOARD target (north, east, down, yaw)
        self._flying = False                            # gates the offboard setpoint stream
        self._cruise_yaw = 0.0

    # --- one-shot telemetry reads (the live link, so always fresh) ---
    @staticmethod
    async def _first(stream):
        async for x in stream:
            return x

    async def pos(self):
        """(north, east, up) in local NED metres, relative to the spawn point."""
        p = (await self._first(self.d.telemetry.position_velocity_ned())).position
        return p.north_m, p.east_m, -p.down_m

    async def yaw(self):
        return (await self._first(self.d.telemetry.attitude_euler())).yaw_deg

    async def armed(self):
        return await self._first(self.d.telemetry.armed())

    async def connect(self):
        """Connect once, wait until actually flyable, and set the flight/landing params."""
        await self.d.connect(system_address="udpin://0.0.0.0:14540")
        async for s in self.d.core.connection_state():
            if s.is_connected:
                break
        async for h in self.d.telemetry.health():
            if h.is_global_position_ok and h.is_home_position_ok and h.is_local_position_ok:
                break
        for k, v in [("PLD_SRCH_ALT", 3.0), ("PLD_HACC_RAD", 0.15), ("PLD_FAPPR_ALT", 0.1),
                     ("PLD_SRCH_TOUT", 20.0), ("MPC_XY_VEL_MAX", 8.0), ("MPC_XY_CRUISE", 8.0),
                     ("MPC_Z_VEL_MAX_UP", 3.0), ("MPC_Z_VEL_MAX_DN", 3.0),
                     ("MPC_LAND_SPEED", 1.0), ("COM_DISARM_LAND", 0.4)]:
            try:
                await self.d.param.set_param_float(k, v)
            except Exception:
                pass
        print("AGENT READY — orders lift off now", flush=True)

    # --- OFFBOARD plumbing ---
    async def _offboard_stream(self):
        """Republish the live setpoint at 10 Hz until the sortie ends. A momentary RPC hiccup must
        not kill this task, or PX4's offboard-loss failsafe would drop the vehicle into LOITER."""
        while self._flying:
            try:
                await self.d.offboard.set_position_ned(self._sp)
            except Exception as e:
                if self._flying:
                    print("offboard setpoint stream error:", repr(e), flush=True)
            await asyncio.sleep(0.1)

    async def _go_to(self, n, e, alt, yaw=None, xy_tol=1.5, z_tol=1.0, secs=200):
        """Steer the live setpoint to (n, e, alt, yaw) and wait until the drone arrives (or `secs`).

        yaw=None means FACE THE DIRECTION OF TRAVEL (heading toward the target) so the drone flies
        forward, not sideways/backward — callers pass an explicit yaw only to align with the tag for
        landing. If the target is already within xy_tol (e.g. a pure descent) the current heading is
        kept rather than spinning to a meaningless bearing.

        Uses ONE position stream for the whole move: re-subscribing each iteration would starve the
        OFFBOARD setpoint RPC (the cause of a 2nd-sortie cruise stall). The stream is consumed at its
        native rate with no in-loop sleep — sleeping inside `async for` buffers samples so reads go
        stale and arrival is never detected — and is bounded by wall-clock time.
        """
        if yaw is None:
            pn, pe, _ = await self.pos()
            yaw = (math.degrees(math.atan2(e - pe, n - pn)) if math.hypot(n - pn, e - pe) > xy_tol
                   else await self.yaw())
        self._sp = PositionNedYaw(n, e, -alt, yaw)
        loop = asyncio.get_event_loop()
        t_end = loop.time() + secs
        stream = self.d.telemetry.position_velocity_ned()
        try:
            async for p in stream:
                pn, pe, pu = p.position.north_m, p.position.east_m, -p.position.down_m
                if math.hypot(pn - n, pe - e) < xy_tol and abs(pu - alt) < z_tol:
                    return
                if loop.time() > t_end:
                    return
        finally:
            await stream.aclose()

    # --- the three sortie phases ---
    async def _takeoff(self, cruise):
        """Lift off and hand to OFFBOARD at cruise altitude; set self._cruise_yaw. Returns ok/False.

        Lifts off in the BASIC AUTO.TAKEOFF mode (a clean vertical climb) and CONFIRMS the climb,
        retrying if it stalls. Two 2nd-sortie failure modes motivate the retry: the AUTO.PRECLAND
        mode left from the previous landing makes arm()+takeoff() a no-op (HOLD clears it), and the
        takeoff is occasionally accepted but never climbs. Once airborne it switches to OFFBOARD
        BEFORE reaching takeoff altitude (so PX4 doesn't default to AUTO.LAND) and climbs the rest
        of the way under offboard.
        """
        await self.d.action.set_takeoff_altitude(cruise)
        airborne = False
        for attempt in range(3):
            try:
                await self.d.action.hold()                             # clear any stale PRECLAND mode
                await asyncio.sleep(0.6)                                # let HOLD settle before arming
            except Exception:
                pass
            try:
                await self.d.action.arm()
            except Exception:
                pass
            await self.d.action.takeoff()
            base = (await self.pos())[2]
            for _ in range(24):                                        # ~12 s to clearly leave the ground
                if (await self.pos())[2] > base + 2.0:
                    airborne = True
                    break
                await asyncio.sleep(0.5)
            if airborne:
                break
            print(f"takeoff did not climb; retrying ({attempt + 1})", flush=True)
        if not airborne:
            return False
        self._cruise_yaw = await self.yaw()
        self._sp = PositionNedYaw(0.0, 0.0, -cruise, self._cruise_yaw)
        for _ in range(10):                                            # prime, then AUTO.TAKEOFF -> OFFBOARD
            await self.d.offboard.set_position_ned(self._sp)
        try:
            await self.d.offboard.start()
        except OffboardError as e:
            print("offboard start failed:", e, flush=True)
            return False
        self._flying = True
        asyncio.create_task(self._offboard_stream())
        for _ in range(240):                                           # offboard finishes the climb to cruise
            if (await self.pos())[2] >= cruise - 1.0:
                break
            await asyncio.sleep(0.5)
        return True

    async def _deliver(self, p, pub):
        """Cruise to the drop point, hover at the winch height, lower the parcel; return drop error (m).

        The lowering itself is done by parcel_manager: this signals it via /tmp/winch_go (with the
        target ground height in /tmp/winch_ground) and waits for /tmp/winch_done.
        """
        await self._go_to(p.dn, p.de, p.cruise, z_tol=2.0)             # face + fly to the drop point
        await self._go_to(p.dn, p.de, DELIV_ALT)                       # descend in place to winch height
        await asyncio.sleep(3)                                          # settle before lowering
        pub("lowering_cable")
        open(WINCH_GROUND, "w").write(str(p.deliv_gz))                 # parcel_manager lowers to this ground z
        open(WINCH_GO, "w").write("go")
        for _ in range(int(HOLD_S)):
            if os.path.exists(WINCH_DONE):
                break
            await asyncio.sleep(1)
        await asyncio.sleep(1.5)
        if os.path.exists(WINCH_GO):
            os.remove(WINCH_GO)
        xy = await asyncio.to_thread(parcel_xy)
        return math.hypot(xy[0] - p.dew, xy[1] - p.dnw) if xy else 99.0   # gz x=E,y=N vs delivery E,N

    async def _land(self, p, pub):
        """Climb back to cruise, transit to the landing station, align to the tag and precision-land.

        Returns (landed, land_error_m). The approach just needs to get the drone over the station at
        3 m; PX4's precision landing then centres to PLD_HACC_RAD (0.15 m) on the AprilTag before
        descending. The tag heading is a gz ENU yaw (CCW from East); the drone's NED heading is 90-enu.
        """
        await self._go_to(p.dn, p.de, p.cruise, z_tol=2.0)            # climb back to cruise at the drop
        await self._go_to(p.ln, p.le, p.cruise, z_tol=2.0)            # face + transit to the station
        await self._go_to(p.ln, p.le, 3.0, 90.0 - p.land_yaw_deg)     # align to the tag for landing
        pub("precision_landing")
        self._flying = False                                           # stop the offboard stream
        await asyncio.sleep(0.3)
        try:
            await self.d.offboard.stop()
        except OffboardError:
            pass
        for _ in range(5):
            request_precland()
            await asyncio.sleep(0.5)
        # Just wait for the landing: precland descends and PX4 auto-disarms on touchdown
        # (COM_DISARM_LAND). No forced disarm — if it never disarms, the sortie genuinely failed.
        landed, pn, pe = False, 0.0, 0.0
        for _ in range(120):
            pn, pe, _ = await self.pos()
            if not await self.armed():
                landed = True
                break
            await asyncio.sleep(0.5)
        return landed, math.hypot(pn - p.ln, pe - p.le)

    def _plan(self, req):
        """Resolve an order into a Plan, and tell the detector this sortie's landing station.

        The requested drop isn't guaranteed to be open ground, so it is snapped to the NEAREST
        delivery SITE (a drop pad on a real clearing) at a known height.
        """
        data = json.load(open(SITES))
        st = {s["id"]: s for s in data["stations"]}
        sites = data.get("sites", [])
        home, land = st[int(req["takeoff_id"])], st[int(req["landing_id"])]
        rw_e, rw_n = home["E"] + float(req["deliver_E"]), home["N"] + float(req["deliver_N"])
        site = min(sites, key=lambda s: math.hypot(s["E"] - rw_e, s["N"] - rw_n)) if sites else None
        dnw, dew, deliv_gz = ((site["N"], site["E"], site.get("Z", 0.0) + 0.2) if site
                              else (rw_n, rw_e, home.get("Z", 0.0)))   # +0.2 = drop-pad top
        # tell the (long-lived) detector THIS sortie's landing station (world N E id marker_z), so
        # repeated orders can land at DIFFERENT stations; marker_z is the tag's world height, which
        # the detector subtracts to get height-above-marker (gen_world is its single source of truth).
        marker_z = land.get("marker_z", land.get("Z", 0.0))
        open(LAND_TARGET, "w").write(f"{land['N']} {land['E']} {land['id']} {marker_z}")
        return Plan(cruise=float(req.get("cruise", 30)),
                    dn=dnw - home["N"], de=dew - home["E"], dnw=dnw, dew=dew, deliv_gz=deliv_gz,
                    ln=land["N"] - home["N"], le=land["E"] - home["E"],
                    land_yaw_deg=float(land.get("yaw_deg", 0.0)))

    async def fly(self, req, pub):
        """Fly one sortie for an order: takeoff -> deliver -> land, publishing status throughout."""
        p = self._plan(req)
        for f in (WINCH_GO, WINCH_DONE):                               # clear stale winch flags
            try:
                os.remove(f)
            except OSError:
                pass

        pub("launching")
        if not await self._takeoff(p.cruise):
            pub("failed", result="FAIL"); print("drone never lifted off", flush=True); return

        pub("enroute_delivery")
        derr = await self._deliver(p, pub)
        pub("delivered", deliver_err=round(derr, 2))

        pub("enroute_landing")
        landed, lerr = await self._land(p, pub)

        ok = derr < 2.0 and landed and lerr < 0.30
        pub("done" if ok else "failed",
            result="PASS" if ok else "FAIL", deliver_err=round(derr, 2), land_err=round(lerr, 2))
        print(f"SORTIE deliver_err={derr:.2f} land_err={lerr:.2f} result={'PASS' if ok else 'FAIL'}",
              flush=True)


async def serve():
    agent = FlightAgent()
    await agent.connect()
    loop = asyncio.get_running_loop()
    cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    async def handle(req):
        oid = req.get("order_id", "?")

        def pub(state, **kw):
            cli.publish(STATUS_TOPIC, json.dumps({"order_id": oid, "state": state, **kw}))
            print(f"[{oid}] -> {state} {kw}", flush=True)

        if agent.lock.locked():
            pub("rejected", reason="another delivery in progress"); return
        async with agent.lock:
            pub("accepted")
            try:
                await agent.fly(req, pub)
            except Exception as e:
                pub("failed", result="FAIL"); print("sortie error:", repr(e), flush=True)

    def on_connect(c, *a):
        print("agent connected to broker; waiting for", REQ_TOPIC, flush=True)
        c.subscribe(REQ_TOPIC)

    def on_message(c, u, msg):
        try:
            req = json.loads(msg.payload.decode())
        except Exception as e:
            print("bad request:", e); return
        asyncio.run_coroutine_threadsafe(handle(req), loop)

    cli.on_connect, cli.on_message = on_connect, on_message
    cli.connect(BROKER, 1883, 60)
    cli.loop_start()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(serve())
