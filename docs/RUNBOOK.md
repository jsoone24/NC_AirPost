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
cd AirPost
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

See [`../kibana/README.md`](../kibana/README.md).

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

Equivalent without the UI:

```bash
TOKEN=$(curl -s -X POST http://localhost:8081/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"user@airpost.local","password":"user"}' | sed 's/.*"token":"//;s/".*//')

curl -s -X POST http://localhost:8081/regist/delivery \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"...":"see Swagger /regist/delivery body"}'
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
cd AirPost
docker compose down        # add -v to also drop the mysql / elasticsearch volumes
```

## Troubleshooting

- **A Go service restarts / unhealthy:** `docker compose logs -f application` (or
  `logic-core` / `health-check`). Confirm `mysql` / `kafka` / `elasticsearch` became
  healthy first — the services `depends_on` those healthchecks.
- **CORS error in the browser:** the UI origin must match `UI_ORIGIN` on `application`
  (default `http://localhost:4173`).
- **No email:** check `logic-core` / `application` logs for SMTP errors; confirm
  `SMTP_HOST=mailhog SMTP_PORT=1025`.
- **Sim can't reach the broker:** set `MQTT_BROKER=localhost` for the flight agent, and the
  app's `MQTT_BROKER=tcp://mosquitto:1883` (already set in compose).
