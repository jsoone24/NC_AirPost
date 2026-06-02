# Runbook — demo end to end

**Goal:** `docker compose up` → register a parcel → watch the sim deliver → read the
"delivered" email in MailHog → track the drone on the map.

Everything except the flight simulator runs in containers. The simulator (PX4 SITL +
Gazebo) runs natively because it needs a GL/GPU context — see
[`../simulation/README.md`](../simulation/README.md). The sim and the backend meet on the
same MQTT broker (`mosquitto:1883`) using the contract in that README.

## 0. Prerequisites

- Docker + Docker Compose v2.
- For the real flight: PX4 v1.17 SITL + Gazebo Harmonic built per
  [`../simulation/PX4_MACOS_BUILD.md`](../simulation/PX4_MACOS_BUILD.md), plus the
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

## 3. Start the live sim + flight agent (native)

In a real Terminal (so the Gazebo window can open):

```bash
cd ../simulation
./run_airpost_live.sh                       # PX4 SITL + Gazebo GUI + winch manager + detector + flight agent
# wait for "AGENT READY"
```

`run_airpost_live.sh` launches the GUI sim plus the persistent flight agent
(`tests/airpost_flight_agent.py`). The agent holds one long-lived MAVSDK connection,
subscribes to `airpost/delivery/request` on the broker, and flies a full autonomous sortie
per order (lift-off in ~2 s), streaming status on `airpost/delivery/status`. It defaults to
the compose broker; override with `MQTT_BROKER`:

```bash
MQTT_BROKER=localhost ./run_airpost_live.sh
```

## 4. Register a parcel

In the UI (http://localhost:4173): log in, go to **Register**, fill the parcel form
(source station + destination tag), submit. You get a tracking number.

Under the hood the UI calls `POST /regist/delivery` on `application:8081`. The
application picks a station + route and publishes a flight request to MQTT
(`airpost/delivery/request`), which the simulator's flight agent consumes.

Equivalent without the UI (this exact call is verified end-to-end — seeded source station 1,
destination tag 30):

```bash
TOKEN=$(curl -s -X POST http://localhost:8081/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@airpost.local","password":"admin"}' | sed 's/.*"token":"//;s/".*//')

curl -s -X POST http://localhost:8081/regist/delivery \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"email":"demo@airpost.local",
       "src_name":"Alice","src_phone":"010-1111-2222","src_station_id":1,
       "dest_name":"Bob","dest_phone":"010-3333-4444","dest_tag_id":30}'
# -> {"order_num":"AP...","drone_id":50,...}  and the sim drone lifts off within ~2 s
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
- **Sensors tab shows "application not found":** the embed URL must be the Kibana 7.6 path
  `http://localhost:5601/app/kibana#/dashboards` (set `VITE_KIBANA_URL` to override).
- **A Go service restarts / unhealthy:** `docker compose logs -f application` (or `logic-core` /
  `health-check`). Confirm `mysql` / `kafka` / `elasticsearch` became healthy first.
- **No email:** check `logic-core` / `application` logs for SMTP errors; confirm
  `SMTP_HOST=mailhog SMTP_PORT=1025`. Read captured mail at `http://localhost:8025`.
- **Sim can't reach the broker:** the flight agent defaults to `127.0.0.1:1883` (the compose
  mosquitto). Override with `MQTT_BROKER=...`; the app uses `tcp://mosquitto:1883` in-network.
- **Compose recreates a duplicate stack:** the project name is pinned to `airpost` in the compose
  file (the repo dir is `AirPost_Backend`); run compose from that directory.
