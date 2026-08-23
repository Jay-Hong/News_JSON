# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project purpose

Crawl Nate (네이트) mobile news rankings (politics + sports + entertainment, top 100 each) with Scrapy and publish the result as JSON. GitHub Actions runs the crawler on a cron schedule and commits the updated JSON back to the repo so external consumers can read it as a static asset.

## Common commands

The single spider lives in [nate_news/](nate_news/) — the Scrapy project root where `scrapy.cfg` and the News-specific `requirements.txt` sit.

```bash
# From repo root — install deps
pip install -r nate_news/requirements.txt

# Then cd into the Scrapy project to run the spider
cd nate_news

# Run the only spider — writes to nate_result.json via FEEDS in settings.py
scrapy crawl nate_news_rank

# Run with verbose log to debug requests / selectors
scrapy crawl nate_news_rank -L DEBUG
```

There is no test suite, no linter config, and no build step. The spider reads Nate's server-rendered EUC-KR HTML directly through Scrapy; no browser or ChromeDriver is used.

## Architecture

### Sequential Scrapy design

The spider [nate_news/nate_news/spiders/nate_news_rank.py](nate_news/nate_news/spiders/nate_news_rank.py) requests six hard-coded ranking pages in this order: politics page 1/2, sports page 1/2, entertainment page 1/2. It collects links in DOM order, then requests one article at a time. Each callback schedules only the next request, and [settings.py](nate_news/nate_news/settings.py) keeps downloader concurrency at one.

The sequential chain is intentional. It preserves ranking order in the JSON feed and preserves the `DuplicatesPipeline` rule that the first occurrence of a title wins. Exact duplicate requests use `dont_filter=True` so Scrapy's duplicate filter cannot break the callback chain before the title-based pipeline sees them.

Scrapy handles download timeouts and transport/HTTP retries. A ranking-page request or selector failure closes the spider and produces incomplete metrics, while an article failure is recorded and the chain continues with the next article. Title or company selector misses receive one explicit parse retry.

The spider writes `crawl_metrics.json` on close. CI compares the final section counts and URL order with the links discovered during that same run, rejects pipeline errors, requires at least 90% coverage, and only publishes after all six ranking pages completed successfully.

### Per-article fallback chain

For each article URL collected from the ranking pages, the spider tries three image selectors in order: `#content_img > a > img` → `div.view_movie > a > img` → `#one_content_img > a > img`. Empty string if all three miss. The `thumbnews.nateimg.co.kr/view610///` prefix is stripped from the resolved image URL.

### Pipelines

Defined in [nate_news/nate_news/pipelines.py](nate_news/nate_news/pipelines.py), wired in [settings.py](nate_news/nate_news/settings.py):

1. `NateNewsPipeline` (300) — drops items with empty `title` or `newsURL`.
2. `DuplicatesPipeline` (400) — drops items whose `title` was already seen.

The spider intentionally sends exact duplicate URLs with `dont_filter=True`. Actual dedup happens in `DuplicatesPipeline` by title, and the sequential request chain makes the first ranking occurrence win deterministically.

### Two-track data flow

```text
[m.news.nate.com] → Scrapy Spider → Pipelines → nate_news/nate_result.json
                          │                              │
                          └─ crawl_metrics.json          └─ (CI) cp → newsURL.json
```

`nate_result.json` (CI output, inside the Scrapy project) and `newsURL.json` (repo root) are intentionally separate files — the workflow copies one to the other so external consumers can hit a stable path at the repo root. `crawl_metrics.json` is a transient CI validation file and is not committed.

### Configuration sourced from a submodule

[news/](news/) is a git submodule pointing to `Jay-Hong/Recru_It_JSON`. It supplies two files at CI time:

- `news/requirements.txt` → copied over the repo-root `requirements.txt`
- `news/config.json` → copied to `nate_news/nate_news/config.json`

This means **editing `requirements.txt` or `nate_news/nate_news/config.json` directly will be overwritten on the next `sub.yml` run**. To make those changes stick, update them in the submodule repo first.

The repo-root shared requirements currently still include `selenium` and `webdriver_manager` for the submodule repository, but `main.yml` deliberately installs [nate_news/requirements.txt](nate_news/requirements.txt) instead. News_JSON therefore no longer installs, imports, or uses the browser packages. Keep these two dependency files distinct unless the cross-repository sync design is changed.

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
- **Keep the request chain sequential** — `CONCURRENT_REQUESTS`, `CONCURRENT_REQUESTS_PER_DOMAIN`, and `CONCURRENT_ITEMS` are all one so feed order and title-dedup first-wins behavior stay deterministic.
- **Do not publish without `crawl_metrics.json` validation** — CI requires all six source pages, 1–50 links per page with at least 45 on each first page, at least 51 links per section and 200 overall, zero pipeline errors, preserved URL order, and 90% coverage against that run's discovered links. Second pages may be naturally short in dawn runs, so their dynamic discovery count remains the coverage baseline.
- **`config.json` is ~285KB** and large diffs in it are normal (it is regenerated by the submodule). Don't review it line-by-line.
- **Do not force-install old `pyOpenSSL` / `cryptography` versions after [requirements.txt](requirements.txt)** — doing so can invalidate the dependency set that pip just resolved. In August 2026, this caused Scrapy 2.18.0 to be combined with incompatible `pyOpenSSL==24.2.1` and `cryptography==43.0.3` versions. If constraints become necessary, resolve the complete dependency set in one install operation and verify it with `python -m pip check`.
- **Commit messages**: CI-generated commits are literally `"News"` (set inside [main.yml](.github/workflows/main.yml)). Human-authored commits use the `<type>: <subject>` form (`fix:`, `chore:`, `docs:` …) per the user's global commit convention. Keep the two distinct so `git log` stays readable.
