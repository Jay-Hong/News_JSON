"""Pure helpers for the degraded fallback crawl. No Scrapy imports.

The fallback path exists for machines that lose access to Nate shortly after a
crawl starts (GitHub-hosted runners since 2026-10-23). It keeps the current
mobile ranking as the source of order and URL, and fills title, company and
image from, in this order: an article page fetched in this run, the PC ranking
list, and the previously published file. Items whose company is still unknown
are left out; an unknown image is published as an empty string.

Both the fallback spider (to decide which article pages are worth requesting)
and the assembler (to build the published file) use resolve_fields(), so the
plan and the result cannot disagree about what is already known.
"""

import argparse
import json
import os
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse


KST = timezone(timedelta(hours=9))
SECTIONS = ("sisa", "spo", "ent")
RANK_PAGES = ("sisa-1", "sisa-2", "spo-1", "spo-2", "ent-1", "ent-2")
SNAPSHOT_SCHEMA = 2
MODE = "fallback"

ARTICLE_PATH = re.compile(r"^/view/((\d{8})n\d+)$")
LEADING_BRACKETS = re.compile(r"^(?:\[[^\]]*\]\s*)+")
# Every image the full crawl has published lives on news.nateimg.co.kr.
PC_THUMBNAIL = re.compile(
    r"^(?:https?:)?//thumbnews\.nateimg\.co\.kr/[^/]+///"
    r"(news\.nateimg\.co\.kr/.+)$"
)

# Why a fallback crawl may stop early and still be published. Anything else
# (a ranking page that failed, a shutdown, an unexpected exception, a request
# chain that ended before its plan) is not a usable result.
STOP_COMPLETE = "complete"
STOP_TRANSPORT = "transport_breaker"
STOP_REFUSED = "access_refused"
STOP_REQUEST_CAP = "request_cap"
STOP_TIME_BUDGET = "time_budget"
PUBLISHABLE_STOP_REASONS = (
    STOP_COMPLETE,
    STOP_TRANSPORT,
    STOP_REFUSED,
    STOP_REQUEST_CAP,
    STOP_TIME_BUDGET,
)
CLOSE_TRANSPORT_PREFIX = "fallback_transport_failures:"
CLOSE_REFUSED_PREFIX = "fallback_access_refused:"
CLOSE_REQUEST_CAP = "fallback_request_cap"
# Scrapy reports "finished" whenever nothing is left to do, including when a
# callback raised and the request chain simply stopped. The spider replaces
# that reason with this one unless the chain reached its planned end.
CLOSE_CHAIN_BROKEN = "fallback_chain_broken"

# Six mobile ranking pages, six PC ranking pages and at most one request per
# ranked article (50 rows per page, two pages, three sections).
MAX_RANKED_ARTICLES = 300
MAX_TOTAL_REQUESTS = 12 + MAX_RANKED_ARTICLES

IMAGE_SOURCES = ("article", "pc", "cache", "empty_confirmed", "empty_unobserved")
COMPANY_SOURCES = ("article", "pc", "cache")
TITLE_SOURCES = ("article", "cache", "ranking")


class FallbackError(ValueError):
    pass


def article_id(url):
    """Return the Nate article id (for example 20261113n08919) or None."""
    if not isinstance(url, str):
        return None
    match = ARTICLE_PATH.match(urlparse(url).path)
    return match.group(1) if match else None


def article_day(identifier):
    """Return the date encoded in an article id, or None if it is not a date."""
    try:
        return datetime.strptime(identifier[:8], "%Y%m%d").date()
    except (TypeError, ValueError):
        return None


def comparison_title(title):
    """Title without leading [tag] groups, for "is this still the same article".

    The mobile ranking list omits some leading tags that the article page
    keeps (for example "[IN REVIEW] "), so the two are compared without them.
    """
    return LEADING_BRACKETS.sub("", " ".join(title.split())).strip()


