#!/usr/bin/env python3
"""Decide whether the fallback crawl in main.yml has anything to do.

The local publisher (tools/local_publish.py) is the primary collector and runs
at fixed KST slots. main.yml runs later and should stay silent when the slot
it follows was already published, so a healthy day sends no requests to Nate
from GitHub at all.

Input is the author date of the newest commit that touched newsURL.json. The
author date is used because sub.yml amends `News` commits, which rewrites the
committer date but keeps the author date. It is an approximation of "when the
data was collected": a commit that restores old data by hand also gets a new
author date, so review the gate when publishing data manually.

Rule: take the newest slot that started at least GRACE_MINUTES ago. If the
data was committed at or after that slot's start, it is fresh; otherwise the
slot was missed and the fallback should run. One exception: from
LEAD_MINUTES before a slot until GRACE_MINUTES after it, the local publisher
may be crawling for that slot, so the gate defers instead of starting a
fallback that would publish first and make the complete local result fail
with remote_moved. Scheduled runs (one hour after each slot) never fall in
that window; only delayed ones do, and the next run decides again.

A last data commit up to FUTURE_TOLERANCE_MINUTES in the future is accepted
as fresh, because the committing machine's clock and GitHub's can differ by
a little; anything further ahead is unknown.

Exit status: 0 with `stale=true|false` on stdout when a decision was made,
2 when the input cannot be trusted (unparsable or future date). An unknown
state is never treated as fresh.
"""

import argparse
import sys
from datetime import datetime, time, timedelta, timezone


KST = timezone(timedelta(hours=9))
# Same times as tools/local.newsjson.publish.plist.template.
SLOTS_KST = (time(5, 44), time(10, 44), time(15, 44), time(20, 44))
# Long enough for a local crawl that started on time to have been pushed.
GRACE_MINUTES = 45
# How long a local run may be busy before and after a slot from the gate's
# point of view: a fallback started LEAD_MINUTES before a slot (the main.yml
# job timeout) could still be pushing when the slot begins.
LEAD_MINUTES = 20
# Clock differences between GitHub and the committing machine.
FUTURE_TOLERANCE_MINUTES = 5


class GateError(ValueError):
    pass


def parse_timestamp(text):
    """Parse an ISO 8601 timestamp that carries a UTC offset."""
    if not isinstance(text, str) or not text.strip():
        raise GateError(f"Missing timestamp: {text!r}")
    cleaned = text.strip()
    if cleaned.endswith(("Z", "z")):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(cleaned)
    except ValueError as error:
        raise GateError(f"Unparsable timestamp: {text!r}") from error
    if value.tzinfo is None:
        raise GateError(f"Timestamp has no UTC offset: {text!r}")
    return value


def due_slot(now):
    """Newest slot start that is at least GRACE_MINUTES before now."""
    cutoff = now.astimezone(KST) - timedelta(minutes=GRACE_MINUTES)
    for days_back in (0, 1):
        day = cutoff.date() - timedelta(days=days_back)
        for slot in sorted(SLOTS_KST, reverse=True):
            start = datetime.combine(day, slot, tzinfo=KST)
            if start <= cutoff:
                return start
    raise GateError(f"No slot found before {now.isoformat()}")


def in_local_run_window(now):
    """True from LEAD_MINUTES before a slot until GRACE_MINUTES after it."""
    local = now.astimezone(KST)
    for days in (-1, 0, 1):
        day = local.date() + timedelta(days=days)
        for slot in SLOTS_KST:
            start = datetime.combine(day, slot, tzinfo=KST)
            if (
                start - timedelta(minutes=LEAD_MINUTES)
                <= local
                < start + timedelta(minutes=GRACE_MINUTES)
            ):
                return True
    return False


def decide(now, last_data_commit):
    """Return (stale, slot_start, verdict) where verdict is fresh, stale or deferred.

    Raises GateError when the input is unusable.
    """
    if now.tzinfo is None or last_data_commit.tzinfo is None:
        raise GateError("Timestamps must carry a UTC offset")
    if last_data_commit > now + timedelta(minutes=FUTURE_TOLERANCE_MINUTES):
        raise GateError(
            "Last data commit is in the future: "
            f"{last_data_commit.isoformat()} > {now.isoformat()}"
        )
    slot = due_slot(now)
    if last_data_commit >= slot:
        return False, slot, "fresh"
    if in_local_run_window(now):
        return False, slot, "deferred"
    return True, slot, "stale"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--last",
        required=True,
        help="author date of the newest commit touching newsURL.json (ISO 8601)",
    )
    parser.add_argument(
        "--now", help="override the current time (ISO 8601 with offset), for tests"
    )
    args = parser.parse_args(argv)

    try:
        now = parse_timestamp(args.now) if args.now else datetime.now(timezone.utc)
        last = parse_timestamp(args.last)
        stale, slot, verdict = decide(now, last)
    except GateError as error:
        print(f"gate_unknown: {error}", file=sys.stderr)
        return 2

    explanation = {
        "fresh": "fresh, nothing to do",
        "stale": "stale, fallback should run",
        "deferred": "stale, but the local publisher may be running now; deferred",
    }[verdict]
    print(
        f"due slot {slot.isoformat()}, last data commit"
        f" {last.astimezone(KST).isoformat()}: {explanation}",
        file=sys.stderr,
    )
    print(f"stale={'true' if stale else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
