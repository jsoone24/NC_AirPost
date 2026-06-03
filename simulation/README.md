# AirPost Simulation (PX4 v1.17 + Gazebo Harmonic, macOS)

Full physics simulation of the AirPost delivery mission: a quadcopter takes off from a station
helipad, cruises to a delivery point (redirected to the **nearest open drop-pad clearing** so the
parcel always lands on clear ground at a known height), **lowers the parcel gently on a motorized
winch cable and sets it down on the drop pad** (using a downward lidar rangefinder for ground
distance), releases it, retracts the cable, then flies on and lands at a landing station — all
driven over MAVLink, on the **real baylands park terrain**, and verified automatically.

**Status:** builds, runs, and flies natively on macOS (Apple Silicon). The **full mission**
(takeoff → forward-facing cruise → winch delivery → cruise to station → descend → **camera
AprilTag precision landing**) is implemented and **reproducible**: a random-takeoff/delivery/
landing harness passes **8/8** with delivery ≈0.2–0.7 m (within the 2 m circle) and **vision
precision landing 0.02–0.11 m**.

## Live order-driven demo (MQTT)  — `./run_airpost_live.sh`
The sim is exposed as a **delivery service over MQTT**, the same messaging interface the AirPost
stack uses. `run_airpost_live.sh` launches the GUI sim **on the real baylands park terrain** +
winch/parcel manager + AprilTag detector + the **persistent flight agent**
(`tests/airpost_flight_agent.py`). The agent holds **one long-lived MAVSDK connection** — the
slow part (connect + EKF GPS/home lock) is paid once at startup, so a delivery request lifts the
drone off ~immediately. The agent arms, lifts off in the **BASIC takeoff mode** (`AUTO.TAKEOFF`,
a clean vertical climb), hands to **OFFBOARD** once airborne, cruises to the delivery point
(**snapped to the nearest drop-pad clearing**), winches the parcel down, then cruises to the
landing station for the AprilTag precision land. **One MQTT order = one sortie**; each request
triggers a full autonomous sortie and streams live status:
```
./run_airpost_live.sh        # GUI sim + agent; wait for "AGENT READY"
# request a delivery (any client / the backend / the UI can publish this)
mosquitto_pub -t airpost/delivery/request -m \
  '{"order_id":"ORD-1","takeoff_id":9,"deliver_N":75,"deliver_E":75,"landing_id":7,"cruise":30}'
mosquitto_sub -t airpost/delivery/status     # live status
```
Verified end-to-end on baylands: `accepted → launching → enroute_delivery → lowering_cable →
delivered(~0.2 m) → enroute_landing → precision_landing → done(PASS, land ~0.03 m)`, with the
"delivery complete" email landing in MailHog. The Go backend/UI drive this by publishing
`airpost/delivery/request`.

**Repeated randomised experiments** — with `run_airpost_live.sh` already up, drive a sequence of
random orders (random delivery clearing + random landing station each time) and watch them all in
the GUI:
```
python run_experiments.py 5      # 5 back-to-back sorties; prints a PASS/FAIL summary
```
The drone spawns once, so the start station is fixed per session; the winch manager re-arms and
the detector retargets between sorties, so each order delivers and lands at a different place.

## The 8-step scenario (matches the real AirPost ops)
1. parcel loaded at the takeoff station → "send"  2. takeoff  3. cruise at a deconfliction
altitude band (forward-facing)  4. at the delivery site, hover and **winch the parcel down by
cable**, release on ground contact, retract  5. cruise to a landing station  6. over the
station's known GPS, descend to ~3 m  7. find the station's **unique AprilTag** and start
precision landing  8. vision-guided precise touchdown.