def pc_thumbnail_to_image(source):
    """Convert a PC ranking thumbnail src to the value the full crawl stores.

    Returns "" when the src does not have the expected thumbnail form, so an
    unfamiliar format is treated as "image not observed" instead of published.
    """
    if not isinstance(source, str):
        return ""
    match = PC_THUMBNAIL.match(source.strip())
    return "https://" + match.group(1) if match else ""


def stop_reason_from_close_reason(close_reason):
    """Map a Scrapy close reason to a stop reason, or None if not publishable."""
    if close_reason == "finished":
        return STOP_COMPLETE
    if close_reason == "closespider_timeout":
        return STOP_TIME_BUDGET
    if close_reason == CLOSE_REQUEST_CAP:
        return STOP_REQUEST_CAP
    if isinstance(close_reason, str):
        if close_reason.startswith(CLOSE_TRANSPORT_PREFIX):
            return STOP_TRANSPORT
        if close_reason.startswith(CLOSE_REFUSED_PREFIX):
            return STOP_REFUSED
    return None


def build_cache(previous_items, kst_day):
    """Index the previously published items that may still be reused.

    Only articles dated today or yesterday (KST, from the article id) are
    kept. This is a date limit of at most about 48 hours, not a freshness
    guarantee: a reused field is as old as the crawl that first saw it. An
    entry with an empty image still supplies its company, because a previous
    fallback publish stores an image it did not observe as "".
    """
    if not isinstance(previous_items, list):
        raise FallbackError("Previously published data is not a JSON list")
    if not isinstance(kst_day, date):
        raise FallbackError(f"Invalid KST day: {kst_day!r}")
    allowed_days = {kst_day, kst_day - timedelta(days=1)}
    cache = {}
    for item in previous_items:
        if not isinstance(item, dict):
            continue
        identifier = article_id(item.get("newsURL"))
        title = item.get("title")
        company = item.get("company")
        image = item.get("image")
        if (
            identifier is None
            or article_day(identifier) not in allowed_days
            or not isinstance(title, str)
            or not isinstance(company, str)
            or not isinstance(image, str)
            or not company
        ):
            continue
        # The first occurrence wins, like the title dedup in the full crawl.
        cache.setdefault(
            identifier, {"title": title, "company": company, "image": image}
        )
    return cache


def merge_pc_rows(pc_rows):
    """Combine the PC rows of each article id, field by field.

    The same article can appear on several PC pages, with a thumbnail only in
    the first page's top block. The company is the first one seen and the
    image is the first non-empty one. If two rows disagree on the company,
    the id was matched to different articles or the page is inconsistent, so
    the PC ranking says nothing about that article.

    Returns (rows_by_id, conflicting_ids).
    """
    merged = {}
    conflicts = set()
    for row in pc_rows:
        current = merged.setdefault(row["id"], {"company": row["company"], "image": ""})
        if row["company"] != current["company"]:
            conflicts.add(row["id"])
        if not current["image"]:
            current["image"] = row["image"]
    for identifier in conflicts:
        del merged[identifier]
    return merged, conflicts


