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

The spider itself has no test suite, linter config, or build step; only the local publisher has offline tests (`python3 -m unittest discover -s tools -p 'test_local_publish.py'`, which use a local bare repository and a fake crawler and send nothing to Nate or GitHub). The spider reads Nate's server-rendered EUC-KR HTML directly through Scrapy; no browser or ChromeDriver is used.

## Architecture

### Sequential Scrapy design

The spider [nate_news/nate_news/spiders/nate_news_rank.py](nate_news/nate_news/spiders/nate_news_rank.py) requests six hard-coded ranking pages in this order: politics page 1/2, sports page 1/2, entertainment page 1/2. It collects links in DOM order, then requests one article at a time. Each callback schedules only the next request, and [settings.py](nate_news/nate_news/settings.py) keeps downloader concurrency at one.

The sequential chain is intentional. It preserves ranking order in the JSON feed and preserves the `DuplicatesPipeline` rule that the first occurrence of a title wins. Exact duplicate requests use `dont_filter=True` so Scrapy's duplicate filter cannot break the callback chain before the title-based pipeline sees them.

Scrapy handles download timeouts and transport/HTTP retries. A ranking-page request or selector failure closes the spider and produces incomplete metrics, while an article failure is recorded and the chain continues with the next article. Title or company selector misses receive one explicit parse retry.

The exception is a transport-failure circuit breaker: when `NATE_MAX_CONSECUTIVE_TRANSPORT_FAILURES` (default 3, must be a positive integer) articles in a row end in a transport failure after retries, the spider closes with `article_transport_failures:<N>`, so validation rejects the run instead of waiting for `CLOSESPIDER_TIMEOUT`. Only the concrete transport exceptions listed in `TRANSPORT_FAILURES` count; local outcomes such as `IgnoreRequest`, cancellation, or an aborted connect attempt neither count nor reset. Any downloaded article response, including a 404 or a 5xx that is retried, resets the streak through the `response_downloaded` signal. The check runs before the next article is scheduled, so reaching the limit on the final articles still fails the run.

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
| `sub.yml` | cron `55 19 * * *` (1×/day) | Pull submodule, copy `requirements.txt` and `config.json`, then amend an existing CI `News` tip or create a new `News` commit |
| `side.yml` | cron `0 22 * * *` (1×/day) + `workflow_dispatch` | Delete `sub.yml` workflow run history via `Mattraks/delete-workflow-runs` |

When the `main.yml` Execution step fails (git pull, crawl, validation, or copy), a `Network diagnostics` step runs from the same runner: one curl to the Nate ranking page with DNS/connect/TLS/first-byte timings, one curl to `github.com` as a control, `getent hosts`, the runner's public IP, and a `crawl_metrics.json` summary. Every probe tolerates failure, and the job still fails, so Commit and Push are skipped.

`main.yml` and `sub.yml` share the `news-${{ github.ref }}` concurrency group so repository-writing runs do not overlap. The `sub.yml` job is capped at 10 minutes so a stalled sync cannot hold that group indefinitely. It pulls with `--ff-only` and only amends when the complete tip commit message is exactly `News`, which identifies a CI-generated commit under this repository's commit convention. If a human-authored commit is the tip, it creates a new `News` commit instead of rewriting that history. The push remains `force_with_lease` because the CI-tip path still amends; a concurrent remote update therefore fails safely rather than being overwritten.

`side.yml` does not join that concurrency group because it only deletes `sub.yml` workflow-run history and does not modify repository contents. Its cleanup job is independently capped at 10 minutes to prevent a stalled API action from consuming the default six-hour job allowance.

### Local publisher (launchd)

Because GitHub-hosted runners lose access to Nate mid-crawl, [tools/local_publish.py](tools/local_publish.py) can do the `main.yml` job from a local machine: crawl, run `validate_result.py`, and push one data commit. On 2026-11-13 the same code and settings collected 300/300 articles from a Korean residential connection while three GitHub runners were cut off after 9–24 responses.

It runs from a state directory outside any working repository (default `~/Library/Application Support/NewsJSON`): `code/` is a detached checkout of one pinned, reviewed commit, `publish/` is a clean clone used only for the data commit, `venv/` holds the dependencies, and `runs/`, `state.json`, `history.jsonl` keep per-run output and outcomes. [tools/local.newsjson.publish.plist.template](tools/local.newsjson.publish.plist.template) is the LaunchAgent template (same KST times as `main.yml`).

