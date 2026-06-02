#!/usr/bin/env python3
"""Fly N drones concurrently in one Gazebo world (multi-vehicle demo).

Each PX4 instance i exposes MAVSDK on udp 14540+i (see run_airpost_fleet.sh). This connects to
all of them (each via its own mavsdk_server port), arms, takes off to a staggered altitude,
hovers, and lands — all at once — proving several drones simulate and are controlled together.

Run:  PX4-Autopilot/.venv/bin/python fleet_demo.py [N]
"""
import asyncio
import sys
from mavsdk import System


async def fly_one(i: int):
    tag = f"drone {i}"
    d = System(port=50060 + i)  # distinct mavsdk_server per drone
    await d.connect(system_address=f"udpin://0.0.0.0:{14540 + i}")
    async for s in d.core.connection_state():
        if s.is_connected:
            break
    async for h in d.telemetry.health():
        if h.is_global_position_ok and h.is_home_position_ok and h.is_local_position_ok:
            break
    alt = 8.0 + 2.0 * i  # staggered altitudes so the fleet doesn't stack
    await d.action.set_takeoff_altitude(alt)
    await d.action.arm()
    await d.action.takeoff()
    print(f"{tag}: armed + taking off to {alt:.0f} m", flush=True)

    for _ in range(60):  # wait until clearly airborne
        async for p in d.telemetry.position_velocity_ned():
            up = -p.position.down_m
            break
        if up > 5.0:
            break
        await asyncio.sleep(0.5)
    print(f"{tag}: airborne ({up:.1f} m), hovering", flush=True)
    await asyncio.sleep(8)

    await d.action.land()
    for _ in range(60):
        if not await _first(d.telemetry.armed()):
            break
        await asyncio.sleep(0.5)
    print(f"{tag}: landed", flush=True)


async def _first(stream):
    async for x in stream:
        return x


async def main(n: int):
    print(f"flying {n} drones concurrently...", flush=True)
    await asyncio.gather(*[fly_one(i) for i in range(n)])
    print("fleet demo complete.", flush=True)


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 2))
