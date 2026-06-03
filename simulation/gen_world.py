#!/usr/bin/env python3
"""Generate the AirPost field world: N stations (helipad + AprilTag, each a DIFFERENT,
RANDOM heading) + M delivery zones (red 2 m circles), all at RANDOM, well-separated
positions (seeded -> reproducible). Stations double as takeoff & landing pads.

Scenes:
  baylands (default) - the Gazebo "baylands" park scenery (the real map) with a flat
                       operating ground at the open origin area, so stations/sites scatter
                       across the baylands world while the pads + precision landing keep a
                       consistent flat z=0 reference (terrain micro-relief would otherwise
                       break the vision precland's altitude reference).
  field              - a plain flat field (fast; used for bulk reproducibility trials).

World frame is ENU: x = East, y = North. Local NED used by the mission: north=y, east=x.
Writes gz/worlds/{OUTNAME}.sdf and tests/{OUTNAME}_sites.json (coords + tag yaws).
"""
import json, math, os, random, sys

HERE = os.path.dirname(os.path.abspath(__file__))
N_STATIONS = int(sys.argv[1]) if len(sys.argv) > 1 else 40
N_SITES = int(sys.argv[2]) if len(sys.argv) > 2 else 20
OUTNAME = sys.argv[3] if len(sys.argv) > 3 else "airpost"   # world name + file stem
TAKEOFF = int(sys.argv[4]) if len(sys.argv) > 4 else 0       # station the parcel spawns on
SCENE = (sys.argv[5] if len(sys.argv) > 5 else os.environ.get("AIRPOST_SCENE", "baylands")).lower()
SEED = int(os.environ.get("AIRPOST_SEED", "7"))             # fixed -> the world is stable across runs
rng = random.Random(SEED)

# operating region (origin-centred, open & flat) + flat-floor size. The baylands park sits at
# its native (205,155) so its dense scenery/water stays NE of the open operating field.
HALF = 150.0 if SCENE == "baylands" else 200.0
FLOOR = 340.0 if SCENE == "baylands" else 1500.0
PAD_Z = 0.02
PAD_BOX_H = 0.4
STN_SEP = 30.0   # min distance between stations (only one tag ever in camera view)
SITE_SEP = 22.0  # min distance between delivery circles
MIX_SEP = 12.0   # min distance station <-> site

def sample(n, others, minsep, self_sep):
    """rejection-sample n points in [-HALF,HALF]^2, >=self_sep apart and >=minsep from `others`."""
    pts = []
    tries = 0
    while len(pts) < n and tries < n * 4000:
        tries += 1
        x, y = rng.uniform(-HALF, HALF), rng.uniform(-HALF, HALF)
        if any(math.hypot(x - p[0], y - p[1]) < self_sep for p in pts):
            continue
        if any(math.hypot(x - o[0], y - o[1]) < minsep for o in others):
            continue
        pts.append((x, y))
    if len(pts) < n:
        raise SystemExit(f"could not place {n} points (got {len(pts)}); enlarge HALF or lower sep")
    return pts

# BAYLANDS REAL TERRAIN: place stations/sites on the actual park terrain, in the open/dry
# CLEARINGS probe_terrain.sh (probe_terrain_lidar.py) found by raycasting the real terrain
# (baylands_clearings.json: x=E, y=N, z=measured ground height, gz WORLD frame — no transform).
# Each pad sits at its clearing's real ground height. baylands is a wetland, so the tree-free,
# water-free open ground is limited; we place as many pads as there are safe clearings.
CLEARINGS = os.path.join(HERE, "baylands_clearings.json")
_clj = json.load(open(CLEARINGS)) if (SCENE == "baylands" and os.path.exists(CLEARINGS)) else {}
_cl = _clj.get("clearings", [])
use_clearings = SCENE == "baylands" and len(_cl) >= 10

