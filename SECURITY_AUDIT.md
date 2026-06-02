# AirPost — Sensitive-Data Audit & Remediation

Read-only audit of the repo + submodules. Status per finding below.
**Legend:** ✅ fixed in working tree · ⚠️ REQUIRES USER ACTION (credential rotation / git-history rewrite — cannot be done safely/automatically).

## CRITICAL

### 1. Leaked Gmail app password in git history
- **Where:** `AirPost_Backend/logic-core/logicService/logic/action.go`, commit `924f101` (also `22cd92e`), reachable from HEAD. Value `<REDACTED-app-password>` next to `<redacted-email>`.
- **Working tree:** ✅ already env-driven (`SMTP_PASS`); no plaintext remains in current source.
- ⚠️ **USER ACTION:** the secret is permanent in git history.
  1. **Revoke/rotate** that Gmail app password in the Google account NOW.
  2. Purge from history: `git filter-repo --replace-text <(echo '<REDACTED-app-password>==>REDACTED')` (or BFG), in the `AirPost_Backend`/`logic-core` repo that holds the history, then **force-push** and have collaborators re-clone.

### 2. Reverse-SSH backdoor / persistent tunnel
- **Where:** `AirPost_Drone/scripts/reverse_ssh_continuous.sh` and `AirPost_Station/scripts/reverse_ssh_continuous.sh` — reverse tunnels exposing local :22/:80 to a hardcoded host `jongsoo@<REDACTED-host>:9709`.
- ✅ **Fixed:** both files deleted from the working tree.
- ⚠️ **USER ACTION:** remove from git history (same purge as #1) and from any deployed devices/cron/systemd.

## HIGH

### 3. Hardcoded client-side login credentials (legacy UI)
- **Where:** `AirPost_UI/ui/src/LoginInfo/auth.js` — plaintext user/password list shipped in the client bundle.
- ✅ **Fixed:** credential list removed; replaced with a pointer to server-side JWT auth (`AirPost_Backend/application/rest/handler/auth.go` + new `ui-next` `src/lib/api.ts`). The legacy `ui/` is being retired by `ui-next/`.

### 4. Hardcoded Kakao Maps JS API key (legacy UI)
- **Where:** `AirPost_UI/ui/.env.development` — real key committed (`5cffce3a…`).
- ✅ **Fixed:** removed from VCS (placeholder `__SET_IN_ENV_LOCAL__` + note).
- ⚠️ **USER ACTION:** rotate the key in the Kakao console, restrict by referrer/domain, and set the real value in an untracked `.env.local`. (Old value remains in history — purge as above.)

### 5. Hardcoded private MQTT broker / operator IP, no auth
- **Where:** `AirPost_Drone/catkin_ws/src/drone_controller/scripts/MQTT.py` and `AirPost_Station/run.py` — `<REDACTED-host>:9708`, no broker username/password.
- ✅ **Fixed (host/port):** now read from `MQTT_BROKER_HOST`/`MQTT_BROKER_PORT` env (default `127.0.0.1:1883`); operator IP removed from source.
- ⚠️ **Remaining (design):** enable MQTT **authentication + TLS** (per-device creds + ACLs) so only authorized devices publish actuator/drone commands. The dev `docker-compose` ships `eclipse-mosquitto` open on `1883` for local use only.

## MEDIUM (design hardening — tracked, not blocking the dev demo)

- **6. DB default creds in compose** (`AirPost_Backend/docker-compose.yml`, `airpost`/`airpost`): fine for throwaway dev; require env-provided secrets for any real deployment (no in-file defaults).
- **7. Services bind 0.0.0.0; ES/Kibana/Kafka host-published:** for non-dev, default-bind to `127.0.0.1`/private iface and keep ES/Kibana/Kafka inside the compose network (not host-published).
- **8. Unauthenticated inter-service HTTP** (logic-core → sink `/actuator`,`/drone` over plain HTTP): add service-to-service auth + TLS on a private network.

## Checked & clear
- CORS is correctly restricted to a configurable `UI_ORIGIN` (prior wildcard+credentials bug already fixed).
- `JWT_SECRET` is env-sourced (no hardcoded secret).
- Naver Map keys are empty strings inside commented-out code.
- `ui-next` mock data and the sim `*_sites.json` are fictional — no real PII.
- No private keys/certs committed (only the standard certifi CA bundle in the sim venv).
