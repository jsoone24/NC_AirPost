# Security checklist

AirPost's `docker-compose.yml` ships **development** defaults so the demo runs out of the
box. Those defaults are insecure on purpose. This checklist is what must change before any
deployment beyond a laptop, grouped by concern. Status reflects the current code.

## Authentication & authorization

- [x] REST API is JWT-gated by default (`AUTH_ENABLED` defaults ON in `application`).
- [x] `POST /auth/login` is the only public route; it issues HS256 JWTs.
- [x] Role split: infrastructure CRUD (sinks/nodes/logic/topics/events) is **admin only**;
      deliveries/tracking are per-user with ownership checks.
- [x] Login compares credentials in constant time (`hmac.Equal`) to avoid timing leaks.
- [ ] **Replace seeded accounts** (`ADMIN_*` / `USER_*` env) and the dev `JWT_SECRET`
      with real, rotated secrets — the compose values are placeholders.
- [ ] Add token expiry/refresh and per-user accounts backed by storage (today: env accounts).
- [ ] Put TLS in front of the API (terminate HTTPS at a reverse proxy); JWTs over plaintext
      are sniffable.

## Secrets

- [x] All secrets come from env vars (`JWT_SECRET`, `DB_PASS`, `SMTP_*`, account creds) —
      none are hard-coded in Go source.
- [ ] **Do not commit real secrets.** The compose file's values are dev-only; supply real
      ones via a secrets manager / Docker secrets / CI secrets, not the YAML.
- [ ] Rotate `JWT_SECRET` and DB credentials on a schedule; rotating `JWT_SECRET`
      invalidates issued tokens (intended).

## CORS

- [x] `application` restricts CORS to a single origin (`UI_ORIGIN`, default the UI's URL)
      with credentials — a wildcard `*` + credentials is rejected by browsers and avoided.
- [ ] Set `UI_ORIGIN` to the exact production UI origin (scheme + host + port).
- [ ] `health-check`'s WebSocket currently accepts **any** Origin
      (`CheckOrigin` returns true) — restrict it to the UI origin before exposing it.

## MQTT (mosquitto)

- [x] Broker is isolated on the compose network; only 1883 is exposed for the local sim.
- [ ] **`allow_anonymous true` is dev-only.** Enable authentication (username/password or
      client certs) and an ACL so only the application and drone can publish/subscribe.
- [ ] Use TLS (mqtts/8883) for any non-loopback broker traffic.
- [ ] Scope topics: the delivery request/status topics should not be world-writable.

## Transport / network

- [ ] Don't publish infra ports (MySQL 3306, Kafka 9092, ES 9200) on a real host — keep
      them on the internal network only; expose just the UI (+ API behind TLS).
- [ ] Elasticsearch/Kibana run with security disabled (dev image config) — enable ES
      security + auth before exposing.

## PII & data handling

- [ ] Delivery records and tracking numbers tie a person to a parcel/location — treat them
      as PII: restrict reads to the owner/admin (ownership checks exist; keep them on).
- [ ] "Delivered" emails contain recipient addresses — in dev they go to MailHog (captured,
      never sent). Point SMTP at a real, authenticated relay only in production.
- [ ] Avoid logging full sensor payloads / coordinates / emails at info level in prod.
- [ ] Set retention/TTL on the `airpost-*` Elasticsearch indices so location/sensor history
      isn't kept indefinitely.

## Build / supply chain

- [x] Go images are multi-stage and run a static binary on a minimal Alpine base (no
      toolchain at runtime).
- [ ] Pin/scan base images and dependencies; rebuild for CVEs.

> Reporting: open a private security advisory on the repo rather than a public issue.
