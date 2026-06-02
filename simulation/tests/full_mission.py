#!/usr/bin/env python3
"""Full AirPost delivery + precision-landing mission (the complete scenario).

  takeoff -> cruise (forward-facing) to delivery site -> hover & winch the parcel down
  gently, release, retract -> cruise to a landing station -> descend to ~3 m over its
  known GPS -> find AprilTag -> AUTO.PRECLAND -> precise touchdown.

The detector (apriltag_detector.py) must be running. Drone takes off from its spawn point.
Usage: full_mission.py <home_N> <home_E> <deliver_N> <deliver_E> <land_N> <land_E> [cruise_alt]
       (all world ENU coords; home is the takeoff station, which is the local-NED origin)
Prints DELIVER_ERR / LAND_ERR / verdict for the trial harness to parse.
"""
import asyncio, math, os, subprocess, sys
from mavsdk import System
from mavsdk.offboard import OffboardError, PositionNedYaw
from pymavlink import mavutil

# all coords are WORLD ENU N,E; PX4 local-NED origin = home (takeoff) so convert by subtracting home
HN, HE = float(sys.argv[1]), float(sys.argv[2])      # home (takeoff) world N,E
DNw, DEw = float(sys.argv[3]), float(sys.argv[4])    # delivery site world N,E
LNw, LEw = float(sys.argv[5]), float(sys.argv[6])    # landing station world N,E
CRUISE = float(sys.argv[7]) if len(sys.argv) > 7 else 30.0
DN, DE = DNw - HN, DEw - HE                          # delivery in local NED
LN, LE = LNw - HN, LEw - HE                          # landing in local NED
GZ = {**{k: v for k, v in os.environ.items() if k != "GZ_PARTITION"}, "GZ_IP": "127.0.0.1"}
mav = mavutil.mavlink_connection("udpout:127.0.0.1:18570", source_system=255, source_component=190)

def set_precland(): mav.mav.command_long_send(1,1,mavutil.mavlink.MAV_CMD_DO_SET_MODE,0,1,4,9,0,0,0,0)
def parcel_pose():
    """parcel world pose via `gz model` (the parcel is the static, kinematic airpost_package
    positioned by tests/parcel_manager.py)."""
    import re
    try: out=subprocess.run(["gz","model","-m","airpost_package","-p"],env=GZ,timeout=4,capture_output=True,text=True).stdout
    except Exception: return None
    m=re.search(r"\[\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*\]",out)
    return (float(m.group(1)),float(m.group(2)),float(m.group(3))) if m else None

