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


class ValidationError(ValueError):
    pass


def _is_non_negative_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def validate_result(items, metrics):
    if not isinstance(items, list):
        raise ValidationError("Spider result is not a JSON list")
    if not isinstance(metrics, dict):
        raise ValidationError("Crawl metrics are not a JSON object")
    if metrics.get("close_reason") != "finished":
        raise ValidationError(
            f"Spider closed with {metrics.get('close_reason')!r}"
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

    expected_total = sum(expected_sections.values())
    processed_total = article_items_yielded + article_failures
    if (
        articles_discovered != expected_total
        or processed_total != expected_total
        or item_error_count != 0
        or article_items_yielded
        != item_scraped_count + item_dropped_count + item_error_count
        or len(items) != item_scraped_count
    ):
        raise ValidationError(
            "Inconsistent crawl counters: "
            f"expected={expected_total}, discovered={articles_discovered}, "
            f"processed={processed_total}, yielded={article_items_yielded}, "
            f"scraped={item_scraped_count}, dropped={item_dropped_count}, "
            f"item_errors={item_error_count}, exported={len(items)}"
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
        "article_failures": article_failures,
        "dropped": item_dropped_count,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("items", type=Path)
    parser.add_argument("metrics", type=Path)
    args = parser.parse_args(argv)

    try:
        items = json.loads(args.items.read_text(encoding="utf-8"))
        metrics = json.loads(args.metrics.read_text(encoding="utf-8"))
        summary = validate_result(items, metrics)
    except (OSError, json.JSONDecodeError, ValidationError) as error:
        print(f"Validation failed: {error}")
        return 1

    print(f"Validation passed: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
