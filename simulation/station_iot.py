#!/usr/bin/env python3
"""Station IoT devices — dynamic self-registration + live sensor streaming.

Each station in the generated world is an autonomous IoT device (NOT a hardcoded backend seed). On
startup every station REGISTERS ITSELF with the backend (POST /regist/node) so the admin map shows
exactly the world that was generated, then continuously:
  - streams its sensors  [temperature, humidity, light, lat, long, alt]  to the Kafka sink
    (node_id STA<id>) -> logic-core -> Elasticsearch, and
  - pushes its live GPS to the backend (POST /regist/node/update) so positions are tracked centrally.
When ambient light drops below a threshold the station raises its pad LIGHT so a drone's downward
camera can still see the AprilTag for precision landing (the lamp state rides in the light sensor
and is signalled to the gz light via /tmp/airpost_lamp_<id>).

Run:  PX4-Autopilot/.venv/bin/python station_iot.py
Env:  API, ADMIN, PASS, KAFKA_BROKER, DARK (1 = force night), TELE=3
"""
import json
import math
import os
import time
import urllib.request

SIMDIR = os.path.dirname(os.path.abspath(__file__))
SITES = os.path.join(SIMDIR, "tests", "airpost_sites.json")
API = os.environ.get("API", "http://localhost:8081")
ADMIN = os.environ.get("ADMIN", "admin@airpost.local")
PASSWORD = os.environ.get("PASS", "admin")
KAFKA_BROKER = os.environ.get("KAFKA_BROKER", "127.0.0.1:9092")
KAFKA_TOPIC = "sensor-data"
TELE = float(os.environ.get("TELE", "3"))
DARK = os.environ.get("DARK", "0") == "1"
LIGHT_ON_LUX = 50.0   # below this the pad lamp switches on so the tag stays visible
SINK_STATION = 2
SINK_DRONE = 1
DRONE_ID_BASE = 50    # fleet instance i -> backend drone id 51+i (matches fleet_service routing)
DRONES = int(os.environ.get("DRONES", "0"))   # number of drones in the running fleet (0 = leave as is)
ORIGIN_LAT, ORIGIN_LON, EARTH_R = 37.5, 127.0, 6371000.0


def en_to_latlon(east, north):
    rad = math.pi / 180
    return (ORIGIN_LAT + (north / EARTH_R) / rad,
            ORIGIN_LON + (east / (EARTH_R * math.cos(ORIGIN_LAT * rad))) / rad)


def _post(path, body, token=None):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode() or "{}")


def login():
    return _post("/auth/login", {"email": ADMIN, "password": PASSWORD})["token"]


def _get(path, token):
    req = urllib.request.Request(API + path, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode() or "[]")


def _delete(path, token):
    req = urllib.request.Request(API + path, method="DELETE",
                                 headers={"Authorization": "Bearer " + token})
    urllib.request.urlopen(req, timeout=10).read()


def sync_drones(token):
    """Make the backend's drone fleet match the SIM exactly: self-register the running fleet's drones
    (id 51+i, attached to station i+1 — the same routing fleet_service uses) and prune any drone the
    backend still lists that is NOT in the running fleet (e.g. hardcoded-seed phantoms). This is the
    IoT model: a drone exists in the backend iff it is actually online."""
    if DRONES <= 0:
        return
    st = {s["id"]: s for s in json.load(open(SITES))["stations"]}
    wanted = set(range(DRONE_ID_BASE + 1, DRONE_ID_BASE + 1 + DRONES))
    existing = {n["id"] for n in (_get(f"/regist/node/{SINK_DRONE}", token) or [])}
    for did in existing - wanted:
        try:
            _delete(f"/regist/node/{did}", token)
            print(f"drone IoT: pruned phantom drone {did} (not in the running fleet)", flush=True)
        except Exception as e:
            print(f"drone IoT: prune {did} failed: {e}", flush=True)
    for i in range(DRONES):
        did = DRONE_ID_BASE + 1 + i
        if did in existing:
            continue
        sid = i + 1
        s = st.get(sid)
        if not s:
            continue
        lat, lon = en_to_latlon(s["E"], s["N"])
        try:
            _post("/regist/node", {"id": did, "name": f"drone-{sid}", "type": f"DRO-{sid}",
                                   "lat": lat, "lng": lon, "alt": s.get("Z", 0.0), "sink_id": SINK_DRONE}, token)
            print(f"drone IoT: self-registered drone {did} @ station {sid}", flush=True)
        except Exception as e:
            print(f"drone IoT: register {did} failed: {e}", flush=True)


