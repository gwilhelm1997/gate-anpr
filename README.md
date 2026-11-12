# Gate ANPR — Home Assistant app

Local number-plate recognition that opens the gate for approved vehicles. It runs entirely on Home
Assistant OS. There's no separate PC, Frigate, cloud service or subscription.

- **Recognition:** FastALPR 0.4 (YOLOv9 plate detector + CCT OCR, ONNX Runtime). It runs in an
  isolated, low-priority worker process inside the app container, with the models baked into the image.
- **Trigger:** the Reolink vehicle detection sensor. **Frames:** 3840×2160 snapshots through Home
  Assistant's Reolink camera entity.
- **Decision:** multi-frame consensus, approach check, detection region, NZ format, and exact
  whitelist match. It fails closed.
- **Gate:** an explicit OPEN command (the relay on the controller's OPEN input, or `cover.open_cover`),
  with a cooldown and confirmation. Never a toggle.
- **Interface:** native entities via MQTT discovery, an auto-created dashboard, and an Ingress panel
  for registrations and history.
- **Log-only by default:** automatic opening is off until you enable it.

User documentation: [gate_anpr/DOCS.md](gate_anpr/DOCS.md).

## Layout

```
repository.yaml           Home Assistant app repository manifest
gate_anpr/                the app (config.yaml, Dockerfile, run.sh, translations, DOCS.md)
  app/gate_anpr/          Python service
    __main__.py           wiring and start-up (Supervisor or local dev mode)
    service.py            recognition session: trigger -> frames -> verdict -> gate
    decision.py           consensus and approach rules (pure)
    recognition.py        FastALPR worker process, region crop, annotation
    gate.py               safe OPEN command with pre-checks, cooldown and confirmation
    ha.py                 Home Assistant REST + WebSocket client
    mqtt.py               MQTT discovery entities
    dashboard.py          Lovelace dashboard
    store.py              SQLite in /data: settings, plates, history
    web.py, static/       Ingress panel
tests/                    pytest suite (unit, gate safety, MQTT broker round-trip, end-to-end)
tools/                    recognise.py (run the engine on images), deploy.ps1, synthetic_plate.py
```

## Development

```bash
py -3.13 -m venv .venv
.venv/Scripts/python -m pip install -r gate_anpr/requirements.txt pytest pytest-asyncio amqtt
.venv/Scripts/python -m pytest -q
```

Try the recognition on your own camera images:

```bash
.venv/Scripts/python tools/recognise.py frame1.jpg frame2.jpg --region 20,30,80,100 --annotate out
```

Run the service locally against a real Home Assistant, in log-only mode, with a long-lived token:
put an `options.json` in a data folder, then set `HA_URL`, `HA_TOKEN`, `DATA_DIR` and optionally
`MQTT_HOST`/`MQTT_USER`/`MQTT_PASSWORD`, and run `python -m gate_anpr` from `gate_anpr/app`.

## Install / update

See [Installation](gate_anpr/DOCS.md#installation). In short: run `tools\deploy.ps1`, then install
**Gate ANPR** from **Local apps**. To update, run it again and press **Update**.
