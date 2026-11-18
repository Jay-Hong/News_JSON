"""Add this machine's recent crawl results to the previous publish.

Used by tools/local_publish.py only. A local crawl that is cut off before it
reaches enough articles fails validation and publishes nothing, but the
article pages it did read are still correct. Without this step the next run
would ask for the same pages again from the same previously published file,
so one failed slot made the next one more likely to fail as well.

The output is the previous publish followed by the items of earlier runs, in
the order of --run (the publisher passes the newest run first), as one JSON
list in the published format. The spider and the assembler read it exactly
like the previously published file: fallback.build_cache() keeps the first
valid entry per article id, so a valid
entry of the previous publish always wins, an invalid one does not hide a
local entry, and the article-id date rule (today or yesterday, KST) and the
image rule (same title and company) apply unchanged. The values are the
results of those runs, not fresh observations: a run's result also contains
fields it took from its own previous publish. Because a valid entry of the
previous publish wins, an image it lists as empty is not filled in from an
earlier run, and that article page may be requested again.

An earlier run is used only if its output passes every check of
validate_result.py in fallback mode except the two coverage thresholds that
a cut-off run is expected to miss, and its metrics name the same run id as
its directory. Which runs are offered (recorded in the history, at most 48
hours old, not the current run) is the publisher's decision.

    python -m nate_news.reuse --previous P --kst-day YYYYMMDD \
        --run DIR [--run DIR ...] --out MERGED --report REPORT
"""

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

# validate_result.py sits next to scrapy.cfg, which `python -m` from the
# Scrapy project directory puts on sys.path.
import validate_result
from nate_news import fallback

RESULT_FILE = "nate_result.json"
METRICS_FILE = "crawl_metrics.json"


class Rejected(Exception):
    def __init__(self, reason, detail=""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def _read_regular_json(path):
    """Return (parsed JSON, sha256 of the bytes) of a regular, non-link file."""
    try:
        if not path.is_symlink() and not path.exists():
            # For example a run that stopped before crawling.
            raise Rejected("missing", path.name)
        if path.is_symlink() or not path.is_file():
            raise Rejected("not_regular_file", path.name)
        data = path.read_bytes()
        return json.loads(data.decode("utf-8")), hashlib.sha256(data).hexdigest()
    # ValueError covers decoding and parsing errors; RecursionError a JSON
    # document nested too deeply to parse.
    except (OSError, ValueError, RecursionError) as error:
        raise Rejected("unreadable", f"{path.name}: {error}") from error


def check_run(run_dir):
    """Return the items of an earlier run that may be reused.

    Returns (items, result_sha256, metrics_sha256) or raises Rejected.
    """
    run_dir = Path(run_dir)
    try:
        is_directory = not run_dir.is_symlink() and run_dir.is_dir()
    except OSError as error:
        raise Rejected("unreadable", f"{run_dir.name}: {error}") from error
    if not is_directory:
        raise Rejected("not_a_directory", run_dir.name)
    items, result_sha256 = _read_regular_json(run_dir / RESULT_FILE)
    metrics, metrics_sha256 = _read_regular_json(run_dir / METRICS_FILE)
    try:
        # Also rejects metrics that are not an object or not from a
        # fallback crawl (a full crawl's metrics carry no mode).
        validate_result.validate_result(
            items, metrics, mode=fallback.MODE, check_coverage=False
        )
    # ValidationError is a ValueError. Anything else the validator raises on
    # files it was never meant to see (for example urlparse on a malformed
    # URL, or a field of an unexpected type) only means this run is not
    # reused; it must not cost the other runs their reuse.
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
        raise Rejected("unsound", f"{type(error).__name__}: {error}"[:300]) from error
    if metrics.get("run_id") != run_dir.name:
        raise Rejected(
            "run_id_mismatch", f"{metrics.get('run_id')!r} in {run_dir.name}"
        )
    return items, result_sha256, metrics_sha256


def merge(previous_items, run_dirs, kst_day):
    """Return (merged item list, report dict)."""
    if not isinstance(previous_items, list):
        raise fallback.FallbackError("Previous publish is not a JSON list")
    merged = list(previous_items)
    accepted = []
    rejected = []
    for run_dir in run_dirs:
        try:
            items, result_sha256, metrics_sha256 = check_run(run_dir)
        except Rejected as rejection:
            rejected.append(
                {
                    "run_id": Path(run_dir).name,
                    "reason": rejection.reason,
                    "detail": rejection.detail,
                }
            )
            continue
        merged.extend(items)
        accepted.append(
            {
                "run_id": Path(run_dir).name,
                "items": len(items),
                "result_sha256": result_sha256,
                "metrics_sha256": metrics_sha256,
            }
        )
    previous_ids = fallback.build_cache(previous_items, kst_day)
    merged_ids = fallback.build_cache(merged, kst_day)
    report = {
        "accepted": accepted,
        "rejected": rejected,
        "rejected_counts": dict(Counter(entry["reason"] for entry in rejected)),
        # Reusable article ids (for this KST day) that the previous publish
        # alone does not provide. How many of them end up in the published
        # file depends on the ranking of the run that reads them.
        "added_ids": len(merged_ids) - len(previous_ids),
    }
    return merged, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--kst-day", required=True)
    parser.add_argument("--run", type=Path, action="append", default=[])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        kst_day = (
            fallback.article_day(args.kst_day + "n")
            if re.fullmatch(r"[0-9]{8}", args.kst_day)
            else None
        )
        if kst_day is None:
            raise fallback.FallbackError(f"Invalid --kst-day: {args.kst_day!r}")
        previous = json.loads(args.previous.read_text(encoding="utf-8"))
        merged, report = merge(previous, args.run, kst_day)
    except (OSError, ValueError) as error:  # FallbackError is a ValueError
        print(f"Reuse failed: {error}")
        return 1

    fallback.write_text_atomically(
        args.out, json.dumps(merged, ensure_ascii=False) + "\n"
    )
    fallback.write_text_atomically(
        args.report, json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        f"reuse: {len(report['accepted'])} of {len(args.run)} earlier runs used,"
        f" {report['added_ids']} article ids added,"
        f" rejected {report['rejected_counts'] or 'none'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
