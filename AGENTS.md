# AGENTS.md — Nexus Controller

Home Assistant custom component (HACS) voor de Zonneplan Nexus thuisbatterij
(Solis S6-EH3P10K-H-ZP omvormer): NOM-selfconsumption met PID, prijsgestuurd
netladen, dynamische teruglevering, warmtepomp-verbruiksvoorspeller.

## Home Assistant server

- Adres: `https://www.meuleman.eu` (REST + websocket). `homeassistant.local` bestaat niet.
- Long-lived access token: `HA_TOKEN` in `~/.vibe/.env` (`export $(grep -E '^HA_TOKEN=' ~/.vibe/.env)`).
- Verbindingstest: `curl -s -H "Authorization: Bearer $HA_TOKEN" https://www.meuleman.eu/api/config`
  → HTTP 200 met `"state": "RUNNING"` betekent verbinding OK; 401 betekent token ongeldig.
- Integratie draait op de server (niet lokaal). Server-versie kan nieuwer zijn dan de `.venv` (2026.10.2) — check met `/api/config`.
- Dashboards: `https://www.meuleman.eu/lovelace/nexus`.

## Handige API-aanroepen (getest)

```bash
export $(grep -E '^HA_TOKEN=' ~/.vibe/.env)
# Alle entities / één entity:
curl -s -H "Authorization: Bearer $HA_TOKEN" https://www.meuleman.eu/api/states
curl -s -H "Authorization: Bearer $HA_TOKEN" https://www.meuleman.eu/api/states/<entity_id>
# Geschiedenis (recorder), laatste 24u van één sensor:
curl -s -H "Authorization: Bearer $HA_TOKEN" \
  "https://www.meuleman.eu/api/history/period/<ISO-start>?filter_entity_id=<entity_id>&minimal_response"
# Service aanroepen met response:
curl -s -X POST -H "Authorization: Bearer $HA_TOKEN" -H "Content-Type: application/json" \
  -d '{"entity_id":"..."}' "https://www.meuleman.eu/api/services/<domain>/<service>?return_response"
```

- Websocket-API (met `aiohttp` uit `.venv`): auth-bericht mag ALLEEN
  `{"type": "auth", "access_token": ...}` zijn — geen `id`-veld. Daarna commands met oplopende `id`.
- Grafana bij problemen eerst kijken: websocket `system_log/list` (in-memory log; `/api/error_log` is vaak leeg).

## Checklist FA-integratie debuggen

1. Integratie-entities: `sensor.kelder_nexus_controller_*` (diagnostiek) en `sensor.nexus_controller_*` (invoer).
2. Configuratie van de entry staat NIET in websocket `config_entries/get` (wordt gestript); wel via REST:
   `POST /api/config/config_entries/options/flow {"handler":"<entry_id>"}` levert het flow-formulier
   met de huidige waarden als `default` terug. Entry-id van Nexus: `01M5MSA7BVTCW74T1VE66HYDVN`.
   Het aanmaken van zo'n flow is onschuldig; die vervalt automatisch.
3. Recorderstatistieken: websocket `recorder/statistics_during_period` — let op: parameter heet
   `statistic_ids` (niet `stat_ids`), met `period: 5minute` en `types: ["mean"]`.
4. `config_entries/diagnostics/...` en `diagnostics/get` zijn voor deze integratie niet beschikbaar.
5.Recorder-file lezen via de `file`-service is geblokkeerd (allowlist alleen `/config/www` en `/media`).

## Drukkende context / bekende punten

- Source-sensors (geconfigureerd): house power `sensor.electricity_house` (template, kW — NIEUW; vroeger stond hier
  per ongeluk de kWh-sensor `sensor.energy_usage_house`), warmtepomp `sensor.wp_vermogen` (min_max, W),
  buitentemp `sensor.remko_mqtt_out_temp`, weer `weather.forecast_thuis`.
- Het warmtepompmodel (v0.5.30+) leert 7 dagen kwartier-profielen; verwacht `minimum_history_days: 3`
  in de diagnostics-attributes van de verbruiksverwachting-sensors; status `partial` totdat elk kwartier
  ≥3 geldige dagen heeft. De backfill leest FA-recorder-statistieken bij het opstarten, dus er is geen
  wachttijd nodig zolang de invoer klopt.
- FA server haalt releases van GitHub (HMeuleman/Nexus_Controller): update-entiteit `update.nexus_controller_update`.

## Development

- `.venv` is FA 2026.10.2 met dev-afhankelijkheden. Tests: `./.venv/Scripts/python.exe -m pytest tests/ -q`
  (Windows Git Bash). Lint: `./.venv/Scripts/python.exe -m ruff check .`
- Lokaal uitproberen: `custom_components/nexus_controller` kopiëren naar de FA-configdirectory en herstarten.

## Releases

- Versie moet overeenkomen in `custom_components/nexus_controller/manifest.json` én `pyproject.toml`.
- Stappen: commit op `main` → push → geannoteerde tag `v0.5.x` → push tag →
  `gh release create v0.5.x --title "..." --notes "..."`.
- GitHub CLI (`gh`) is geïnstalleerd en geauthenticeerd als HMeuleman — push en releases gaan direct.
</content>
