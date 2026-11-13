"""Offline tests for the fallback path: gate, merge rules, validator mode, spider.

Nothing here contacts Nate or GitHub. The spider tests serve made-up pages
from a local HTTP proxy and are skipped unless the Scrapy version pinned in
nate_news/requirements.txt is installed.
"""

import importlib
import json
import os
import plistlib
import re
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parent.parent
PROJECT = REPO / "nate_news"
WORKFLOW = REPO / ".github" / "workflows" / "main.yml"
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(PROJECT))

import freshness_gate  # noqa: E402  (needs sys.path above)
import validate_result  # noqa: E402
from nate_news import fallback  # noqa: E402

KST = timezone(timedelta(hours=9))
# A day that can never be the real date, so a spider that ignores the
# configured day and uses the clock fails these tests.
DAY = "20200102"
SECTIONS = ("sisa", "spo", "ent")

# The spider tests need the Scrapy version pinned in nate_news/requirements.txt;
# an older Scrapy cannot even import the project's spiders.
try:
    fallback_spider = importlib.import_module("nate_news.spiders.nate_news_fallback")
    SPIDER_IMPORTABLE, SPIDER_SKIP_REASON = True, ""
except ImportError as error:
    fallback_spider = None
    SPIDER_IMPORTABLE = False
    SPIDER_SKIP_REASON = f"project spiders cannot be imported here: {error}"


def kst(text):
    return datetime.fromisoformat(text).replace(tzinfo=KST)


class FreshnessGateTest(unittest.TestCase):
    def test_slot_becomes_due_only_after_the_grace_period(self):
        self.assertEqual(
            freshness_gate.due_slot(kst("2026-11-14T11:28:59")),
            kst("2026-11-14T05:44:00"),
        )
        self.assertEqual(
            freshness_gate.due_slot(kst("2026-11-14T11:29:00")),
            kst("2026-11-14T10:44:00"),
        )

    def test_before_the_first_slot_the_previous_evening_is_due(self):
        self.assertEqual(
            freshness_gate.due_slot(kst("2026-11-14T03:00:00")),
            kst("2026-11-13T20:44:00"),
        )
        # 00:10 minus the grace period is still the previous day.
        self.assertEqual(
            freshness_gate.due_slot(kst("2026-11-14T00:10:00")),
            kst("2026-11-13T20:44:00"),
        )
        self.assertEqual(
            freshness_gate.due_slot(kst("2026-11-14T06:28:59")),
            kst("2026-11-13T20:44:00"),
        )

    def test_commit_at_or_after_the_due_slot_is_fresh(self):
        now = kst("2026-11-14T11:44:00")
        self.assertEqual(
            freshness_gate.decide(now, kst("2026-11-14T10:56:00")),
            (False, kst("2026-11-14T10:44:00"), "fresh"),
        )
        self.assertEqual(freshness_gate.decide(now, kst("2026-11-14T10:44:00"))[2], "fresh")
        self.assertEqual(freshness_gate.decide(now, kst("2026-11-14T10:43:59"))[2], "stale")

    def test_a_delayed_run_does_not_redo_a_published_slot(self):
        # The 11:44 run starts at 15:10: the 10:44 slot is still the due one.
        self.assertEqual(
            freshness_gate.decide(kst("2026-11-14T15:10:00"), kst("2026-11-14T10:56:00")),
            (False, kst("2026-11-14T10:44:00"), "fresh"),
        )

    def test_night_gap_after_a_missed_morning_slot(self):
        self.assertEqual(
            freshness_gate.decide(kst("2026-11-14T06:44:00"), kst("2026-11-13T20:57:00")),
            (True, kst("2026-11-14T05:44:00"), "stale"),
        )

    def test_defers_while_the_local_publisher_may_be_crawling(self):
        last = kst("2026-11-13T20:57:00")  # the 05:44 slot was missed
        cases = (
            ("2026-11-14T10:23:59", True, "2026-11-14T05:44:00", "stale"),
            ("2026-11-14T10:24:00", False, "2026-11-14T05:44:00", "deferred"),
            ("2026-11-14T10:50:00", False, "2026-11-14T05:44:00", "deferred"),
            ("2026-11-14T11:28:59", False, "2026-11-14T05:44:00", "deferred"),
            # From here the 10:44 slot is due, and it was missed too.
            ("2026-11-14T11:29:00", True, "2026-11-14T10:44:00", "stale"),
        )
        for now, stale, slot, verdict in cases:
            with self.subTest(now=now):
                self.assertEqual(
                    freshness_gate.decide(kst(now), last), (stale, kst(slot), verdict)
                )
        # Fresh data is reported as fresh inside the window as well.
        self.assertEqual(
            freshness_gate.decide(kst("2026-11-14T10:50:00"), kst("2026-11-14T05:56:00"))[2],
            "fresh",
        )

    def test_utc_now_on_the_other_side_of_the_kst_date_line(self):
        # The 21:44 UTC cron run is 06:44 KST on the next day.
        now = datetime(2026, 11, 13, 21, 44, tzinfo=timezone.utc)
        self.assertEqual(
            freshness_gate.decide(now, kst("2026-11-13T20:57:00")),
            (True, kst("2026-11-14T05:44:00"), "stale"),
        )
        self.assertEqual(
            freshness_gate.decide(now, freshness_gate.parse_timestamp("2026-11-13T20:56:00Z"))[2],
            "fresh",
        )

    def test_future_commit_is_unknown_not_fresh(self):
        now = kst("2026-11-14T11:44:00")
        with self.assertRaises(freshness_gate.GateError):
            freshness_gate.decide(now, kst("2026-11-14T11:50:00"))
        # Up to five minutes of clock difference is tolerated.
        self.assertEqual(freshness_gate.decide(now, kst("2026-11-14T11:49:00"))[2], "fresh")

    def test_unusable_timestamps_are_rejected(self):
        for text in ("", "null", "2026-11-14", "2026-11-14T10:00:00", None):
            with self.subTest(text=text):
                with self.assertRaises(freshness_gate.GateError):
                    freshness_gate.parse_timestamp(text)

    def test_slots_match_the_launch_agent_template(self):
        template = (REPO / "tools" / "local.newsjson.publish.plist.template").read_bytes()
        intervals = plistlib.loads(template)["StartCalendarInterval"]
        self.assertEqual(
            sorted((entry["Hour"], entry["Minute"]) for entry in intervals),
            sorted((slot.hour, slot.minute) for slot in freshness_gate.SLOTS_KST),
        )

    def test_workflow_runs_one_hour_after_each_slot_and_judges_it(self):
        match = re.search(r"- cron: '(\d+) ([\d,]+) \* \* \*'", WORKFLOW.read_text())
        self.assertIsNotNone(match)
        minute, hours = int(match.group(1)), {int(hour) for hour in match.group(2).split(",")}
        expected = set()
        for slot in freshness_gate.SLOTS_KST:
            self.assertEqual(minute, slot.minute)
            expected.add((slot.hour + 1 - 9) % 24)
        self.assertEqual(hours, expected)
        # A run on time is outside every deferral window and judges the slot
        # it follows.
        for hour in sorted(hours):
            now = datetime(2026, 11, 14, hour, minute, tzinfo=timezone.utc)
            slot = freshness_gate.due_slot(now)
            self.assertEqual(now - slot, timedelta(hours=1))
            self.assertEqual(
                freshness_gate.decide(now, slot - timedelta(seconds=1)), (True, slot, "stale")
            )

    def test_cli_exit_codes(self):
        script = str(REPO / "tools" / "freshness_gate.py")

        def run(*arguments):
            return subprocess.run(
                [sys.executable, script, *arguments],
                capture_output=True,
                text=True,
            )

        fresh = run("--now", "2026-11-14T11:44:00+09:00", "--last", "2026-11-14T01:56:00Z")
        self.assertEqual((fresh.returncode, fresh.stdout), (0, "stale=false\n"))
        stale = run("--now", "2026-11-14T11:44:00+09:00", "--last", "2026-11-13T12:00:00Z")
        self.assertEqual((stale.returncode, stale.stdout), (0, "stale=true\n"))
        deferred = run("--now", "2026-11-14T10:50:00+09:00", "--last", "2026-11-13T12:00:00Z")
        self.assertEqual((deferred.returncode, deferred.stdout), (0, "stale=false\n"))
        self.assertIn("deferred", deferred.stderr)
        dateline = run("--now", "2026-11-13T21:44:00Z", "--last", "2026-11-13T11:57:00Z")
        self.assertEqual(dateline.stdout, "stale=true\n")
        for last in ("", "null", "2026-11-15T00:00:00Z"):
            with self.subTest(last=last):
                unknown = run("--now", "2026-11-14T11:44:00+09:00", "--last", last)
                self.assertEqual((unknown.returncode, unknown.stdout), (2, ""))
                self.assertIn("gate_unknown", unknown.stderr)


