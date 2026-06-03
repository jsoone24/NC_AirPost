#!/usr/bin/env python3
"""MQTT-driven multi-drone delivery service.

Runs one flight controller per drone in the Gazebo fleet (see run_airpost_fleet.sh) and flies the
AirPost delivery contract concurrently: each backend order names the drone, where to pick the parcel
up, where to drop it and where to land, plus a cruise altitude band the control tower picked so two
airborne drones never share an altitude.

Order  (airpost/delivery/request):
  {order_id, drone_id, takeoff_id, pickup_id, deliver_N, deliver_E, landing_id, cruise}
  deliver_N/E are METRES from the pickup station to the drop point.
Status (airpost/delivery/status):
  {order_id, state, deliver_err, land_err, result}

Drone routing: backend drone id (51..) maps to fleet instance (drone_id - DRONE_ID_BASE - 1), the
same order run_airpost_fleet.sh spawns them (instance i parks on station i+1).

Run (started for you by run_airpost_fleet.sh):
  MQTT_BROKER=127.0.0.1 PX4-Autopilot/.venv/bin/python fleet_service.py [N]
"""
import asyncio
import datetime
import json
import math
import os
import random
import sys

import paho.mqtt.client as mqtt
from mavsdk import System
from mavsdk.offboard import OffboardError, PositionNedYaw
from pymavlink import mavutil

SIMDIR = os.path.dirname(os.path.abspath(__file__))
SITES = os.path.join(SIMDIR, "tests", "airpost_sites.json")
BROKER = os.environ.get("MQTT_BROKER", "127.0.0.1")
REQ_TOPIC = "airpost/delivery/request"
STATUS_TOPIC = "airpost/delivery/status"

# Backend drone node ids start at DRONE_ID_BASE+1 (seed.go: droneIDBase=50 -> drone 51 on station 1),
# so fleet instance i flies backend drone DRONE_ID_BASE+1+i.
DRONE_ID_BASE = 50
DELIVER_ALT = 6.0  # metres above ground to hover while dropping the parcel
LAND_ALT = 1.0     # metres to descend to before handing off to PX4's land
PRECLAND_ALT = 3.0
PRECISION_LANDING = os.environ.get("PRECISION_LANDING", "1") != "0"
LAND_TARGET_PREFIX = os.environ.get("LAND_TARGET_PREFIX", "/tmp/airpost_land_target")

# Telemetry sink: drone + station sensor readings are produced to this Kafka topic, where logic-core
# consumes them, maps values onto each node's sensor schema (seed.go) and indexes into Elasticsearch.
KAFKA_BROKER = os.environ.get("KAFKA_BROKER", "127.0.0.1:9092")
KAFKA_TOPIC = "sensor-data"
STATION_COUNT = 8     # seeded stations 1..8 publish environmental sensors
TELE_PERIOD = 3.0     # seconds between telemetry frames
# Geo origin shared with seed.go, so station sensor lat/lon match the backend's node coordinates.
ORIGIN_LAT, ORIGIN_LON, EARTH_R = 37.5, 127.0, 6371000.0


def en_to_latlon(east, north):
    rad = math.pi / 180
    lat = ORIGIN_LAT + (north / EARTH_R) / rad
    lon = ORIGIN_LON + (east / (EARTH_R * math.cos(ORIGIN_LAT * rad))) / rad
    return lat, lon


def stations():
    """Map station id -> {E, N, Z, ...} from the shared sites file."""
    return {s["id"]: s for s in json.load(open(SITES))["stations"]}


