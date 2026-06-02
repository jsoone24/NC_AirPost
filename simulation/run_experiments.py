#!/usr/bin/env python3
"""Drive repeated, randomised delivery experiments against the live flight agent.

The drone spawns once (at TAKEOFF), so the start station is fixed for a session; this publishes a
sequence of orders with a RANDOM delivery site and a RANDOM landing station each time, waits for
each sortie to finish before sending the next (the agent serves one order at a time), and prints a
PASS/FAIL summary. Watch it fly in the Gazebo GUI started by run_airpost_live.sh.

Prereq: run_airpost_live.sh is up (sim + winch manager + detector + flight agent) and an MQTT
broker is reachable. Then:  python run_experiments.py [N]      (N orders, default 5)
Env: MQTT_BROKER (default 127.0.0.1), TAKEOFF (spawn station id, default 1), SEED, CRUISE.
"""
import json, os, random, sys, threading, time
import paho.mqtt.client as mqtt

HERE = os.path.dirname(os.path.abspath(__file__))
SITES = os.path.join(HERE, "tests", "airpost_sites.json")
BROKER = os.environ.get("MQTT_BROKER", "127.0.0.1")
TAKEOFF = int(os.environ.get("TAKEOFF", "1"))
CRUISE = float(os.environ.get("CRUISE", "30"))
N = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("N", "5"))
TIMEOUT_S = float(os.environ.get("ORDER_TIMEOUT_S", "240"))
REQ, STATUS = "airpost/delivery/request", "airpost/delivery/status"
TERMINAL = {"done", "failed", "rejected"}

rng = random.Random(int(os.environ.get("SEED", "1")))
data = json.load(open(SITES))
stations = {s["id"]: s for s in data["stations"]}
sites = data.get("sites", [])
if TAKEOFF not in stations:
    raise SystemExit(f"takeoff station {TAKEOFF} not in {sorted(stations)}")

result = {}                      # order_id -> FIRST terminal status payload (latched)
current = [None]                 # the order we're currently waiting on (ignore stray/other ids)
done_evt = threading.Event()


def on_message(c, u, msg):
    try:
        s = json.loads(msg.payload.decode())
    except Exception:
        return
    oid = s.get("order_id")
    if oid != current[0]:                                   # ignore status for any other order
        return
    print(f"  [{oid}] {s.get('state')} {({k: v for k, v in s.items() if k not in ('order_id', 'state')})}", flush=True)
    if s.get("state") in TERMINAL and oid not in result:    # latch the FIRST terminal state only
        result[oid] = s
        done_evt.set()


cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
cli.on_message = on_message
cli.connect(BROKER, 1883, 60)
cli.subscribe(STATUS)
cli.loop_start()

home = stations[TAKEOFF]
passed = 0
for i in range(N):
    landing = rng.choice([k for k in stations if k != TAKEOFF])     # random destination station
    site = rng.choice(sites) if sites else home                    # random delivery clearing
    order = {
        "order_id": f"exp{i + 1}",
        "takeoff_id": TAKEOFF,
        "deliver_N": round(site["N"] - home["N"], 2),              # offset from takeoff (local NED)
        "deliver_E": round(site["E"] - home["E"], 2),
        "landing_id": landing,
        "cruise": CRUISE,
    }
    print(f"\n=== experiment {i + 1}/{N}: deliver near site {site.get('id')} -> land station {landing} ===", flush=True)
    done_evt.clear()
    current[0] = order["order_id"]
    cli.publish(REQ, json.dumps(order))
    if not done_evt.wait(TIMEOUT_S):
        print(f"  [{order['order_id']}] TIMEOUT after {TIMEOUT_S:.0f}s", flush=True)
        result[order["order_id"]] = {"state": "timeout"}
    r = result.get(order["order_id"], {})
    if r.get("result") == "PASS":
        passed += 1
    time.sleep(2)                                                   # let the agent settle/disarm

cli.loop_stop()
print(f"\n==== {passed}/{N} PASS ====", flush=True)
for oid in sorted(result):
    r = result[oid]
    print(f"  {oid}: {r.get('result', r.get('state'))} "
          f"deliver_err={r.get('deliver_err', '?')} land_err={r.get('land_err', '?')}", flush=True)
sys.exit(0 if passed == N else 1)
