# AirPost — Production-Grade Showcase Roadmap

**Target tier:** Production-*grade* showcase (Tier A) — polished GUI, bugs fixed,
security/privacy solid, the **full delivery mission simulated in physics** (no hardware).
**Simulator:** PX4 **v1.17.0 stable** + **Gazebo Harmonic (gz-sim8)**.
**Sim host:** attempt native macOS first; Linux/Docker fallback (and CI target).

## Status — what is built (current)

The vertical spine is DONE and verified end to end: register a parcel in the UI → backend
persists + dispatches over MQTT → the drone's onboard agent flies a full sortie on the **real
baylands park terrain** (take off from a station helipad in an open clearing → cruise → winch the
parcel down onto a delivery drop pad → cruise → AprilTag precision landing) → a "delivery
complete" email lands in MailHog → the order is trackable. Typical errors: delivery ~0.2 m,
landing ~0.03 m.

- **Phase 1 backend:** haversine geo, panic guards, server-generated order numbers, FK
  constraints, email-map mutex, unit/API tests — build/test/vet pass.
- **Phase 2 security:** JWT auth + roles on every route, CORS locked to the UI origin, secrets
  env-driven, reverse-SSH backdoor removed, git history purged locally. *(Operator still must
  force-push the rewritten history and rotate the exposed Gmail/Kakao keys.)*
- **Phase 3 sim:** real baylands terrain; stations + drop pads in tree-free / water-free
  **clearings across the whole park**, mapped by a gpu_lidar raycast survey (`probe_terrain.sh`);
  raised box pads at the real ground height; downward-camera AprilTag → `LANDING_TARGET` → PX4
  precision landing; winch delivery; persistent MAVSDK flight agent (one sortie per MQTT order);
  one-command live demo `run_airpost_live.sh`; headless pytest path on the fast field scene.
- **Phase 4 GUI:** Vite + React + TS + Tailwind `ui-next` (register / track / admin).
- **Phase 5 packaging:** full `docker compose up -d`, CI workflow, README/RUNBOOK rewritten
  around the new flow, Kibana dashboard ndjson.

Deferred (Phase 6 hardening): real TLS on REST, per-device MQTT auth/ACL/TLS, PII encryption at
rest, gz sim in CI, and back-to-back-sortie performance on the heavy baylands GUI mesh (single
orders are reliable; sustained random stress slows the renderer). The checklists below are the
original plan and are kept for reference.

> The entire user case is modeled and physically simulated: a **station**, a **drone**
> with a downward camera, a **package** attached by a **winch/detachable joint**, and
> **camera-based AprilTag landing** — verified across the whole sequence
> **takeoff → deliver (release package) → return (precision-land)**.

> Non-goals (explicitly deferred): real outdoor flights, BVLOS, regulatory approval
> (항공안전법), commercial public service, physical winch hardware. These are Tier B/C
> and live in "Phase 6 — Future work."

### Simulation building blocks (already installed / shipped — verified)

| Need | Asset |
|---|---|
| Simulator | Gazebo Harmonic `gz-sim8` 8.12.0 (brew) — rendering + camera sensors |
| Drone + downward camera | PX4 model `x500_mono_cam_down` |
| Fiducial marker (AprilTag-class) | PX4 model `arucotag` + world `aruco.sdf` |
| Package release / winch | `gz-sim8-detachable-joint-system` (DetachableJoint plugin) |
| Precision landing | PX4 `landing_target` / PrecLand (commander) |

