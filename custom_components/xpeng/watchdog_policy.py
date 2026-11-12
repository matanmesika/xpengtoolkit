"""When the MQTT silence watchdog should stop being polite.

The watchdog reconnects and sends a passive state query. Both are cloud-side
only and never wake the car, which is why they are free to run unconditionally.
But a reconnect does not always clear a stalled fanout, and when it does not,
the passive query is not a weaker version of the fix — it is not a fix at all.

Measured 2026-10-06: 11h31m with the socket up and ~66 reconnects, each one
authenticating, subscribing and sending a passive query, and not one byte back.
The hourly REST fallback reported the car's state normally throughout, so the
account, the tokens and the car were all fine; only the fanout was wedged. At
08:57:07 a single `service_type=19 / cmd_type:2` produced a 4492-byte full state
1.2 seconds later. The passive query sent four minutes earlier had produced
nothing.

So there is a point at which waiting is no longer the safe choice. That is what
this module decides, and it is a separate module with no Home Assistant import
so the decision can be tested: the answer wakes a physical car, and a policy
that wakes a car should not be verifiable only by watching one.

The two bounds are both required:

  * ESCALATE_AFTER_SECONDS of *unbroken* silence before a force-upload is
    allowed at all. Reconnecting gets this long to work first.
  * ESCALATE_BACKOFF_SECONDS between force-uploads, whatever happens in
    between.

The second bound is what keeps this from becoming the scheduled `cmd_type:2`
this project deliberately avoids. It has to be time-based rather than
"per stall" or "reset once data returns", because a force-upload during a
wedged fanout yields exactly *one* full state and then the silence resumes —
so treating that one message as recovery would let a stalled car be woken every
half hour. Counting from the last escalation instead means the worst case is one
wake per ESCALATE_BACKOFF, and a stall that begins after a healthy stretch still
escalates promptly, because by then the previous escalation is long past.

The first bound was two hours and is now half an hour. Nothing about the wake
*rate* changes — that is the backoff's job, and a car parked and asleep all day
is woken at most twice either way. What changes is how long a stalled
*transition* goes unnoticed. A parked car answers the passive query with a stub,
and that is why the decision is keyed on staleness rather than silence: a stub
resets the *silence* clock (so no reconnect is wasted on a car that is simply
asleep) and not this one, because it carries nothing. A car that stub-answers for
half an hour without delivering a byte therefore does escalate. In
that state a drive or a charge can begin completely unseen, because the fanout
that should have announced it is the thing that is wedged, and a passive query
does not punch through one (observed 2026-08-30, mid-drive: ~1h of zero data
until a force-upload). Two hours of that is two hours of driving that never
reaches Home Assistant or EVConduit.

Half an hour rather than something shorter because it has to stay well clear of
the reconnect cadence — RECONNECT_BACKOFF, 10 minutes, in __init__.py — so a
reconnect and its passive query get several honest attempts before we conclude
they cannot work.
"""

from __future__ import annotations

#: Unbroken silence before a force-upload is allowed. Half an hour: several
#: reconnect cycles' worth, so a connection that merely needed a nudge has had
#: its chance, and short enough that a stalled start-of-drive is noticed while
#: the drive is still happening.
ESCALATE_AFTER_SECONDS: float = 30 * 60

#: Minimum gap between force-uploads. Twelve hours bounds the cost of a fanout
#: that is wedged for days to at most two wakes a day, and the hourly REST
#: fallback keeps the car's core values current in between.
ESCALATE_BACKOFF_SECONDS: float = 12 * 3600

#: Minimum gap between two force-uploads that are justified by *motion*. The
#: number above exists to bound how often a parked car is woken; a moving car is
#: already awake, so its escalation only needs a cadence, not a budget. Ten
#: minutes: faster than a drive can hide, slow enough that a wedged fanout
#: cannot turn every watchdog tick into a force-upload.
MOTION_ESCALATE_BACKOFF_SECONDS: float = 10 * 60


def should_escalate(
    silence_seconds: float,
    last_escalation: float,
    now: float,
    escalate_after: float = ESCALATE_AFTER_SECONDS,
    escalate_backoff: float = ESCALATE_BACKOFF_SECONDS,
) -> bool:
    """Whether to send a force-upload (which wakes the car) right now.

    `last_escalation` is 0.0 when none has been sent this session, which is a
    real value rather than "unknown": it means the backoff has nothing to count
    from, so the first escalation is governed only by the silence bound.
    """
    if silence_seconds < escalate_after:
        return False
    if last_escalation and (now - last_escalation) < escalate_backoff:
        return False
    return True


def should_escalate_moving(
    silence_seconds: float,
    last_escalation: float,
    now: float,
    last_motion_at: float,
    escalate_after: float = ESCALATE_AFTER_SECONDS,
    motion_backoff: float = MOTION_ESCALATE_BACKOFF_SECONDS,
) -> bool:
    """Whether motion evidence justifies a force-upload outside the 12 h budget.

    Measured on issue #3's capture (2026-10-21): the fanout stalled mid-drive at
    16:15, and from 16:46 until the cloud recovered by itself at 17:01 the
    watchdog refused 16 escalations as "one per 12h" - the budget had been spent
    on a parked stall earlier that day. Telemetry stayed frozen for 46 minutes
    while the car was being driven, where a force-upload cannot wake anything.

    Three bounds replace the sleeping-car budget, all of them cheap to satisfy
    only when they should be:

    * the caller's staleness bound still applies - motion is a reason to skip the
      12 h budget, not the 30 minute patience;
    * the motion must be *newer than the last escalation*, so one episode of
      motion buys one wake rather than one per watchdog tick, and a fanout that
      stays wedged cannot keep escalating on the same old evidence;
    * `motion_backoff` since that wake, as a floor for the case where the wake
      did produce data and the car is still moving.

    `last_motion_at` is 0.0 when the last accepted state said the car was parked
    or said nothing about driving; both mean "do not spend the sleeping car's
    allowance on this".
    """
    if silence_seconds < escalate_after:
        return False
    if not last_motion_at:
        return False
    if last_escalation and (now - last_escalation) < motion_backoff:
        return False
    if last_escalation and last_motion_at <= last_escalation:
        return False
    return True


# GPS must be tracked independently of unrelated MQTT sensor changes. Requiring
# RECENT independent motion evidence prevents old cached D/P from waking a car
# that has since parked. These bounds are deliberately not configuration knobs.
GPS_STALE_AFTER_SECONDS: float = 30 * 60
GPS_MOTION_MAX_AGE_SECONDS: float = 15 * 60


def should_escalate_stale_gps(
    fix_age_seconds: float | None,
    last_escalation: float,
    now: float,
    last_motion_at: float,
) -> bool:
    """Recover a frozen GPS during *recently confirmed* activity.

    A charge/HVAC partial can keep generic MQTT freshness low for hours while
    the tracker receives no new GPS. A GPS-only stall can therefore use the
    same bounded motion escalation as an all-state stall, but only after a
    real timestamped fix has aged 30min and new motion evidence was seen in
    the last 15min. An asleep car or an unknown fix age never qualifies.
    """
    if fix_age_seconds is None or fix_age_seconds < GPS_STALE_AFTER_SECONDS:
        return False
    if not last_motion_at or last_motion_at > now:
        return False
    if now - last_motion_at > GPS_MOTION_MAX_AGE_SECONDS:
        return False
    return should_escalate_moving(
        fix_age_seconds, last_escalation, now, last_motion_at
    )