def ambient_lux(t):
    """Simulated ambient light: a slow day/night cycle (or forced dark). Real stations would read a
    photoresistor here."""
    if DARK:
        return 5.0
    # ~2-minute demo day/night cycle so the lamp behaviour is visible without waiting hours.
    return max(0.0, 400.0 * (0.5 + 0.5 * math.sin(t / 19.0)))


def main():
    token = login()
    stations = json.load(open(SITES))["stations"]
    devices = []  # (sim_id, backend_node_id, lat, lon, alt)
    for s in stations:
        lat, lon = en_to_latlon(s["E"], s["N"])
        alt = s.get("Z", 0.0)
        try:
            # type "STA" is the backend's station contract: RegistNode persists it AND attaches the
            # night-LED logic (light sensor in the dark range -> "LED ON" actuator), exactly the
            # "turn the pad light on when dark" behaviour.
            # Register with the EXPLICIT sim station id so the backend node id == the id the sim and
            # MQTT contract use (takeoff_id/landing_id) — otherwise the geometric landing picks a
            # backend id the sim can't resolve. Stations already seeded (1..8) just conflict here
            # (harmless); the rest of the generated field (0, 9..39) is added so the admin map shows
            # the whole world.
            _post("/regist/node", {"id": s["id"], "name": f"station-{s['id']}", "type": "STA",
                                   "lat": lat, "lng": lon, "alt": alt, "sink_id": SINK_STATION}, token)
            devices.append((s["id"], s["id"], lat, lon, alt))
        except Exception as e:
            print(f"station {s['id']} register failed: {e}", flush=True)
    print(f"station IoT: {len(devices)} stations self-registered with the backend", flush=True)

    sync_drones(token)   # make the backend drone fleet match the running sim (no phantoms)

    try:
        from confluent_kafka import Producer
        producer = Producer({"bootstrap.servers": KAFKA_BROKER})
    except Exception as e:
        producer = None
        print(f"sink disabled ({e!r})", flush=True)

    t0 = time.time()
    while True:
        t = time.time() - t0
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        lux = round(ambient_lux(t), 0)
        lamp_on = lux < LIGHT_ON_LUX
        for sim_id, nid, lat, lon, alt in devices:
            # Sensor frame to the sink. The light value reports the EFFECTIVE illumination on the tag:
            # when ambient is dark the pad lamp lifts it back above the visibility threshold.
            light = max(lux, LIGHT_ON_LUX + 30.0) if lamp_on else lux
            vals = [round(20 + 6 * math.sin(t / 17.0 + sim_id), 1),   # temperature C
                    round(55 + 15 * math.cos(t / 23.0 + sim_id), 1),  # humidity %
                    light, round(lat, 7), round(lon, 7), round(alt, 2)]
            if producer is not None:
                producer.produce(KAFKA_TOPIC, json.dumps(
                    {"node_id": f"STA{nid}", "values": vals, "timestamp": ts}).encode())
            # Signal the gz pad lamp (a light entity over the tag) so the drone camera sees the tag.
            open(f"/tmp/airpost_lamp_{sim_id}", "w").write("1" if lamp_on else "0")
        if producer is not None:
            producer.poll(0)
        print(f"stations: lux={lux:.0f} lamp={'ON' if lamp_on else 'off'} ({len(devices)} streaming)", flush=True)
        time.sleep(TELE)


if __name__ == "__main__":
    main()