def article_number(section, position):
    return SECTIONS.index(section) * 1000 + position


def article_id_for(section, position, day=DAY):
    return f"{day}n{article_number(section, position):05d}"


def article_url(identifier, section, scheme="https"):
    return (
        f"{scheme}://m.news.nate.com/view/{identifier}"
        f"?sect={section}&list=rank&cate=interest"
    )


def ranking_rows(counts, scheme="https"):
    rows = []
    for section in SECTIONS:
        for position in range(1, counts[section] + 1):
            identifier = article_id_for(section, position)
            page = f"{section}-{1 if position <= 50 else 2}"
            rows.append(
                {
                    "id": identifier,
                    "url": article_url(identifier, section, scheme),
                    "section": section,
                    "page": page,
                    "page_position": position if position <= 50 else position - 50,
                    "title": f"{section} 제목 {position}",
                }
            )
    return rows


def make_snapshot(
    ranking,
    pc_rows=(),
    articles=None,
    plan=None,
    close_reason="finished",
    planned_before_cap=None,
):
    articles = dict(articles or {})
    plan = list(plan if plan is not None else articles)
    return {
        "schema": fallback.SNAPSHOT_SCHEMA,
        "run_id": "run-1",
        "kst_day": DAY,
        "close_reason": close_reason,
        "spider_errors": 0,
        "rank_pages_completed": list(fallback.RANK_PAGES),
        "ranking": ranking,
        "pc_pages_completed": [],
        "pc_stop_reason": None,
        "pc_rows": list(pc_rows),
        "plan_built": True,
        "article_plan": plan,
        "planned_before_cap": len(plan) if planned_before_cap is None else planned_before_cap,
        "articles": articles,
        "requests": {"mobile_rank": 6, "pc_rank": 6, "article": len(articles)},
    }


def published(identifier, section, title, company="이전일보", image="https://img/old.jpg"):
    return {
        "title": title,
        "company": company,
        "image": image,
        "newsURL": article_url(identifier, section),
    }


def ok_article(company="기사신문", image="https://img/a.jpg", title="기사 제목"):
    return {"status": "ok", "title": title, "company": company, "image": image}


def sources(fields):
    return (
        fields["company"],
        fields["company_source"],
        fields["image"],
        fields["image_source"],
    )


class FallbackHelpersTest(unittest.TestCase):
    def test_article_id(self):
        self.assertEqual(
            fallback.article_id("https://m.news.nate.com/view/20261113n08919?sect=spo"),
            "20261113n08919",
        )
        self.assertEqual(
            fallback.article_id("//news.nate.com/view/20261114n00083?mid=n1006"),
            "20261114n00083",
        )
        for url in ("", None, "https://m.news.nate.com/rank/list", "https://x/view/abc"):
            self.assertIsNone(fallback.article_id(url))

    def test_comparison_title_ignores_leading_tags_only(self):
        self.assertEqual(
            fallback.comparison_title("[IN REVIEW]  [단독] 손흥민 [영상] 골"),
            "손흥민 [영상] 골",
        )

    def test_pc_thumbnail_conversion(self):
        expected = "https://news.nateimg.co.kr/orgImg/a/1.jpg"
        for prefix in ("//", "https://", "http://"):
            self.assertEqual(
                fallback.pc_thumbnail_to_image(
                    prefix + "thumbnews.nateimg.co.kr/news90///news.nateimg.co.kr/orgImg/a/1.jpg"
                ),
                expected,
            )
        # An unfamiliar format is "not observed", never published as is.
        for source in (
            "https://example.com/a.jpg",
            "//thumbnews.nateimg.co.kr/news90///example.com/a.jpg",
            "/relative/a.jpg",
            "",
            None,
        ):
            self.assertEqual(fallback.pc_thumbnail_to_image(source), "")

    def test_close_reason_mapping_agrees_with_the_validator(self):
        samples = (
            "finished",
            "closespider_timeout",
            fallback.CLOSE_REQUEST_CAP,
            fallback.CLOSE_TRANSPORT_PREFIX + "2",
            fallback.CLOSE_REFUSED_PREFIX + "403",
            fallback.CLOSE_CHAIN_BROKEN,
            "shutdown",
            "cancelled",
            "rank_page_invalid:spo-2",
            "rank_page_transport_failed:sisa-1",
            "rank_page_request_failed:sisa-1",
        )
        for close_reason in samples:
            with self.subTest(close_reason=close_reason):
                matching = [
                    stop
                    for stop, matches in validate_result.FALLBACK_STOP_REASONS.items()
                    if matches(close_reason)
                ]
                expected = fallback.stop_reason_from_close_reason(close_reason)
                self.assertEqual(matching, [expected] if expected else [])
        self.assertEqual(
            set(validate_result.FALLBACK_STOP_REASONS), set(fallback.PUBLISHABLE_STOP_REASONS)
        )

    def test_cache_keeps_only_today_and_yesterday(self):
        day = date(2026, 11, 14)
        items = [
            published("20261114n00001", "sisa", "오늘"),
            published("20261113n00002", "sisa", "어제"),
            published("20261112n00003", "sisa", "그제"),
            published("20261114n00004", "sisa", "언론사 없음", company=""),
            published("20261114n00005", "sisa", "이미지 없음", image=""),
            published("20261114n00001", "sisa", "같은 기사 두 번째", company="다른일보"),
            {"title": "no url"},
            "not an item",
        ]
        cache = fallback.build_cache(items, day)
        self.assertEqual(
            sorted(cache), ["20261113n00002", "20261114n00001", "20261114n00005"]
        )
        # The first occurrence wins; an empty image still supplies a company.
        self.assertEqual(cache["20261114n00001"]["company"], "이전일보")
        self.assertEqual(cache["20261114n00005"], {"title": "이미지 없음", "company": "이전일보", "image": ""})
        with self.assertRaises(fallback.FallbackError):
            fallback.build_cache({"not": "a list"}, day)

    def test_pc_rows_are_merged_field_by_field(self):
        rows = [
            {"id": "a", "company": "가", "image": "https://img/a.jpg"},
            {"id": "a", "company": "가", "image": ""},
            {"id": "b", "company": "나", "image": ""},
            {"id": "b", "company": "나", "image": "https://img/b.jpg"},
            {"id": "c", "company": "다", "image": "https://img/c.jpg"},
            {"id": "c", "company": "라", "image": ""},
        ]
        merged, conflicts = fallback.merge_pc_rows(rows)
        self.assertEqual(
            merged,
            {
                "a": {"company": "가", "image": "https://img/a.jpg"},
                "b": {"company": "나", "image": "https://img/b.jpg"},
            },
        )
        self.assertEqual(conflicts, {"c"})

    def test_field_sources_never_override_with_something_unobserved(self):
        cached = {"title": "[단독] 제목", "company": "가나일보", "image": "https://img/c.jpg"}
        # A PC row without a thumbnail says nothing about the image.
        self.assertEqual(
            sources(fallback.resolve_fields("제목", {"company": "가나일보", "image": ""}, cached, None)),
            ("가나일보", "pc", "https://img/c.jpg", "cache"),
        )
        self.assertEqual(
            sources(
                fallback.resolve_fields(
                    "제목", {"company": "가나일보", "image": "https://img/p.jpg"}, cached, None
                )
            ),
            ("가나일보", "pc", "https://img/p.jpg", "pc"),
        )
        # A fetched article page wins, including a confirmed missing image.
        self.assertEqual(
            sources(fallback.resolve_fields("제목", None, cached, ok_article("다라신문", ""))),
            ("다라신문", "article", "", "empty_confirmed"),
        )
        # A failed fetch observed nothing.
        self.assertEqual(
            sources(fallback.resolve_fields("제목", None, cached, {"status": "failed", "reason": "x"})),
            ("가나일보", "cache", "https://img/c.jpg", "cache"),
        )
        # An empty cached image is "not observed", not "confirmed empty".
        empty = dict(cached, image="")
        self.assertEqual(
            sources(fallback.resolve_fields("제목", None, empty, None)),
            ("가나일보", "cache", "", "empty_unobserved"),
        )
        self.assertEqual(
            sources(fallback.resolve_fields("제목", None, None, None)),
            ("", "none", "", "empty_unobserved"),
        )

    def test_old_image_is_not_reused_when_the_article_changed(self):
        cached = {"title": "원래 제목", "company": "가나일보", "image": "https://img/c.jpg"}
        self.assertEqual(
            sources(fallback.resolve_fields("바뀐 제목", None, cached, None)),
            ("가나일보", "cache", "", "empty_unobserved"),
        )
        self.assertEqual(
            sources(fallback.resolve_fields("원래 제목", {"company": "다른일보", "image": ""}, cached, None)),
            ("다른일보", "pc", "", "empty_unobserved"),
        )

    def test_title_prefers_the_article_page_then_the_same_published_title(self):
        cached = {"title": "[IN REVIEW] 손흥민 골", "company": "가", "image": ""}
        fields = fallback.resolve_fields("손흥민 골", None, cached, None)
        self.assertEqual((fields["title"], fields["title_source"]), ("[IN REVIEW] 손흥민 골", "cache"))
        fields = fallback.resolve_fields("손흥민 결승골", None, cached, None)
        self.assertEqual((fields["title"], fields["title_source"]), ("손흥민 결승골", "ranking"))
        fields = fallback.resolve_fields("손흥민 골", None, cached, ok_article(title="[SPO 이슈] 손흥민 골"))
        self.assertEqual((fields["title"], fields["title_source"]), ("[SPO 이슈] 손흥민 골", "article"))
        # An article page without a title keeps the next best title.
        fields = fallback.resolve_fields("손흥민 골", None, None, ok_article(title=""))
        self.assertEqual((fields["title"], fields["title_source"]), ("손흥민 골", "ranking"))

    def test_plan_puts_missing_company_first_and_interleaves_sections(self):
        ranking = ranking_rows({"sisa": 3, "spo": 3, "ent": 3})
        ids = {
            (section, position): article_id_for(section, position)
            for section in SECTIONS
            for position in (1, 2, 3)
        }
        pc_rows = [
            {"id": ids[("sisa", 1)], "company": "가", "image": "https://img/1.jpg"},
            {"id": ids[("sisa", 2)], "company": "가", "image": ""},
            {"id": ids[("spo", 1)], "company": "나", "image": ""},
            {"id": ids[("ent", 3)], "company": "다", "image": ""},
        ]
        cache = {
            ids[("ent", 1)]: {"title": "ent 제목 1", "company": "라", "image": "https://img/e.jpg"},
            ids[("spo", 3)]: {"title": "ent 다른 제목", "company": "마", "image": "https://img/s.jpg"},
        }
        self.assertEqual(
            fallback.plan_enrichment(ranking, pc_rows, cache),
            [
                # no company anywhere: rank 2 of spo and ent, then rank 3 of sisa
                ids[("spo", 2)],
                ids[("ent", 2)],
                ids[("sisa", 3)],
                # company known, image missing
                ids[("spo", 1)],
                ids[("sisa", 2)],
                ids[("spo", 3)],
                ids[("ent", 3)],
            ],
        )

    def test_plan_requests_an_article_ranked_twice_only_once(self):
        ranking = ranking_rows({"sisa": 1, "spo": 0, "ent": 0})
        twin = dict(ranking[0], section="spo", page="spo-1", url=article_url(ranking[0]["id"], "spo"))
        self.assertEqual(
            fallback.plan_enrichment(ranking + [twin], [], {}), [ranking[0]["id"]]
        )

    def test_dump_items_matches_the_published_file_format(self):
        text = (REPO / "newsURL.json").read_text(encoding="utf-8")
        self.assertEqual(fallback.dump_items(json.loads(text)), text)
        tricky = [{"title": "따옴표 \" 역슬래시 \\   😀", "company": "가", "image": "", "newsURL": "u"}]
        self.assertEqual(json.loads(fallback.dump_items(tricky)), tricky)
        self.assertEqual(fallback.dump_items([]), "[\n\n]")