## Layout
```
simulation/
  PX4_MACOS_BUILD.md          build PX4 v1.17 SITL + gz on macOS (+ the patches)
  run_airpost_live.sh         LIVE demo: GUI sim (real baylands terrain) + winch manager + detector + persistent flight agent
  run_airpost_sim.sh          launch sim + GUI only (no agent)
  run_experiments.py          drive N random orders against a running live demo (PASS/FAIL summary)
  fly.sh                      fly one full delivery + precision landing (standalone GUI demo)
  fly10.sh                    10 m winch lowering demo (standalone GUI)
  probe_terrain.sh            ONE-TIME setup: launch a rendered baylands(visual) gz server and
                              run probe_terrain_lidar.py (see "Baylands terrain & clearings")
  probe_terrain_lidar.py      raycast a grid over the whole park, classify open ground vs. tree
                              canopy, and write open CLEARINGS -> baylands_clearings.json
  baylands_clearings.json     measured open-ground clearings (gz world frame x,y,z) the pads sit on
  gen_world.py                generate the world: N station helipads + M delivery drop pads placed
                              in the measured baylands clearings (real terrain heights), or on a flat field
  gen_markers.py              generate per-station UNIQUE-id ArUco tag models
  gz/worlds/airpost.sdf       40 station helipads + 20 drop pads in baylands clearings (auto-generated)
  gz/models/
    baylands_scenery/         baylands park, VISUAL-ONLY (collision stripped) so DART stays fast
    airpost_pad/              raised station helipad box (grey sides, green top) — sits on real ground
    airpost_drop_pad/         raised delivery drop box (grey sides, red top) — parcel lands on top
    terrain_probe/            teleportable downward gpu_lidar used by probe_terrain to map clearings
    airpost_delivery_drone/   x500 + down camera + down lidar + visual winch
    airpost_tag_<id>/         unique ArUco tag per station (id = station index)
    airpost_package/          the parcel
  tests/
    _simctl.sh                robust sim launch/teardown (per-pattern kill, lock cleanup)
    airpost_flight_agent.py   persistent MAVSDK agent: one MQTT order -> one autonomous sortie
    parcel_manager.py         winch/cable: positions the parcel under the winch + lowers it
    apriltag_detector.py      gz camera -> ArUco (unique id, proximity-gated) -> MAVLink LANDING_TARGET
    full_mission.py           the full 8-step mission + DELIVER_ERR/LAND_ERR checks
    winch10_kinematic.py      10 m winch lowering flight (used by fly10.sh)
    airpost_sites.json        station/site coordinates
    test_mission.py -> run_one_delivery.sh -> full_mission.py   headless pytest/CI path
```
Models/worlds resolve via `GZ_SIM_RESOURCE_PATH`; airframe `4022_gz_airpost_delivery_drone` spawns the drone.

## Precision landing (real-vision pipeline, mirrors Jetson Nano + Intel T265)
The detector subscribes to the drone's downward gz camera, detects the station's **unique
ArUco id** (each pad has its own id; it ignores all other pads, and a proximity gate to the
known GPS rejects neighbours), computes the marker's world position by **bearing × altitude**
(robust — avoids solvePnP planar-pose ambiguity), converts to PX4 local NED, and streams
MAVLink `LANDING_TARGET`. PX4 publishes `landing_target_pose` and **AUTO.PRECLAND** flies the
vision-guided touchdown. Verified as *vision* (not GPS): from a deliberate GPS offset the drone
is pulled onto the marker to a few cm.

## The 10 m winch delivery (works) — `fly10.sh` + `tests/winch10_kinematic.py` + `tests/parcel_manager.py`
The drone carries a **visible winch**: a reel/drum (with flanges) on a bracket at the airframe
**centre** (on the pitch/roll axis), a thin **cable** to a hook, and the small (9 cm) parcel
wound snug against the winch (a gz `DetachableJoint`). It **hovers at 10 m over the delivery
point and pays the cable out, lowering the parcel straight down to the ground, then releases
and reels in** — verified: drone tilt **≤0.5° throughout the lower**, parcel set down **≈0.1 m**
from the target.