def resolve_fields(ranking_title, pc_row, cached, article):
    """Decide title, company and image for one ranked article.

    Each source is "not observed", "has a value" or, for an article page
    fetched in this run, "confirmed empty". A source that did not observe a
    field never overrides one that did: a PC row without a thumbnail says
    nothing about the image, while a fetched article page without an image
    clears it.

    The title is the article page's (normalized like the full crawl) when it
    was fetched, else the previously published title when it is the same
    title up to leading [tags], else the ranking list's. The ranking list
    drops some leading tags the article page keeps, so this keeps the title
    stable across full and fallback publishes where it can.

    Returns a dict with title, title_source, company, company_source, image
    and image_source. company is "" when no source knows it.
    """
    article_ok = isinstance(article, dict) and article.get("status") == "ok"
    same_as_cached = bool(cached) and comparison_title(
        cached["title"]
    ) == comparison_title(ranking_title)

    if article_ok and article["title"]:
        title, title_source = article["title"], "article"
    elif same_as_cached:
        title, title_source = cached["title"], "cache"
    else:
        title, title_source = ranking_title, "ranking"

    if article_ok:
        company, company_source = article["company"], "article"
    elif pc_row and pc_row.get("company"):
        company, company_source = pc_row["company"], "pc"
    elif cached:
        company, company_source = cached["company"], "cache"
    else:
        company, company_source = "", "none"

    if article_ok:
        image = article["image"]
        image_source = "article" if image else "empty_confirmed"
    elif pc_row and pc_row.get("image"):
        image, image_source = pc_row["image"], "pc"
    elif (
        cached
        and cached["image"]
        # A changed title or company means the article was edited or the id
        # was matched wrongly; do not attach the old picture to it.
        and cached["company"] == company
        and same_as_cached
    ):
        image, image_source = cached["image"], "cache"
    else:
        image, image_source = "", "empty_unobserved"

    return {
        "title": title,
        "title_source": title_source,
        "company": company,
        "company_source": company_source,
        "image": image,
        "image_source": image_source,
    }


def plan_enrichment(ranking, pc_rows, cache):
    """Order the article pages worth requesting, most valuable first.

    Articles with no known company come first, because they cannot be
    published at all without it; articles that only lack an image follow.
    Inside each group the sections are interleaved by in-section rank, so a
    run that is cut off early has enriched the top of every section instead of
    only politics.
    """
    pc_by_id, _ = merge_pc_rows(pc_rows)
    position_in_section = Counter()
    missing_company = []
    missing_image = []
    planned = set()
    for row in ranking:
        position_in_section[row["section"]] += 1
        key = (
            position_in_section[row["section"]],
            SECTIONS.index(row["section"]),
        )
        # One request per article, even if it is ranked twice.
        if row["id"] in planned:
            continue
        planned.add(row["id"])
        fields = resolve_fields(
            row["title"], pc_by_id.get(row["id"]), cache.get(row["id"]), None
        )
        if not fields["company"]:
            missing_company.append((key, row["id"]))
        elif not fields["image"]:
            missing_image.append((key, row["id"]))
    return [
        identifier
        for group in (missing_company, missing_image)
        for _, identifier in sorted(group)
    ]


def _require(condition, message):
    if not condition:
        raise FallbackError(message)


def _is_count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _check_snapshot(snapshot, run_id):
    _require(isinstance(snapshot, dict), "Snapshot is not a JSON object")
    _require(
        snapshot.get("schema") == SNAPSHOT_SCHEMA,
        f"Unsupported snapshot schema: {snapshot.get('schema')!r}",
    )
    # A snapshot left behind by an earlier, interrupted run must never be
    # assembled as if it belonged to this one.
    _require(
        isinstance(run_id, str) and run_id and snapshot.get("run_id") == run_id,
        f"Snapshot run id {snapshot.get('run_id')!r} does not match {run_id!r}",
    )
    raw_day = snapshot.get("kst_day")
    kst_day = (
        article_day(raw_day + "n")
        if isinstance(raw_day, str) and re.fullmatch(r"[0-9]{8}", raw_day)
        else None
    )
    _require(kst_day is not None, f"Invalid kst_day: {raw_day!r}")
    for key in (
        "ranking",
        "pc_rows",
        "rank_pages_completed",
        "pc_pages_completed",
        "article_plan",
    ):
        _require(isinstance(snapshot.get(key), list), f"Snapshot {key} is not a list")
    for key in ("articles", "requests"):
        _require(isinstance(snapshot.get(key), dict), f"Snapshot {key} is not an object")
    _require(
        isinstance(snapshot.get("close_reason"), str),
        "Snapshot has no close reason",
    )
    _require(
        isinstance(snapshot.get("plan_built"), bool),
        "Snapshot does not say whether the article plan was built",
    )
    for key in ("planned_before_cap", "spider_errors"):
        _require(_is_count(snapshot.get(key)), f"Snapshot {key} is not a count")
    _require(
        snapshot["planned_before_cap"] >= len(snapshot["article_plan"]),
        "Snapshot plan is longer than the plan before the cap",
    )
    return kst_day


