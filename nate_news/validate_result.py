import argparse
import json
import math
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urlparse


REQUIRED_PAGES = (
    "sisa-1",
    "sisa-2",
    "spo-1",
    "spo-2",
    "ent-1",
    "ent-2",
)
REQUIRED_SECTIONS = ("sisa", "spo", "ent")
MINIMUM_TOTAL = 200
MINIMUM_PER_SECTION = 51
# Nate normally exposes 50 rows per page. The first page should stay nearly
# full, while a second page may legitimately be short during dawn runs.
MINIMUM_FIRST_PAGE = 45
MAXIMUM_PER_PAGE = 50
MINIMUM_COVERAGE = 0.90

# "full" is the normal crawl: every ranked article page was requested.
# "fallback" is the degraded crawl for machines that lose access to Nate
# mid-crawl (nate_news_fallback + nate_news.fallback). It keeps every ranking,
# item and coverage rule below. It differs in two places only: the per-article
# request accounting (and with it the pipeline-error check, since the
# fallback has no item pipelines), and the close reasons that still count as
# a usable result. An empty image is allowed in both modes; an empty title or
# company is allowed in neither.
MODES = ("full", "fallback")
# stop reason -> how the spider's close reason must look for it. Checked here
# independently of nate_news.fallback, which derives one from the other.
FALLBACK_STOP_REASONS = {
    "complete": lambda reason: reason == "finished",
    "transport_breaker": lambda reason: reason.startswith(
        "fallback_transport_failures:"
    ),
    "access_refused": lambda reason: reason.startswith("fallback_access_refused:"),
    "request_cap": lambda reason: reason == "fallback_request_cap",
    "time_budget": lambda reason: reason == "closespider_timeout",
}
FALLBACK_RANK_REQUESTS = 6
FALLBACK_MAXIMUM_PC_REQUESTS = 6
FALLBACK_SOURCE_GROUPS = {
    "title_sources": ("article", "cache", "ranking"),
    "company_sources": ("article", "pc", "cache"),
    "image_sources": ("article", "pc", "cache", "empty_confirmed", "empty_unobserved"),
}


class ValidationError(ValueError):
    pass


def _is_non_negative_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _full_counters(metrics, expected_total, exported):
    articles_discovered = metrics.get("articles_discovered")
    article_items_yielded = metrics.get("article_items_yielded")
    article_failures = metrics.get("article_failures")
    item_scraped_count = metrics.get("item_scraped_count")
    item_dropped_count = metrics.get("item_dropped_count")
    item_error_count = metrics.get("item_error_count")
    metric_counts = {
        "articles_discovered": articles_discovered,
        "article_items_yielded": article_items_yielded,
        "article_failures": article_failures,
        "item_scraped_count": item_scraped_count,
        "item_dropped_count": item_dropped_count,
        "item_error_count": item_error_count,
    }
    invalid_metric_counts = {
        name: value
        for name, value in metric_counts.items()
        if not _is_non_negative_int(value)
    }
    if invalid_metric_counts:
        raise ValidationError(
            f"Invalid article counters: {invalid_metric_counts}"
        )

    processed_total = article_items_yielded + article_failures
    if (
        articles_discovered != expected_total
        or processed_total != expected_total
        or item_error_count != 0
        or article_items_yielded
        != item_scraped_count + item_dropped_count + item_error_count
        or exported != item_scraped_count
    ):
        raise ValidationError(
            "Inconsistent crawl counters: "
            f"expected={expected_total}, discovered={articles_discovered}, "
            f"processed={processed_total}, yielded={article_items_yielded}, "
            f"scraped={item_scraped_count}, dropped={item_dropped_count}, "
            f"item_errors={item_error_count}, exported={exported}"
        )
    return {
        "article_failures": article_failures,
        "dropped": item_dropped_count,
    }


