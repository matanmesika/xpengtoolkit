# XPENG Toolkit 0.6.18b4 — Signed reverse speed validation

This build supersedes the earlier beta **0.6.18b2** with one focused fix:
- `shift_state == 2` is already recognised as Reverse (R), hence **R with speed zero** counts as an active drive gear; this was correct.
- `sensor.speed` uses `round(float(raw), 1)`, **not** `abs(raw)`. Negative speeds are left negative.
- The derived movement state previously checked `float(speed) > 0`. This ignored a negative signed speed if a gear frame was missing or older.
- It now checks for **finite, nonzero** signed speed; forwards and backwards both count as motion. A newer, fresh P still takes precedence over an old speed.
- Extra automated tests exercise signed speed, P/R priority, reverse at 0, malformed NaN/infinite values and old data.

**Observed evidence:** The previously submitted drive logs contain only D and P gear states and non-negative speeds (0 to 116.9). They do not prove whether this G6 actually emits a negative numeric speed in Reverse; validate that on the vehicle with logging.

Follow the [complete backup, installation, sensor and rollback instructions for the previous test build](BETA_TESTING_0.6.18b2.md), **but install only the latest successful 0.6.18b4 ZIP artifact from [draft PR #7](https://github.com/matanmesika/xpengtoolkit/pull/7).** Do not install older b2 artifacts.

**Specific extra on-car test:** With a passenger monitoring Home Assistant or from the saved history afterwards, perform a short safe reverse movement, then P, then D. Confirm:
- Raw `sensor.shift_state`: R, P, D (subject to XPENG cloud delivery).
- `Motion diagnosis`: Driving upon R/signed speed; Parked after a current P; Driving upon D.
- Raw speed: preserve the actual sign the cloud sends; do not claim negative until observed.
- `Operational state`: Driving once fresh reverse movement is detected, even if raw cloud `vehicle_mode` remains Standby.
- If no MQTT update arrives, sensors eventually become unknown/silent; this cannot prove sleep.

Do not operate a phone during driving. This beta changes no wake or battery policy.


## b4 pre-install audit additions

- A newly detected D/R transition from either Parked or Unknown requests a bounded full-state upload if automatic wake permission is enabled. Passive updates alone were insufficient in the captured case.
- Charging-state and power fields have independent source timestamps to prevent old charging values overriding newer stop/start reports.
- A cold Home Assistant restart shares the same durable 12-hour automatic wake cooldown as the watchdog. An offline broker at startup does not use up the wake allowance.
- The beta's checks now compile all integration Python modules and run the focused scenarios, the full unittest suite and the full pytest suite on Python 3.13 and 3.14. The latest validated run is https://github.com/matanmesika/xpengtoolkit/actions/runs/39524437477 (104 focused passed, 750 unit tests run with 5 skipped, 831 pytest passed with 5 skipped).

**Still unverified without a real vehicle:** The cloud may fail to push any new D/R or speed packet at the start of a trip; in that case the integration cannot know instantly that the car left P, and it will preserve the 30-minute watchdog/12-hour parked wake policy. 'Silent' is not proof of vehicle sleep. Full client/firmware behavior and power impact require the first controlled road test. This is a draft, not a stable release.