def _check_ranking(ranking, rank_pages_completed):
    _require(
        rank_pages_completed == list(RANK_PAGES[: len(rank_pages_completed)]),
        f"Ranking pages completed out of order: {rank_pages_completed!r}",
    )
    last_page_index = -1
    last_position = 0
    pages_with_rows = set()
    for index, row in enumerate(ranking):
        _require(isinstance(row, dict), f"Ranking row {index} is not an object")
        for field in ("id", "url", "section", "page", "title"):
            _require(
                isinstance(row.get(field), str) and row[field],
                f"Ranking row {index} has no {field}",
            )
        parsed = urlparse(row["url"])
        _require(
            parsed.scheme in ("http", "https")
            and parsed.hostname == "m.news.nate.com"
            and article_id(row["url"]) == row["id"]
            and parse_qs(parsed.query).get("sect", [""])[0] == row["section"]
            and row["section"] in SECTIONS
            and row["page"] in rank_pages_completed
            and row["page"].split("-")[0] == row["section"],
            f"Ranking row {index} is inconsistent: {row!r}",
        )
        # Rows must come page by page in ranking order, positions rising
        # within each page, exactly as the spider read them.
        page_index = RANK_PAGES.index(row["page"])
        position = row.get("page_position")
        _require(
            _is_count(position) and position >= 1,
            f"Ranking row {index} has no page position: {row!r}",
        )
        if page_index != last_page_index:
            _require(
                page_index > last_page_index,
                f"Ranking row {index} goes back to an earlier page",
            )
            last_page_index, last_position = page_index, 0
        _require(
            position > last_position,
            f"Ranking row {index} is out of order within {row['page']}",
        )
        last_position = position
        pages_with_rows.add(row["page"])
    _require(
        pages_with_rows == set(rank_pages_completed),
        "A completed ranking page has no rows",
    )


def _check_pc_rows(pc_rows):
    for index, row in enumerate(pc_rows):
        _require(
            isinstance(row, dict)
            and isinstance(row.get("id"), str)
            and article_day(row["id"]) is not None
            and isinstance(row.get("company"), str)
            and row["company"]
            and isinstance(row.get("image"), str),
            f"PC row {index} is invalid: {row!r}",
        )


def _check_articles(articles, article_plan, ranking_ids):
    _require(
        all(isinstance(identifier, str) for identifier in article_plan)
        and len(set(article_plan)) == len(article_plan)
        and set(article_plan) <= ranking_ids,
        "Article plan repeats an id or names an unranked article",
    )
    _require(
        set(articles) <= set(article_plan),
        "An article outside the plan was requested",
    )
    for identifier, result in articles.items():
        _require(
            isinstance(result, dict) and result.get("status") in ("ok", "failed"),
            f"Article result for {identifier} is invalid: {result!r}",
        )
        if result["status"] == "ok":
            _require(
                isinstance(result.get("title"), str)
                and isinstance(result.get("company"), str)
                and result["company"]
                and isinstance(result.get("image"), str),
                f"Article result for {identifier} is incomplete: {result!r}",
            )


