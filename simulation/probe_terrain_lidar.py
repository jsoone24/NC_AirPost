#!/usr/bin/env python3
"""Map the baylands VISUAL terrain by RAYCAST (no physics, so no collision tunnelling): teleport a
downward gpu_lidar across a world-frame grid and read the range to the first surface below. That
surface is the rendered ground (open clearing), tree canopy (obstructed), or nothing (off-map).
Outputs the open, tree-free ground points (gz world frame) where helipads can sit on the visible
field. Run inside a rendered gz server with the baylands_scenery visual model (see the .sh).
"""
import json, math, os, sys, time
from gz.transport13 import Node
from gz.msgs10.pose_pb2 import Pose
from gz.msgs10.boolean_pb2 import Boolean
from gz.msgs10.laserscan_pb2 import LaserScan

WORLD = "probe"
HERE = os.path.dirname(os.path.abspath(__file__))
X0, X1, STEP = -160, 560, 16          # grid over the whole park footprint
Y0, Y1 = -160, 460
PROBE_Z = 120.0                        # ray origin, above the tallest trees
node = Node()

scan = {"r": None}
node.subscribe(LaserScan, "/terrain_probe/scan", lambda m: scan.__setitem__("r", m.ranges[0] if m.ranges else None))

def move(x, y):
    req = Pose(); req.name = "terrain_probe"
    req.position.x = float(x); req.position.y = float(y); req.position.z = PROBE_Z
    req.orientation.w = 1.0
    node.request(f"/world/{WORLD}/set_pose", req, Pose, Boolean, 1000)

def height_at(x, y):
    scan["r"] = None
    move(x, y)
    t = time.time()
    while time.time() - t < 0.5 and scan["r"] is None:
        time.sleep(0.02)
    r = scan["r"]
    if r is None or r != r or r >= 290:    # no return (off-map / out of range)
        return None
    return PROBE_Z - r                      # world z of the first surface below

# 1) raycast the grid
hmap = {}
xs = list(range(X0, X1 + 1, STEP)); ys = list(range(Y0, Y1 + 1, STEP))
print(f"raycasting {len(xs)*len(ys)} grid points...", flush=True)
for x in xs:
    for y in ys:
        h = height_at(x, y)
        if h is not None:
            hmap[(x, y)] = h
print(f"got {len(hmap)} surface returns", flush=True)
if not hmap:
    print("NO returns — lidar not rendering?"); sys.exit(1)

# 2) classify. Ground sits in a low band; tree canopy returns high. An OPEN clearing = a surface
# near ground level whose grid neighbours (±STEP) are ALSO near ground (no canopy within a rotor
# radius). Ground band: take returns below (min + canopy gap).
hs = sorted(hmap.values())
gmax = hs[0] + 6.0                          # ground is within ~6 m of the lowest surface; trees are higher
print(f"surface z {hs[0]:.1f}..{hs[-1]:.1f}; ground band <= {gmax:.1f}", flush=True)
ground = {xy for xy, h in hmap.items() if h <= gmax}
clear = [{"x": float(x), "y": float(y), "z": round(hmap[(x, y)], 3)}
         for (x, y) in ground
         if all((x + dx, y + dy) in ground for dx, dy in ((STEP, 0), (-STEP, 0), (0, STEP), (0, -STEP)))]
print(f"ground cells {len(ground)} -> clearings (clear {STEP} m radius): {len(clear)}", flush=True)

# 3) spread well-separated pads across the field
clear.sort(key=lambda c: (c["x"], c["y"]))
def thin(sep):
    out = []
    for c in clear:
        if all(math.hypot(c["x"] - o["x"], c["y"] - o["y"]) >= sep for o in out):
            out.append(c)
    return out
chosen = []
for sep in (40, 32, 26, 22, 18):
    chosen = thin(sep)
    if len(chosen) >= 65:
        break
out = {"metadata": {"frame": "gz world (x=E,y=N,z=up)", "method": "gpu_lidar raycast",
                    "n_returns": len(hmap), "n_clearings": len(clear), "n_selected": len(chosen),
                    "surface_z": [round(hs[0], 1), round(hs[-1], 1)]},
       "clearings": chosen}
json.dump(out, open(os.path.join(HERE, "baylands_clearings.json"), "w"), indent=1)
print(f"SELECTED {len(chosen)} clearings; examples: {chosen[:4]}", flush=True)
