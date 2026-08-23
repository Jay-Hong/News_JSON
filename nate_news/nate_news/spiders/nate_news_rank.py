import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import scrapy
from scrapy import signals
from scrapy.exceptions import CloseSpider

from nate_news.items import NateNewsItem


class NateNewsRankSpider(scrapy.Spider):
    name = "nate_news_rank"
    allowed_domains = ["m.news.nate.com"]

    RANK_PAGES = (
        ("sisa-1", "sisa", "https://m.news.nate.com/rank/list?page=1"),
        ("sisa-2", "sisa", "https://m.news.nate.com/rank/list?page=2"),
        ("spo-1", "spo", "https://m.news.nate.com/rank/list?section=spo"),
        (
            "spo-2",
            "spo",
            "https://m.news.nate.com/rank/list?section=spo&page=2",
        ),
        ("ent-1", "ent", "https://m.news.nate.com/rank/list?section=ent"),
        (
            "ent-2",
            "ent",
            "https://m.news.nate.com/rank/list?section=ent&page=2",
        ),
    )

    LINKS_SELECTOR = "#contents > div.rank_news > ol > li > a::attr(href)"
    RANK_CONTAINER_SELECTOR = "#contents > div.rank_news > ol"
    TITLE_SELECTOR = "#artcTitle"
    COMPANY_SELECTOR = (
        "#contents > div.responsive_wrap > section.rwd_left > div > "
        "header > div.medium > div > em > b"
    )
    IMAGE_SELECTORS = (
        "#content_img > a > img",
        "div.view_movie > a > img",
        "#one_content_img > a > img",
    )

    THUMBNAIL_PREFIX = re.compile(r"thumbnews\.nateimg\.co\.kr/view610///")
    SQUARE_BRACKET_PREFIX = re.compile(
        r"\[[ㄱ-ㅣ가-힣A-Za-z0-9\t\n\r\f\v]+\]\s*"
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.article_requests = []
        self.rank_pages_completed = []
        self.discovered_by_page = {}
        self.discovered_by_section = Counter()
        self.article_failures = 0
        self.article_items_yielded = 0
        self.next_article_index = 0

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        crawler.signals.connect(
            spider._record_item_error,
            signal=signals.item_error,
        )
        return spider

    async def start(self):
        yield self._rank_request(0)

    def _rank_request(self, page_index, *, parse_retry=0):
        page_name, _, page_url = self.RANK_PAGES[page_index]
        return scrapy.Request(
            page_url,
            callback=self.parse_rank_page,
            errback=self.rank_errback,
            dont_filter=True,
            meta={
                "nate_rank_page_index": page_index,
                "nate_rank_page_name": page_name,
                "nate_parse_retry": parse_retry,
            },
        )

    def parse_rank_page(self, response):
        page_index = response.meta["nate_rank_page_index"]
        page_name, section, _ = self.RANK_PAGES[page_index]
        links = response.css(self.LINKS_SELECTOR).getall()
        article_urls = []
        invalid_links = []
        for link in links:
            article_url = response.urljoin(link.strip())
            parsed_url = urlparse(article_url)
            article_section = parse_qs(parsed_url.query).get("sect", [""])[0]
            if (
                parsed_url.hostname == "m.news.nate.com"
                and parsed_url.path.startswith("/view/")
                and article_section == section
            ):
                article_urls.append(article_url)
            else:
                invalid_links.append(article_url)
                self.logger.warning(
                    "%s ignored unexpected ranking link: %s", page_name, article_url
                )

        if (
            not response.css(self.RANK_CONTAINER_SELECTOR)
            or not article_urls
            or invalid_links
        ):
            parse_retry = response.meta["nate_parse_retry"]
            if parse_retry < 1:
                self.crawler.stats.inc_value("nate/rank_parse_retries")
                self.logger.warning(
                    "%s raw HTML failed ranking-page validation; retrying once",
                    page_name,
                )
                yield self._rank_request(page_index, parse_retry=parse_retry + 1)
                return

            self.crawler.stats.inc_value("nate/rank_page_failures")
            raise CloseSpider(reason=f"rank_page_invalid:{page_name}")

        self.rank_pages_completed.append(page_name)
        self.discovered_by_page[page_name] = len(article_urls)
        self.discovered_by_section[section] += len(article_urls)

        for page_position, article_url in enumerate(article_urls, start=1):
            self.article_requests.append(
                {
                    "url": article_url,
                    "section": section,
                    "page": page_name,
                    "page_position": page_position,
                }
            )

        self.logger.info("%s: discovered %d article links", page_name, len(article_urls))

        next_page_index = page_index + 1
        if next_page_index < len(self.RANK_PAGES):
            yield self._rank_request(next_page_index)
            return

        self.crawler.stats.set_value(
            "nate/articles_discovered", len(self.article_requests)
        )
        next_request = self._next_article_request()
        if next_request is None:
            raise CloseSpider(reason="no_articles_discovered")
        yield next_request

    def rank_errback(self, failure):
        page_name = failure.request.meta["nate_rank_page_name"]
        self.crawler.stats.inc_value("nate/rank_page_failures")
        self.logger.error(
            "%s request failed after retries: %r", page_name, failure.value
        )
        raise CloseSpider(reason=f"rank_page_request_failed:{page_name}")

    def _next_article_request(self):
        if self.next_article_index >= len(self.article_requests):
            return None

        article = self.article_requests[self.next_article_index]
        article_index = self.next_article_index + 1
        self.next_article_index += 1
        return self._article_request(article, article_index)

    def _article_request(self, article, article_index, *, parse_retry=0):
        return scrapy.Request(
            article["url"],
            callback=self.parse_article,
            errback=self.article_errback,
            dont_filter=True,
            meta={
                "nate_article": article,
                "nate_article_index": article_index,
                "nate_parse_retry": parse_retry,
            },
        )

    def parse_article(self, response):
        article = response.meta["nate_article"]
        article_index = response.meta["nate_article_index"]
        title = self.SQUARE_BRACKET_PREFIX.sub(
            "", self._extract_text(response, self.TITLE_SELECTOR)
        )
        company = self._extract_text(response, self.COMPANY_SELECTOR)

        if not title or not company:
            parse_retry = response.meta["nate_parse_retry"]
            if parse_retry < 1:
                self.crawler.stats.inc_value("nate/article_parse_retries")
                self.logger.warning(
                    "Article %d is missing title/company; retrying once: %s",
                    article_index,
                    article["url"],
                )
                yield self._article_request(
                    article,
                    article_index,
                    parse_retry=parse_retry + 1,
                )
                return

            self._record_article_failure(
                article_index,
                article["url"],
                "title/company missing after parse retry",
            )
        else:
            image = self._extract_image(response)
            self.article_items_yielded += 1
            self.crawler.stats.inc_value("nate/article_items_yielded")
            self.logger.info("%d. %s | %s", article_index, title, company)
            yield NateNewsItem(
                title=title,
                company=company,
                image=image,
                newsURL=article["url"],
            )

        next_request = self._next_article_request()
        if next_request is not None:
            yield next_request

    def article_errback(self, failure):
        article = failure.request.meta["nate_article"]
        article_index = failure.request.meta["nate_article_index"]
        self._record_article_failure(
            article_index,
            article["url"],
            repr(failure.value),
        )

        next_request = self._next_article_request()
        if next_request is not None:
            yield next_request

    @staticmethod
    def _extract_text(response, selector):
        nodes = response.css(selector)
        if not nodes:
            return ""
        value = nodes[0].xpath("string(.)").get(default="")
        return " ".join(value.split())

    def _extract_image(self, response):
        for selector in self.IMAGE_SELECTORS:
            source = response.css(f"{selector}::attr(src)").get()
            if source:
                absolute_url = response.urljoin(source)
                return self.THUMBNAIL_PREFIX.sub("", absolute_url)
        return ""

    def _record_article_failure(self, article_index, article_url, reason):
        self.article_failures += 1
        self.crawler.stats.inc_value("nate/article_failures")
        self.logger.error(
            "Article %d failed: %s (%s)", article_index, article_url, reason
        )

    def _record_item_error(self, item, response, failure):
        self.crawler.stats.inc_value("item_error_count")

    def closed(self, reason):
        stats = self.crawler.stats
        metrics = {
            "close_reason": reason,
            "rank_pages_completed": self.rank_pages_completed,
            "discovered_by_page": self.discovered_by_page,
            "discovered_by_section": {
                section: self.discovered_by_section[section]
                for section in ("sisa", "spo", "ent")
            },
            "articles_discovered": len(self.article_requests),
            "discovered_urls": [
                article["url"] for article in self.article_requests
            ],
            "article_failures": self.article_failures,
            "article_items_yielded": self.article_items_yielded,
            "item_scraped_count": stats.get_value("item_scraped_count", 0),
            "item_dropped_count": stats.get_value("item_dropped_count", 0),
            "item_error_count": stats.get_value("item_error_count", 0),
            "rank_parse_retries": stats.get_value(
                "nate/rank_parse_retries", 0
            ),
            "article_parse_retries": stats.get_value(
                "nate/article_parse_retries", 0
            ),
        }
        metrics_path = Path(
            self.crawler.settings.get(
                "NATE_CRAWL_METRICS_PATH", "crawl_metrics.json"
            )
        )
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        metrics_path.write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.logger.info(
            "Crawl metrics written to %s: discovered=%d, yielded=%d, "
            "scraped=%d, dropped=%d, item_errors=%d, article_failures=%d",
            metrics_path,
            metrics["articles_discovered"],
            metrics["article_items_yielded"],
            metrics["item_scraped_count"],
            metrics["item_dropped_count"],
            metrics["item_error_count"],
            metrics["article_failures"],
        )