def _stop_reason(snapshot):
    """Stop reason for the metrics, or a label that validation rejects."""
    close_reason = snapshot["close_reason"]
    stop_reason = stop_reason_from_close_reason(close_reason)
    if snapshot["spider_errors"]:
        return f"unexpected:spider_errors:{snapshot['spider_errors']}"
    if stop_reason is None:
        return f"unexpected:{close_reason}"
    plan_done = snapshot["plan_built"] and len(snapshot["articles"]) == len(
        snapshot["article_plan"]
    )
    capped = snapshot["planned_before_cap"] > len(snapshot["article_plan"])
    # "complete" and "request_cap" both claim that every planned request was
    # made. Check it, so that a run that merely went idle cannot pass as one.
    if stop_reason == STOP_COMPLETE and not (plan_done and not capped):
        return f"unexpected:{close_reason}:plan_not_done"
    if stop_reason == STOP_REQUEST_CAP and not (plan_done and capped):
        return f"unexpected:{close_reason}:plan_not_done"
    return stop_reason


def assemble(snapshot, previous_items, run_id):
    """Build the published item list and its metrics from a crawl snapshot.

    Items, discovery counts and article-request outcomes are derived from the
    raw rows. requests.* are the spider's own counts of requests that got an
    outcome; they are copied, and the validator cross-checks them against the
    outcomes recorded here.
    """
    kst_day = _check_snapshot(snapshot, run_id)
    ranking = snapshot["ranking"]
    pc_rows = snapshot["pc_rows"]
    articles = snapshot["articles"]
    article_plan = snapshot["article_plan"]
    _check_ranking(ranking, snapshot["rank_pages_completed"])
    _check_pc_rows(pc_rows)
    ranking_ids = {row["id"] for row in ranking}
    _check_articles(articles, article_plan, ranking_ids)

    cache = build_cache(previous_items, kst_day)
    pc_by_id, pc_conflicts = merge_pc_rows(pc_rows)

    items = []
    titles_seen = set()
    urls_seen = set()
    excluded_no_company = 0
    dropped_duplicate_title = 0
    dropped_duplicate_url = 0
    title_sources = Counter()
    company_sources = Counter()
    image_sources = Counter()
    discovered_by_page = Counter()
    discovered_by_section = Counter()
    for row in ranking:
        discovered_by_page[row["page"]] += 1
        discovered_by_section[row["section"]] += 1
        fields = resolve_fields(
            row["title"],
            pc_by_id.get(row["id"]),
            cache.get(row["id"]),
            articles.get(row["id"]),
        )
        if not fields["company"]:
            excluded_no_company += 1
            continue
        # The ranking can shift between two page requests, so the same
        # article may be listed twice; the first listing wins.
        if row["url"] in urls_seen:
            dropped_duplicate_url += 1
            continue
        # Same rule as DuplicatesPipeline: the first ranked title wins.
        if fields["title"] in titles_seen:
            dropped_duplicate_title += 1
            continue
        urls_seen.add(row["url"])
        titles_seen.add(fields["title"])
        title_sources[fields["title_source"]] += 1
        company_sources[fields["company_source"]] += 1
        image_sources[fields["image_source"]] += 1
        items.append(
            {
                "title": fields["title"],
                "company": fields["company"],
                "image": fields["image"],
                "newsURL": row["url"],
            }
        )

    attempted = len(articles)
    succeeded = sum(1 for result in articles.values() if result["status"] == "ok")
    requests = snapshot["requests"]
    metrics = {
        "mode": MODE,
        "run_id": snapshot["run_id"],
        "kst_day": snapshot["kst_day"],
        "close_reason": snapshot["close_reason"],
        "stop_reason": _stop_reason(snapshot),
        "spider_errors": snapshot["spider_errors"],
        "rank_pages_completed": snapshot["rank_pages_completed"],
        "discovered_by_page": {
            page: discovered_by_page[page]
            for page in RANK_PAGES
            if page in snapshot["rank_pages_completed"]
        },
        "discovered_by_section": {
            section: discovered_by_section[section] for section in SECTIONS
        },
        "articles_discovered": len(ranking),
        "discovered_urls": [row["url"] for row in ranking],
        "items_exported": len(items),
        "excluded_no_company": excluded_no_company,
        "dropped_duplicate_url": dropped_duplicate_url,
        "dropped_duplicate_title": dropped_duplicate_title,
        "title_sources": {source: title_sources[source] for source in TITLE_SOURCES},
        "company_sources": {
            source: company_sources[source] for source in COMPANY_SOURCES
        },
        "image_sources": {source: image_sources[source] for source in IMAGE_SOURCES},
        "article_requests": {
            "planned_before_cap": snapshot["planned_before_cap"],
            "skipped_by_cap": snapshot["planned_before_cap"] - len(article_plan),
            "planned": len(article_plan),
            "attempted": attempted,
            "ok": succeeded,
            "failed": attempted - succeeded,
            "not_requested": len(article_plan) - attempted,
        },
        "requests": {
            "mobile_rank": requests.get("mobile_rank"),
            "pc_rank": requests.get("pc_rank"),
            "article": requests.get("article"),
        },
        "pc": {
            "pages_completed": snapshot["pc_pages_completed"],
            "stop_reason": snapshot.get("pc_stop_reason"),
            "rows": len(pc_rows),
            "distinct_articles": len(pc_by_id) + len(pc_conflicts),
            "company_conflicts": len(pc_conflicts),
            "cache_entries": len(cache),
        },
    }
    return items, metrics