def _fallback_counters(metrics, expected_total, exported):
    article_requests = metrics.get("article_requests")
    requests = metrics.get("requests")
    if not isinstance(article_requests, dict) or not isinstance(requests, dict):
        raise ValidationError(
            "Invalid fallback request metrics: "
            f"article_requests={article_requests!r}, requests={requests!r}"
        )
    counts = {
        "articles_discovered": metrics.get("articles_discovered"),
        "items_exported": metrics.get("items_exported"),
        "excluded_no_company": metrics.get("excluded_no_company"),
        "dropped_duplicate_url": metrics.get("dropped_duplicate_url"),
        "dropped_duplicate_title": metrics.get("dropped_duplicate_title"),
        "spider_errors": metrics.get("spider_errors"),
        "article_requests.planned_before_cap": article_requests.get(
            "planned_before_cap"
        ),
        "article_requests.skipped_by_cap": article_requests.get("skipped_by_cap"),
        "article_requests.planned": article_requests.get("planned"),
        "article_requests.attempted": article_requests.get("attempted"),
        "article_requests.ok": article_requests.get("ok"),
        "article_requests.failed": article_requests.get("failed"),
        "article_requests.not_requested": article_requests.get("not_requested"),
        "requests.mobile_rank": requests.get("mobile_rank"),
        "requests.pc_rank": requests.get("pc_rank"),
        "requests.article": requests.get("article"),
    }
    for group, sources in FALLBACK_SOURCE_GROUPS.items():
        values = metrics.get(group)
        if not isinstance(values, dict) or set(values) != set(sources):
            raise ValidationError(f"Invalid fallback {group}: {values!r}")
        for source in sources:
            counts[f"{group}.{source}"] = values[source]
    invalid_counts = {
        name: value
        for name, value in counts.items()
        if not _is_non_negative_int(value)
    }
    if invalid_counts:
        raise ValidationError(f"Invalid fallback counters: {invalid_counts}")

    def total(group):
        return sum(counts[f"{group}.{source}"] for source in FALLBACK_SOURCE_GROUPS[group])

    if (
        counts["articles_discovered"] != expected_total
        or counts["items_exported"] != exported
        or counts["items_exported"]
        + counts["excluded_no_company"]
        + counts["dropped_duplicate_url"]
        + counts["dropped_duplicate_title"]
        != expected_total
        or any(total(group) != exported for group in FALLBACK_SOURCE_GROUPS)
        or counts["spider_errors"] != 0
        or counts["article_requests.attempted"]
        != counts["article_requests.ok"] + counts["article_requests.failed"]
        or counts["article_requests.planned"]
        != counts["article_requests.attempted"]
        + counts["article_requests.not_requested"]
        or counts["article_requests.planned_before_cap"]
        != counts["article_requests.planned"]
        + counts["article_requests.skipped_by_cap"]
        or counts["article_requests.planned_before_cap"] > expected_total
        # Each planned article is requested at most once, and nothing else is.
        or counts["requests.article"] != counts["article_requests.attempted"]
        or counts["requests.mobile_rank"] != FALLBACK_RANK_REQUESTS
        or counts["requests.pc_rank"] > FALLBACK_MAXIMUM_PC_REQUESTS
    ):
        raise ValidationError(f"Inconsistent fallback counters: {counts}")

    # A crawl that claims it made every planned request must have made them.
    stop_reason = metrics["stop_reason"]
    if stop_reason in ("complete", "request_cap") and (
        counts["article_requests.not_requested"] != 0
        or (counts["article_requests.skipped_by_cap"] > 0)
        != (stop_reason == "request_cap")
    ):
        raise ValidationError(
            f"Fallback stop reason {stop_reason!r} does not match the article "
            f"requests: {article_requests}"
        )
    return {
        "excluded_no_company": counts["excluded_no_company"],
        "dropped": counts["dropped_duplicate_url"] + counts["dropped_duplicate_title"],
        "article_requests": article_requests,
        "stop_reason": stop_reason,
    }