class Drone:
    """One drone's persistent MAVSDK link. Its local NED origin is its spawn station, fixed for the
    drone's whole life, so every world target is converted relative to that spawn (a drone that has
    flown to another station still measures NED from where it booted)."""

    def __init__(self, inst, spawn_n, spawn_e, spawn_z):
        self.inst = inst
        self.spawn_n, self.spawn_e = spawn_n, spawn_e
        self.spawn_z = spawn_z  # spawn ground elevation; the local NED altitude origin
        self.d = System(port=50060 + inst)
        self._mav = mavutil.mavlink_connection(
            f"udpout:127.0.0.1:{18570 + inst}",
            source_system=255,
            source_component=190,
        )
        self.land_target_file = f"{LAND_TARGET_PREFIX}_{inst}"
        self.lock = asyncio.Lock()
        self._sp = PositionNedYaw(0.0, 0.0, 0.0, 0.0)
        self._flying = False
        self.ready_ok = False

    @staticmethod
    async def _first(stream):
        async for x in stream:
            return x

    async def pos(self):
        p = (await self._first(self.d.telemetry.position_velocity_ned())).position
        return p.north_m, p.east_m, -p.down_m

    async def yaw(self):
        return (await self._first(self.d.telemetry.attitude_euler())).yaw_deg

    async def armed(self):
        return await self._first(self.d.telemetry.armed())

    def ned(self, world_n, world_e):
        """World (N, E) metres -> this drone's local NED (north, east)."""
        return world_n - self.spawn_n, world_e - self.spawn_e

    def agl(self, station_z, height):
        """Local up-altitude `height` m above the given station's ground. The NED altitude origin is
        the SPAWN ground, so descending to a fixed altitude over a station at a different terrain
        height would miss the ground (hover above it, or drive into it and never disarm). Correcting
        by the station's elevation makes the descent land cleanly regardless of where it set off from."""
        return (station_z - self.spawn_z) + height

    async def telemetry(self):
        """Snapshot for the sink: [lat, long, alt, velocity, batteryPer, done] (seed.go drone schema)."""
        pos = await self._first(self.d.telemetry.position())
        v = (await self._first(self.d.telemetry.position_velocity_ned())).velocity
        bat = await self._first(self.d.telemetry.battery())
        speed = math.sqrt(v.north_m_s ** 2 + v.east_m_s ** 2 + v.down_m_s ** 2)
        pct = bat.remaining_percent
        pct = pct * 100.0 if pct <= 1.0 else pct  # mavsdk reports 0..1; publish a percentage
        flying = 1.0 if self.lock.locked() else 0.0
        return [round(pos.latitude_deg, 7), round(pos.longitude_deg, 7),
                round(pos.relative_altitude_m, 2), round(speed, 2), round(pct, 1), flying]

    async def _await_connected(self):
        async for s in self.d.core.connection_state():
            if s.is_connected:
                return

    async def _await_flyable(self):
        async for h in self.d.telemetry.health():
            if h.is_global_position_ok and h.is_home_position_ok and h.is_local_position_ok:
                return

    async def connect(self):
        await self.d.connect(system_address=f"udpin://0.0.0.0:{14540 + self.inst}")
        # Bound every wait with a hard timeout: if an instance's mavsdk_server failed to start
        # (gz model-spawn race under heavy fleet load), its telemetry stream never yields, so an
        # un-bounded `async for` would deadlock the whole fleet's startup. On timeout this drone is
        # left not-ready (its orders are rejected) and the rest of the fleet still serves.
        try:
            await asyncio.wait_for(self._await_connected(), timeout=60)
            await asyncio.wait_for(self._await_flyable(), timeout=90)
            self.ready_ok = True
        except asyncio.TimeoutError:
            print(f"drone {self.inst}: not ready (timed out connecting), serving degraded", flush=True)
            return
        float_params = [
            ("PLD_SRCH_ALT", PRECLAND_ALT), ("PLD_HACC_RAD", 0.15), ("PLD_FAPPR_ALT", 0.1),
            ("PLD_SRCH_TOUT", 20.0), ("MPC_XY_VEL_MAX", 8.0), ("MPC_XY_CRUISE", 8.0),
            ("MPC_Z_VEL_MAX_UP", 3.0), ("MPC_Z_VEL_MAX_DN", 3.0),
            ("MPC_LAND_SPEED", 1.0), ("MPC_LAND_ALT1", 3.0), ("MPC_LAND_ALT2", 1.0),
            ("COM_DISARM_LAND", 0.4), ("EKF2_MIN_RNG", 0.03), ("EKF2_RNG_A_HMAX", 8.0),
            ("EKF2_RNG_A_VMAX", 2.0), ("EKF2_RNG_POS_X", 0.22), ("EKF2_RNG_POS_Y", 0.0),
            ("EKF2_RNG_POS_Z", 0.05),
        ]
        int_params = [("PLD_MAX_SRCH", 3), ("EKF2_RNG_CTRL", 1)]
        for k, v in float_params:
            try:
                await self.d.param.set_param_float(k, v)
            except Exception:
                pass
        for k, v in int_params:
            try:
                await self.d.param.set_param_int(k, v)
            except Exception:
                pass
        print(f"drone {self.inst}: ready (NED origin @ N{self.spawn_n:.0f} E{self.spawn_e:.0f})", flush=True)

    async def _offboard_stream(self):
        while self._flying:
            try:
                await self.d.offboard.set_position_ned(self._sp)
            except Exception:
                pass
            await asyncio.sleep(0.1)

    async def _go_to(self, n, e, alt, yaw=None, xy_tol=2.0, z_tol=1.5, secs=200):
        """Steer the live setpoint to (n, e, alt) and wait until arrival or `secs`. yaw=None faces the
        direction of travel so the drone flies forward."""
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

    async def _takeoff(self, cruise):
        """Lift off and hand to OFFBOARD at the cruise band. Returns True once at cruise."""
        await self.d.action.set_takeoff_altitude(min(cruise, 20.0))
        airborne = False
        for attempt in range(3):
            try:
                await self.d.action.hold()
                await asyncio.sleep(0.6)
                await self.d.action.arm()
            except Exception:
                pass
            await self.d.action.takeoff()
            base = (await self.pos())[2]
            for _ in range(24):
                if (await self.pos())[2] > base + 2.0:
                    airborne = True
                    break
                await asyncio.sleep(0.5)
            if airborne:
                break
            print(f"drone {self.inst}: takeoff stalled, retry {attempt + 1}", flush=True)
        if not airborne:
            return False
        cy = await self.yaw()
        self._sp = PositionNedYaw(0.0, 0.0, -cruise, cy)
        for _ in range(10):
            await self.d.offboard.set_position_ned(self._sp)
        try:
            await self.d.offboard.start()
        except OffboardError:
            return False
        self._flying = True
        asyncio.create_task(self._offboard_stream())
        for _ in range(240):
            if (await self.pos())[2] >= cruise - 1.0:
                break
            await asyncio.sleep(0.5)
        return True

    def _write_land_target(self, station):
        marker_z = station.get("marker_z", station.get("Z", 0.0))
        tmp = f"{self.land_target_file}.tmp"
        with open(tmp, "w") as f:
            f.write(f"{station['N']} {station['E']} {station['id']} {marker_z}")
        os.replace(tmp, self.land_target_file)

    def _request_precland(self):
        # MAVSDK has no direct PRECLAND action. This matches the working single-drone path and sends
        # PX4 custom mode AUTO.PRECLAND to this instance's GCS MAVLink port (18570 + instance).
        self._mav.mav.command_long_send(
            self.inst + 1, 1, mavutil.mavlink.MAV_CMD_DO_SET_MODE, 0,
            1, 4, 9, 0, 0, 0, 0,
        )

    async def _land_here(self, ln, le, landing):
        """Align over the landing station and wait for PX4's genuine touchdown disarm.

        Default path is precision landing: the per-drone detector streams LANDING_TARGET to this PX4
        instance, and AUTO.PRECLAND centres on the station tag before touchdown. PRECISION_LANDING=0
        keeps the old AUTO.LAND path for diagnostics, still without any forced disarm."""
        land_alt = self.agl(landing.get("Z", 0.0), LAND_ALT)
        if PRECISION_LANDING:
            self._write_land_target(landing)
            yaw = 90.0 - float(landing.get("yaw_deg", 0.0))
            await self._go_to(ln, le, self.agl(landing.get("Z", 0.0), PRECLAND_ALT),
                              yaw=yaw, xy_tol=1.0, z_tol=0.8)
        else:
            await self._go_to(ln, le, land_alt, z_tol=0.8)
        self._flying = False
        await asyncio.sleep(0.3)
        try:
            await self.d.offboard.stop()
        except OffboardError:
            pass
        pn, pe = ln, le
        for i in range(180):  # up to ~90 s for PX4 to land + auto-disarm on touchdown
            if i % 16 == 0:    # (re)issue mode command in case an ack was dropped under fleet load
                try:
                    if PRECISION_LANDING:
                        self._request_precland()
                    else:
                        await self.d.action.land()
                except Exception:
                    pass
            pn, pe, _ = await self.pos()
            if not await self.armed():
                return True, math.hypot(pn - ln, pe - le)
            await asyncio.sleep(0.5)
        return False, math.hypot(pn - ln, pe - le)

    async def fly(self, req, st, pub):
        """Fly one order: takeoff -> (ferry to pickup) -> drop -> land at nearest station."""
        cruise = float(req.get("cruise", 30))
        takeoff = st[int(req["takeoff_id"])]
        pickup = st[int(req["pickup_id"])]
        landing = st[int(req["landing_id"])]
        drop_n = pickup["N"] + float(req["deliver_N"])
        drop_e = pickup["E"] + float(req["deliver_E"])

        pub("launching")
        if not await self._takeoff(cruise):
            pub("failed", result="FAIL")
            return

        # Ferry leg: if the drone lifted off somewhere other than the parcel's source, fly there first.
        if int(req["takeoff_id"]) != int(req["pickup_id"]):
            pub("enroute_pickup")
            pn, pe = self.ned(pickup["N"], pickup["E"])
            await self._go_to(pn, pe, cruise, z_tol=2.0)

        pub("enroute_delivery")
        dn, de = self.ned(drop_n, drop_e)
        await self._go_to(dn, de, cruise, z_tol=2.0)                       # cruise to the drop point
        await self._go_to(dn, de, self.agl(pickup.get("Z", 0.0), DELIVER_ALT))  # descend to winch height
        await asyncio.sleep(2)
        derr = math.hypot(*[a - b for a, b in zip(await self.pos(), (dn, de))][:2])
        pub("delivered", deliver_err=round(derr, 2))

        pub("enroute_landing")
        ln, le = self.ned(landing["N"], landing["E"])
        await self._go_to(ln, le, cruise, z_tol=2.0)   # climb back + transit to the landing station
        if PRECISION_LANDING:
            pub("precision_landing")
        landed, lerr = await self._land_here(ln, le, landing)

        ok = derr < 3.0 and landed
        pub("done" if ok else "failed", result="PASS" if ok else "FAIL",
            deliver_err=round(derr, 2), land_err=round(lerr, 2))
        print(f"drone {self.inst}: deliver_err={derr:.2f} land_err={lerr:.2f} "
              f"result={'PASS' if ok else 'FAIL'}", flush=True)


