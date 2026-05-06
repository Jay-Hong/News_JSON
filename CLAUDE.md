# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project purpose

Crawl Nate (네이트) mobile news rankings (politics + sports + entertainment, top 100 each) with Scrapy + Selenium and publish the result as JSON. GitHub Actions runs the crawler on a cron schedule and commits the updated JSON back to the repo so external consumers can read it as a static asset.

## Common commands

The single spider lives in [nate_news/](nate_news/) — the Scrapy project root where `scrapy.cfg` sits. `requirements.txt` is at the **repo root**, not inside the Scrapy project, so install from there.

```bash
# From repo root — install deps
pip install -r requirements.txt

# Then cd into the Scrapy project to run the spider
cd nate_news

# Run the only spider — writes to nate_result.json via FEED_URI in settings.py
scrapy crawl nate_news_rank

# Run with verbose log to debug Selenium / selectors
scrapy crawl nate_news_rank -L DEBUG
```

There is no test suite, no linter config, and no build step. Selenium drives a real headless Chrome — `webdriver_manager` downloads the matching driver on first run, so the first invocation needs network access.

## Architecture

### Hybrid Scrapy + Selenium design

The spider [nate_news/nate_news/spiders/nate_news_rank.py](nate_news/nate_news/spiders/nate_news_rank.py) is unusual. Scrapy still performs the **initial fetch** of `start_urls` through its own downloader (this is the SSL/TLS path that surfaced the recent pyOpenSSL/Twisted compatibility bug), but inside `parse(self, response)` the `response` body is discarded — only `response.url` is used as a seed. All real page navigation is done by a Selenium `webdriver.Chrome` instantiated in `__init__`, hitting six hard-coded ranking pages.

Net effect: the data the spider yields comes from Selenium, not from Scrapy's response. Scrapy is kept for the Item / Pipeline / FeedExporter machinery and for that one initial request.

Implications:

- Editing `start_urls` only changes which URL receives that single Scrapy fetch (whose body is unused). To change crawl targets, edit the `self.driver.get(...)` calls inside `parse()`.
- `DOWNLOAD_DELAY` and downloader middlewares affect only that single initial request — they do not throttle or modify the Selenium-driven traffic.

### Per-article fallback chain

For each article URL collected from the ranking pages, the spider tries three image selectors in order: `#content_img > a > img` → `div.view_movie > a > img` → `#one_content_img > a > img`. Empty string if all three miss. The `thumbnews.nateimg.co.kr/view610///` prefix is stripped from the resolved image URL.

### Pipelines

Defined in [nate_news/nate_news/pipelines.py](nate_news/nate_news/pipelines.py), wired in [settings.py](nate_news/nate_news/settings.py):

1. `NateNewsPipeline` (300) — drops items with empty `title` or `newsURL`.
2. `DuplicatesPipeline` (400) — drops items whose `title` was already seen.

Note the dedup block at [nate_news_rank.py:106-107](nate_news/nate_news/spiders/nate_news_rank.py#L106-L107) (`newsURL_set = newsURL_list; newsURL_list = newsURL_set`) is a no-op — it does **not** convert to a `set`. Actual dedup happens in `DuplicatesPipeline` by title. Don't "fix" the spider-side block without checking the pipeline still does the work you expect.

### Two-track data flow

```text
[m.news.nate.com] → Selenium → Spider → Pipelines → nate_news/nate_result.json
                                                             │
                                                             └─ (CI) cp → newsURL.json (repo root)
```

`nate_result.json` (CI output, inside the Scrapy project) and `newsURL.json` (repo root) are intentionally separate files — the workflow copies one to the other so external consumers can hit a stable path at the repo root.

### Configuration sourced from a submodule

[news/](news/) is a git submodule pointing to `Jay-Hong/Recru_It_JSON`. It supplies two files at CI time:

- `news/requirements.txt` → copied over the repo-root `requirements.txt`
- `news/config.json` → copied to `nate_news/nate_news/config.json`

This means **editing `requirements.txt` or `nate_news/nate_news/config.json` directly will be overwritten on the next `sub.yml` run**. To make those changes stick, update them in the submodule repo first.

### Three-workflow CI split

Workflows in [.github/workflows/](.github/workflows/) are deliberately separated:

| File | Triggers | Role |
| --- | --- | --- |
| `main.yml` | cron `44 01,06,11,20 * * *` (4×/day) + `workflow_dispatch` | Run the spider, regenerate `nate_result.json` and `newsURL.json`, commit & push |
| `sub.yml` | cron `55 19 * * *` (1×/day) | Pull submodule, copy `requirements.txt` and `config.json` into place, **`git commit --amend` + `force_with_lease` push** |
| `side.yml` | cron `0 22 * * *` (1×/day) + `workflow_dispatch` | Delete `sub.yml` workflow run history via `Mattraks/delete-workflow-runs` |

`sub.yml` amends rather than creating a new commit (this is why `side.yml` exists — to keep the run history clean since the amend leaves orphan runs). Do not change `sub.yml` to a normal commit without also updating `side.yml`.

## Project-specific gotchas

- **`ROBOTSTXT_OBEY = False`** in settings.py is intentional for this target site; do not flip it without checking the deploy scenario.
- **`config.json` is ~285KB** and large diffs in it are normal (it is regenerated by the submodule). Don't review it line-by-line.
- **`pyOpenSSL==24.2.1` / `cryptography==43.0.3` pins in [main.yml](.github/workflows/main.yml) are load-bearing** — the runner ships Twisted 24.3.0 (apt) which still calls `X509.get_extension()`, an API removed in pyOpenSSL 26.x. Don't unpin without simultaneously upgrading Twisted in the submodule's `requirements.txt`.
- **Commit messages**: CI-generated commits are literally `"News"` (set inside [main.yml](.github/workflows/main.yml)). Human-authored commits use the `<type>: <subject>` form (`fix:`, `chore:`, `docs:` …) per the user's global commit convention. Keep the two distinct so `git log` stays readable.