def validate_result(items, metrics, mode="full"):
    if mode not in MODES:
        raise ValidationError(f"Unknown validation mode: {mode!r}")
    if not isinstance(items, list):
        raise ValidationError("Spider result is not a JSON list")
    if not isinstance(metrics, dict):
        raise ValidationError("Crawl metrics are not a JSON object")
    # The caller states which crawl it ran; metrics cannot relax the rules by
    # declaring a mode themselves. Metrics from the full crawl carry no mode.
    metrics_mode = metrics.get("mode", "full")
    if metrics_mode != mode:
        raise ValidationError(
            f"Metrics are from a {metrics_mode!r} crawl, "
            f"but validation mode is {mode!r}"
        )
    if mode == "full":
        if metrics.get("close_reason") != "finished":
            raise ValidationError(
                f"Spider closed with {metrics.get('close_reason')!r}"
            )
    else:
        stop_reason = metrics.get("stop_reason")
        close_reason = metrics.get("close_reason")
        matches = FALLBACK_STOP_REASONS.get(stop_reason) if isinstance(
            stop_reason, str
        ) else None
        if (
            matches is None
            or not isinstance(close_reason, str)
            or not matches(close_reason)
        ):
            raise ValidationError(
                f"Fallback crawl stopped with {stop_reason!r} "
                f"(close reason {close_reason!r})"
            )
    if metrics.get("rank_pages_completed") != list(REQUIRED_PAGES):
        raise ValidationError(
            "Not all ranking pages completed in order: "
            f"{metrics.get('rank_pages_completed')!r}"
        )

    expected_pages = metrics.get("discovered_by_page")
    if not isinstance(expected_pages, dict) or set(expected_pages) != set(
        REQUIRED_PAGES
    ):
        raise ValidationError(
            f"Invalid per-page discovery metrics: {expected_pages!r}"
        )
    invalid_pages = {
        page: {
            "actual": expected_pages[page],
            "minimum": MINIMUM_FIRST_PAGE if page.endswith("-1") else 1,
            "maximum": MAXIMUM_PER_PAGE,
        }
        for page in REQUIRED_PAGES
        if not _is_non_negative_int(expected_pages[page])
        or expected_pages[page]
        < (MINIMUM_FIRST_PAGE if page.endswith("-1") else 1)
        or expected_pages[page] > MAXIMUM_PER_PAGE
    }
    if invalid_pages:
        raise ValidationError(
            f"Invalid per-page discovery counts: {invalid_pages}"
        )

    expected_sections = metrics.get("discovered_by_section")
    if not isinstance(expected_sections, dict) or set(expected_sections) != set(
        REQUIRED_SECTIONS
    ):
        raise ValidationError(
            f"Invalid per-section discovery metrics: {expected_sections!r}"
        )
    invalid_expected_sections = {
        section: expected_sections[section]
        for section in REQUIRED_SECTIONS
        if not _is_non_negative_int(expected_sections[section])
        or expected_sections[section] < MINIMUM_PER_SECTION
    }
    if invalid_expected_sections:
        raise ValidationError(
            f"Source exposed too few articles: {invalid_expected_sections}"
        )

    expected_from_pages = {
        "sisa": expected_pages["sisa-1"] + expected_pages["sisa-2"],
        "spo": expected_pages["spo-1"] + expected_pages["spo-2"],
        "ent": expected_pages["ent-1"] + expected_pages["ent-2"],
    }
    if expected_sections != expected_from_pages:
        raise ValidationError(
            "Per-page and per-section discovery metrics disagree: "
            f"pages={expected_from_pages}, sections={expected_sections}"
        )

    expected_total = sum(expected_sections.values())
    counters = (_full_counters if mode == "full" else _fallback_counters)(
        metrics, expected_total, len(items)
    )

    articles_discovered = metrics.get("articles_discovered")
    discovered_urls = metrics.get("discovered_urls")
    if (
        not isinstance(discovered_urls, list)
        or len(discovered_urls) != articles_discovered
        or any(not isinstance(url, str) or not url for url in discovered_urls)
    ):
        raise ValidationError(
            "Invalid discovered URL sequence: "
            f"expected={articles_discovered}, value={discovered_urls!r}"
        )

    discovered_url_sections = Counter()
    for url in discovered_urls:
        parsed = urlparse(url)
        if parsed.hostname != "m.news.nate.com" or not parsed.path.startswith(
            "/view/"
        ):
            raise ValidationError(f"Invalid discovered article URL: {url!r}")
        query = parse_qs(parsed.query)
        discovered_url_sections[query.get("sect", [""])[0]] += 1
    if dict(discovered_url_sections) != expected_sections:
        raise ValidationError(
            "Discovered URL sections disagree with metrics: "
            f"urls={dict(discovered_url_sections)}, metrics={expected_sections}"
        )

    sections = Counter()
    exported_urls = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValidationError(f"Item {index} is not a JSON object")
        news_url = item.get("newsURL", "")
        if not isinstance(news_url, str):
            raise ValidationError(f"Item {index} has a non-string newsURL")
        if not news_url:
            raise ValidationError(f"Item {index} has an empty newsURL")
        for field in ("title", "company", "image"):
            if not isinstance(item.get(field), str):
                raise ValidationError(
                    f"Item {index} has a non-string {field} field"
                )
        if not item["title"] or not item["company"]:
            raise ValidationError(f"Item {index} has an empty title/company")
        query = parse_qs(urlparse(news_url).query)
        sections[query.get("sect", [""])[0]] += 1
        exported_urls.append(news_url)

    discovered_iterator = iter(discovered_urls)
    for index, exported_url in enumerate(exported_urls):
        if not any(
            candidate == exported_url for candidate in discovered_iterator
        ):
            raise ValidationError(
                "Exported URL order does not follow discovery order at "
                f"item {index}: {exported_url!r}"
            )

    unexpected_sections = {
        section: count
        for section, count in sections.items()
        if section not in REQUIRED_SECTIONS
    }
    if unexpected_sections:
        raise ValidationError(
            f"Unexpected or missing section values: {unexpected_sections}"
        )
    if len(items) < MINIMUM_TOTAL:
        raise ValidationError(
            f"Expected at least {MINIMUM_TOTAL} items, got {len(items)}"
        )

    incomplete = {}
    for section in REQUIRED_SECTIONS:
        expected = expected_sections[section]
        required = max(
            MINIMUM_PER_SECTION,
            math.ceil(expected * MINIMUM_COVERAGE),
        )
        actual = sections[section]
        if actual < required or actual > expected:
            incomplete[section] = {
                "expected": expected,
                "required": required,
                "actual": actual,
            }
    if incomplete:
        raise ValidationError(f"Section coverage check failed: {incomplete}")

    return {
        "items": len(items),
        "sections": dict(sections),
        "discovered": expected_sections,
        **counters,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("items", type=Path)
    parser.add_argument("metrics", type=Path)
    parser.add_argument("--mode", choices=MODES, default="full")
    args = parser.parse_args(argv)

    try:
        items = json.loads(args.items.read_text(encoding="utf-8"))
        metrics = json.loads(args.metrics.read_text(encoding="utf-8"))
        summary = validate_result(items, metrics, args.mode)
    except (OSError, json.JSONDecodeError, ValidationError) as error:
        print(f"Validation failed: {error}")
        return 1

    print(f"Validation passed: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
