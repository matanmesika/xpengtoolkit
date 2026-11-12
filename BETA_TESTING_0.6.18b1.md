# XPENG Toolkit 0.6.18b1 — isolated telemetry test build

**Status:** EXPERIMENTAL. Do not merge into main / do not publish as a stable release before road testing.

## Scope

- Deduplicate repeated MQTT partial responses so duplicate stubs cannot keep the data freshness watchdog falsely healthy.
- Merge partial `instant.*` and `interval.*` sections by field, preserving old coordinates and charge settings when a narrow speed/door update arrives. Explicit numeric zero is preserved.
- Determine movement from the freshest actual shift and/or speed source (speed is carried by `interval.drive.speed`, shift by `instant.drive.shift_state`).
- Expire cached D/positive speed evidence after ten minutes of silence to prevent endless forced uploads against a sleeping car; expired charging evidence is also not treated as actively charging.
- Derive `vehicle_mode = Driving` from recent observed vehicle motion even if the cloud's `ext.vehicleMode` says `Standby`; report speed 0 when a fresher P signal is received.
- Preserve the 30-minute escalation and 12-hour wake cooldown; no change to credential renewal, CAPTCHA or MQTT broker.

## Download

Go to the [beta pull request](https://github.com/matanmesika/xpengtoolkit/pull/6), open its **Checks** tab and the latest green **XPENG live-location freshness regression** run. Under **Artifacts**, download `XPENG-Toolkit-0.6.18b1`. The downloaded GitHub artifact contains `XPENG-Toolkit-0.6.18b1.zip`, with a `custom_components/xpeng` folder inside.

Only use artifacts from **green** runs. They expire after 30 days.

## Before a local test

1. Make a Home Assistant backup. Also copy the current `/config/custom_components/xpeng` directory somewhere safe for rollback. Never include or publish the contents of `/config/xpeng-certs`.
2. If you have a separate test Home Assistant instance, install this beta there first. You cannot load two integrations with domain `xpeng` side-by-side into a single Home Assistant instance.
3. To test on your normal HA system, stop Home Assistant, extract **the inner beta zip** into `/config` so it replaces files under `/config/custom_components/xpeng`, and restart. Confirm `manifest.json` says `0.6.18b1`. This is an intentional temporary replacement, **not** a HACS update. Do not let HACS overwrite the beta during the experiment.
4. Your certificates must remain in the separate `/config/xpeng-certs` directory. Never reset/recreate the integration or credential renewer merely to test this beta.

## Test sequence

- Before driving: note the `polling_status` attributes (`cadence`, `seconds_since_data`, `last_query`), the `device_tracker` GPS age, vehicle mode, speed and shift.
- During a normal drive (without interacting with a phone while driving): record the start/end times and let MQTT push data on its own. Do **not** manually wake during the initial diagnostic interval.
- After parking: confirm shift returns to P, speed reads 0 and old D/charging data does not keep force-uploading once it becomes stale.
- If GPS remains stuck, note exactly when the app displayed the new location and when HA last got a fix. A single manual `Wake vehicle` after safely parking is a valid comparison test, but record when it was pressed.
- Export debug logs and device-tracker/speed/shift state history for comparison. Redact VIN, account IDs, tokens, exact coordinates and credential material before publishing any logs.

## Rollback

Stop HA, restore the backed-up `/config/custom_components/xpeng`, then restart. HACS can be used to reinstall the original published version. The beta does not write certificates or change login details.

## Known limits

The main fault observed in the 2026-10-20 logs is a parked-to-driving transition missed while XPENG's cloud returned only tiny passive-query stubs. This beta prevents false freshness and cache corruption, but it cannot guarantee **instant** motion detection if XPENG sends no push at all; automatic force-upload escalation is intentionally conservative to protect battery life. Promotion to stable depends on on-car testing, not just CI.
