# XPENG Toolkit 0.6.18b4 — beta review and safe test

**Status: experimental. Not merged into main. No live vehicle tests yet.**

Latest validated GitHub Actions run: https://github.com/matanmesika/xpengtoolkit/actions/runs/39524437477

On Python 3.13 and 3.14, all integration Python modules compiled, 104 focused tests passed, 750 unittest tests ran (5 skipped), and full pytest passed 831 tests (5 skipped, plus 182 passing subtests).

## Corrections checked

- MQTT duplicate and partial packet handling; valid zero fields and GPS preserved.
- Shift P/D/R with signed speeds including negative reversing; unknown-to-driving transition.
- Fresh versus cached GPS, gear, speed and raw cloud Standby; three diagnostic sensors.
- Active versus parked polling and real success/failure of MQTT publishes.
- Charge state versus charge power ordering, pause, stop and expiry.
- Shared persistent 12-hour automatic wake backoff for cold start and watchdog; offline startup does not spend the wake budget.
- Tests of initial partial packets, race-safe callbacks, charging, reverse, P/D transitions, restarts and command permissions.

## Known risks that tests cannot close

If XPENG cloud sends no new shift or speed update after the car starts to move, Home Assistant cannot immediately detect motion from silence. A passive query may return only a short acknowledgement; automatic wake remains conservatively limited. The raw mode field may report Standby while driving; a silent connection does not prove the physical car is asleep. R speed sign on this specific G6 has not been observed. Cloud delivery, firmware-specific fields, charging power sign and parked battery drain require actual car evidence.

## Safe trial and rollback

Create a full Home Assistant backup and separately copy /config/custom_components/xpeng. Install only the latest green 0.6.18b4 GitHub Actions artifact, not b2/b3. Stop Home Assistant, extract the artifact's inner zip under /config so it replaces custom_components/xpeng, restart and verify version in manifest.json. Do not modify credentials or certificates. The same HA instance cannot run two versions of domain xpeng together.

Observe Operational state, Motion diagnosis, GPS freshness, polling_status, raw vehicle_mode, speed, shift_state and device_tracker. Log parked for at least an hour, opening the door, P -> D, stopping in D, D -> P, P -> R -> P, a 10–20 minute drive, a charging stop/restart and recovery after an MQTT outage. Check safely after driving; do not operate a phone at the wheel. Do not press Wake initially, so we can measure automatic transition detection. If GPS becomes stuck, first record debug logs and the official app state, then press Wake once for comparison.

To roll back, stop HA, restore the backed-up custom_components/xpeng directory and restart. Never upload unredacted logs containing GPS, identity or access credentials.

Only promote to stable after all automated checks, actual HA startup, driving/reverse/parking, long parked/sleep and charging/renewal scenarios are validated.