class AssembleTest(unittest.TestCase):
    def setUp(self):
        self.counts = {"sisa": 60, "spo": 55, "ent": 100}
        self.ranking = ranking_rows(self.counts)

    def companies_for_all(self):
        return [{"id": row["id"], "company": "가나일보", "image": ""} for row in self.ranking]

    def rejected(self, snapshot, previous=()):
        items, metrics = fallback.assemble(snapshot, list(previous), "run-1")
        with self.assertRaises(validate_result.ValidationError) as caught:
            validate_result.validate_result(items, metrics, "fallback")
        return metrics, str(caught.exception)

    def test_keeps_ranking_order_and_reports_sources(self):
        first, second, third = (row["id"] for row in self.ranking[:3])
        pc_rows = self.companies_for_all()
        pc_rows[0] = {"id": first, "company": "가나일보", "image": "https://img/pc.jpg"}
        previous = [published(second, "sisa", "sisa 제목 2", company="가나일보")]
        articles = {third: ok_article(title="[기획] 기사 제목")}
        items, metrics = fallback.assemble(
            make_snapshot(self.ranking, pc_rows, articles), previous, "run-1"
        )
        self.assertEqual([item["newsURL"] for item in items], [row["url"] for row in self.ranking])
        self.assertEqual(
            [(item["title"], item["company"], item["image"]) for item in items[:4]],
            [
                ("sisa 제목 1", "가나일보", "https://img/pc.jpg"),
                ("sisa 제목 2", "가나일보", "https://img/old.jpg"),
                ("[기획] 기사 제목", "기사신문", "https://img/a.jpg"),
                ("sisa 제목 4", "가나일보", ""),
            ],
        )
        self.assertEqual(metrics["company_sources"], {"article": 1, "pc": 214, "cache": 0})
        self.assertEqual(metrics["title_sources"], {"article": 1, "cache": 1, "ranking": 213})
        self.assertEqual(
            metrics["image_sources"],
            {"article": 1, "pc": 1, "cache": 1, "empty_confirmed": 0, "empty_unobserved": 212},
        )
        self.assertEqual(metrics["discovered_by_section"], self.counts)
        self.assertEqual(metrics["stop_reason"], "complete")
        summary = validate_result.validate_result(items, metrics, "fallback")
        self.assertEqual(summary["items"], 215)

    def test_articles_without_a_company_are_left_out(self):
        pc_rows = self.companies_for_all()[5:]
        items, metrics = fallback.assemble(make_snapshot(self.ranking, pc_rows), [], "run-1")
        self.assertEqual(len(items), 210)
        self.assertEqual(metrics["excluded_no_company"], 5)
        self.assertTrue(all(item["company"] for item in items))
        validate_result.validate_result(items, metrics, "fallback")

    def test_first_ranked_title_wins_among_publishable_rows(self):
        self.ranking[7]["title"] = self.ranking[2]["title"]
        items, metrics = fallback.assemble(
            make_snapshot(self.ranking, self.companies_for_all()), [], "run-1"
        )
        self.assertEqual(metrics["dropped_duplicate_title"], 1)
        urls = [item["newsURL"] for item in items]
        self.assertNotIn(self.ranking[7]["url"], urls)
        self.assertIn(self.ranking[2]["url"], urls)
        validate_result.validate_result(items, metrics, "fallback")
        # A first row without a company does not suppress a later one.
        pc_rows = [row for row in self.companies_for_all() if row["id"] != self.ranking[2]["id"]]
        items, metrics = fallback.assemble(make_snapshot(self.ranking, pc_rows), [], "run-1")
        self.assertIn(self.ranking[7]["url"], [item["newsURL"] for item in items])
        self.assertEqual(metrics["dropped_duplicate_title"], 0)

    def test_an_article_listed_on_two_pages_is_published_once(self):
        # Rank 50 slid to rank 51 between the two page requests.
        repeated = dict(self.ranking[49], page="sisa-2", page_position=1)
        ranking = self.ranking[:50] + [repeated] + [
            dict(row, page_position=row["page_position"] + 1) for row in self.ranking[50:60]
        ] + self.ranking[60:]
        pc_rows = [{"id": row["id"], "company": "가", "image": ""} for row in ranking]
        items, metrics = fallback.assemble(make_snapshot(ranking, pc_rows), [], "run-1")
        self.assertEqual(metrics["dropped_duplicate_url"], 1)
        self.assertEqual(len(items), len({item["newsURL"] for item in items}))
        validate_result.validate_result(items, metrics, "fallback")

    def test_snapshot_from_another_run_is_refused(self):
        snapshot = make_snapshot(self.ranking, self.companies_for_all())
        for run_id in ("run-2", ""):
            with self.assertRaises(fallback.FallbackError):
                fallback.assemble(snapshot, [], run_id)
        with self.assertRaises(fallback.FallbackError):
            fallback.assemble(dict(snapshot, run_id=""), [], "")
        with self.assertRaises(fallback.FallbackError):
            fallback.assemble(dict(snapshot, schema=1), [], "run-1")

    def test_inconsistent_snapshots_are_refused(self):
        def broken(change):
            snapshot = make_snapshot(self.ranking, self.companies_for_all())
            change(snapshot)
            with self.assertRaises(fallback.FallbackError):
                fallback.assemble(snapshot, [], "run-1")

        first = self.ranking[0]["id"]
        outside = {"20200102n99999": ok_article()}
        broken(lambda s: s.update(articles=outside))
        broken(lambda s: s["ranking"][0].update(section="spo"))
        broken(lambda s: s["ranking"][0].update(url="https://example.com/view/20200102n00001?sect=sisa"))
        broken(lambda s: s["ranking"][0].update(url=s["ranking"][0]["url"].replace("https:", "javascript:")))
        broken(lambda s: s["ranking"][0].update(title=""))
        broken(lambda s: s["ranking"][0].pop("page_position"))
        broken(lambda s: s["ranking"][1].update(page_position=1))
        broken(lambda s: s["ranking"].reverse())
        broken(lambda s: s.update(ranking=s["ranking"][50:60] + s["ranking"][:50] + s["ranking"][60:]))
        broken(lambda s: s.update(rank_pages_completed=["sisa-2", "sisa-1"]))
        broken(lambda s: s.update(rank_pages_completed=list(fallback.RANK_PAGES[:2])))
        broken(lambda s: s["pc_rows"].append({"id": "nope", "company": "x", "image": ""}))
        broken(lambda s: s.update(article_plan=[first] * 2))
        broken(lambda s: s.update(article_plan=[first], articles={first: dict(ok_article(), company="")}))
        broken(lambda s: s.update(article_plan=[first], articles={first: {"status": "ok", "company": "x", "image": ""}}))
        broken(lambda s: s.update(planned_before_cap=-1))
        broken(lambda s: s.update(article_plan=[first], planned_before_cap=0))
        broken(lambda s: s.pop("plan_built"))
        broken(lambda s: s.update(spider_errors=None))
        broken(lambda s: s.update(kst_day="2020-01-02"))

    def test_unusable_close_reasons_are_not_publishable(self):
        for close_reason in (
            "shutdown",
            "cancelled",
            "rank_page_invalid:spo-2",
            fallback.CLOSE_CHAIN_BROKEN,
        ):
            with self.subTest(close_reason=close_reason):
                metrics, message = self.rejected(
                    make_snapshot(self.ranking, self.companies_for_all(), close_reason=close_reason)
                )
                self.assertTrue(metrics["stop_reason"].startswith("unexpected:"))
                self.assertIn("Fallback crawl stopped", message)

    def test_spider_errors_are_not_publishable(self):
        snapshot = make_snapshot(self.ranking, self.companies_for_all())
        snapshot["spider_errors"] = 1
        metrics, message = self.rejected(snapshot)
        self.assertEqual(metrics["stop_reason"], "unexpected:spider_errors:1")

    def test_complete_and_request_cap_require_the_plan_to_be_done(self):
        first, second = self.ranking[0]["id"], self.ranking[1]["id"]
        pc_rows = self.companies_for_all()
        # "finished" with an unrequested article: the chain stopped early.
        metrics, _ = self.rejected(
            make_snapshot(self.ranking, pc_rows, {first: ok_article()}, plan=[first, second])
        )
        self.assertEqual(metrics["stop_reason"], "unexpected:finished:plan_not_done")
        # "finished" although the cap cut the plan short.
        metrics, _ = self.rejected(
            make_snapshot(self.ranking, pc_rows, {first: ok_article()}, planned_before_cap=2)
        )
        self.assertEqual(metrics["stop_reason"], "unexpected:finished:plan_not_done")
        # "finished" before the plan was even built (a callback raised).
        snapshot = make_snapshot(self.ranking, pc_rows)
        snapshot["plan_built"] = False
        metrics, _ = self.rejected(snapshot)
        self.assertEqual(metrics["stop_reason"], "unexpected:finished:plan_not_done")
        # The request cap without a cap.
        metrics, _ = self.rejected(
            make_snapshot(self.ranking, pc_rows, {first: ok_article()}, close_reason=fallback.CLOSE_REQUEST_CAP)
        )
        self.assertEqual(metrics["stop_reason"], "unexpected:fallback_request_cap:plan_not_done")
        # A real cap passes, and says how much it skipped.
        items, metrics = fallback.assemble(
            make_snapshot(
                self.ranking,
                pc_rows,
                {first: ok_article()},
                close_reason=fallback.CLOSE_REQUEST_CAP,
                planned_before_cap=40,
            ),
            [],
            "run-1",
        )
        self.assertEqual(metrics["stop_reason"], "request_cap")
        self.assertEqual(metrics["article_requests"]["skipped_by_cap"], 39)
        validate_result.validate_result(items, metrics, "fallback")

    def test_stops_that_may_leave_requests_unsent_are_publishable(self):
        first, second = self.ranking[0]["id"], self.ranking[1]["id"]
        for close_reason, stop_reason in (
            ("closespider_timeout", "time_budget"),
            (fallback.CLOSE_TRANSPORT_PREFIX + "2", "transport_breaker"),
            (fallback.CLOSE_REFUSED_PREFIX + "429", "access_refused"),
        ):
            with self.subTest(close_reason=close_reason):
                items, metrics = fallback.assemble(
                    make_snapshot(
                        self.ranking,
                        self.companies_for_all(),
                        {first: ok_article()},
                        plan=[first, second],
                        close_reason=close_reason,
                        planned_before_cap=10,
                    ),
                    [],
                    "run-1",
                )
                self.assertEqual(metrics["stop_reason"], stop_reason)
                validate_result.validate_result(items, metrics, "fallback")

    def test_pc_company_conflicts_are_not_used(self):
        first = self.ranking[0]["id"]
        pc_rows = self.companies_for_all() + [{"id": first, "company": "다른일보", "image": ""}]
        items, metrics = fallback.assemble(make_snapshot(self.ranking, pc_rows), [], "run-1")
        self.assertEqual(metrics["pc"]["company_conflicts"], 1)
        self.assertEqual(metrics["excluded_no_company"], 1)
        self.assertNotIn(self.ranking[0]["url"], [item["newsURL"] for item in items])

    def test_cli_writes_result_and_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            (directory / "snapshot.json").write_text(
                json.dumps(make_snapshot(self.ranking, self.companies_for_all())), encoding="utf-8"
            )
            (directory / "previous.json").write_text("[]", encoding="utf-8")
            arguments = [
                "--snapshot", str(directory / "snapshot.json"),
                "--previous", str(directory / "previous.json"),
                "--out", str(directory / "out.json"),
                "--metrics", str(directory / "metrics.json"),
            ]
            environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            wrong = subprocess.run(
                [sys.executable, "-m", "nate_news.fallback", *arguments, "--run-id", "other"],
                cwd=PROJECT, capture_output=True, text=True, env=environment,
            )
            self.assertEqual(wrong.returncode, 1)
            self.assertFalse((directory / "out.json").exists())
            done = subprocess.run(
                [sys.executable, "-m", "nate_news.fallback", *arguments, "--run-id", "run-1"],
                cwd=PROJECT, capture_output=True, text=True, env=environment,
            )
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn("image: 0 of 215", done.stdout)
            checked = subprocess.run(
                [
                    sys.executable, "validate_result.py", "--mode", "fallback",
                    str(directory / "out.json"), str(directory / "metrics.json"),
                ],
                cwd=PROJECT, capture_output=True, text=True, env=environment,
            )
            self.assertEqual(checked.returncode, 0, checked.stdout)
            strict = subprocess.run(
                [
                    sys.executable, "validate_result.py",
                    str(directory / "out.json"), str(directory / "metrics.json"),
                ],
                cwd=PROJECT, capture_output=True, text=True, env=environment,
            )
            self.assertEqual(strict.returncode, 1)


