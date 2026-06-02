# Kibana saved objects

`airpost-sensor-dashboard.ndjson` is a Kibana **saved-objects** export (NDJSON,
Kibana 7.6.x — matches the `kibana:7.6.1` image in `../AirPost/docker-compose.yml`).

It contains:

| Saved object | Type | What it shows |
|---|---|---|
| `airpost-*` | index pattern | All sensor indices logic-core writes (`airpost-<value>-<sink>`), time field `timestamp` |
| AirPost — Readings over time by node | line viz | Reading count over time, split by `node.name` |
| AirPost — Readings per node | bar viz | Total readings ingested per node |
| **AirPost — Sensor Streams** | dashboard | The two visualizations, auto-refresh every 10s |

## Where the data comes from

Sink nodes publish sensor readings over MQTT → the **Sink** server forwards them to
the Kafka **`sensor-data`** topic → **logic-core** consumes, enriches, runs rules, and
**bulk-indexes** them into Elasticsearch under `airpost-*` indices. Kibana reads those
indices. See `../README.md` and `../AirPost/logic-core`.

> The dashboard is empty until at least one sensor reading has been indexed (i.e. the
> Sink → Kafka → logic-core → Elasticsearch path has carried real data). Field names
> assume logic-core's document shape: `timestamp`, `node.name`, `node_id`, `values.*`.

## Import

With the stack up (`docker compose up` in `../AirPost`), Kibana is at
http://localhost:5601.

**UI:** Management → Saved Objects → **Import** → choose
`airpost-sensor-dashboard.ndjson` → keep "Automatically overwrite conflicts" → Import.
Then open **Dashboard → AirPost — Sensor Streams**.

**API (scriptable):**

```bash
curl -s -X POST "http://localhost:5601/api/saved_objects/_import?overwrite=true" \
  -H "kbn-xsrf: true" \
  --form file=@kibana/airpost-sensor-dashboard.ndjson
```