> macOS risk: PX4's `gz_bridge` has a known macOS build failure on v1.17 *alpha*
> (issue #27026). Try v1.17.0 *stable* native; fall back to PX4-in-Docker (Linux),
> headless camera via software rendering (llvmpipe) — which is also the CI path.

---

## North star — definition of done

A reviewer can run `docker compose up`, open the new UI, register a parcel, watch a
**PX4 SITL drone fly takeoff→goto→land in QGroundControl**, see the live track on the UI
map, and receive a "delivered" email (captured by a dev mailserver) — with **auth on every
endpoint, no plaintext secrets, and `go test ./...` green in CI.**

---

## The one vertical spine (build this first, everything hangs off it)

```
UI (register parcel)
  → backend /regist/delivery  (pick station + route)
  → MQTT (authenticated)      (flight path)
  → drone_controller.py       (MAVROS commands)
  → PX4 SITL                  (22 takeoff → 16 goto → 21 land)  ── visualized in QGC
  → "delivered" event
  → logic-core email action   (captured by MailHog in dev)
  → UI tracking map           (live drone coords via health-check WS)
```
The winch/cable release and the AprilTag landing are **fully simulated in physics** (gz
DetachableJoint + downward camera + PrecLand) — not mocked or replayed.

---

## Phase 0 — Foundations & sim bring-up  *(≈ week 0–1)*  — ✅ SIM BRING-UP DONE

**Goal:** unblock everything; get PX4 + Gazebo flying empirically.

> **Done:** PX4 **v1.17.0** SITL + **Gazebo Harmonic** build, run, and **fly** natively on
> macOS (Apple Silicon). Takeoff smoke test climbs to target altitude. Build patches +
> steps in `simulation/PX4_MACOS_BUILD.md`. (Remaining Phase-0 items below: docker-compose.)

- [ ] Repo hygiene: stop tracking `AirPost_UI/ui/node_modules`, `npm-debug.log`, `trace.out`; add `.gitignore` entries.
- [ ] Write **backend `docker-compose.yml`** (mysql, kafka, zookeeper, elasticsearch, kibana, mailhog) — currently missing.
- [ ] **Check out PX4 `v1.17.0` stable tag** (local tree is on a dev commit) and pin it.
- [ ] **PX4 + Gazebo bring-up spike** (timeboxed ~1 day):
  - Toolchain risk: cmake 4.3.3 + Python 3.14.5 are newer than PX4 supports → use a **pyenv 3.11 venv**.
  - Try native: `make px4_sitl gz_x500_mono_cam_down` (gz Harmonic already installed); XQuartz for the gz GUI, or run headless + QGroundControl over UDP.
  - If `gz_bridge` fails to build (issue #27026): **fallback to PX4-in-Docker (Linux)**, gz headless with software rendering (llvmpipe), QGC on the Mac over the network.
- [ ] Confirm the **downward camera topic** publishes and is viewable (`gz topic`/RViz/QGC).

**Exit:** `x500_mono_cam_down` flies in a gz world, its camera streams, it's reachable from QGroundControl, and `docker compose up` brings up backend deps.

---

## Phase 1 — Backend correctness  *(≈ week 1–2)*

**Goal:** fix the real bugs, lock each with a test.

- [ ] `GetShortestPathStation(tagid)` — actually filter by `tagid`; guard empty slice (no `pl[0]` panic).
- [ ] Nil-pointer panics: handlers returning `err.Error()` on `err==nil` branches (`RegistDelivery` len==0 / droneid==-1; `UnregistNode` len>0). Return proper 4xx/5xx.
- [ ] Replace Euclidean lat/lon distance with **haversine** (or wire up Naver routing behind a key).
- [ ] Reconcile `OrderNum` (string in model vs int in `GetDeliveryByOrderNum`); **server-generate** unique order numbers (don't trust client).
- [ ] Re-enable drone path persistence (`RegistLogic` is commented out in `RegistDelivery`).
- [ ] Add FK constraints (currently commented out in `init.go`); remove `runtime/trace` from logic-core prod path; add mutex to email interval map (data race).
- [ ] **Tests:** L1 unit (usecases, haversine, panic guards) + L3 API E2E happy path via `httptest`; L2 DB integration via `testcontainers-go`.

**Exit:** `go test ./...` green; E2E test proves the correct station is chosen for a delivery.

---

## Phase 2 — Security & privacy  *(≈ week 2–3)*

**Goal:** close the deployment-blocking issues; do it before the spine hardens to avoid rework.

- [ ] **AuthN/AuthZ:** JWT (or sessions); split **admin** (node CRUD, logic) vs **user** (own deliveries) roles; middleware on every route.
- [ ] **Tracking/PII authz:** can't read a delivery without owning it (or admin); order numbers random + unguessable.
- [ ] **Transport:** TLS on the REST API; restrict CORS to the real UI origin (fix the invalid `*` + `AllowCredentials:true`); bind internal services to private interfaces, not `0.0.0.0`.
- [ ] **MQTT:** per-device credentials + ACLs + TLS so only the backend can publish flight paths.
- [ ] **Secrets:** move Gmail/Naver/DB creds to env/secret-manager; remove the empty-pass-in-source pattern; rotate any exposed keys; **delete `reverse_ssh_continuous.sh`** from the drone.
- [ ] **Data:** encrypt PII at rest; add a retention policy; DB user with a real password (no empty-pass fallback).

**Exit:** a written security checklist passes; unauthenticated/cross-user access is blocked in tests.

---

## Phase 3 — Full-flow physics simulation  *(≈ week 3–5)* — the centerpiece — ✅ MOSTLY DONE

**Goal:** the entire user case modeled and physically simulated, then **automatically checked**.

> **Done (`simulation/`):** baylands-based worlds (`airpost.sdf` lite + `airpost_baylands.sdf`)
> with takeoff station, delivery point, landing station; delivery-drone model (x500 +
> downward camera + **DetachableJoint** parcel); PX4 airframe `4022_gz_airpost_delivery_drone`.
> `tests/full_mission.py` flies **takeoff → delivery point → release parcel → landing station →
> land** and **passes automated checks** (parcel rests ~0.3 m from the drop point).
> **Remaining:** wire the camera → AprilTag detection → PX4 PrecLand (3b below); connect to
> the real backend over MQTT instead of the standalone mission script (3c).

### 3a. World & models
- [ ] **AirPost world** (`airpost.sdf`): a **source station** pad, a **delivery tag** (`arucotag`) on the ground, and a **return/landing station** carrying an AprilTag marker. (Swap the ArUco texture for the AirPost AprilTag for fidelity.)
- [ ] **Drone:** `x500_mono_cam_down` (downward camera for marker detection).
- [ ] **Package + winch:** a package link attached to the drone via the **DetachableJoint** plugin; model the winch lowering (prismatic joint or scripted descent) then **detach = release**. Package free-falls and rests on the ground.

### 3b. Perception → precision landing
- [ ] Feed the **gz downward-camera topic** into a marker detector (AprilTag/ArUco) → emit MAVLink `LANDING_TARGET`. Reuse AirPost's existing `apriltag` + `vision_to_mavros` pipeline, sourced from the sim camera instead of RealSense.
- [ ] Enable PX4 **PrecLand** so the drone aligns to and lands on the station AprilTag.

### 3c. Mission orchestration (driven by the real backend)
- [ ] Wire backend → authenticated MQTT → `drone_controller.py` → PX4 (MAVSDK/pymavlink). The mission comes from `RegistDelivery`, not a hardcoded script.
- [ ] Full sequence: **arm + takeoff at source station (22) → fly to delivery tag (16) → descend, lower winch, release package → fly to landing station → precision-land on AprilTag (21) → disarm.**
- [ ] "Delivered" event → logic-core email action → captured by **MailHog**; live coords → health-check WS → UI map.

### 3d. Automated verification (the "checked" requirement)
- [ ] A **pytest + MAVSDK** harness runs the mission headless and asserts:
  - each waypoint reached within tolerance;
  - **package detached and final pose within X m of the delivery tag** (read gz model pose);
  - **precision-land final XY error < threshold**, then landed + disarmed;
  - whole mission completes under a timeout.
- [ ] Runs headless in CI (Linux container, llvmpipe software rendering for the camera).

**Exit:** register a parcel in the UI → the modeled drone takes off, flies, **drops the package on the delivery tag, returns, and precision-lands on the station AprilTag** in Gazebo → delivered email in MailHog → track on the UI map. The pytest mission test passes in CI.

---

## Phase 4 — GUI redesign  *(parallel, ≈ week 2–5)*

**Goal:** make it *look* production-grade (half the perceived value of a showcase).

- [ ] Mockups first (admin dashboard + user flow), then build on **Vite + React + TS + Tailwind + shadcn/ui** (retire old CRA + dated admin template, drop committed `node_modules`).
- [ ] **Admin dashboard:** node management (drone/station/tag CRUD), live health-check status, logic/rule editor, embedded Kibana panels.
- [ ] **User flow:** register parcel form → issued tracking number → **live map tracking** (drone + src/dest markers, route line) → delivery status timeline.
- [ ] Real-time updates over the health-check WebSocket; clean empty/error/loading states; responsive.

**Exit:** a polished, cohesive UI demoable end-to-end against the real backend.

---

## Phase 5 — Observability, packaging, docs  *(≈ week 5–6)*

- [ ] Kibana dashboards for sensor streams (the data-viz selling point).
- [ ] **`docker compose up`** brings up the *entire* stack (deps + 3 Go services + UI + sim hook).
- [ ] **CI:** `go test ./...` + `go vet`/lint + frontend build on every push.
- [ ] Rewrite README around the new flow; record a fresh demo video; ship an architecture diagram and a one-page "how to run the demo" script.

**Exit:** clean clone → `docker compose up` → documented demo runs.

---

## Phase 6 — Future work (explicitly deferred)

Real hardware HIL (Pixhawk + Jetson + winch bench), tethered → free flight, geofencing &
fail-safes (RTL on low battery / link loss), command signing, BVLOS, and **regulatory
approval** — none are coding-only and all are out of Tier A scope. Listed so reviewers see
the boundary is intentional.

---

## Risk register

| Risk | Mitigation |
|---|---|
| PX4 `gz_bridge` build breaks on macOS (issue #27026) | Try **v1.17.0 stable** native; **PX4-in-Docker (Linux)** fallback decided in Phase 0 |
| cmake 4.x / Python 3.14 too new for PX4 | pyenv **3.11** venv |
| Camera rendering on Apple Silicon flaky | Run gz **headless with llvmpipe** (software rendering) — also the CI path |
| Winch physics | Use shipped **DetachableJoint** plugin (already installed) — real physics, no mock |
| Scope explosion (huge project) | **Spine-first**: one vertical end-to-end before breadth |
| Old frontend deps / CVEs | **Rebuild** on Vite/modern stack, don't patch CRA |
| Security done last → rework | Phase 2 **before** spine hardens |

---

## Suggested sequencing

Phases **0 → 1 → 2 → 3** are the critical path. **Phase 4 (GUI)** runs in parallel from
week 2. **Phase 5** closes it out. Each checkbox should land with its test/doc so nothing
regresses.