def full_crawl(counts, failures=0):
    """Items and metrics shaped like a complete run of the full spider."""
    ranking = ranking_rows(counts)
    kept = ranking[: len(ranking) - failures]
    items = [
        {"title": row["title"], "company": "가나일보", "image": "", "newsURL": row["url"]}
        for row in kept
    ]
    pages = {page: 0 for page in validate_result.REQUIRED_PAGES}
    for row in ranking:
        pages[row["page"]] += 1
    metrics = {
        "close_reason": "finished",
        "rank_pages_completed": list(validate_result.REQUIRED_PAGES),
        "discovered_by_page": pages,
        "discovered_by_section": dict(counts),
        "articles_discovered": len(ranking),
        "discovered_urls": [row["url"] for row in ranking],
        "article_failures": failures,
        "article_items_yielded": len(kept),
        "item_scraped_count": len(kept),
        "item_dropped_count": 0,
        "item_error_count": 0,
    }
    return items, metrics


class ValidatorModeTest(unittest.TestCase):
    def setUp(self):
        self.counts = {"sisa": 100, "spo": 60, "ent": 100}
        ranking = ranking_rows(self.counts)
        pc_rows = [{"id": row["id"], "company": "가나일보", "image": ""} for row in ranking]
        self.items, self.metrics = fallback.assemble(
            make_snapshot(ranking, pc_rows), [], "run-1"
        )

    def rejects(self, items, metrics, mode):
        with self.assertRaises(validate_result.ValidationError) as caught:
            validate_result.validate_result(items, metrics, mode)
        return str(caught.exception)

    def changed(self, **updates):
        metrics = json.loads(json.dumps(self.metrics))
        for path, value in updates.items():
            target = metrics
            *parents, leaf = path.split("__")
            for parent in parents:
                target = target[parent]
            target[leaf] = value
        return metrics

    def test_full_mode_is_the_default_and_unchanged_for_full_crawls(self):
        items, metrics = full_crawl(self.counts)
        self.assertEqual(
            validate_result.validate_result(items, metrics),
            {
                "items": 260,
                "sections": self.counts,
                "discovered": self.counts,
                "article_failures": 0,
                "dropped": 0,
            },
        )
        items, metrics = full_crawl(self.counts, failures=5)
        self.assertEqual(validate_result.validate_result(items, metrics)["items"], 255)
        items, metrics = full_crawl(self.counts, failures=11)
        self.assertIn("Section coverage", self.rejects(items, metrics, "full"))
        items, metrics = full_crawl(self.counts)
        metrics["close_reason"] = "article_transport_failures:3"
        self.assertIn("Spider closed with", self.rejects(items, metrics, "full"))
        items, metrics = full_crawl(self.counts)
        metrics["article_failures"] = 1
        self.assertIn("Inconsistent crawl counters", self.rejects(items, metrics, "full"))
        items, metrics = full_crawl(self.counts)
        metrics["item_error_count"] = 1
        self.assertIn("Inconsistent crawl counters", self.rejects(items, metrics, "full"))

    def test_the_mode_is_chosen_by_the_caller_not_by_the_metrics(self):
        validate_result.validate_result(self.items, self.metrics, "fallback")
        self.assertIn("'fallback' crawl", self.rejects(self.items, self.metrics, "full"))
        items, metrics = full_crawl(self.counts)
        self.assertIn("'full' crawl", self.rejects(items, metrics, "fallback"))
        for mode in (None, "", 1):
            self.assertIn("crawl", self.rejects(self.items, self.changed(mode=mode), "fallback"))
        self.assertIn("Unknown validation mode", self.rejects(self.items, self.metrics, "other"))

    def test_stop_reason_must_match_the_close_reason(self):
        for stop_reason, close_reason in (
            ("complete", "shutdown"),
            ("complete", "closespider_timeout"),
            ("time_budget", "finished"),
            ("transport_breaker", "fallback_access_refused:403"),
            ("access_refused", None),
            ("unexpected:shutdown", "shutdown"),
            (None, "finished"),
            ("", "finished"),
        ):
            with self.subTest(stop_reason=stop_reason, close_reason=close_reason):
                self.assertIn(
                    "Fallback crawl stopped",
                    self.rejects(
                        self.items,
                        self.changed(stop_reason=stop_reason, close_reason=close_reason),
                        "fallback",
                    ),
                )

    def test_company_and_title_stay_required_but_image_may_be_empty(self):
        self.assertTrue(all(item["image"] == "" for item in self.items))
        validate_result.validate_result(self.items, self.metrics, "fallback")
        for field in ("company", "title"):
            items = [dict(item) for item in self.items]
            items[3][field] = ""
            self.assertIn("empty title/company", self.rejects(items, self.metrics, "fallback"))

    def test_ranking_and_coverage_rules_are_the_same_as_full(self):
        ranking = ranking_rows(self.counts)
        # 11 of 100 politics rows have no company: 89 is under the 90% floor.
        pc_rows = [{"id": row["id"], "company": "가", "image": ""} for row in ranking[11:]]
        items, metrics = fallback.assemble(make_snapshot(ranking, pc_rows), [], "run-1")
        self.assertIn("Section coverage", self.rejects(items, metrics, "fallback"))
        # A ranking that stopped after three pages is rejected.
        short = ranking_rows({"sisa": 100, "spo": 50, "ent": 0})
        pc_rows = [{"id": row["id"], "company": "가", "image": ""} for row in short]
        snapshot = make_snapshot(short, pc_rows)
        snapshot["rank_pages_completed"] = list(fallback.RANK_PAGES[:3])
        items, metrics = fallback.assemble(snapshot, [], "run-1")
        self.assertIn("Not all ranking pages", self.rejects(items, metrics, "fallback"))
        # Too few articles overall.
        small = ranking_rows({"sisa": 51, "spo": 51, "ent": 51})
        pc_rows = [{"id": row["id"], "company": "가", "image": ""} for row in small]
        items, metrics = fallback.assemble(make_snapshot(small, pc_rows), [], "run-1")
        self.assertIn("at least 200", self.rejects(items, metrics, "fallback"))
        # Reordered output.
        items = [dict(item) for item in self.items]
        items[0], items[1] = items[1], items[0]
        self.assertIn("order", self.rejects(items, self.metrics, "fallback"))

    def test_fallback_counters_must_add_up(self):
        for metrics in (
            self.changed(items_exported=259),
            self.changed(excluded_no_company=1),
            self.changed(dropped_duplicate_url=1),
            self.changed(dropped_duplicate_url=None),
            self.changed(spider_errors=1),
            self.changed(article_requests__attempted=1),
            self.changed(article_requests__ok=1),
            self.changed(article_requests__planned=1),
            self.changed(article_requests__planned=261, article_requests__not_requested=261),
            self.changed(article_requests__skipped_by_cap=1),
            self.changed(article_requests__planned_before_cap=1, article_requests__skipped_by_cap=1),
            self.changed(requests__article=1),
            self.changed(requests__mobile_rank=7),
            self.changed(requests__pc_rank=7),
            self.changed(requests__pc_rank=None),
            self.changed(requests__pc_rank=True),
            self.changed(article_requests=None),
            self.changed(company_sources__pc=259),
            self.changed(company_sources__cache=9999),
            self.changed(company_sources__pc=-1),
            self.changed(image_sources=None),
            self.changed(title_sources__ranking=1),
        ):
            self.assertIn("fallback", self.rejects(self.items, metrics, "fallback").lower())
        self.assertIn(
            "Inconsistent fallback counters",
            self.rejects(self.items[:-1], self.metrics, "fallback"),
        )

    def test_complete_with_requests_left_is_rejected(self):
        metrics = self.changed(
            article_requests__planned_before_cap=8,
            article_requests__planned=8,
            article_requests__not_requested=8,
        )
        self.assertIn("does not match the article requests", self.rejects(self.items, metrics, "fallback"))


