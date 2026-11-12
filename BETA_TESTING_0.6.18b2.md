# XPENG Toolkit 0.6.18b2 — isolated state-transition audit

**Beta only.** No stable/main merge and no vehicle-wake policy change.

## What was audited and changed

- Duplicate MQTT response detection and safe deep-ish section merges. Numeric zero remains a real update; null and empty patches are not deletions.
- Motion detection compares **actual** gear and speed field ages, not stale mixed payload snapshots, with a 10-minute upper bound. Gear P can override a previous cached speed.
- Charging detection requires a fresh charge frame (within 10 minutes); the car cannot be considered charging indefinitely from a cached flag.
- Vehicle mode remains the *raw* XPENG cloud value (can be Standby even in motion); a separate inferred state sensor explains conflicts.
- Three new diagnostic sensors are installed automatically. They poll Home Assistant state rather than contacting or waking the car:
  - `sensor.xpeng_<VIN suffix>_operational_state` (search "Operational state" in the UI), with raw cloud vehicleMode, meaningful-data age, source-specific GPS/speed/shift/mode ages, and last transition.
  - `sensor.xpeng_<VIN suffix>_motion_diagnosis` (search "Motion diagnosis"), showing Parked, Driving or Unknown from recent shift/speed evidence and transition count.
  - `sensor.xpeng_<VIN suffix>_gps_freshness` (search "GPS freshness"), Fresh/Stale/Unknown/Cached and age of last real GPS-bearing MQTT frame.
- The last-known latitude, longitude and heading become unknown after 15 minutes without a real GPS-bearing packet; speed/gear become unknown after 10 minutes without a real matching field (except a newly observed P reports zero speed).
- The 30-minute watchdog escalation still has a 12-hour wake cap. This beta intentionally does **not** wake a parked car every minute to make movement immediately visible.
- Transition comparison now occurs on the HA event loop, not the paho network thread. Failed MQTT publishes are tracked separately from actual sends.

## Build and installation

Open [PR #7](https://github.com/matanmesika/xpengtoolkit/pull/7), select a successful **XPENG live-location freshness regression** workflow, and download artifact `XPENG-Toolkit-0.6.18b2`. The GitHub artifact contains an **inner** ZIP with a `custom_components/xpeng` directory.

1. Make a full Home Assistant backup and an extra copy of the existing `/config/custom_components/xpeng` directory.
2. Prefer a separate test Home Assistant. You **cannot** run two versions of an integration with domain `xpeng` simultaneously in the same Home Assistant process.
3. On the test HA instance or after backing up the live one, stop Home Assistant. Extract the *inner ZIP* under `/config` replacing `custom_components/xpeng`, then restart. Confirm `manifest.json` reports `0.6.18b2`.
4. Leave `/config/xpeng-certs`, the installed credential renewer, and captcha solver unchanged. Do not publish tokens, full VIN or GPS tracks.
5. Do not click "Update" in HACS during the beta; it can overwrite the test build.

## On-car validation

Record timestamps safely. Never operate a phone while driving.

| Test | Expected result |
|---|---|
| After boot or refresh with no new MQTT | Operational state is Cached / Silent / Unknown, not falsely Driving; GPS freshness does not claim new fix |
| Parked, shift P | Motion diagnosis says Parked, speed zero, GPS last known but eventually Stale if no fixes |
| Car wakes with Standby mode | Raw vehicle_mode may read Standby; derived state shows Standby only if motion has not been verified |
| Change P → D and drive | Motion diagnosis Driving once real shift/speed message arrives; transition count increments on genuine P→D |
| Stable speed with real frames | Speed remains displayed until source fields become stale; GPS age tracks GPS messages, not other sensor pings |
| Brief stop in D | Still Driving (active cadence), even at zero speed if fresh D |
| D → P, end journey | Parked and speed zero on real P signal; no spurious repeat transitions |
| Park 10–20 minutes | Stale drive and charge flags expire; no old 110-km/h speed presented as live |
| MQTT broker outage | Operational state Disconnected; no false last-query success |
| Cloud only sends ~309-byte stubs | GPS freshness ages and watchdog may eventually escalate under existing 30min/12h policy |
| Manual Wake after parked GPS stuck | Last GPS age resets only when a GPS-bearing frame arrives |

For an investigation, export Diagnostics and Debug Logging, plus state history of the above sensors and `polling_status` (`seconds_since_data`, `last_query`, `last_query_failed_at`, `mqtt_connected`). Redact account and location information before posting anywhere public.

**Limit of proof:** A car that begins driving while the cloud delivers no pushes cannot be detected instantaneously from silence alone. `Silent` cannot establish `Sleep`. A green test suite is not the same as road-tested GPS reliability.

## Rollback

Stop HA, restore the previous `custom_components/xpeng` from backup, restart, and verify sensors. Do not remove the certificate directory or credential add-ons. The beta does not alter credentials.
