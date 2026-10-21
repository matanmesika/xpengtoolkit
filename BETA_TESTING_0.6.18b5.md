# XPENG Toolkit 0.6.18b5 — Fix GPS freshness timer thread error

**Draft beta only.** Not yet released for HACS until GitHub Actions tests pass. Do not merge into stable.

## Confirmed live error, October 21, 2026

Uploaded Home Assistant log captured 15:04–17:15 local. After startup, 131 times (about once a minute) Home Assistant reported:

`RuntimeError: Detected that custom integration 'xpeng' calls async_write_ha_state from a thread other than the event loop`

The originating call site was `device_tracker.py::_refresh_location_freshness` scheduled by `async_track_time_interval` every 60 seconds. Its synchronous function lacked Home Assistant's `@callback`, so the scheduler executed it in an executor and the call to `async_write_ha_state()` violated HA's event-loop threading rules.

**Fix:** Mark `_refresh_location_freshness` with `@callback` and import it from `homeassistant.core`. This causes HA to run the handler on its event loop. Regression test now inspects the actual scheduled bound method for the callback marker and fails if absent.

## Findings from the same log

- 15:04–16:01: passive state queries returned ~309-byte stubs; no fresh full vehicle snapshot.
- 16:02:22: full state arrived on its own, vehicle motion detected, and transition-triggered force-upload sent.
- 16:03–16:14: real full frames and partial updates during observed activity. Eleven transition detections; cooldown bounded immediate force-upload requests.
- 16:28: MQTT credentials updated and client reconnected.
- 17:01:42: full state received again without watchdog force-upload.
- 15:34 onwards: watchdog abstained because the stored previous automatic wake was about 470 minutes ago; shared 12-hour backoff held.
- 15:04 indicated previous automatic wake approximately 440 minutes ago (~07:44), matching the earlier observed renewed GPS, but the original 07:44 log is needed to determine cold-start versus watchdog wake.
- Raw cloud `vehicleMode` stayed Standby in full-frame info logs despite inferred driving.
- The physical moment of shift into D/R, precise GPS-coordinate freshness and whether eleven transition detections match actual gear changes cannot be proven from this logging alone.

## Deployment

Once checks pass, install **only** the separately published HACS pre-release v0.6.18b5 from the user-owned fork. Take a full HA backup. No credentials, config-entry records, or car certificates need to be removed. Restart HA. Check the warnings/errors no longer occur after several GPS-expiry timer ticks (e.g. 5–10 minutes); then repeat parked/drive and compare Motion diagnosis, Operational state, GPS freshness, and raw gear history.

Do not post raw logs publicly: they may contain VIN, account details, precise GPS and cloud credentials.
