#!/usr/bin/env python3
"""10 m winch delivery - MAVSDK side (the drone only HOVERS; tests/parcel_manager.py pays the
cable out concurrently). The load is never a free pendulum on the drone, so the hover stays
rock-steady - which solves the slung-load instability (rigid/sprung cables diverge ~1.5-1.8 m).

Flow: arm -> take off -> fly to the delivery point at HOVER_ALT -> hold a steady hover while
the winch lowers the parcel (HOLD_S) -> verify the parcel is on the ground near the target ->
climb away. Mirrors a real winch sortie (the drone brakes-for-hold while the winch deploys).

Args: HOME_N HOME_E DELIVER_N DELIVER_E   (world coords; takeoff station = home)
Env:  HOVER_ALT (default 10)   HOLD_S (seconds to hold for the lower, default 24)
"""
import asyncio, math, os, re, subprocess, sys
from mavsdk import System
from mavsdk.offboard import OffboardError, PositionNedYaw

GZ = {**os.environ, "GZ_IP": "127.0.0.1"}; GZ.pop("GZ_PARTITION", None)
ALT = float(os.environ.get("HOVER_ALT", "10"))
HOLD = float(os.environ.get("HOLD_S", "24"))
HN, HE, DNw, DEw = map(float, sys.argv[1:5])
DN, DE = DNw - HN, DEw - HE                                  # local NED target (origin = home)

def parcel_world():
    try:
        out = subprocess.run(["gz", "model", "-m", "airpost_package", "-p"], env=GZ,
                             capture_output=True, text=True, timeout=4).stdout
        m = re.search(r"\[\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*\]", out)
        return (float(m.group(1)), float(m.group(2)), float(m.group(3))) if m else None
    except Exception: return None

async def main():
    d = System(); await d.connect(system_address="udp://:14540")
    async for s in d.core.connection_state():
        if s.is_connected: break
    async for h in d.telemetry.health():
        if h.is_global_position_ok and h.is_home_position_ok: break
    print("connected + home lock", flush=True)
    await d.action.arm()
    for k, v in [("MPC_XY_VEL_MAX", 8.0), ("MPC_XY_CRUISE", 8.0), ("MPC_Z_VEL_MAX_DN", 3.0),
                 ("MPC_Z_VEL_MAX_UP", 3.0), ("MPC_TKO_SPEED", 2.0)]:
        try: await d.param.set_param_float(k, v)
        except Exception: pass

    yaw = math.degrees(math.atan2(DE, DN)); sp = [PositionNedYaw(0, 0, -1.5, yaw)]; run = [True]
    async def pump():
        while run[0]:
            await d.offboard.set_position_ned(sp[0]); await asyncio.sleep(0.1)
    async def cur_alt():
        async for p in d.telemetry.position(): return p.relative_altitude_m
    async def pos():
        async for p in d.telemetry.position_velocity_ned(): return p.position.north_m, p.position.east_m
    async def att():
        async for a in d.telemetry.attitude_euler(): return math.hypot(a.roll_deg, a.pitch_deg)

    # start offboard with a GENTLE straight-up setpoint (no simultaneous sideways move, which
    # was kicking the airframe into an attitude-fail takeoff abort)
    await d.offboard.set_position_ned(sp[0])
    try: await d.offboard.start()
    except OffboardError as e: print("RESULT=FAIL (offboard)", e, flush=True); return 1
    asyncio.create_task(pump())

    # 1) lift straight up over the takeoff spot to ALT (gentle), 2) THEN cruise to the delivery
    climb_tilt = 0.0
    sp[0] = PositionNedYaw(0, 0, -ALT, yaw)
    print("lifting off (straight up)", flush=True)
    for _ in range(60):
        a = await cur_alt(); climb_tilt = max(climb_tilt, await att())
        if a >= ALT - 0.6: break
        await asyncio.sleep(0.5)
    print(f"TAKEOFF_MAX_TILT={climb_tilt:.1f} deg", flush=True)
    sp[0] = PositionNedYaw(DN, DE, -ALT, yaw)
    print(f"-> delivery ({DNw:.1f},{DEw:.1f}) at {ALT:.0f} m", flush=True)
    for _ in range(90):
        n, e = await pos()
        if math.hypot(n - DN, e - DE) < 1.2: break
        await asyncio.sleep(0.5)
    print(f"hover at delivery; winch lowering the parcel", flush=True)
    try: os.remove("/tmp/winch_done")
    except OSError: pass
    open("/tmp/winch_go", "w").write("go")                   # tell winch_lower we are hovering here
    for _ in range(int(HOLD)):                               # hold until winch_lower signals done
        if os.path.exists("/tmp/winch_done"): break
        await asyncio.sleep(1)
    await asyncio.sleep(1.5)                                 # let it settle on the ground
    try: os.remove("/tmp/winch_go")
    except OSError: pass

    pp = parcel_world() or (0, 0, 99)
    derr = math.hypot(pp[0] - DEw, pp[1] - DNw)              # gz pose is (x=E, y=N)
    print(f"DELIVER_ERR={derr:.2f} parcel_z={pp[2]:.2f}", flush=True)
    ok = derr < 2.0 and pp[2] < 0.3
    print("RESULT=" + ("PASS" if ok else "FAIL"), flush=True)

    sp[0] = PositionNedYaw(DN, DE, -ALT, yaw); await asyncio.sleep(3)   # climb away
    run[0] = False; await asyncio.sleep(0.3)
    try: await d.offboard.stop()
    except OffboardError: pass
    try: await d.action.land()
    except Exception: pass
    return 0 if ok else 1

try:
    sys.exit(asyncio.run(asyncio.wait_for(main(), timeout=200)))
except asyncio.TimeoutError:
    print("RESULT=FAIL (timeout)", flush=True); sys.exit(1)