# --- spider tests against a local stand-in for Nate ---

PC_FIRST_PAGE = tuple(range(1, 31))
# On 2026-11-14 the PC "page 2" listed mostly the same articles as page 1
# again, renumbered from 51 and without the thumbnail block.
PC_SECOND_PAGE = tuple(range(1, 26)) + tuple(range(31, 36))


class FakeNate:
    """Local stand-in for m.news.nate.com and news.nate.com, used as a proxy.

    Request numbers count every request the stand-in receives, from 1.
    """

    def __init__(
        self,
        counts,
        *,
        block_after=None,
        hang=(),
        close_paths=(),
        statuses=None,
        article_latency=0.0,
        refresh=False,
        octet_articles=(),
        no_company=(),
        redirect_articles=(),
        off_site_link=None,
        tag_only_title=None,
        pc_broken_page=None,
        pc_empty_page=None,
    ):
        self.counts = counts
        self.block_after = block_after
        self.hang = set(hang)
        self.close_paths = set(close_paths)
        self.statuses = statuses or {}
        self.article_latency = article_latency
        self.refresh = refresh
        self.octet_articles = set(octet_articles)
        self.no_company = set(no_company)
        self.redirect_articles = set(redirect_articles)
        self.off_site_link = off_site_link
        self.tag_only_title = tag_only_title
        self.pc_broken_page = pc_broken_page
        self.pc_empty_page = pc_empty_page
        self.requests = []
        self.release = threading.Event()
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *arguments):
                pass

            def do_GET(self):
                fake.handle(self)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        # A cut-off client drops its connections; that is not worth a traceback.
        self.server.handle_error = lambda *arguments: None
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()

    def kinds(self):
        return [kind for kind, _ in self.requests]

    def send(self, handler, status, body, content_type="text/html; charset=utf-8", headers=()):
        data = body.encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(data)))
        for name, value in headers:
            handler.send_header(name, value)
        handler.end_headers()
        handler.wfile.write(data)

    def kind(self, url):
        if url.hostname == "m.news.nate.com" and url.path == "/rank/list":
            return "mobile_rank"
        if url.hostname == "news.nate.com" and url.path == "/rank/interest":
            return "pc_rank"
        if url.hostname == "m.news.nate.com" and url.path.startswith("/view/"):
            return "article"
        return "other"

    def page(self, handler, body):
        head = (
            f'<meta http-equiv="refresh" content="0; url={handler.path}">' if self.refresh else ""
        )
        return f"<html><head>{head}</head><body>{body}</body></html>"

    def handle(self, handler):
        url = urlparse(handler.path)
        kind = self.kind(url)
        with self.lock:
            self.requests.append((kind, handler.path))
            number = len(self.requests)
        if number in self.hang or (self.block_after is not None and number > self.block_after):
            self.release.wait(60)  # the runner's view of a dropped connection
            return
        if url.path in self.close_paths:
            handler.close_connection = True  # closed without any answer
            return
        status = self.statuses.get(number) or self.statuses.get(kind)
        if status:
            self.send(handler, status, "refused")
            return
        query = parse_qs(url.query)
        if kind in ("mobile_rank", "pc_rank") and query.get("date", [""])[0] != DAY:
            self.send(handler, 404, "wrong day")
            return
        if kind == "mobile_rank":
            if query.get("rmode", [""])[0] != "interest":
                self.send(handler, 404, "wrong list")
                return
            section = query["section"][0]
            page = int(query["page"][0])
            self.send(handler, 200, self.page(handler, self.mobile_rank(section, page)))
        elif kind == "pc_rank":
            if query.get("p", [""])[0] != "day":
                self.send(handler, 404, "wrong list")
                return
            page = 2 if query.get("page", ["1"])[0] == "2" else 1
            self.send(handler, 200, self.page(handler, self.pc_rank(query["sc"][0], page)))
        elif kind == "article":
            identifier = url.path.rsplit("/", 1)[-1]
            if self.article_latency:
                time.sleep(self.article_latency)
            if identifier in self.redirect_articles:
                self.send(
                    handler, 302, "", headers=[("Location", "http://m.news.nate.com/view/20200102n99999")]
                )
            elif identifier in self.octet_articles:
                self.send(handler, 200, "\x00\x01\x02binary", content_type="application/octet-stream")
            else:
                self.send(handler, 200, self.page(handler, self.article(identifier)))
        else:
            self.send(handler, 404, "unknown")

    def positions(self, section, page):
        total = self.counts[section]
        return range(1, min(total, 50) + 1) if page == 1 else range(51, total + 1)

    def mobile_rank(self, section, page):
        rows = []
        for position in self.positions(section, page):
            title = (
                "[속보]"
                if self.tag_only_title == (section, position)
                else f"[속보] {section} 제목 {position}"
            )
            rows.append(
                f'<li class="r{position}"><a href="//m.news.nate.com/view/'
                f'{article_id_for(section, position)}?sect={section}&amp;list=rank&amp;cate=interest">'
                f'<span class="cnt">{position}</span><h2 class="context">{title}</h2></a></li>'
            )
        if self.off_site_link == (section, page):
            rows.append('<li><a href="https://example.com/view/20200102n00001?sect=sisa"><h2 class="context">x</h2></a></li>')
        advert = '<li class="ad"><iframe src="about:blank"></iframe></li>'
        return (
            '<div id="contents"><div class="rank_news"><ol>'
            + "".join(rows) + advert + "</ol></div></div>"
        )

    def pc_rank(self, section, page):
        if self.pc_empty_page == (section, page):
            return "<div>점검 중</div>"
        limit = self.counts[section]
        positions = [
            position
            for position in (PC_FIRST_PAGE if page == 1 else PC_SECOND_PAGE)
            if position <= limit
        ]
        top, rest = (positions[:5], positions[5:]) if page == 1 else ([], positions)

        def link(position):
            if self.pc_broken_page == (section, page) and position == positions[-1]:
                return "//[news.nate.com/view/broken"
            return f"//news.nate.com/view/{article_id_for(section, position)}?mid=n1006"

        top_rows = "".join(
            f'<div class="mduSubjectList f_clear"><dl class="mduRank"><dt><em>{position}</em></dt></dl>'
            f'<div class="mlt01"><a href="{link(position)}" class="lt1"><span class="ib">'
            f'<em class="mediatype"><img src="//thumbnews.nateimg.co.kr/news90///news.nateimg.co.kr/'
            f'orgImg/pc/{article_id_for(section, position)}.jpg" /></em></span>'
            f'<span class="tb"><h2 class="tit">잘린 제목...</h2>요약</span></a>'
            f'<span class="medium">피씨일보<em>2026-11-14</em></span></div></div>'
            for position in top
        )
        list_rows = "".join(
            f'<li><dl class="mduRank"><dt><em>{position + (50 if page == 2 else 0)}</em></dt></dl>'
            f'<a href="{link(position)}"><h2>잘린 제목...</h2></a>'
            f'<span class="medium">피씨일보</span></li>'
            for position in rest
        )
        return (
            '<div class="postRankSubjectList f_clear">' + top_rows + "</div>"
            '<div class="postRankSubject" id="postRankSubject">'
            '<ul class="mduSubject mduRankSubject">' + list_rows + "</ul></div>"
        )

    def article(self, identifier):
        image = (
            ""
            if identifier.endswith("7")
            else '<div id="content_img"><a><img src="https://thumbnews.nateimg.co.kr/view610///'
            f'news.nateimg.co.kr/orgImg/art/{identifier}.jpg"></a></div>'
        )
        company = (
            ""
            if identifier in self.no_company
            else '<div class="medium"><div><em><b>기사신문</b></em></div></div>'
        )
        return (
            '<div id="contents"><div class="responsive_wrap">'
            '<section class="rwd_left"><div><header>'
            f'<h1 id="artcTitle">[IN REVIEW] 기사 제목 {identifier}</h1>'
            + company + "</header>" + image + "</div></section></div></div>"
        )