def station_sensor(s):
    """Simulated environmental reading for a station: [temperature, humidity, light, lat, long, alt]
    (seed.go station schema). Gazebo carries no ambient temp/humidity/light sensors, so these are
    synthesised here; the lat/lon are the station's real coordinates."""
    lat, lon = en_to_latlon(s["E"], s["N"])
    return [round(20 + 6 * random.random(), 1),     # temperature C
            round(40 + 25 * random.random(), 1),    # humidity %
            round(250 + 300 * random.random(), 0),  # light lux
            round(lat, 7), round(lon, 7), round(s.get("Z", 0.0), 2)]


async def telemetry_loop(drones, st, producer):
    """Stream live drone telemetry and station sensors to the Kafka sink at TELE_PERIOD."""
    while True:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for dr in drones:
            try:
                vals = await dr.telemetry()
                producer.produce(KAFKA_TOPIC, json.dumps(
                    {"node_id": f"DRO{DRONE_ID_BASE + 1 + dr.inst}", "values": vals, "timestamp": ts}
                ).encode())
            except Exception:
                pass
        for sid in range(1, STATION_COUNT + 1):
            if sid in st:
                producer.produce(KAFKA_TOPIC, json.dumps(
                    {"node_id": f"STA{sid}", "values": station_sensor(st[sid]), "timestamp": ts}
                ).encode())
        producer.poll(0)
        await asyncio.sleep(TELE_PERIOD)