How it is made stable (this is the crux). A real winch pays its line out under **motor
control**; a *physically free* swinging cable cannot be — the standard fix is a chain of links
+ a **slung-load (anti-sway) controller**
([UToronto Gazebo SITL slung-payload](https://flight.utias.utoronto.ca/wp-content/uploads/2025/03/Quadrotor_Based_Slung_Payload_Transportation__A_Gazebo_SITL_Simulation_Approach-1.pdf),
[ROS Answers: modeling a tether in Gazebo](https://answers.ros.org/question/285740/modeling-tether-cable-in-gazebo/)),
which PX4 lacks. Measured failure modes in this sim: a **rigid** prismatic cable diverges past
~1.5 m extension (the load's inertia couples into pitch/roll as `m·L²`); a **free universal/
sprung** cable flips the light airframe at spawn. So the winch is modelled the way a real one
works — **position-controlled pay-out**: the drone holds a rock-steady 10 m hover while
`tests/parcel_manager.py` (in-process gz transport) detaches the parcel and drives it down
the line via the gz `set_pose` service (which zeroes velocity each step → smooth, comes to rest
on the ground), drawing the cable as a vertical `/marker` cylinder. Two more hard-won details:
the winch must be **centred** (an off-centre/rear/front mount tips this short airframe nose-/
tail-down at spawn), and takeoff must be a **gentle straight-up lift then cruise** (a combined
up+sideways offboard jump tripped an attitude-fail takeoff abort).

## Mission sites — open clearings across baylands (seeded, reproducible)
On the baylands scene `gen_world.py` reads `baylands_clearings.json` (the open, tree-free clearings
the terrain probe measured — see below) and places **40 station helipads** and **20 delivery drop
pads** in those clearings, **shuffled and well-separated**, spread ACROSS THE WHOLE park. There is
**no synthetic flat floor** on baylands: each pad is a **raised box** standing on the real ground at
that clearing's measured terrain height (grey sides, coloured top), and the pad's own box top is the
landing/drop surface — so the vision precision-landing keeps a clean, known altitude reference per
pad even though the underlying terrain is uneven. Each station gets a **random AprilTag heading**.
The seed (`AIRPOST_SEED`, default 7) fixes the layout across runs; a trial then **randomly picks**
takeoff / delivery / landing among them. `SCENE=field` swaps the baylands map for a plain flat field
(a green plane + z=0 collision, faster for bulk trials), where the pads become thin flat markers.

### Baylands terrain & clearings (one-time setup)
The live demo flies on the **real OpenRobotics "baylands" park terrain** — the actual visual map,
not a flat green floor. Getting pads to sit correctly on that uneven, tree-covered wetland needs a
one-time survey of where the open ground is:

- **`gz/models/baylands_scenery/`** — the baylands park included **visual-only** (every
  `<collision>` stripped). The full fuel park's 400 MB+ collision mesh would drag DART to a crawl,
  and the drone never touches the park, so only the look is kept. The **pads** supply the landing
  collision. Mesh/textures resolve from the local Gazebo Fuel cache; download once:
  `gz fuel download -u "https://fuel.gazebosim.org/1.0/OpenRobotics/models/baylands"`.
- **`probe_terrain.sh`** → **`probe_terrain_lidar.py`** — the survey. `probe_terrain.sh` launches a
  rendered gz server holding the baylands visual model plus **`gz/models/terrain_probe/`** (a
  teleportable downward `gpu_lidar`). `probe_terrain_lidar.py` then **raycasts a world-frame grid
  over the whole park**, reading the range to the first visible surface below each grid point. It
  classifies each return as **open ground** (in a low band near the lowest surface), **tree canopy**
  (returns from higher up), or **off-map** (no return), keeps only points whose neighbours within a
  ~16 m rotor-span radius are also clear ground, then thins them to a well-separated set.
  Raycasting the **visual** surface is deliberate: earlier dropped-box probes tunnelled straight
  through the thin baylands collision mesh and reported wrong heights, so the probe measures the
  surface the user actually sees and there is no collision-tunnelling.
- **`baylands_clearings.json`** — the output: the selected open clearings as gz **world-frame**
  `{x (East), y (North), z (measured ground height)}`. `gen_world.py` consumes this and places the
  pads at those real heights.
- **`gz/models/airpost_pad/`** — the station helipad: a 5×5×0.4 m raised box (grey sides, green top)
  whose origin is the box bottom, so it stands up from the clearing ground; the AprilTag sits just
  above its top. Has collision (the drone lands on it).
- **`gz/models/airpost_drop_pad/`** — the delivery drop zone: a 2×2×0.3 m raised box (grey sides,
  **red top**); the parcel is winched down onto its top face.

## Run it
One-time: build PX4 SITL + gz per `PX4_MACOS_BUILD.md`. Run from a real Terminal
(Terminal.app/iTerm) so the GUI can open a window.

**The demo (order-driven, one command):** launch the GUI sim + winch manager + detector + the
persistent flight agent, then drive it from the UI or MQTT:
```bash
cd /Users/js/ws/NC_AirPost/simulation
./run_airpost_live.sh         # opens the Gazebo GUI; wait for "AGENT READY"
```
With the backend stack up (`cd AirPost_Backend && docker compose up -d`), register a parcel in the UI
(http://localhost:4173) — the order flows over MQTT to the agent and the drone takes off in
~2 s, flies, winch-delivers, and precision-lands. Or publish a request directly:
```bash
mosquitto_pub -t airpost/delivery/request -m \
  '{"order_id":"ORD-1","takeoff_id":1,"deliver_N":25,"deliver_E":25,"landing_id":1,"cruise":30}'
```

**Standalone single-shot demos** (no agent / no backend), each opens the GUI:
```bash
cd /Users/js/ws/NC_AirPost/simulation
./fly.sh                      # one full delivery + AprilTag precision landing
# ./fly.sh <deliver_N> <deliver_E> <landing_station_id> <cruise_alt>
./fly10.sh                    # 10 m winch lowering demo
```
Expected from `fly.sh`: `DELIVER_ERR=…` (within the 2 m circle) then `RESULT=PASS` with
`LAND_ERR<0.30 m`.

**Headless test path (pytest/CI):** `tests/test_mission.py` → `tests/run_one_delivery.sh` →
`tests/full_mission.py` runs a full sortie headless and asserts the delivery/landing tolerances.
It uses the **fast flat FIELD scene** (no heavy baylands mesh, no terrain probe); the live GUI
demo (`run_airpost_live.sh`) is the one that flies the real baylands terrain.

Options:
- `TAKEOFF=<id> LANDING=<id> ./run_airpost_live.sh` — pick takeoff/landing stations (default 1, the backend demo seed).
- `MQTT_BROKER=<host> ./run_airpost_live.sh` — point the agent at a non-local broker.
- `TAKEOFF=<id> ./run_airpost_sim.sh` (+ `TAKEOFF=<id> ./fly.sh`) — start on a different station.
- `SCENE=field ./run_airpost_sim.sh` — plain flat field instead of baylands scenery (faster).
- `AIRPOST_SEED=<n> ./run_airpost_sim.sh` — a different random station/site layout.
- `GUI=0 ./run_airpost_sim.sh` — headless, no window (for CI / servers).
- First baylands run downloads ~400 MB of scenery from Gazebo Fuel into `~/.gz/fuel`.

### GUI notes (macOS)
The launcher uses PX4's native launch: with `GUI=1` (default) it leaves `HEADLESS` unset, so
`px4-rc.gzsim` starts the Gazebo **server and the GUI** (`gz sim -g`) — the same path as on
Linux (there is no macOS-specific gate). The window shows the drone, parcel, pads, and markers
(Qt + Metal/ogre2). If no window appears: run from Terminal.app/iTerm (not SSH/tmux without a
display); rendering can be slow on Apple Silicon. A world must have **no inline `<plugin>`
entries** (they'd override PX4's `server.config` and drop sensors → "sensor missing" preflight).

## How the 10 m winch delivery works
The parcel is wound snug against the centre winch (`cable_end` hook + gz **DetachableJoint**)
for takeoff and cruise. The lowering is coordinated between the MAVSDK flight script and the
gz-side winch driver via two flag files:
- `tests/winch10_kinematic.py` (MAVSDK): gentle straight-up takeoff → cruise to the delivery
  point → hold a steady 10 m hover and touch `/tmp/winch_go` → wait for `/tmp/winch_done` → leave.
- `tests/parcel_manager.py` (gz transport): waits for `/tmp/winch_go`, **detaches** the
  parcel and drives it straight down the line at a fixed rate via the gz `set_pose` service
  (velocity-reset each step → smooth descent that rests on the ground), drawing the cable as a
  vertical cylinder `/marker` from the drum to the parcel, then touches `/tmp/winch_done`.
- `/airpost/package/release` (gz.msgs.Empty) detaches the parcel; `tests/full_mission.py` reuses
  the same hold-and-lower step inside the full 8-stage sortie.

The drone never carries a free-swinging pendulum, so the 10 m hover stays rock-steady
(tilt ≤0.5°) — see the section above for *why* this position-controlled model is used instead
of a physically free cable.

## gz transport note (macOS)
The sim runs with `GZ_IP=127.0.0.1`. To talk to it from another shell (topic pub, pose
queries), set `GZ_IP=127.0.0.1` and **unset `GZ_PARTITION`** (an empty string does NOT
match) — otherwise `gz topic`/`gz model` see nothing.

## Camera / next step: AprilTag precision landing
The downward camera streams on
`/world/airpost/model/airpost_delivery_drone_0/link/camera_link/sensor/imager/image`.
Next: feed it to an AprilTag/ArUco detector -> MAVLink `LANDING_TARGET` -> PX4 PrecLand,
so the final landing aligns to the station marker via vision (not just GPS).

## Multi-drone fleet — `run_airpost_fleet.sh`
Fly several drones in ONE Gazebo world via PX4 multi-vehicle SITL.

```bash
GUI=0 ./run_airpost_fleet.sh 8                 # boot 8 drones, then a self-contained take-off/land demo
GUI=0 SERVICE=1 ./run_airpost_fleet.sh 8       # boot 8 drones, then the MQTT delivery service (real orders)
```

It starts one gz server holding the world, launches N px4 instances with `PX4_GZ_STANDALONE=1`
(each spawns `airpost_delivery_drone_<i>` at the seeded station helipads — station ids 1..N — and
exposes MAVSDK on `udp 14540+i`), then runs the flight program:

- default → `fleet_demo.py`: all N drones take off, hover at staggered altitudes and land together.
- `SERVICE=1` → `fleet_service.py`: the MQTT delivery service. It holds one MAVSDK link per drone,
  consumes `airpost/delivery/request`, routes each order to its `drone_id` (instance `drone_id-51`),
  and flies takeoff → ferry-to-pickup (if needed) → drop → precision-land-at-nearest-station
  concurrently, each at the backend-assigned cruise band. It also streams live drone telemetry and
  (simulated) station sensors to Kafka `sensor-data` (`KAFKA_BROKER`, default `127.0.0.1:9092`) →
  logic-core → ES.

**The gz↔PX4 sensor bridge.** A *manually* started gz server must resolve PX4's stock models
itself, so `run_airpost_fleet.sh` adds `$PX4/Tools/simulation/gz/models` to `GZ_SIM_RESOURCE_PATH`
(the `x500_base` model carries the IMU/baro/mag). Without it every instance boots with
`gyro/accel/baro missing` and can never arm. The default fleet model is
`gz_airpost_delivery_drone`, which adds the per-instance downward camera and rangefinder used by
precision landing.