```bash
python3 tools/local_publish.py init --sha <reviewed full SHA>   # clone, venv, record pin
"$STATE/venv/bin/python" "$STATE/code/tools/local_publish.py" check-auth   # dry-run push
"$STATE/venv/bin/python" "$STATE/code/tools/local_publish.py" run          # one cycle
"$STATE/venv/bin/python" "$STATE/code/tools/local_publish.py" status
python3 tools/local_publish.py repin --sha <new reviewed full SHA>
```

Rules the publisher enforces, and that changes to it must keep:

- Nothing is pulled or installed during a run. The code checkout must equal the pin with no local changes, and `pip freeze --all` must match the fingerprint recorded at `init`/`repin` (a missing fingerprint is a configuration error). The fingerprint detects a changed package set; it does not prove the integrity of installed files.
- `run`, `init`, and `repin` share one lock. `repin` builds the new venv beside the old one and switches code, venv, and fingerprint together; if a step raises an error, the previous combination is restored. A process killed in the middle of the switch is not covered, so rerun `repin` after such an interruption.
- Compared with the pin, the remote tip may differ only in the two data files and the paths `sub.yml` syncs (`news`, `requirements.txt`, `nate_news/nate_news/config.json`), and the two data paths must be regular files (not symlinks). **Any other net difference between the pin and the remote tip, including a docs-only one, stops publishing with `remote_code_changed` until someone reviews it and runs `repin`.** The check compares the two trees, so a change that was later reverted is not detected.
- Only validated output is published, as a single commit that touches exactly `nate_news/nate_result.json` and `newsURL.json`; identical data creates no commit.
- If the remote branch moved while crawling, the run aborts with `remote_moved` instead of rebuilding on the new tip, so an older crawl never replaces a newer publish. The push is never forced.
- One crawl per run and no automatic retry; a failed run waits for the next scheduled slot. Failures raise a macOS notification and are recorded in `state.json`.
- Git authenticates through the user's existing credential helper. No token is written to the plist, the environment, or the state directory. `check-auth` is a dry-run push: it proves authentication, not that the server will accept a real push.
- The LaunchAgent uses the machine's local time, so the template's 05:44/10:44/15:44/20:44 only match `main.yml` when the Mac's time zone is Asia/Seoul.

The publisher's scheduled commits use the message `News`, like `main.yml`, because it replaces that CI job.

## Project-specific gotchas

- **`ROBOTSTXT_OBEY = False`** in settings.py is intentional for this target site; do not flip it without checking the deploy scenario.
- **Keep the request chain sequential** — `CONCURRENT_REQUESTS`, `CONCURRENT_REQUESTS_PER_DOMAIN`, and `CONCURRENT_ITEMS` are all one so feed order and title-dedup first-wins behavior stay deterministic.
- **GitHub-hosted runners lose access to Nate mid-crawl** — since 2026-10-23, crawls at ~100 requests/min hit persistent download/connection timeouts after 11–95 s of normal responses and never recovered within a run. This happened in all 11 Azure regions the runners were assigned to, while the same URL answered normally from a Korean residential IP when checked on 2026-11-13. Who drops the traffic (Nate or an upstream device) is unconfirmed. `DOWNLOAD_DELAY = 2.0` (randomized 1–3 s) is an experimental mitigation, not a known threshold; do not lower it again without checking the `Network diagnostics` output across several runs.
- **Do not publish without `crawl_metrics.json` validation** — CI requires all six source pages, 1–50 links per page with at least 45 on each first page, at least 51 links per section and 200 overall, zero pipeline errors, preserved URL order, and 90% coverage against that run's discovered links. Second pages may be naturally short in dawn runs, so their dynamic discovery count remains the coverage baseline.
- **`config.json` is ~285KB** and large diffs in it are normal (it is regenerated by the submodule). Don't review it line-by-line.
- **Do not force-install old `pyOpenSSL` / `cryptography` versions after [requirements.txt](requirements.txt)** — doing so can invalidate the dependency set that pip just resolved. In August 2026, this caused Scrapy 2.18.0 to be combined with incompatible `pyOpenSSL==24.2.1` and `cryptography==43.0.3` versions. If constraints become necessary, resolve the complete dependency set in one install operation and verify it with `python -m pip check`.
- **Commit messages**: automatically generated data commits are literally `"News"` — set inside [main.yml](.github/workflows/main.yml) and `sub.yml` for CI, and by [tools/local_publish.py](tools/local_publish.py) for the local scheduled publisher. Human-authored commits, including a manual data refresh, use the `<type>: <subject>` form (`fix:`, `chore:`, `docs:` …) per the user's global commit convention. Keep the two distinct so `git log` stays readable.
