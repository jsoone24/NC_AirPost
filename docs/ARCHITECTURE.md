# Architecture

AirPost is one vertical spine: a user registers a parcel in the UI, the backend picks a
station + route and dispatches a flight over MQTT, the drone (simulated in PX4 + Gazebo)
flies the delivery and precision-lands, a "delivered" email is sent, and the flight is
tracked live on a map. A parallel sensor pipeline streams sink readings through Kafka into
Elasticsearch/Kibana.

See [`RUNBOOK.md`](./RUNBOOK.md) for the end-to-end demo and
[`../simulation/README.md`](../simulation/README.md) for the flight simulator.

## Components

```mermaid
flowchart LR
  subgraph Frontend
    UI["ui-next (Vite/React)\n:4173"]
  end

  subgraph Backend["Go backend (AirPost/)"]
    APP["application\n:8081 REST"]
    LC["logic-core\n:8084"]
    HC["health-check\n:8083 / WS :8085"]
  end

  subgraph Infra
    DB[("MySQL :3306")]
    MQTT(["mosquitto MQTT :1883"])
    KAFKA[("Kafka :9092 + Zookeeper :2181")]
    ES[("Elasticsearch :9200")]
    KB["Kibana :5601"]
    MH["MailHog\nSMTP :1025 / web :8025"]
  end

  subgraph Edge["Edge / field"]
    SINK["AirPost_Sink\nMQTT -> Kafka"]
    SIM["PX4 SITL + Gazebo\n(simulation/)"]
  end

  UI -->|REST + JWT| APP
  UI -->|WebSocket| HC
  APP -->|GORM| DB
  APP -->|register logic services / events| LC
  APP -->|publish flight request| MQTT
  APP -->|delivered email| MH
  LC -->|delivered email| MH
  MQTT -->|delivery request| SIM
  SIM -->|delivery status| MQTT
  SIM -->|drone coords / status| HC
  SINK -->|sensor-data topic| KAFKA
  KAFKA -->|consume| LC
  LC -->|bulk index airpost-*| ES
  ES --> KB
```

## The delivery sequence

```mermaid
sequenceDiagram
  participant U as User (UI :4173)
  participant A as application :8081
  participant M as mosquitto :1883
  participant S as PX4 sim
  participant E as logic-core / email
  participant H as health-check :8085
  participant MH as MailHog :8025

  U->>A: POST /auth/login
  A-->>U: JWT
  U->>A: POST /regist/delivery (parcel)
  A->>A: pick station + route
  A->>M: publish airpost/delivery/request
  M->>S: delivery request
  S->>M: status: launching .. delivered .. done
  S->>H: live drone coords (WS source)
  U->>H: WebSocket /health-check
  H-->>U: live coords -> map
  Note over A,E: on "delivered"
  A->>MH: SMTP delivered email
  U->>MH: read email (web :8025)
```

## Why these boundaries

- **application** owns persistence (MySQL) and the public REST surface; it is the only
  service the browser writes to, so auth + CORS live here.
- **logic-core** owns the data pipeline and rule engine (Kafka → rules → Elasticsearch,
  plus the email action), kept separate so sensor throughput never blocks the REST API.
- **health-check** owns only the live-tracking WebSocket fan-out — a small, single-purpose
  service so a slow browser client can't stall the API.
- **mosquitto** is the seam between software and the drone: the same MQTT contract works
  for the simulator today and real hardware later.

## Ports

| Component | In-network | Host |
|---|---|---|
| application | application:8081 | 8081 |
| logic-core | logic-core:8084 | 8084 |
| health-check | health-check:8083 / :8085 | 8083 / 8085 |
| ui-next | ui-next:4173 | 4173 |
| MySQL | mysql:3306 | 3306 |
| Kafka | kafka:29092 | 9092 |
| Zookeeper | zookeeper:2181 | 2181 |
| Elasticsearch | elasticsearch:9200 | 9200 |
| Kibana | kibana:5601 | 5601 |
| MailHog | mailhog:1025 / :8025 | 1025 / 8025 |
| mosquitto | mosquitto:1883 | 1883 |