async def main() -> int:
    d = System(); await d.connect(system_address="udpin://0.0.0.0:14540")
    async def wait_conn():
        async for s in d.core.connection_state():
            if s.is_connected: return
    async def wait_health():
        async for h in d.telemetry.health():
            if h.is_global_position_ok and h.is_home_position_ok: return
    try:
        await asyncio.wait_for(wait_conn(), timeout=45)
        # GUI sim runs in real time, so the first EKF GPS/home lock can take ~30 s; give it room
        await asyncio.wait_for(wait_health(), timeout=120)
    except asyncio.TimeoutError:
        print("DELIVER_ERR=99\nLAND_ERR=99 landed=False\nRESULT=FAIL (no GPS/home lock)", flush=True); return 1
    print("ready", flush=True)
    # report the spawn attitude (read before arming) so the test harness can assert the drone
    # spawns level; a tilted spawn corrupts takeoff and the whole mission
    async for att in d.telemetry.attitude_euler():
        print(f"SPAWN_RPY={att.roll_deg:.2f},{att.pitch_deg:.2f},{att.yaw_deg:.2f}", flush=True); break
    for k,v in [("PLD_SRCH_ALT",3.0),("PLD_HACC_RAD",0.15),("PLD_FAPPR_ALT",0.1),
                ("PLD_SRCH_TOUT",20.0),("PLD_MAX_SRCH",3),("MPC_XY_VEL_MAX",6.0),
                ("MPC_XY_CRUISE",5.0),("MPC_Z_VEL_MAX_DN",3.0),("MPC_Z_VEL_MAX_UP",3.0),
                # fast touchdown + immediate disarm (default COM_DISARM_LAND=2 s is the lag)
                ("MPC_LAND_SPEED",1.0),("COM_DISARM_LAND",0.4)]:
        try:
            await (d.param.set_param_float(k,v) if isinstance(v,float) else d.param.set_param_int(k,v))
        except Exception as e: print(f" param {k}: {e}")

    sp=[PositionNedYaw(0,0,-CRUISE,0)]; run=[True]
    async def pump():
        while run[0]:
            await d.offboard.set_position_ned(sp[0]); await asyncio.sleep(0.1)
    async def pos():
        async for p in d.telemetry.position_velocity_ned(): return p.position.north_m,p.position.east_m,-p.position.down_m
    async def cur_alt():
        async for p in d.telemetry.position(): return p.relative_altitude_m
    async def descend_to(n, e, alt, yaw_deg, tol=1.0, to=45):
        sp[0] = PositionNedYaw(n, e, -alt, yaw_deg)
        for _ in range(to):
            a = await cur_alt()
            if a <= alt + tol: return a
            await asyncio.sleep(1)
        return a
    async def goto(n,e,alt, tol=1.5, to=120):
        cn,ce,_=await pos()
        hdg=math.degrees(math.atan2(e-ce, n-cn))           # face travel direction (DEGREES)
        sp[0]=PositionNedYaw(n,e,-alt,hdg)
        async def r():
            async for p in d.telemetry.position_velocity_ned():
                if math.hypot(p.position.north_m-n,p.position.east_m-e)<tol: return
        try: await asyncio.wait_for(r(),timeout=to)
        except asyncio.TimeoutError: pass

    # 1-2. takeoff
    try: os.remove("/tmp/winch_go")
    except OSError: pass
    try: os.remove("/tmp/winch_done")
    except OSError: pass
    # PX4 takeoff is a clean STRAIGHT-UP climb to cruise altitude; wait only until we actually
    # reach it (not a fixed blind sleep), then switch to offboard and cruise horizontally.
    await d.action.set_takeoff_altitude(CRUISE); await d.action.arm(); await d.action.takeoff()
    for _ in range(60):
        if await cur_alt() >= CRUISE - 1.0: break
        await asyncio.sleep(0.5)
    await d.offboard.set_position_ned(sp[0])
    try: await d.offboard.start()
    except OffboardError as e: print("offboard fail",e); return 1
    asyncio.create_task(pump())

    # 3-4. cruise to delivery site (forward-facing), hover at the WINCH altitude, then signal the
    # kinematic winch (tests/parcel_manager.py) to lower the parcel to the ground on its cable.
    # The drone hovers HIGH over the delivery point and never descends to the ground itself; the
    # parcel_manager owns the static parcel + draws the cable, and touches /tmp/winch_done when the
    # parcel is on the ground. (A physically-jointed load tips the light airframe in DART.)
    DELIV_ALT = float(os.environ.get("DELIV_ALT", "10"))
    HOLD = float(os.environ.get("HOLD_S", "45"))
    print("-> delivery site", flush=True)
    await goto(DN,DE,CRUISE)
    da = await descend_to(DN, DE, DELIV_ALT, sp[0].yaw_deg)   # hover at the winch altitude
    await asyncio.sleep(4)                                    # settle to a stable hover
    print(f"hover {da:.1f} m over delivery; winch lowering the parcel", flush=True)
    try: os.remove("/tmp/winch_done")
    except OSError: pass
    open("/tmp/winch_go","w").write("go")                    # tell parcel_manager we are hovering here
    grounded = False
    for _ in range(int(HOLD)):                                # wait for the lower to finish
        if os.path.exists("/tmp/winch_done"): grounded = True; break
        await asyncio.sleep(1)
    await asyncio.sleep(1.5)                                  # let it settle on the ground
    try: os.remove("/tmp/winch_go")
    except OSError: pass
    p = await asyncio.to_thread(parcel_pose)
    derr = math.hypot(p[0]-DEw, p[1]-DNw) if p else 99       # gz pose is (x=E, y=N); target is (N,E)
    print(f"DELIVER_ERR={derr:.2f} grounded={grounded}", flush=True)
    sp[0]=PositionNedYaw(DN,DE,-CRUISE, sp[0].yaw_deg); await asyncio.sleep(4)  # climb away

    # 5-6. cruise to landing station, descend to 3 m over its known GPS
    print("-> landing station", flush=True)
    await goto(LN,LE,CRUISE)
    cn,ce,_=await pos(); hdg=math.degrees(math.atan2(LE-ce,LN-cn))
    a = await descend_to(LN, LE, 3.0, hdg)               # GPS-descend to ~3 m over the known station
    print(f"at {a:.1f} m over station", flush=True)

    # 7-8. precision landing
    print(">>> AUTO.PRECLAND", flush=True)
    run[0]=False; await asyncio.sleep(0.3)
    try: await d.offboard.stop()
    except OffboardError: pass
    for _ in range(5): set_precland(); await asyncio.sleep(0.5)
    landed=False
    async def w():
        async for armed in d.telemetry.armed():
            if not armed: return
    try: await asyncio.wait_for(w(),timeout=150); landed=True
    except asyncio.TimeoutError: pass
    fn=fe=0.0
    async for p in d.telemetry.position_velocity_ned(): fn,fe=p.position.north_m,p.position.east_m; break
    falt = await cur_alt()
    landed = landed or (falt < 0.5)        # on the ground even if the disarm flag lagged
    lerr=math.hypot(fn-LN,fe-LE)
    print(f"LAND_ERR={lerr:.2f} landed={landed} alt={falt:.2f}", flush=True)
    ok = (derr<2.0) and landed and lerr<0.30
    print("RESULT=" + ("PASS" if ok else "FAIL"), flush=True)
    return 0 if ok else 1

async def _guarded():
    try:
        return await asyncio.wait_for(main(), timeout=420)
    except asyncio.TimeoutError:
        print("DELIVER_ERR=99\nLAND_ERR=99 landed=False\nRESULT=FAIL (mission timeout)", flush=True); return 1

sys.exit(asyncio.run(_guarded()))
