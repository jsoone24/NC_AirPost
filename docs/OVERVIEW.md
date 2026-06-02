# AirPost — What it is, in plain terms

## One sentence
**AirPost is an autonomous drone parcel-delivery system:** a user registers a parcel in a web
app, the backend assigns a drone and route, and a drone flies the parcel to the destination,
**winches it down, and precision-lands** — with live tracking, sensor logging, and email
notifications along the way.

## The vertical spine (one delivery, end to end)
1. **Register** — a user fills in a delivery (source station, destination tag) in the web UI.
2. **Dispatch** — the backend picks a drone, computes the route, and publishes a delivery
   request over **MQTT**.
3. **Fly** — the drone takes off, cruises (facing forward), and **lowers the parcel on a winch
   cable** onto the drop pad at the destination.
4. **Land** — it flies to a landing station and uses a **downward camera + AprilTag** to
   precision-land (vision, not just GPS).
5. **Observe** — drones/stations stream sensor + health data over MQTT → Kafka → Elasticsearch;
   Kibana visualizes it; a "delivered" email lands in the inbox.

## What each repo does
| Repo | Plain-language role | Runs on |
|---|---|---|
| **NC_AirPost** | The umbrella: ties all repos together (git submodules) + holds the **physics simulation** and docs. | dev machine |
| **AirPost_Backend** | The brain (cloud/server). 3 Go services: **application** (REST API + DB: deliveries, nodes, sinks, topics, auth), **logic-core** (consumes sensor/event streams from Kafka → stores in Elasticsearch + runs rules like "send email on delivery"), **health-check** (live position/health over WebSocket). | server |
| **AirPost_Drone** | The drone's onboard software (ROS/catkin): flight + sensors + the AprilTag landing pipeline. | drone (Jetson) |
| **AirPost_Station** | Ground-station software (Python): sensors + actuators at a pickup/landing station. | station |
| **AirPost_Sink** | A data "sink": ingests a class of node data (drone / station / tag) into the pipeline. | server |
| **AirPost_UI** | The web frontend (`ui-next`, Vite + React): login with role-based views (user vs admin), parcel registration + live tracking on an OpenStreetMap, and an admin console (drones/stations/tags/sinks/Kafka-topics CRUD + embedded Kibana). | browser |
| **docker-elasticsearch-kibana** | A vendored ELK (Elasticsearch + Kibana) stack used for sensor logging/dashboards. | server |

## What's running when you `docker compose up` (AirPost_Backend)
**Core delivery path:** `mysql` (DB) · `mosquitto` (MQTT broker — the drone/sim contract) ·
`application` (:8081 REST) · `health-check` (:8083/8085 tracking WS) · `ui-next` (:4173 web app).
**Observability/logic:** `zookeeper`+`kafka` (event bus) · `logic-core` (:8084 rules) ·
`elasticsearch` (:9200) · `kibana` (:5601 dashboards).
**Notifications:** `mailhog` (:8025, a fake inbox that catches "delivered" emails).

For just "register → drone flies" you only need the core path; the rest is monitoring/logic.

## The simulation (NC_AirPost/simulation)
The drone hardware is expensive and slow to iterate on, so the whole delivery mission is also
flown in **PX4 SITL + Gazebo on the real Gazebo "baylands" park terrain**: stations/drop-pads are
placed on lidar-measured open clearings, and a persistent flight agent flies a full sortie per
MQTT order (takeoff → winch delivery → AprilTag precision land). It speaks the **same MQTT
contract** as the backend, so the sim is a drop-in stand-in for a real drone.

## What is this ultimately for?
A **showcase / capstone of an end-to-end autonomous logistics system** — not a commercial
product. The point is to demonstrate the full loop working together: a web order driving a real
(or simulated) autonomous drone that delivers and precision-lands, observable in real time. A
realistic "done" is: **the demo runs reliably end to end (UI order → drone delivers → tracked →
logged → notified), in simulation, with the architecture mapping cleanly onto real hardware.**

## How far should it go (honest ceiling)
- **Now (works):** the simulation flies repeated deliveries reliably (3/3 PASS, ~0.1 m landings);
  the backend serves the API + JWT auth + tracking; the **web app is usable end to end** (login,
  role-based user/admin views, parcel register/track on a real map, admin CRUD wired to the
  backend, Kibana embed); the full stack comes up in Docker.
- **Next (polish):** make `logic-core` robust to startup ordering (it currently crashes if Kafka
  isn't ready); add map-click node placement (admin currently types coordinates).
- **Aspirational (real deployment):** real Jetson/PX4 drone + ground stations, real map-based
  operations and airspace deconfliction, multi-drone scheduling, TLS + per-device auth, PII
  encryption. These are research/engineering efforts well beyond a demo and are not required for
  the showcase to be "complete."

## Known gaps (as of this writing)
- **`logic-core`** crashes if Kafka isn't ready at startup (no retry/backoff) — start it after
  Kafka is healthy, or add a reconnect loop.
- **Admin node placement** uses coordinate input (lat/lng prompts), not map-click. Functional, but
  a map picker would be nicer.
- The legacy `ui/` app has been **removed** — `ui-next` is the sole, complete frontend.

## Seeded demo logins (dev only; override in real deployments)
- Admin: `admin@airpost.local` / `admin` — sees the admin console + sensors.
- User:  `user@airpost.local` / `user` — sees parcel registration + tracking only.
