# Runbook — demo end to end

**Goal:** `docker compose up` → register a parcel → watch the sim deliver → read the
"delivered" email in MailHog → track the drone on the map.

Everything except the flight simulator runs in containers. The simulator (PX4 SITL +
Gazebo) runs natively because it needs a GL/GPU context — see
[`simulation/README.md`](https://github.com/jsoone24/NC_AirPost/blob/main/simulation/README.md). The sim and the backend meet on the
same MQTT broker (`mosquitto:1883`) using the contract in that README.

## 0. Prerequisites

- Docker + Docker Compose v2.
- For the real flight: PX4 v1.17 SITL + Gazebo Harmonic built per
  [`simulation/PX4_MACOS_BUILD.md`](https://github.com/jsoone24/NC_AirPost/blob/main/simulation/PX4_MACOS_BUILD.md), plus the
  `mosquitto_pub`/`mosquitto_sub` CLI.

## 1. Bring up the whole stack

```bash
cd AirPost_Backend
docker compose up --build -d
docker compose ps        # wait until application, logic-core, health-check, ui-next are healthy/up
```

What you get:

| URL | Service |
|---|---|
| http://localhost:4173 | UI (ui-next) |
| http://localhost:8081/swagger/index.html | application REST API (Swagger) |
| ws://localhost:8085/health-check | live tracking WebSocket |
| http://localhost:5601 | Kibana |
| http://localhost:8025 | MailHog (captured emails) |
| tcp://localhost:1883 | MQTT (mosquitto) |

Seeded dev accounts (set in `docker-compose.yml`, override for anything real):
`admin@airpost.local` / `admin`, `user@airpost.local` / `user`.

## 2. (Optional) load the Kibana dashboard

```bash
curl -s -X POST "http://localhost:5601/api/saved_objects/_import?overwrite=true" \
  -H "kbn-xsrf: true" --form file=@../kibana/airpost-sensor-dashboard.ndjson
```

## 3. Start the sim (native)

In a real Terminal (so the Gazebo window can open). Two modes:

### 3a. Multi-drone fleet (recommended — matches the seeded 8-station topology)

```bash
cd ../simulation
SERVICE=1 ./run_airpost_fleet.sh 8          # 8 drones in one Gazebo world + MQTT delivery service
# wait for "fleet service: 8 drones connected; waiting for orders"
# GUI=0 for headless; SERVICE=1 selects the MQTT-driven service (default just demos take-off/land)
```

`run_airpost_fleet.sh` starts one Gazebo server holding the world, spawns N PX4 instances at
the seeded station helipads (distinct positions), then runs `fleet_service.py`. The service
holds one MAVSDK link per drone, routes each order to the named drone by `drone_id`, and flies
takeoff → (ferry to pickup) → drop → land-at-nearest-station concurrently — each at the cruise
altitude band the backend assigned, so airborne drones never share an altitude. It also streams
live drone telemetry and station sensors to Kafka (see §8). Override the broker with `MQTT_BROKER`.

### 3b. Single-drone showcase (winch + AprilTag precision landing)

```bash
cd ../simulation
./run_airpost_live.sh                       # GUI sim + winch manager + AprilTag detector + flight agent
# wait for "AGENT READY"
```

This persistent agent (`tests/airpost_flight_agent.py`) flies one drone with the full
winch-lowered parcel and AprilTag precision landing. Use it for the close-up single-drone demo.

## 4. Register a parcel

In the UI (http://localhost:4173): log in, go to **Register**, fill the parcel form
(source station + destination tag), submit. You get a tracking number.

Under the hood the UI calls `POST /regist/delivery` on `application:8081`. The
application picks a station + route and publishes a flight request to MQTT
(`airpost/delivery/request`), which the simulator's flight agent consumes.

The seed lays down 8 stations (ids 1–8), one drone parked on each (ids 51–58) and one drop tag
per station (ids 31–38). The dispatcher picks the nearest free drone (ferrying one in if the
source station is empty), lands it at the station nearest the drop, and gives each concurrent
mission its own altitude band. Equivalent without the UI (source station 1, drop tag 32):

```bash
TOKEN=$(curl -s -X POST http://localhost:8081/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@airpost.local","password":"admin"}' | sed 's/.*"token":"//;s/".*//')

curl -s -X POST http://localhost:8081/regist/delivery \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"email":"demo@airpost.local",
       "src_name":"Alice","src_phone":"010-1111-2222","src_station_id":1,
       "dest_name":"Bob","dest_phone":"010-3333-4444","dest_tag_id":32}'
# -> {"order_num":"AP...","drone_id":51,...}  and that drone flies the sortie
```

## 5. Watch the sim deliver

In the Gazebo window (or QGroundControl) the drone flies
takeoff → cruise → winch the parcel down → release → cruise to the landing
station → **AprilTag precision-land**. Live status streams on MQTT:

```bash
mosquitto_sub -h localhost -t airpost/delivery/status
# accepted -> launching -> enroute_delivery -> lowering_cable -> delivered ->
# enroute_landing -> precision_landing -> done(PASS)
```

## 6. Email in MailHog

On the "delivered" event the email action fires SMTP to `mailhog:1025`. Open
http://localhost:8025 and read the "delivered" email.

## 7. Track on the map

In the UI open **Track / `/track/<trackingNumber>`**. The page opens the health-check
WebSocket (`ws://localhost:8085/health-check`) and plots the live drone coordinates on
the map as the sortie progresses.

## 8. Telemetry → sink → Elasticsearch

With the fleet service running (§3a) every drone's live position/battery and every station's
(simulated) environmental sensors are produced to the Kafka `sensor-data` topic. `logic-core`
consumes them, maps each value array onto the node's sensor schema and archives a document per
reading into Elasticsearch — one index per node and sink:

```bash
curl -s "http://localhost:9200/_cat/indices/airpost*?h=index,docs.count&s=index"
# airpost-1-drone-sink, airpost-1-station-sink, ... airpost-8-drone-sink, airpost-8-station-sink

curl -s "http://localhost:9200/airpost-1-drone-sink/_search?size=1" | python3 -m json.tool
# values: {lat, long, alt, velocity, batteryper, done}, node{...}, timestamp
```

Explore the same data in Kibana (http://localhost:5601 → Discover / Dashboard).

## Teardown

```bash
cd AirPost_Backend
docker compose down        # add -v to also drop the mysql / elasticsearch volumes
```

## Troubleshooting

- **`logic-core` keeps exiting:** it self-registers against `application:/event` at startup and
  panics if that fails. It needs Kafka healthy first; bring the stack up with `docker compose up`
  (it `depends_on` kafka health) or `docker start airpost-logic-core` after Kafka is up. It now
  has `restart: unless-stopped`, so transient startup races self-heal.
- **Admin "Add" does nothing / 401 in the browser:** you must be logged in as **admin**
  (`admin@airpost.local`) — admin endpoints require the admin JWT. If it still fails right after a
  backend change, the browser may be holding a stale **CORS preflight** (cached up to 12 h): open
  an **incognito window** or clear site data. (curl works regardless because it skips CORS.)
- **Kibana shows no AirPost data:** the indices appear only once the fleet service streams
  telemetry (§8). Create a data view for `airpost-*` in Kibana, then use Discover.
- **A Go service restarts / unhealthy:** `docker compose logs -f application` (or `logic-core` /
  `health-check`). Confirm `mysql` / `kafka` / `elasticsearch` became healthy first.
- **No email:** check `logic-core` / `application` logs for SMTP errors; confirm
  `SMTP_HOST=mailhog SMTP_PORT=1025`. Read captured mail at `http://localhost:8025`.
- **Sim can't reach the broker:** the flight agent defaults to `127.0.0.1:1883` (the compose
  mosquitto). Override with `MQTT_BROKER=...`; the app uses `tcp://mosquitto:1883` in-network.
- **Compose recreates a duplicate stack:** the project name is pinned to `airpost` in the compose
  file (the repo dir is `AirPost_Backend`); run compose from that directory.