def make_producer():
    """Kafka producer for the telemetry sink, or None if the client/broker is unavailable (flights
    must run even without the analytics pipeline)."""
    try:
        from confluent_kafka import Producer
        return Producer({"bootstrap.servers": KAFKA_BROKER})
    except Exception as e:
        print(f"telemetry sink disabled ({e!r})", flush=True)
        return None


async def serve(n):
    st = stations()
    # Instance i parks on station i+1 (matches run_airpost_fleet.sh spawn order and seed.go).
    drones = [Drone(i, st[i + 1]["N"], st[i + 1]["E"], st[i + 1].get("Z", 0.0)) for i in range(n)]
    # Stagger the connects: each System() spawns its own mavsdk_server, and starting 8 at once races
    # (a server occasionally fails to come up). A short gap between them makes startup reliable.
    tasks = []
    for dr in drones:
        tasks.append(asyncio.create_task(dr.connect()))
        await asyncio.sleep(2)
    await asyncio.gather(*tasks)
    flyable = sum(dr.ready_ok for dr in drones)
    print(f"fleet service: {n} drones connected ({flyable} flyable); waiting for orders", flush=True)

    producer = make_producer()
    if producer is not None:
        asyncio.create_task(telemetry_loop(drones, st, producer))
        print(f"telemetry sink: streaming to Kafka {KAFKA_BROKER} topic {KAFKA_TOPIC}", flush=True)

    loop = asyncio.get_running_loop()
    cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    async def handle(req):
        oid = req.get("order_id", "?")

        def pub(state, **kw):
            cli.publish(STATUS_TOPIC, json.dumps({"order_id": oid, "state": state, **kw}))
            print(f"[{oid}] {state} {kw}", flush=True)

        inst = int(req.get("drone_id", 0)) - DRONE_ID_BASE - 1
        if not 0 <= inst < len(drones):
            pub("rejected", reason=f"no such drone {req.get('drone_id')}")
            return
        dr = drones[inst]
        if not dr.ready_ok:
            pub("rejected", reason=f"drone {req.get('drone_id')} not ready")
            return
        if dr.lock.locked():
            pub("rejected", reason="drone busy")
            return
        async with dr.lock:
            pub("accepted")
            try:
                await dr.fly(req, st, pub)
            except Exception as e:
                pub("failed", result="FAIL")
                print(f"drone {inst}: sortie error {e!r}", flush=True)

    def on_connect(c, *a):
        c.subscribe(REQ_TOPIC)
        print(f"fleet service connected to broker; subscribed {REQ_TOPIC}", flush=True)

    def on_message(c, u, msg):
        try:
            req = json.loads(msg.payload.decode())
        except Exception:
            return
        asyncio.run_coroutine_threadsafe(handle(req), loop)

    cli.on_connect, cli.on_message = on_connect, on_message
    cli.connect(BROKER, 1883, 60)
    cli.loop_start()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(serve(int(sys.argv[1]) if len(sys.argv) > 1 else 2))
