# AirPost — Future Work / Roadmap

Where the project is today and where it can go. Status legend: ✅ done · 🔶 partial · ⬜ not started.

## Where we are now

- ✅ End-to-end delivery in **simulation** (PX4 SITL + Gazebo): order → dispatch → takeoff →
  winch parcel onto the drop pad → AprilTag **camera precision landing** → "delivered" email + live map.
- ✅ **Two flight-control paths** against the same PX4/gz (see `simulation/`):
  - `run_airpost_fleet.sh` — host-side **MAVSDK** service; verified **4 concurrent** drones, altitude
    bands + point-locks + serialized precision landing.
  - `run_ros2_fleet.sh` — the **real on-drone ROS 2 node** (`AirPost_Drone` branch `main`) driving PX4
    v1.17 over native **uXRCE-DDS** — the code that ships on the Jetson+Pixhawk. Verified **single
    drone** end-to-end: takeoff → cruise → **winch deliver at 10 m** → return → **3 m vision precision
    land on the pad**, streaming `data/<id>` telemetry, with **onboard local obstacle avoidance**
    (steered around an obstacle mid-cruise).
- ✅ **Architecture split implemented** (backend = fleet brain, drone = its own brain):
  - **Backend control tower** (`AirPost_Backend/application/fleet`): altitude-band assignment
    (dispatcher) + **live position monitoring & HOLD/CLEAR deconfliction** (unit-tested). Talks to each
    drone over MQTT (`data/<id>` up, `command/downlink/Hold/<id>` down).
  - **Onboard `drone_node`**: executes the assigned mission, **honours backend HOLD**, and does its
    **own local obstacle avoidance** (PX4's built-in Collision Prevention is Position-mode-only, so in
    OFFBOARD the companion owns avoidance — by PX4's design).
- ✅ **CI on every repo**: Drone (ros:humble build), Backend (Go), UI (npm), Sink/Station (py), and
  the umbrella (Go+npm+sim).
- 🔶 Precision-landing **accuracy** is centimetre-level when vision tracks continuously, but not yet
  *consistent* every flight (occasional ~0.7 m). **Hardening this is item #1 below.**
- ⬜ Real-hardware flight of the full loop (the sim mirrors it, but it hasn't flown on the real drone end-to-end).

---

## 0. Verify on Linux (the ROS 2 path's remaining checks)

The ROS 2 / uXRCE-DDS stack is built and single-drone-verified on macOS, but two things are deferred to
a native-Linux run (where DDS multicast and per-drone namespaces "just work" — on macOS we pin Fast-DDS
to the loopback and hit per-namespace discovery flakiness):

- ⬜ **Multi-drone ROS 2** — `run_ros2_fleet.sh N` is wired for N drones (each on DDS namespace
  `px4_<key>`), but only N=1 is verified on macOS. Run N≥2 on Linux and confirm telemetry + the backend
  **HOLD** actually fires when two drones converge (the deconfliction logic is unit-tested; the live
  sim loop is not).
- ⬜ **Unify dispatch onto the drone_node contract** — the backend dispatcher still publishes flight
  orders to `airpost/delivery/request` (for the MAVSDK `fleet_service.py`). The HOLD path already talks
  directly to `drone_node`; finish the picture by having the dispatcher publish the ROS 2 order
  (`deliver_ned` / `deliver_world` / `landing_world`, as `send_ros2_order.py` computes) to
  `command/downlink/ActuatorReq/<id>` so the backend drives `drone_node` directly, end to end.
- ✅ **Merged `catkin_ws` + `catkin_ws_build`** into one ROS 1 workspace on the `noetic` branch, with
  all five packages (drone_controller, realsense-ros, ros_comm, vision_to_mavros, apriltag_ros) as
  proper SSU-NC-22 submodules (the forks' `airpost` branches hold the in-use versions). `main`/`humble`
  are now ROS 2-only (`ros2_ws`); the ROS 1 workspace lives on `noetic`.

Reproduce on Linux: install ros-humble + px4_msgs (release/1.17), build `airpost_drone`, run the
Micro-XRCE-DDS Agent, then `./run_ros2_fleet.sh N`. See `AirPost_Drone/README.md`.

---

## 1. Precision landing — make it consistent (highest priority)

The mechanism works; the goal is **every** landing within a few centimetres, in any load/wind.

- **Vision-gated touchdown.** Don't let the autopilot disarm until the landing-target estimator
  confirms the drone is *both* low *and* centred (rel-pos < ~5 cm). Today, with search disabled, a
  marker loss makes PX4 "land in place" — great against drift, but it can settle off-centre if the
  tag is lost early. Gate the final descent on the live vision estimate instead.
- **Keep the marker in view to the ground.** Add a second, wider/short-range marker (a smaller
  AprilTag nested inside the big one) so the camera still sees a tag at <0.5 m when the big tag
  overflows the frame — classic nested-tag precision-landing trick.
- **Match the real sensor stack in sim.** The physical drone has an Intel RealSense T265 (visual
  odometry) feeding PX4; bringing VIO into the sim (not just GPS+rangefinder) would reduce the EKF
  drift that the vision currently has to fight.
- **Disturbance rejection.** Test and tune landing under simulated wind and ground effect.
- **Make accuracy a CI gate.** Run `tests/verify_truth.py` on a GPU-capable runner and fail the
  build if any landing exceeds, say, 15 cm ground-truth — so accuracy never silently regresses.

## 2. Close the sim-to-real gap (hardware)

- Fly the **full mission on the real drone** (Jetson + Pixhawk + RealSense), driven by the same
  backend + MQTT contract the sim uses.
- **Hardware-in-the-loop (HITL)** with the real Pixhawk before field flights.
- Field-calibrate the camera intrinsics, the AprilTag size, and the landing pad lamp.
- Validate the **winch** mechanism and parcel release on real hardware.

## 3. Smarter fleet & mission planning

- **Battery-aware dispatch:** choose a drone by battery + distance; auto return-to-base / land-at-
  nearest when low; model flight energy.
- **Charging / battery-swap stations** and a charge scheduler.
- **Real trajectory planning** instead of straight-line + fixed altitude bands: 3D path planning,
  geofencing, no-fly zones, and proper **air-traffic management** that scales past ~8 drones (the
  band scheme has limited vertical capacity).
- **Dynamic re-routing** for weather, wind, and newly-appearing obstacles.
- Multi-parcel / multi-leg sorties; order **scheduling, priority, and cancellation**.

## 4. Safety & reliability

- **Failsafes:** lost-link return-to-launch, low-battery land-at-nearest, GPS-denied behaviour,
  and avoidance of **non-cooperative** obstacles (today only the own-fleet is deconflicted).
- **Geofence + regulatory:** Remote ID, BVLOS compliance, altitude/area limits.
- **Redundant comms:** LTE + telemetry-radio failover.
- **Resilient dispatch state:** the "drone is busy" flag is currently in-memory (lost on restart);
  move it to the DB/Redis with a TTL so a crashed or offline drone frees itself automatically.

## 5. Backend / platform hardening (production)

- **Security:** replace seeded credentials with real auth (OAuth/JWT + roles), TLS everywhere,
  authenticated/ACL'd MQTT (it is anonymous today — see `SECURITY.md`), secrets management.
- **Observability:** Prometheus metrics + Grafana, distributed tracing, structured logs, alerting.
- **High availability:** DB backups + migrations, Kafka/Elasticsearch clustering, service replicas,
  health-based restarts; deploy to **Kubernetes**.
- **API maturity:** versioning, pagination, idempotency keys, rate limiting, OpenAPI contract tests.

## 6. Data & ML

- Use the sensor history for **predictive maintenance**, **weather-aware routing**, and **anomaly
  detection** (a drone/station behaving abnormally).
- **Vision upgrades:** learned landing-pad detection beyond AprilTag, obstacle detection, and camera
  **proof-of-delivery** (photo + parcel verification).
- **Demand forecasting** and fleet-positioning optimisation.

## 7. Product & UX

- The live map is **already Leaflet + OpenStreetMap** (source/dest/drone markers + path, fed by the
  health-check WebSocket). Polish it: drone heading/trail, ETAs, and clustering for many drones.
- **Customer experience:** SMS/push notifications, delivery photo, QR/signature confirmation, a
  recipient tracking page, and a mobile app.
- **Operator console:** fleet health board, alerting, and manual **takeover/override** of a drone.

## 8. Simulation & testing depth

- **Full-mission CI** on a GPU/llvmpipe runner (today it is gated behind `RUN_SIM`).
- **Scenario suite:** wind, GPS noise, sensor dropout, comms loss, many-drone congestion, pad blocked.
- **Scale tests** (10–20 drones) to find where the current deconfliction breaks and design the next one.

## 9. Sustainability & scale-out

- Solar-charged stations, battery-health tracking, energy dashboards.
- Multi-site / multi-region coordination; an **edge-cloud split** (on-drone autonomy + cloud-level
  fleet coordination).

---

### Suggested near-term order

1. **Precision-landing consistency** (#1) — finish what's in flight.
2. **Accuracy CI gate + scenario tests** (#8) — lock in quality so it can't regress.
3. **Security + observability basics** (#5) — minimum bar before any real deployment.
4. **Battery-aware dispatch + failsafes** (#3, #4) — the next features that make it field-credible.
5. **Real-hardware HITL → first real full mission** (#2).