RUNNER = """
import sys
from scrapy.crawler import CrawlerProcess
from scrapy.utils.project import get_project_settings
from nate_news.spiders.nate_news_fallback import NateNewsFallbackSpider


class OfflineFallbackSpider(NateNewsFallbackSpider):
    # Same pages as production, but plain http so the local proxy can serve them.
    MOBILE_RANK_URL = NateNewsFallbackSpider.MOBILE_RANK_URL.replace("https://", "http://")
    PC_RANK_URL = NateNewsFallbackSpider.PC_RANK_URL.replace("https://", "http://")


settings = get_project_settings()
for argument in sys.argv[1:]:
    key, value = argument.split("=", 1)
    settings.set(key, value, priority="cmdline")
process = CrawlerProcess(settings)
process.crawl(OfflineFallbackSpider)
process.start()
"""

COUNTS = {"sisa": 100, "spo": 57, "ent": 91}
RANKED = sum(COUNTS.values())
PC_THUMBNAILS = 15


def previous_for_all(counts=COUNTS, image=""):
    """A previous publish that knows every ranked article's company."""
    return [
        published(article_id_for(section, position), section, f"{section} 제목 {position}", company="이전일보", image=image)
        for section in SECTIONS
        for position in range(1, counts[section] + 1)
    ]


