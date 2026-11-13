import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import scrapy
from scrapy import signals
from scrapy.exceptions import CloseSpider
from scrapy.spidermiddlewares.httperror import HttpError

from nate_news import fallback
from nate_news.spiders.nate_news_rank import NateNewsRankSpider


class NateNewsFallbackSpider(scrapy.Spider):
    """Degraded crawl for machines that lose access to Nate mid-crawl.

    It reads the six mobile ranking pages (order, URL, title), then the six PC
    ranking pages (company, top thumbnails), then requests article pages only
    for the articles whose company or image is still unknown after looking at
    the previously published file, most valuable first, until it finishes or
    is cut off. It yields no items: closed() writes a raw snapshot, and
    `python -m nate_news.fallback` turns that snapshot into the published
    format.

    This class deliberately does not inherit from NateNewsRankSpider, so the
    full crawl's request chain, signal handlers and metrics cannot leak into
    it. Only selectors, the transport failure list and the two extraction
    helpers are shared.

    Request budget: Scrapy issues every URL once. There are no download
    retries, no parse retries, no HTTP redirects and no meta refreshes, so at
    most 6 + 6 + 300 requests leave the spider. Below Scrapy, Twisted may send
    a GET a second time when a reused keep-alive connection is closed before
    any byte of the answer arrives; a silent drop (the runner cut-off) ends in
    a timeout instead and is not resent.
    """

    name = "nate_news_fallback"
    allowed_domains = ["m.news.nate.com", "news.nate.com"]

    custom_settings = {
        # The value the full crawl used until 2026-11-13. The 54 blocked runs
        # at this rate and the three at 2.0 s were cut off after a similar
        # number of seconds, so the slower experimental delay mainly reduced
        # how many pages arrived; what triggers the cut-off is unconfirmed.
        # Not faster than the old operating value, and one request at a time.
        "DOWNLOAD_DELAY": 0.5,
        "RETRY_ENABLED": False,
        "REDIRECT_ENABLED": False,
        # MetaRefreshMiddleware has its own switch; REDIRECT_ENABLED does not
        # cover it.
        "METAREFRESH_ENABLED": False,
        # The published file is written by the assembler, not by a feed.
        "FEEDS": {},
        "ITEM_PIPELINES": {},
        # Overall time budget for one fallback crawl.
        "CLOSESPIDER_TIMEOUT": 10 * 60,
    }

    # Both lists are asked for the same KST day, fixed when the crawl starts,
    # so a run that crosses midnight cannot mix two days.
    MOBILE_RANK_URL = (
        "https://m.news.nate.com/rank/list?mid=m00&section={section}"
        "&rmode=interest&date={day}&page={page}"
    )
    PC_RANK_URL = "https://news.nate.com/rank/interest?sc={section}&p=day&date={day}"
    SECTIONS = fallback.SECTIONS

    MOBILE_ROW_SELECTOR = "#contents > div.rank_news > ol > li > a"
    MOBILE_TITLE_SELECTOR = "h2.context"
    PC_TOP_ROW_SELECTOR = "div.postRankSubjectList > div.mduSubjectList"
    PC_LIST_ROW_SELECTOR = "#postRankSubject ul.mduRankSubject > li"

    RANK_CONTAINER_SELECTOR = NateNewsRankSpider.RANK_CONTAINER_SELECTOR
    TITLE_SELECTOR = NateNewsRankSpider.TITLE_SELECTOR
    COMPANY_SELECTOR = NateNewsRankSpider.COMPANY_SELECTOR
    IMAGE_SELECTORS = NateNewsRankSpider.IMAGE_SELECTORS
    THUMBNAIL_PREFIX = NateNewsRankSpider.THUMBNAIL_PREFIX
    SQUARE_BRACKET_PREFIX = NateNewsRankSpider.SQUARE_BRACKET_PREFIX
    TRANSPORT_FAILURES = NateNewsRankSpider.TRANSPORT_FAILURES
    _extract_text = staticmethod(NateNewsRankSpider._extract_text)
    _extract_image = NateNewsRankSpider._extract_image

    # An explicit refusal ends the run: asking again would only repeat it.
    REFUSAL_STATUSES = (403, 429)

    DEFAULT_TRANSPORT_FAILURE_LIMIT = 2
    TRANSPORT_FAILURE_LIMIT_SETTING = (
        "NATE_FALLBACK_MAX_CONSECUTIVE_TRANSPORT_FAILURES"
    )
    MAX_ARTICLE_REQUESTS_SETTING = "NATE_FALLBACK_MAX_ARTICLE_REQUESTS"
    SNAPSHOT_PATH_SETTING = "NATE_FALLBACK_SNAPSHOT_PATH"
    PREVIOUS_PATH_SETTING = "NATE_FALLBACK_PREVIOUS_PATH"
    RUN_ID_SETTING = "NATE_FALLBACK_RUN_ID"
    KST_DAY_SETTING = "NATE_FALLBACK_KST_DAY"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ranking = []
        self.rank_pages_completed = []
        self.pc_rows = []
        self.pc_pages_completed = []
        self.pc_stop_reason = None
        self.plan_built = False
        self.chain_complete = False
        self.article_plan = []
        self.planned_before_cap = 0
        self.articles = {}
        self.next_plan_index = 0
        self.url_by_id = {}
        # Requests that got an outcome (a response or a failure). A request
        # that was built but never sent because the spider was closing does
        # not count.
        self.request_counts = {"mobile_rank": 0, "pc_rank": 0, "article": 0}
        self.spider_errors = 0
        self.consecutive_transport_failures = 0
        self.started_at = None
        self.cache = {}

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        settings = crawler.settings
        spider.transport_failure_limit = cls._read_positive_int(
            settings,
            cls.TRANSPORT_FAILURE_LIMIT_SETTING,
            cls.DEFAULT_TRANSPORT_FAILURE_LIMIT,
        )
        spider.max_article_requests = min(
            cls._read_positive_int(
                settings,
                cls.MAX_ARTICLE_REQUESTS_SETTING,
                fallback.MAX_RANKED_ARTICLES,
            ),
            fallback.MAX_RANKED_ARTICLES,
        )
        spider.run_id = cls._read_required(settings, cls.RUN_ID_SETTING)
        spider.snapshot_path = Path(
            cls._read_required(settings, cls.SNAPSHOT_PATH_SETTING)
        )
        # A snapshot from an earlier run must not survive into this one.
        if spider.snapshot_path.exists():
            raise ValueError(
                f"{cls.SNAPSHOT_PATH_SETTING} already exists:"
                f" {spider.snapshot_path}"
            )
        spider.started_at = datetime.now(fallback.KST)
        raw_day = settings.get(cls.KST_DAY_SETTING) or spider.started_at.strftime(
            "%Y%m%d"
        )
        spider.kst_day = str(raw_day)
        day = (
            fallback.article_day(spider.kst_day + "n")
            if re.fullmatch(r"[0-9]{8}", spider.kst_day)
            else None
        )
        if day is None:
            raise ValueError(
                f"{cls.KST_DAY_SETTING} must be YYYYMMDD, got {raw_day!r}"
            )
        previous_path = Path(cls._read_required(settings, cls.PREVIOUS_PATH_SETTING))
        # Read before any request is sent: a broken previous file is a
        # configuration problem, not something to discover after crawling.
        previous = json.loads(previous_path.read_text(encoding="utf-8"))
        spider.cache = fallback.build_cache(previous, day)
        crawler.signals.connect(spider._record_spider_error, signal=signals.spider_error)
        return spider

    @staticmethod
    def _read_required(settings, name):
        value = settings.get(name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} is required for the fallback crawl")
        return value

    @staticmethod
    def _read_positive_int(settings, name, default):
        # settings.getint() would silently turn 1.5 or True into 1.
        raw = settings.get(name, default)
        value = raw
        if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer, got {raw!r}")
        return value

    def _request(self, url, callback, errback, meta):
        # dont_redirect also holds if a command-line setting turns a redirect
        # middleware back on.
        return scrapy.Request(
            url,
            callback=callback,
            errback=errback,
            dont_filter=True,
            meta={**meta, "dont_redirect": True},
        )

    async def start(self):
        yield self._mobile_rank_request(0)

    # --- mobile ranking: order, URL and title. Any failure ends the run. ---

    def _mobile_pages(self):
        return [
            (
                f"{section}-{page}",
                section,
                self.MOBILE_RANK_URL.format(section=section, day=self.kst_day, page=page),
            )
            for section in self.SECTIONS
            for page in (1, 2)
        ]

    def _mobile_rank_request(self, page_index):
        page_name, _, page_url = self._mobile_pages()[page_index]
        return self._request(
            page_url,
            self.parse_mobile_rank,
            self.mobile_rank_errback,
            {"nate_page_index": page_index, "nate_page_name": page_name},
        )

    def parse_mobile_rank(self, response):
        self.request_counts["mobile_rank"] += 1
        self.consecutive_transport_failures = 0
        page_index = response.meta["nate_page_index"]
        page_name, section, _ = self._mobile_pages()[page_index]
        rows = []
        invalid = []
        for position, anchor in enumerate(
            response.css(self.MOBILE_ROW_SELECTOR), start=1
        ):
            article_url = response.urljoin(anchor.attrib.get("href", "").strip())
            parsed = urlparse(article_url)
            identifier = fallback.article_id(article_url)
            raw_title = self._extract_text(anchor, self.MOBILE_TITLE_SELECTOR)
            # Same normalization as the full crawl; a title that is nothing
            # but a [tag] keeps its raw text instead of becoming empty.
            title = self.SQUARE_BRACKET_PREFIX.sub("", raw_title) or raw_title
            if (
                parsed.scheme in ("http", "https")
                and parsed.hostname == "m.news.nate.com"
                and identifier is not None
                and parse_qs(parsed.query).get("sect", [""])[0] == section
                and title
            ):
                rows.append(
                    {
                        "id": identifier,
                        "url": article_url,
                        "section": section,
                        "page": page_name,
                        "page_position": position,
                        "title": title,
                    }
                )
            else:
                invalid.append(article_url)

        if not response.css(self.RANK_CONTAINER_SELECTOR) or not rows or invalid:
            self.logger.error(
                "%s failed ranking-page validation (rows=%d, invalid=%r)",
                page_name,
                len(rows),
                invalid[:3],
            )
            raise CloseSpider(reason=f"rank_page_invalid:{page_name}")

        self.ranking.extend(rows)
        self.rank_pages_completed.append(page_name)
        self.logger.info("%s: %d ranked articles", page_name, len(rows))

        if page_index + 1 < len(self._mobile_pages()):
            yield self._mobile_rank_request(page_index + 1)
        else:
            yield self._pc_rank_request(0)

    def mobile_rank_errback(self, failure):
        self.request_counts["mobile_rank"] += 1
        page_name = failure.request.meta["nate_page_name"]
        self.logger.error("%s request failed: %r", page_name, failure.value)
        self._close_if_refused(failure)
        # Only a failure without any answer is worth a network probe in the
        # workflow; Nate did answer an HTTP error.
        if failure.check(*self.TRANSPORT_FAILURES):
            raise CloseSpider(reason=f"rank_page_transport_failed:{page_name}")
        raise CloseSpider(reason=f"rank_page_request_failed:{page_name}")

    # --- PC ranking: company for many rows, a thumbnail for the top five. ---
    # Optional. Whatever goes wrong here ends only this stage, except an
    # explicit refusal, which ends the run. The stage is never re-entered.

    def _pc_pages(self):
        pages = []
        for section in self.SECTIONS:
            base = self.PC_RANK_URL.format(section=section, day=self.kst_day)
            pages.append((f"pc-{section}-1", base))
            pages.append((f"pc-{section}-2", base + "&page=2"))
        return pages

    def _pc_rank_request(self, page_index):
        page_name, page_url = self._pc_pages()[page_index]
        return self._request(
            page_url,
            self.parse_pc_rank,
            self.pc_rank_errback,
            {"nate_page_index": page_index, "nate_page_name": page_name},
        )

    def parse_pc_rank(self, response):
        self.request_counts["pc_rank"] += 1
        self.consecutive_transport_failures = 0
        page_index = response.meta["nate_page_index"]
        page_name = response.meta["nate_page_name"]
        try:
            rows = self._pc_rows_of(response)
        except Exception:
            # An unparsable optional page must not end the whole crawl.
            self.logger.exception("%s could not be parsed; leaving the PC stage", page_name)
            yield from self._leave_pc_stage(f"parse_error:{page_name}")
            return
        if not rows:
            self.logger.warning("%s: no usable rows; leaving the PC stage", page_name)
            yield from self._leave_pc_stage(f"no_rows:{page_name}")
            return

        self.pc_rows.extend(rows)
        self.pc_pages_completed.append(page_name)
        self.logger.info("%s: %d rows with a company", page_name, len(rows))
        if page_index + 1 < len(self._pc_pages()):
            yield self._pc_rank_request(page_index + 1)
        else:
            yield from self._leave_pc_stage(None)

    def _pc_rows_of(self, response):
        rows = []
        for row in response.css(self.PC_TOP_ROW_SELECTOR):
            rows.append(
                self._pc_row(
                    response,
                    row.css("div.mlt01 > a"),
                    row.css("div.mlt01 > span.medium"),
                )
            )
        for row in response.css(self.PC_LIST_ROW_SELECTOR):
            rows.append(
                self._pc_row(response, row.xpath("./a"), row.css("span.medium"))
            )
        return [row for row in rows if row is not None]

    def _pc_row(self, response, anchors, mediums):
        if not anchors or not mediums:
            return None
        identifier = fallback.article_id(
            response.urljoin(anchors[0].attrib.get("href", "").strip())
        )
        # The press name is the element's own text; the nested <em> is a date.
        company = " ".join("".join(mediums[0].xpath("./text()").getall()).split())
        if identifier is None or not company:
            return None
        image = fallback.pc_thumbnail_to_image(
            anchors[0].css("img::attr(src)").get()
        )
        return {"id": identifier, "company": company, "image": image}

    def pc_rank_errback(self, failure):
        self.request_counts["pc_rank"] += 1
        page_name = failure.request.meta["nate_page_name"]
        self.logger.warning("%s request failed: %r", page_name, failure.value)
        self._close_if_refused(failure)
        if failure.check(*self.TRANSPORT_FAILURES):
            # Carried into the article stage: one more transport failure in a
            # row there ends the run instead of starting a fresh count.
            self._count_transport_failure()
            yield from self._leave_pc_stage(f"transport_failure:{page_name}")
        else:
            if failure.check(HttpError):
                self.consecutive_transport_failures = 0
            yield from self._leave_pc_stage(f"request_failed:{page_name}")

    def _leave_pc_stage(self, reason):
        self.pc_stop_reason = reason
        plan = fallback.plan_enrichment(self.ranking, self.pc_rows, self.cache)
        self.planned_before_cap = len(plan)
        self.article_plan = plan[: self.max_article_requests]
        self.url_by_id = {}
        for row in self.ranking:
            self.url_by_id.setdefault(row["id"], row["url"])
        self.plan_built = True
        self.logger.info(
            "PC stage ended (%s): %d PC rows, %d cache entries, %d article"
            " pages planned, %d after the request cap",
            reason or "complete",
            len(self.pc_rows),
            len(self.cache),
            self.planned_before_cap,
            len(self.article_plan),
        )
        yield from self._next_article_or_stop()

    # --- article pages: only for what is still unknown, one request each. ---

    def _next_article_or_stop(self):
        if self.next_plan_index < len(self.article_plan):
            identifier = self.article_plan[self.next_plan_index]
            self.next_plan_index += 1
            yield self._request(
                self.url_by_id[identifier],
                self.parse_article,
                self.article_errback,
                {"nate_article_id": identifier},
            )
        elif self.planned_before_cap > len(self.article_plan):
            raise CloseSpider(reason=fallback.CLOSE_REQUEST_CAP)
        else:
            self.chain_complete = True

    def parse_article(self, response):
        self.request_counts["article"] += 1
        self.consecutive_transport_failures = 0
        identifier = response.meta["nate_article_id"]
        company = self._extract_text(response, self.COMPANY_SELECTOR)
        if fallback.article_id(response.url) != identifier:
            # Cannot happen while redirects are off; never attach another
            # article's fields to this one.
            self.articles[identifier] = {
                "status": "failed",
                "reason": f"answered for another URL: {response.url}",
            }
        elif company:
            self.articles[identifier] = {
                "status": "ok",
                "title": self.SQUARE_BRACKET_PREFIX.sub(
                    "", self._extract_text(response, self.TITLE_SELECTOR)
                ),
                "company": company,
                "image": self._extract_image(response),
            }
        else:
            # No parse retry: one request per article is the budget.
            self.articles[identifier] = {
                "status": "failed",
                "reason": "company missing",
            }
        yield from self._next_article_or_stop()

    def article_errback(self, failure):
        self.request_counts["article"] += 1
        identifier = failure.request.meta["nate_article_id"]
        self.articles[identifier] = {
            "status": "failed",
            "reason": repr(failure.value),
        }
        self._close_if_refused(failure)
        if failure.check(*self.TRANSPORT_FAILURES):
            self._count_transport_failure()
        elif failure.check(HttpError):
            # Nate answered, so this is not part of a transport failure streak.
            self.consecutive_transport_failures = 0
        yield from self._next_article_or_stop()

    # --- shared stop rules ---

    def _close_if_refused(self, failure):
        if failure.check(HttpError):
            status = failure.value.response.status
            if status in self.REFUSAL_STATUSES:
                raise CloseSpider(
                    reason=f"{fallback.CLOSE_REFUSED_PREFIX}{status}"
                )

    def _count_transport_failure(self):
        self.consecutive_transport_failures += 1
        if self.consecutive_transport_failures >= self.transport_failure_limit:
            raise CloseSpider(
                reason=(
                    f"{fallback.CLOSE_TRANSPORT_PREFIX}"
                    f"{self.consecutive_transport_failures}"
                )
            )

    def _record_spider_error(self, failure, response, spider):
        self.spider_errors += 1

    def closed(self, reason):
        if reason == "finished" and not self.chain_complete:
            # A callback raised or the chain stopped some other way; Scrapy
            # still calls that "finished".
            reason = fallback.CLOSE_CHAIN_BROKEN
        snapshot = {
            "schema": fallback.SNAPSHOT_SCHEMA,
            "run_id": self.run_id,
            "kst_day": self.kst_day,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": datetime.now(fallback.KST).isoformat(timespec="seconds"),
            "close_reason": reason,
            "spider_errors": self.spider_errors,
            "rank_pages_completed": self.rank_pages_completed,
            "ranking": self.ranking,
            "pc_pages_completed": self.pc_pages_completed,
            "pc_stop_reason": self.pc_stop_reason,
            "pc_rows": self.pc_rows,
            "plan_built": self.plan_built,
            "article_plan": self.article_plan,
            "planned_before_cap": self.planned_before_cap,
            "articles": self.articles,
            "requests": self.request_counts,
        }
        fallback.write_text_atomically(
            self.snapshot_path,
            json.dumps(snapshot, ensure_ascii=False, indent=1) + "\n",
        )
        self.logger.info(
            "Fallback snapshot written to %s: reason=%s, ranked=%d, pc_rows=%d,"
            " article pages requested=%d of %d planned, spider errors=%d",
            self.snapshot_path,
            reason,
            len(self.ranking),
            len(self.pc_rows),
            self.request_counts["article"],
            len(self.article_plan),
            self.spider_errors,
        )