def dump_items(items):
    """Serialize like Scrapy's JSON feed exporter with indent 4."""
    body = ",\n".join(
        json.dumps(item, ensure_ascii=False, indent=4) for item in items
    )
    return "[\n" + body + "\n]" if items else "[\n\n]"


def write_text_atomically(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def summary_lines(metrics):
    """Human-readable quality summary for the workflow log."""
    image = metrics["image_sources"]
    company = metrics["company_sources"]
    title = metrics["title_sources"]
    exported = metrics["items_exported"]
    with_image = image["article"] + image["pc"] + image["cache"]
    requested = metrics["article_requests"]
    pc = metrics["pc"]
    return [
        f"fallback result: {exported} of {metrics['articles_discovered']} ranked"
        f" articles (no company: {metrics['excluded_no_company']},"
        f" duplicate URL: {metrics['dropped_duplicate_url']},"
        f" duplicate title: {metrics['dropped_duplicate_title']})",
        f"stop reason: {metrics['stop_reason']} ({metrics['close_reason']})",
        f"company from: article page {company['article']}, PC ranking"
        f" {company['pc']}, previous publish {company['cache']}",
        f"image: {with_image} of {exported} (article page {image['article']},"
        f" PC ranking {image['pc']}, previous publish {image['cache']},"
        f" none on the page {image['empty_confirmed']},"
        f" not observed {image['empty_unobserved']})",
        f"title from: article page {title['article']}, previous publish"
        f" {title['cache']}, ranking list {title['ranking']}",
        f"article pages: needed {requested['planned_before_cap']}, skipped by"
        f" the request cap {requested['skipped_by_cap']}, requested"
        f" {requested['attempted']}, ok {requested['ok']}, failed"
        f" {requested['failed']}, not reached {requested['not_requested']}",
        f"PC ranking: pages {len(pc['pages_completed'])}, rows {pc['rows']},"
        f" distinct articles {pc['distinct_articles']}, company conflicts"
        f" {pc['company_conflicts']}, stage end {pc['stop_reason'] or 'complete'}",
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Assemble a fallback crawl snapshot into the published format."
    )
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
        previous = json.loads(args.previous.read_text(encoding="utf-8"))
        items, metrics = assemble(snapshot, previous, args.run_id)
    except (OSError, json.JSONDecodeError, FallbackError) as error:
        print(f"Fallback assembly failed: {error}")
        return 1

    write_text_atomically(args.out, dump_items(items))
    write_text_atomically(
        args.metrics, json.dumps(metrics, ensure_ascii=False, indent=2) + "\n"
    )
    for line in summary_lines(metrics):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