@unittest.skipUnless(SPIDER_IMPORTABLE, SPIDER_SKIP_REASON)
class FallbackSpiderTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.work = Path(self.directory.name)
        self.fake = None

    def crawl(self, previous=(), **settings):
        self.addCleanup(self.fake.close)
        (self.work / "previous.json").write_text(
            json.dumps(list(previous), ensure_ascii=False), encoding="utf-8"
        )
        (self.work / "runner.py").write_text(RUNNER, encoding="utf-8")
        options = {
            "NATE_FALLBACK_RUN_ID": "run-1",
            "NATE_FALLBACK_SNAPSHOT_PATH": str(self.work / "snapshot.json"),
            "NATE_FALLBACK_PREVIOUS_PATH": str(self.work / "previous.json"),
            "NATE_FALLBACK_KST_DAY": DAY,
            "DOWNLOAD_DELAY": "0",
            "DOWNLOAD_TIMEOUT": "1",
            "TELNETCONSOLE_ENABLED": "0",
            "LOG_LEVEL": "INFO",
        }
        options.update(settings)
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.upper().endswith("_PROXY")
        }
        environment.update(
            http_proxy=f"http://127.0.0.1:{self.fake.port}",
            PYTHONPATH=str(PROJECT),
            PYTHONDONTWRITEBYTECODE="1",
            SCRAPY_SETTINGS_MODULE="nate_news.settings",
        )
        self.process = subprocess.run(
            [sys.executable, str(self.work / "runner.py")]
            + [f"{key}={value}" for key, value in options.items()],
            cwd=self.work, capture_output=True, text=True, env=environment, timeout=120,
        )
        snapshot_path = self.work / "snapshot.json"
        if not snapshot_path.exists():
            return None
        return json.loads(snapshot_path.read_text(encoding="utf-8"))

    def fresh_work(self):
        self.work = Path(tempfile.mkdtemp(dir=self.directory.name))

    def assemble(self, snapshot, previous=()):
        return fallback.assemble(snapshot, list(previous), "run-1")

    def assert_each_url_requested_once(self):
        paths = [path for _, path in self.fake.requests]
        self.assertEqual(len(paths), len(set(paths)), "a URL was requested twice")
        self.assertLessEqual(len(paths), fallback.MAX_TOTAL_REQUESTS)

    def assert_counts_match_the_server(self, snapshot):
        kinds = self.fake.kinds()
        for kind in ("mobile_rank", "pc_rank", "article"):
            self.assertEqual(snapshot["requests"][kind], kinds.count(kind), kind)

    def assert_not_publishable(self, snapshot, previous=()):
        items, metrics = self.assemble(snapshot, previous)
        with self.assertRaises(validate_result.ValidationError):
            validate_result.validate_result(items, metrics, "fallback")
        return metrics

    def test_budget_settings_are_pinned(self):
        settings = fallback_spider.NateNewsFallbackSpider.custom_settings
        self.assertGreaterEqual(settings["DOWNLOAD_DELAY"], 0.5)
        for name in ("RETRY_ENABLED", "REDIRECT_ENABLED", "METAREFRESH_ENABLED"):
            self.assertIs(settings[name], False, name)
        self.assertGreater(settings["CLOSESPIDER_TIMEOUT"], 0)
        self.assertEqual((settings["FEEDS"], settings["ITEM_PIPELINES"]), ({}, {}))
        project = importlib.import_module("nate_news.settings")
        self.assertEqual(
            (project.CONCURRENT_REQUESTS, project.CONCURRENT_REQUESTS_PER_DOMAIN), (1, 1)
        )
        self.assertEqual(fallback_spider.NateNewsFallbackSpider.DEFAULT_TRANSPORT_FAILURE_LIMIT, 2)

    def test_unblocked_run_requests_only_what_is_missing(self):
        self.fake = FakeNate(COUNTS)
        # Five articles are fully known from the previous publish.
        previous = [
            published(article_id_for("sisa", position), "sisa", f"sisa 제목 {position}")
            for position in range(41, 46)
        ]
        snapshot = self.crawl(previous=previous)
        self.assertEqual(snapshot["close_reason"], "finished", self.process.stderr[-2000:])
        kinds = self.fake.kinds()
        self.assertEqual(kinds[:12], ["mobile_rank"] * 6 + ["pc_rank"] * 6)
        self.assertEqual(kinds.count("article"), RANKED - PC_THUMBNAILS - 5)
        requested = {path for _, path in self.fake.requests}
        for position in range(41, 46):
            self.assertFalse(
                any(article_id_for("sisa", position) in path for path in requested)
            )
        self.assert_each_url_requested_once()
        self.assert_counts_match_the_server(snapshot)
        self.assertFalse((self.work / "nate_result.json").exists(), "the feed must stay off")
        self.assertEqual(len({row["id"] for row in snapshot["pc_rows"]}), 105)
        items, metrics = self.assemble(snapshot, previous)
        self.assertEqual(len(items), RANKED)
        self.assertEqual(
            (items[0]["title"], items[0]["company"], items[0]["image"]),
            ("sisa 제목 1", "피씨일보", f"https://news.nateimg.co.kr/orgImg/pc/{article_id_for('sisa', 1)}.jpg"),
        )
        self.assertEqual(
            (items[40]["title"], items[40]["company"], items[40]["image"]),
            ("sisa 제목 41", "이전일보", "https://img/old.jpg"),
        )
        self.assertEqual(
            (items[50]["title"], items[50]["company"], items[50]["image"]),
            (
                f"[IN REVIEW] 기사 제목 {article_id_for('sisa', 51)}",
                "기사신문",
                f"https://news.nateimg.co.kr/orgImg/art/{article_id_for('sisa', 51)}.jpg",
            ),
        )
        self.assertEqual(metrics["image_sources"]["empty_unobserved"], 0)
        self.assertEqual(metrics["image_sources"]["pc"], PC_THUMBNAILS)
        self.assertGreater(metrics["image_sources"]["empty_confirmed"], 0)
        validate_result.validate_result(items, metrics, "fallback")

    def test_cut_off_run_keeps_what_it_has_and_stops_after_two_failures(self):
        self.fake = FakeNate(COUNTS, block_after=40)
        previous = previous_for_all()
        # spo 30: a reusable image, with the company the PC ranking also shows.
        previous[100 + 30 - 1].update(image="https://img/spo30.jpg", company="피씨일보")
        snapshot = self.crawl(previous=previous)
        self.assertEqual(snapshot["close_reason"], "fallback_transport_failures:2")
        self.assertEqual(len(self.fake.requests), 42)  # 40 answered, 2 dropped, then nothing
        self.assert_each_url_requested_once()
        self.assert_counts_match_the_server(snapshot)
        items, metrics = self.assemble(snapshot, previous)
        self.assertEqual(len(items), RANKED)
        self.assertEqual(metrics["stop_reason"], "transport_breaker")
        self.assertEqual(metrics["article_requests"]["ok"], 28)
        self.assertEqual(metrics["article_requests"]["failed"], 2)
        self.assertEqual(metrics["image_sources"]["cache"], 1)
        # The top of every section was enriched first.
        with_image = {item["newsURL"] for item in items if item["image"]}
        for section in SECTIONS:
            for position in range(1, 15):
                identifier = article_id_for(section, position)
                # The stand-in's article pages whose id ends in 7 have no image.
                if not identifier.endswith("7"):
                    self.assertIn(article_url(identifier, section, "http"), with_image)
        validate_result.validate_result(items, metrics, "fallback")

    def test_time_budget_stop_is_publishable(self):
        self.fake = FakeNate(COUNTS, article_latency=0.2)
        previous = previous_for_all()
        snapshot = self.crawl(previous=previous, CLOSESPIDER_TIMEOUT="3")
        self.assertEqual(snapshot["close_reason"], "closespider_timeout")
        self.assert_counts_match_the_server(snapshot)
        self.assertEqual(snapshot["requests"]["article"], len(snapshot["articles"]))
        items, metrics = self.assemble(snapshot, previous)
        self.assertEqual(metrics["stop_reason"], "time_budget")
        self.assertGreater(metrics["article_requests"]["not_requested"], 0)
        validate_result.validate_result(items, metrics, "fallback")

    def test_transport_streak_is_reset_by_any_answer(self):
        for name, behaviour, expected in (
            ("answer between", {"hang": {15, 17}}, "finished"),
            ("error page between", {"hang": {15, 17}, "statuses": {16: 500}}, "finished"),
            ("two in a row", {"hang": {15, 16}}, "fallback_transport_failures:2"),
        ):
            with self.subTest(case=name):
                self.fresh_work()
                self.fake = FakeNate(COUNTS, **behaviour)
                snapshot = self.crawl(NATE_FALLBACK_MAX_ARTICLE_REQUESTS="8")
                expected_close = "fallback_request_cap" if expected == "finished" else expected
                self.assertEqual(snapshot["close_reason"], expected_close)
                self.assert_counts_match_the_server(snapshot)

    def test_a_connection_closed_without_an_answer_is_a_transport_failure(self):
        # The first two planned articles: no company known, top rank first.
        first, second = article_id_for("sisa", 36), article_id_for("spo", 36)
        self.fake = FakeNate(COUNTS, close_paths={f"/view/{first}", f"/view/{second}"})
        snapshot = self.crawl()
        self.assertEqual(snapshot["close_reason"], "fallback_transport_failures:2")
        self.assertEqual(list(snapshot["articles"]), [first, second])
        # Twisted may send a GET once more on a reused connection that closed
        # before answering; Scrapy itself sends each URL once.
        paths = [path for kind, path in self.fake.requests if kind == "article"]
        self.assertTrue(all(paths.count(path) <= 2 for path in paths))

    def test_cut_off_during_ranking_is_not_publishable(self):
        self.fake = FakeNate(COUNTS, block_after=3)
        snapshot = self.crawl()
        self.assertEqual(snapshot["close_reason"], "rank_page_transport_failed:spo-2")
        self.assertEqual(len(self.fake.requests), 4)
        self.assert_not_publishable(snapshot)

    def test_ranking_page_errors_and_refusals(self):
        for name, behaviour, close_reason, requests in (
            ("403", {"statuses": {1: 403}}, "fallback_access_refused:403", 1),
            ("429", {"statuses": {4: 429}}, "fallback_access_refused:429", 4),
            ("503", {"statuses": {2: 503}}, "rank_page_request_failed:sisa-2", 2),
            ("off-site link", {"off_site_link": ("sisa", 1)}, "rank_page_invalid:sisa-1", 1),
        ):
            with self.subTest(case=name):
                self.fresh_work()
                self.fake = FakeNate(COUNTS, **behaviour)
                snapshot = self.crawl()
                self.assertEqual(snapshot["close_reason"], close_reason)
                self.assertEqual(len(self.fake.requests), requests)
                self.assert_not_publishable(snapshot)

    def test_tag_only_title_keeps_its_text(self):
        self.fake = FakeNate(COUNTS, tag_only_title=("sisa", 3))
        snapshot = self.crawl(NATE_FALLBACK_MAX_ARTICLE_REQUESTS="1")
        self.assertEqual(snapshot["ranking"][2]["title"], "[속보]")
        self.assertEqual(snapshot["ranking"][3]["title"], "sisa 제목 4")

    def test_pc_stage_failure_is_skipped_but_counts_toward_the_breaker(self):
        # Request 7 (the first PC page) is dropped, then the first article too.
        self.fake = FakeNate(COUNTS, block_after=6)
        snapshot = self.crawl()
        self.assertEqual(snapshot["pc_stop_reason"], "transport_failure:pc-sisa-1")
        self.assertEqual(snapshot["close_reason"], "fallback_transport_failures:2")
        self.assertEqual(self.fake.kinds()[6:], ["pc_rank", "article"])

    def test_pc_problems_end_only_the_pc_stage(self):
        for name, behaviour, stop in (
            ("error page", {"statuses": {"pc_rank": 404}}, "request_failed:pc-sisa-1"),
            ("empty page", {"pc_empty_page": ("sisa", 1)}, "no_rows:pc-sisa-1"),
            ("unparsable page", {"pc_broken_page": ("spo", 1)}, "parse_error:pc-spo-1"),
        ):
            with self.subTest(case=name):
                self.fresh_work()
                self.fake = FakeNate(COUNTS, **behaviour)
                snapshot = self.crawl(NATE_FALLBACK_MAX_ARTICLE_REQUESTS="20")
                self.assertEqual(snapshot["pc_stop_reason"], stop)
                self.assertEqual(snapshot["close_reason"], "fallback_request_cap")
                self.assertEqual(snapshot["spider_errors"], 0)
                self.assertEqual(self.fake.kinds().count("article"), 20)
                self.assertTrue(snapshot["plan_built"])

    def test_refusal_on_a_pc_or_article_page_ends_the_run(self):
        for name, statuses, expected in (
            ("pc", {"pc_rank": 403}, 7),
            ("article", {20: 429}, 20),
        ):
            with self.subTest(stage=name):
                self.fresh_work()
                self.fake = FakeNate(COUNTS, statuses=statuses)
                snapshot = self.crawl()
                self.assertTrue(snapshot["close_reason"].startswith("fallback_access_refused:"))
                self.assertEqual(len(self.fake.requests), expected)
                self.assert_counts_match_the_server(snapshot)

    def test_article_problems_are_recorded_and_the_run_goes_on(self):
        first = article_id_for("sisa", 36)
        for name, behaviour, reason in (
            ("no company", {"no_company": {first}}, "company missing"),
            ("redirect", {"redirect_articles": {first}}, "HttpError"),
        ):
            with self.subTest(case=name):
                self.fresh_work()
                self.fake = FakeNate(COUNTS, **behaviour)
                snapshot = self.crawl(NATE_FALLBACK_MAX_ARTICLE_REQUESTS="3")
                self.assertEqual(snapshot["close_reason"], "fallback_request_cap")
                self.assertEqual(snapshot["articles"][first]["status"], "failed")
                self.assertIn(reason, snapshot["articles"][first]["reason"])
                self.assertEqual(len(snapshot["articles"]), 3)
                self.assert_each_url_requested_once()

    def test_meta_refresh_is_never_followed(self):
        self.fake = FakeNate(COUNTS, refresh=True)
        snapshot = self.crawl(NATE_FALLBACK_MAX_ARTICLE_REQUESTS="5")
        self.assertEqual(snapshot["close_reason"], "fallback_request_cap")
        self.assertEqual(len(self.fake.requests), 6 + 6 + 5)
        self.assert_each_url_requested_once()

    def test_an_exception_in_a_callback_is_not_publishable(self):
        # The first planned article (the previous publish knows every company,
        # so the plan starts with images, after the five PC thumbnails)
        # answers with a body Scrapy cannot parse.
        self.fake = FakeNate(COUNTS, octet_articles={article_id_for("sisa", 6)})
        previous = previous_for_all()
        snapshot = self.crawl(previous=previous)
        self.assertEqual(snapshot["close_reason"], fallback.CLOSE_CHAIN_BROKEN)
        self.assertEqual(snapshot["spider_errors"], 1)
        metrics = self.assert_not_publishable(snapshot, previous)
        self.assertTrue(metrics["stop_reason"].startswith("unexpected:"))

    def test_both_lists_are_asked_for_the_configured_day(self):
        self.fake = FakeNate(COUNTS)
        self.crawl(NATE_FALLBACK_MAX_ARTICLE_REQUESTS="1")
        for kind, path in self.fake.requests[:12]:
            self.assertEqual(parse_qs(urlparse(path).query)["date"], [DAY], path)

    def test_missing_configuration_fails_before_any_request(self):
        for name, settings in (
            ("no run id", {"NATE_FALLBACK_RUN_ID": ""}),
            ("bad day", {"NATE_FALLBACK_KST_DAY": "2020-01-02"}),
            ("bad cap", {"NATE_FALLBACK_MAX_ARTICLE_REQUESTS": "1.5"}),
            ("zero cap", {"NATE_FALLBACK_MAX_ARTICLE_REQUESTS": "0"}),
            ("bad limit", {"NATE_FALLBACK_MAX_CONSECUTIVE_TRANSPORT_FAILURES": "x"}),
            ("missing previous", {"NATE_FALLBACK_PREVIOUS_PATH": "/nonexistent/previous.json"}),
        ):
            with self.subTest(case=name):
                self.fresh_work()
                self.fake = FakeNate(COUNTS)
                self.assertIsNone(self.crawl(**settings))
                self.assertEqual(self.fake.requests, [])
        self.fresh_work()
        self.fake = FakeNate(COUNTS)
        (self.work / "snapshot.json").write_text("{}", encoding="utf-8")
        self.crawl()
        self.assertEqual(self.fake.requests, [])
        self.assertEqual((self.work / "snapshot.json").read_text(encoding="utf-8"), "{}")

    def test_article_cap_cannot_exceed_the_ranking_size(self):
        from scrapy.utils.test import get_crawler

        previous = self.work / "previous.json"
        previous.write_text("[]", encoding="utf-8")
        crawler = get_crawler(
            fallback_spider.NateNewsFallbackSpider,
            {
                "NATE_FALLBACK_RUN_ID": "run-1",
                "NATE_FALLBACK_SNAPSHOT_PATH": str(self.work / "snapshot.json"),
                "NATE_FALLBACK_PREVIOUS_PATH": str(previous),
                "NATE_FALLBACK_KST_DAY": DAY,
                "NATE_FALLBACK_MAX_ARTICLE_REQUESTS": "301",
            },
        )
        spider = fallback_spider.NateNewsFallbackSpider.from_crawler(crawler)
        self.assertEqual(spider.max_article_requests, fallback.MAX_RANKED_ARTICLES)