if use_clearings:
    pts = [(round(c["x"], 2), round(c["y"], 2), round(c["z"], 2)) for c in _cl]
    rng.shuffle(pts)                                    # spread stations AND sites across the field
    n_st = min(N_STATIONS, max(8, len(pts) * 2 // 3))   # most clearings -> stations, the rest -> sites
    stn_pts, site_pts = pts[:n_st], pts[n_st:n_st + N_SITES]
    stations = [{"id": i, "E": x, "N": y, "Z": z, "yaw_deg": round(rng.uniform(0, 360), 1)}
                for i, (x, y, z) in enumerate(stn_pts)]
    sites = [{"id": j, "E": x, "N": y, "Z": z} for j, (x, y, z) in enumerate(site_pts)]
else:
    stn_pts = sample(N_STATIONS, [], 0.0, STN_SEP)
    site_pts = sample(N_SITES, stn_pts, MIX_SEP, SITE_SEP)
    stations = [{"id": i, "E": round(x, 2), "N": round(y, 2), "Z": 0.0, "yaw_deg": round(rng.uniform(0, 360), 1)}
                for i, (x, y) in enumerate(stn_pts)]
    sites = [{"id": j, "E": round(x, 2), "N": round(y, 2), "Z": 0.0} for j, (x, y) in enumerate(site_pts)]

def inc(uri, name, x, y, z, yaw=0.0):
    return (f'    <include>\n      <uri>model://{uri}</uri>\n      <name>{name}</name>\n'
            f'      <pose>{x} {y} {z} 0 0 {math.radians(yaw):.4f}</pose>\n    </include>\n')

body = []
if SCENE == "baylands":
    # the real baylands park as VISUAL-ONLY scenery (collision stripped — see
    # gz/models/baylands_scenery): the full fuel park's 400 MB+ collision mesh drags DART to a
    # crawl, and the drone never touches the park, so we keep only the look. Placed at its
    # native pose, NE of the open flat operating area where the stations/pads live.
    body.append(inc("baylands_scenery", "park", 205, 155, -1))
# On real terrain use RAISED BOX pads (grey sides + coloured top) that stand on the ground, with
# the tag/drone on the box top; on the flat field keep the thin flat pads. PAD_H/DROP_H = box top.
PAD_MODEL, PAD_H = ("airpost_pad", PAD_BOX_H) if use_clearings else ("helipad", 0.0)
DROP_MODEL, DROP_H = ("airpost_drop_pad", 0.2) if use_clearings else ("airpost_delivery_zone", 0.0)
for s in stations:
    # the pad and its tag share the SAME (random) heading, so the whole station is rotated together.
    # Record the tag's world height (marker_z) so the detector/agent need not hardcode the pad height.
    tag_z = s["Z"] + PAD_H + (0.01 if use_clearings else PAD_Z)
    s["marker_z"] = round(tag_z, 3)
    body.append(inc(PAD_MODEL, f"station_{s['id']}_pad", s["E"], s["N"], s["Z"] + (0.0 if use_clearings else PAD_Z - 0.01), s["yaw_deg"]))
    body.append(inc(f"airpost_tag_{s['id']}", f"station_{s['id']}_tag", s["E"], s["N"], tag_z, s["yaw_deg"]))
for st in sites:
    body.append(inc(DROP_MODEL, f"site_{st['id']}", st["E"], st["N"], st["Z"] + (0.0 if use_clearings else PAD_Z - 0.01)))
# static, collision-less parcel; parcel_manager.py repositions it every tick (rides under the
# forward winch, then lowered at delivery). Spawn it under the winch at the takeoff station.
body.append(inc("airpost_package", "airpost_package",
                stations[TAKEOFF]["E"] + 0.11, stations[TAKEOFF]["N"], stations[TAKEOFF]["Z"] + 0.13))
# optional observer camera (side view) for headless visual checks; OFF by default because the
# extra render pass makes the GUI choppy. Enable with AIRPOST_OBSERVER=1.
if os.environ.get("AIRPOST_OBSERVER") == "1":
    _ox, _oy = stations[TAKEOFF]["E"] + 2.2, stations[TAKEOFF]["N"]
    body.append(f'''    <model name="observer_cam"><static>true</static>
      <pose>{_ox} {_oy} 0.9 0 0.28 3.14159</pose>
      <link name="cam_link"><sensor name="obs" type="camera">
        <camera><horizontal_fov>1.1</horizontal_fov><image><width>1000</width><height>820</height></image>
          <clip><near>0.05</near><far>200</far></clip></camera>
        <always_on>1</always_on><update_rate>10</update_rate><topic>/observer/image</topic>
      </sensor></link>
    </model>
''')

floor_rgb = "0.32 0.42 0.26" if SCENE == "baylands" else "0.35 0.5 0.3"   # baylands-grass tint
# On real baylands terrain the helipads (their own collision) ARE the landing surfaces at the
# clearing ground heights, so there is NO synthetic flat floor. Otherwise emit the flat ground
# (a green visual plane + an infinite z=0 collision) for the plain field scene.
ground_plane = "" if use_clearings else (
    '    <!-- flat operating ground (field scene only): consistent z=0 landing reference. -->\n'
    '    <model name="ground_plane"><static>true</static><link name="link">\n'
    '      <collision name="collision"><geometry><plane><normal>0 0 1</normal><size>1 1</size></plane></geometry><surface><friction><ode/></friction></surface></collision>\n'
    f'      <visual name="visual"><geometry><plane><normal>0 0 1</normal><size>{FLOOR} {FLOOR}</size></plane></geometry>\n'
    f'        <material><ambient>{floor_rgb} 1</ambient><diffuse>{floor_rgb} 1</diffuse></material></visual>\n'
    '    </link></model>')
sdf = f'''<?xml version="1.0" encoding="UTF-8"?>
<!-- AUTO-GENERATED by gen_world.py: scene={SCENE}, seed={SEED}, {N_STATIONS} stations
     (random positions + random AprilTag headings) + {N_SITES} red delivery circles.
     No inline <plugin> (PX4 server.config supplies the sensor systems). -->
<sdf version="1.9">
  <world name="{OUTNAME}">
    <physics type="ode"><max_step_size>0.004</max_step_size><real_time_factor>1.0</real_time_factor><real_time_update_rate>250</real_time_update_rate></physics>
    <gravity>0 0 -9.8</gravity>
    <magnetic_field>6e-06 2.3e-05 -4.2e-05</magnetic_field>
    <atmosphere type="adiabatic"/>
    <scene><grid>false</grid><ambient>0.6 0.6 0.6 1</ambient><background>0.7 0.8 0.9 1</background><shadows>true</shadows></scene>
{ground_plane}
    <light name="sunUTC" type="directional"><pose>0 0 500 0 0 0</pose><cast_shadows>true</cast_shadows><intensity>1</intensity>
      <direction>0.001 0.625 -0.78</direction><diffuse>0.9 0.9 0.9 1</diffuse><specular>0.27 0.27 0.27 1</specular>
      <attenuation><range>2000</range><linear>0</linear><constant>1</constant><quadratic>0</quadratic></attenuation></light>
{''.join(body)}    <spherical_coordinates><surface_model>EARTH_WGS84</surface_model><world_frame_orientation>ENU</world_frame_orientation>
      <latitude_deg>37.412173071650805</latitude_deg><longitude_deg>-121.998878727967</longitude_deg><elevation>0</elevation></spherical_coordinates>
  </world>
</sdf>
'''

with open(os.path.join(HERE, f"gz/worlds/{OUTNAME}.sdf"), "w") as f:
    f.write(sdf)
with open(os.path.join(HERE, f"tests/{OUTNAME}_sites.json"), "w") as f:
    json.dump({"stations": stations, "sites": sites}, f, indent=2)
print(f"wrote {OUTNAME}.sdf (scene={SCENE}, seed={SEED}, {N_STATIONS} stations + {N_SITES} sites)")