class WorkflowDiagnosticsTest(unittest.TestCase):
    """The diagnostics step may contact Nate only after a transport failure."""

    def diagnostics_script(self):
        text = WORKFLOW.read_text()
        start = text.index("    - name: Network diagnostics")
        end = text.index("    - name:", start + 10)
        block = text[start:end]
        return textwrap.dedent(block.split("      run: |\n", 1)[1])

    def run_diagnostics(self, close_reason):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            stubs = directory / "bin"
            stubs.mkdir()
            log = directory / "calls.log"
            for name in ("curl", "getent", "timeout"):
                stub = stubs / name
                stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\n')
                stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
            if close_reason is not None:
                (directory / "fallback_snapshot_7-1.json").write_text(
                    json.dumps({"close_reason": close_reason}), encoding="utf-8"
                )
            environment = dict(
                os.environ,
                PATH=f"{stubs}{os.pathsep}{os.environ['PATH']}",
                RUNNER_TEMP=str(directory),
                GITHUB_RUN_ID="7",
                GITHUB_RUN_ATTEMPT="1",
                PYTHONDONTWRITEBYTECODE="1",
            )
            subprocess.run(
                ["bash", "-c", self.diagnostics_script()],
                cwd=PROJECT, env=environment, capture_output=True, text=True, timeout=60,
            )
            return log.read_text() if log.exists() else ""

    def test_probe_only_after_a_ranking_transport_failure(self):
        calls = self.run_diagnostics("rank_page_transport_failed:sisa-1")
        self.assertIn("m.news.nate.com", calls)
        for close_reason in (
            "fallback_access_refused:403",
            "rank_page_request_failed:sisa-1",
            "rank_page_invalid:sisa-1",
            "fallback_transport_failures:2",
            "finished",
            None,
        ):
            with self.subTest(close_reason=close_reason):
                self.assertNotIn("nate", self.run_diagnostics(close_reason))


if __name__ == "__main__":
    unittest.main()
