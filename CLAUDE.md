# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project purpose

Crawl Nate (네이트) mobile news rankings (politics + sports + entertainment, top 100 each) with Scrapy and publish the result as JSON committed to this repo, so external consumers can read it as a static asset. A scheduled local publisher is the primary collector; GitHub Actions runs a degraded fallback crawl for slots the local publisher did not publish.

## Common commands

The spiders live in [nate_news/](nate_news/) — the Scrapy project root where `scrapy.cfg` and the News-specific `requirements.txt` sit.

```bash
# From repo root — install deps
pip install -r nate_news/requirements.txt

# Then cd into the Scrapy project to run the spider
cd nate_news

# Run the full spider — writes to nate_result.json via FEEDS in settings.py
scrapy crawl nate_news_rank

# Run with verbose log to debug requests / selectors
scrapy crawl nate_news_rank -L DEBUG
```

There is no linter config or build step. Offline tests live in [tools/](tools/) and send nothing to Nate or GitHub: `python -m unittest discover -s tools -p 'test_*.py'`, run with an interpreter that has `nate_news/requirements.txt` installed (a bare `python3` with another Scrapy reports OK with the spider tests skipped). `test_local_publish.py` covers the local publisher with a local bare repository and a fake crawler. `test_fallback.py` covers the freshness gate (including its agreement with the LaunchAgent template and the `main.yml` cron line), the fallback merge rules, both validator modes and the `main.yml` diagnostics condition, and runs the fallback spider against a local stand-in for Nate; those spider tests are skipped unless the Scrapy version from `nate_news/requirements.txt` is installed. The full spider has no unit tests. Both spiders read Nate's server-rendered EUC-KR HTML directly through Scrapy; no browser or ChromeDriver is used.

## Architecture

### Sequential Scrapy design

The spider [nate_news/nate_news/spiders/nate_news_rank.py](nate_news/nate_news/spiders/nate_news_rank.py) requests six hard-coded ranking pages in this order: politics page 1/2, sports page 1/2, entertainment page 1/2. It collects links in DOM order, then requests one article at a time. Each callback schedules only the next request, and [settings.py](nate_news/nate_news/settings.py) keeps downloader concurrency at one.

The sequential chain is intentional. It preserves ranking order in the JSON feed and preserves the `DuplicatesPipeline` rule that the first occurrence of a title wins. Exact duplicate requests use `dont_filter=True` so Scrapy's duplicate filter cannot break the callback chain before the title-based pipeline sees them.

Scrapy handles download timeouts and transport/HTTP retries. A ranking-page request or selector failure closes the spider and produces incomplete metrics, while an article failure is recorded and the chain continues with the next article. Title or company selector misses receive one explicit parse retry.

The exception is a transport-failure circuit breaker: when `NATE_MAX_CONSECUTIVE_TRANSPORT_FAILURES` (default 3, must be a positive integer) articles in a row end in a transport failure after retries, the spider closes with `article_transport_failures:<N>`, so validation rejects the run instead of waiting for `CLOSESPIDER_TIMEOUT`. Only the concrete transport exceptions listed in `TRANSPORT_FAILURES` count; local outcomes such as `IgnoreRequest`, cancellation, or an aborted connect attempt neither count nor reset. Any downloaded article response, including a 404 or a 5xx that is retried, resets the streak through the `response_downloaded` signal. The check runs before the next article is scheduled, so reaching the limit on the final articles still fails the run.

The spider writes `crawl_metrics.json` on close. `validate_result.py` (mode `full`, the default) compares the final section counts and URL order with the links discovered during that same run, rejects pipeline errors, requires at least 90% coverage, and only publishes after all six ranking pages completed successfully.

### Per-article fallback chain

For each article URL collected from the ranking pages, the spider tries three image selectors in order: `#content_img > a > img` → `div.view_movie > a > img` → `#one_content_img > a > img`. Empty string if all three miss. The `thumbnews.nateimg.co.kr/view610///` prefix is stripped from the resolved image URL.

### Pipelines

Defined in [nate_news/nate_news/pipelines.py](nate_news/nate_news/pipelines.py), wired in [settings.py](nate_news/nate_news/settings.py):

1. `NateNewsPipeline` (300) — drops items with empty `title` or `newsURL`.
2. `DuplicatesPipeline` (400) — drops items whose `title` was already seen.

The spider intentionally sends exact duplicate URLs with `dont_filter=True`. Actual dedup happens in `DuplicatesPipeline` by title, and the sequential request chain makes the first ranking occurrence win deterministically.

### Fallback crawl (GitHub-hosted runners)

GitHub-hosted runners lose access to Nate some seconds into a crawl (see the gotchas), so `main.yml` does not run the full spider. It runs [nate_news_fallback.py](nate_news/nate_news/spiders/nate_news_fallback.py) and then [fallback.py](nate_news/nate_news/fallback.py), which collect in this order:

1. The six mobile ranking pages give order, URL and the list title (same normalization as the full crawl). Both lists are asked for the KST day fixed when the crawl starts, so a run that crosses midnight cannot mix two days. A failure here ends the run and nothing is published.
2. The six PC ranking pages (`news.nate.com/rank/interest`) give the company for the rows they list and a thumbnail for the top five rows of each section's first page. Rows are joined to the mobile ranking by article id, never by position, and merged field by field when an article appears more than once; an article whose PC rows disagree on the company gets nothing from the PC ranking. PC titles are truncated and are not used. On 2026-11-14 at dawn each PC page listed about 30 rows and `&page=2` repeated mostly the same articles without the thumbnail block. This stage is optional: a download error, an empty page or a page that cannot be parsed ends only this stage, and it is never re-entered.
3. The previously published `newsURL.json` supplies title, company and image for articles that are still ranked, limited to article ids dated today or yesterday (KST). An old image is not reused when the title (ignoring leading `[tags]`) or the company changed.
4. Article pages are requested only for articles that still lack a company (first) or an image, top ranks of every section first, one request per article, until the plan is done or the runner is cut off.

Per article, company and image come from the article page fetched in this run, else the PC ranking, else the previous publish. A source that did not observe a field never overrides one that did: a PC row without a thumbnail keeps the reused image, while a fetched article page without an image clears it. The title is the article page's when one was fetched, else the previously published title when it is the same up to leading `[tags]`, else the mobile list title. The mobile list drops some leading tags that the article page keeps (3 of 192 shared articles on 2026-11-14, for example `[IN REVIEW] `), so this keeps a title stable between full and fallback publishes where it can. If the same URL is listed twice (the ranking shifted between two page requests), the first listing wins, like a repeated title.

Stop rules: no download retries, no parse retries, no HTTP redirects and no meta refreshes (`METAREFRESH_ENABLED` is a separate switch from `REDIRECT_ENABLED`), so Scrapy issues every URL once, at most 6 + 6 + 300 requests. Below Scrapy, Twisted may send a GET a second time when a reused keep-alive connection is closed before any byte of the answer arrives; a silent drop ends in a timeout and is not resent. Two transport failures in a row close the spider, any answer (including an HTTP error) resets the streak, and a transport failure on a PC page counts toward it. A 403 or 429 on any page closes the spider at once. The whole crawl has a 10-minute budget. `DOWNLOAD_DELAY` is 0.5 for this spider only.

The fallback spider yields no items and writes no feed. `closed()` writes a raw snapshot to a per-run path, including how many callbacks raised. Scrapy reports `finished` whenever nothing is left to do, also when a callback raised and the request chain simply stopped, so the spider reports `fallback_chain_broken` instead unless the chain reached the end of its plan. Requests are counted when they get an outcome, so a request built while the spider was closing is not counted. `python3 -m nate_news.fallback` checks that the snapshot belongs to this run and that the ranking rows are in page order, derives items, discovery counts and article outcomes from the raw rows (the per-stage request counts are the spider's and are cross-checked by the validator), and writes `nate_result.json` and `crawl_metrics.json` (with `mode: fallback`). A snapshot with callback errors, or one that claims `complete`/`request_cap` without having made every planned request, gets an `unexpected:*` stop reason.

`validate_result.py --mode fallback` then applies the same ranking, item and coverage rules as the full mode: title and company are required and an image may be empty. It differs from full mode in two places. The per-article request accounting is replaced (there are no item pipelines, so the pipeline-error check is full-mode only): source counts must add up, callback errors must be zero, and every planned request must be accounted for. And instead of requiring `finished`, it accepts five stop reasons, each only with its matching close reason: `complete` (`finished`), `transport_breaker`, `access_refused`, `request_cap` and `time_budget` (`closespider_timeout`). The mode is chosen on the command line; metrics that declare another mode are rejected.

Limits to keep in mind:

- Articles whose company is unknown are left out, so a run with too few known companies fails the 90% section coverage rule and publishes nothing. On 2026-11-14 at dawn the PC ranking covered about 40% of the mobile ranking (95 of 239). With the previous publish from 20 minutes earlier, 48 article pages would have filled every gap; without a usable previous publish it would not validate. Daytime coverage has not been measured yet.
- A reused field is as old as the crawl that first saw it. The previous publish covers a median of about one third of the next ranking, so reuse softens the first missed slot and little after that.
- The JSON does not say whether it came from a full or a fallback crawl. The workflow summary and the `fallback-crawl` artifact record how many fields came from each source.

### Two-track data flow

```text
[m.news.nate.com] → Scrapy Spider → Pipelines → nate_news/nate_result.json
                          │                              │
                          └─ crawl_metrics.json          └─ (CI) cp → newsURL.json
```

`nate_result.json` (crawl output, inside the Scrapy project) and `newsURL.json` (repo root) are intentionally separate files — the workflow copies one to the other so external consumers can hit a stable path at the repo root. `crawl_metrics.json` is a transient validation file and is not committed. In the fallback crawl the same two files are written by `nate_news.fallback` instead of the feed exporter, in the same JSON layout.

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
| `main.yml` | cron `44 02,07,12,21 * * *` (4×/day, one hour after each local publisher slot) + `workflow_dispatch` | Freshness gate; if the due slot was not published, run the fallback crawl, validate it, commit `nate_result.json` and `newsURL.json`, and push |
| `sub.yml` | cron `55 19 * * *` (1×/day) | Pull submodule, copy `requirements.txt` and `config.json`, then amend an existing CI `News` tip or create a new `News` commit |
| `side.yml` | cron `0 22 * * *` (1×/day) + `workflow_dispatch` | Delete `sub.yml` workflow run history via `Mattraks/delete-workflow-runs` |

`main.yml` starts with a freshness gate ([tools/freshness_gate.py](tools/freshness_gate.py)). It takes the newest local publisher slot (05:44, 10:44, 15:44, 20:44 KST) that started at least 45 minutes ago, and asks the GitHub API for the author date of the newest commit that touched `newsURL.json`, starting from the branch tip after the run's initial `git pull --ff-only`. If the data was committed at or after that slot, the run ends successfully without contacting Nate. From 20 minutes before a slot until 45 minutes after it the local publisher may be crawling for that slot, so the gate also ends the run without crawling then ("deferred"); otherwise a fallback could publish first and make the complete local result fail with `remote_moved`. Scheduled runs (one hour after each slot) never fall in that window, only delayed ones do. The rule is slot-based because scheduled runs on GitHub started a median of about three hours late in this repository. The author date is used because a `sub.yml` amend keeps it and rewrites only the committer date. It approximates the collection time: a commit that restores old data by hand also looks fresh, so review the gate when publishing data manually. An API error, an unparsable date or a date more than five minutes in the future (a small clock difference between the committing machine and GitHub is tolerated) stops the run with `gate_unknown` instead of counting as fresh.

After a validated fallback crawl, the run commits `News` and pushes to `master` only if the remote branch still equals the tip it started from. Otherwise the result is dropped, because someone published in the meantime (normally the local publisher, with complete data). There is no rebase and no force. `workflow_dispatch` takes `ignore_gate`, `publish` and `max_article_requests` for testing. A run from any ref other than `refs/heads/master` never commits, and neither does a run with `ignore_gate`, since it would put a degraded result over a slot that was already published. A failed artifact upload does not stop a validated result from being published.

When the Execution step fails because a ranking page request got no answer at all (`rank_page_transport_failed:*` in the snapshot), a `Network diagnostics` step runs from the same runner: one curl to the Nate ranking page with DNS/connect/TLS/first-byte timings, one curl to `github.com` as a control, `getent hosts`, and the runner's public IP. That is the one failure a network probe explains, and the crawl has sent at most six requests by then. Any other failure (a refusal, another HTTP error, a short ranking page, a failed validation) sends nothing more to Nate. The job still fails, so Commit and Push are skipped.

`main.yml` and `sub.yml` share the `news-${{ github.ref }}` concurrency group so repository-writing runs do not overlap. The `sub.yml` job is capped at 10 minutes so a stalled sync cannot hold that group indefinitely. It pulls with `--ff-only` and only amends when the complete tip commit message is exactly `News`, which identifies a CI-generated commit under this repository's commit convention. If a human-authored commit is the tip, it creates a new `News` commit instead of rewriting that history. The push remains `force_with_lease` because the CI-tip path still amends; a concurrent remote update therefore fails safely rather than being overwritten.

`side.yml` does not join that concurrency group because it only deletes `sub.yml` workflow-run history and does not modify repository contents. Its cleanup job is independently capped at 10 minutes to prevent a stalled API action from consuming the default six-hour job allowance.

### Local publisher (launchd)

Because GitHub-hosted runners lose access to Nate mid-crawl, [tools/local_publish.py](tools/local_publish.py) is the primary collector. It does the full crawl from a local machine: crawl, run `validate_result.py`, and push one data commit. On 2026-11-13 the same code and settings collected 300/300 articles from a Korean residential connection while three GitHub runners were cut off after 9–24 responses.

It runs from a state directory outside any working repository (default `~/Library/Application Support/NewsJSON`): `code/` is a detached checkout of one pinned, reviewed commit, `publish/` is a clean clone used only for the data commit, `venv/` holds the dependencies, and `runs/`, `state.json`, `history.jsonl` keep per-run output and outcomes. [tools/local.newsjson.publish.plist.template](tools/local.newsjson.publish.plist.template) is the LaunchAgent template (05:44, 10:44, 15:44 and 20:44 local time; `main.yml` follows each slot one hour later as the fallback). [tools/freshness_gate.py](tools/freshness_gate.py) carries the same four times, and a test keeps the two in step.

```bash
python3 tools/local_publish.py init --sha <reviewed full SHA>   # clone, venv, record pin
"$STATE/venv/bin/python" "$STATE/code/tools/local_publish.py" check-auth   # dry-run push
"$STATE/venv/bin/python" "$STATE/code/tools/local_publish.py" run          # one cycle
"$STATE/venv/bin/python" "$STATE/code/tools/local_publish.py" status
python3 tools/local_publish.py repin --sha <new reviewed full SHA>
```

Rules the publisher enforces, and that changes to it must keep:

- Nothing is pulled or installed during a run. The code checkout must equal the pin with no local changes, and `pip freeze --all` must match the fingerprint recorded at `init`/`repin` (a missing fingerprint is a configuration error). The fingerprint also covers the base interpreter's version and location, because `venv/bin/python` is a link to it; upgrading that interpreter (for example a conda update) stops runs with `dependency_drift` until `repin`. The fingerprint detects a changed package set or interpreter; it does not prove the integrity of installed files, and `init`/`repin` still resolve dependencies from PyPI without hashes.
- `run` only works as the pinned script under the pinned venv (`not_pinned_invocation` otherwise), and child processes do not inherit `GIT_*`, `PYTHON*`, `PIP_*`, or `SCRAPY_*` variables. The crawl and the validator look for bytecode only in a per-run path that is never written, so a stale git-ignored `__pycache__` in the checkout is not imported.
- `run`, `init`, and `repin` share one lock. `repin` builds the new venv beside the old one and switches code, venv, and fingerprint together; an error or Ctrl-C while the new venv is being built restores the previous combination, and during the final swap Ctrl-C is held back until the swap has completed or been rolled back. A process killed outright in the middle of the switch is not covered: keep the state directory as it is and inspect `code/`, `venv*/`, and `config.json` before repairing by hand, because `run` and `repin` both refuse a checkout that does not match the pin. A failed `init` removes what it created.
- Compared with the pin, the remote tip may differ only in the two data files and the paths `sub.yml` syncs (`news`, `requirements.txt`, `nate_news/nate_news/config.json`), and the two data paths must be regular files (not symlinks). **Any other net difference between the pin and the remote tip, including a docs-only one, stops publishing with `remote_code_changed` until someone reviews it and runs `repin`.** The check compares the two trees, so a change that was later reverted is not detected.
- Everything that can fail without Nate is checked before crawling: the pin, the fingerprint, the remote tip (with a short retry, since a run right after wake can beat the network), a forced reset of the publish clone, and a dry-run push.
- Only validated output is published, as a single commit that touches nothing but `nate_news/nate_result.json` and `newsURL.json`; identical data creates no commit, and the committed blobs are compared with the validated file.
- If the wall clock ran more than two minutes ahead of the awake clock since the crawl began (the Mac slept, or the system clock was moved), the run stops with `suspended_or_clock_jump`. This is checked after the crawl and again immediately before the push, which keeps a lid-close from publishing an hours-old ranking in nearly all cases; a suspension that begins after the push has started is not covered.
- If the remote branch moved while crawling, the run aborts with `remote_moved` instead of rebuilding on the new tip, so an older crawl never replaces a newer publish. The push is never forced. A fallback publish from `main.yml` that lands during the crawl has the same effect: the complete local result is discarded and the degraded one stays until the next slot.
- One crawl per run and no automatic retry; a failed run waits for the next scheduled slot. Failures, including unexpected exceptions (`internal_error`), raise a macOS notification and are recorded in `state.json` and `history.jsonl`. A run that never starts (Mac off, logged out, job unloaded) produces no signal, so check `status` when the data looks old.
- Git authenticates through whatever credential helper the user already has configured for github.com (on the current Mac that is `gh auth git-credential`, set in `~/.gitconfig`). No token is written to the plist, the environment, or the state directory. `check-auth` is a dry-run push: it proves authentication, not that the server will accept a real push.
- `sub.yml` still amends a `News` tip and pushes with `force_with_lease`, so a commit published here can be replaced by an amended one with the same data; `published_commit` in the history may then no longer exist on the remote, while `data_sha256` stays valid. If that amend lands while a crawl is in progress, the run ends with `remote_moved` and the next slot tries again. Never pin (`repin`) to a `News` commit.
- To pause publishing (travel, another network), unload the agent with `launchctl bootout gui/$(id -u)/local.newsjson.publish`.
- The LaunchAgent uses the machine's local time, so the template's 05:44/10:44/15:44/20:44 only match the KST slots that the `main.yml` freshness gate assumes when the Mac's time zone is Asia/Seoul.

The publisher's scheduled commits use the message `News`, like `main.yml`, because both are automatically generated data commits.

## Project-specific gotchas

- **`ROBOTSTXT_OBEY = False`** in settings.py is intentional for this target site; do not flip it without checking the deploy scenario.
- **Keep the request chain sequential** — `CONCURRENT_REQUESTS`, `CONCURRENT_REQUESTS_PER_DOMAIN`, and `CONCURRENT_ITEMS` are all one so feed order and title-dedup first-wins behavior stay deterministic.
- **GitHub-hosted runners lose access to Nate mid-crawl** — since 2026-10-23, crawls at ~100 requests/min hit persistent download/connection timeouts after 11–95 s of normal responses and never recovered within a run. This happened in all 11 Azure regions the runners were assigned to, while the same URL answered normally from a Korean residential IP when checked on 2026-11-13. Who drops the traffic (Nate or an upstream device) is unconfirmed. Slowing down did not help: with `DOWNLOAD_DELAY = 2.0` three runners were still cut off after 20–57 s (9–24 responses), and 54 blocked runs at 0.5 s were cut off after a median of 36 s (62 responses), which fits a time-based cut-off better than a fixed request count, although what triggers it is unconfirmed. On 2026-11-14 one runner read all twelve mobile and PC ranking pages in 35 s at 2 s spacing. The full crawl keeps `DOWNLOAD_DELAY = 2.0` (randomized 1–3 s); the fallback spider overrides it to 0.5, the old operating value, and must not go faster or use concurrency to outrun the cut-off.
- **Do not publish without `crawl_metrics.json` validation** — both publishers require all six source pages, 1–50 links per page with at least 45 on each first page, at least 51 links per section and 200 overall, zero pipeline errors, preserved URL order, and 90% coverage against that run's discovered links. Second pages may be naturally short in dawn runs, so their dynamic discovery count remains the coverage baseline. The fallback mode keeps the page, section, order, item and coverage rules, replaces the per-article request accounting (and the pipeline-error check) with its own, and accepts its listed stop reasons; do not relax the company requirement there without the owner's decision.
- **`config.json` is ~285KB** and large diffs in it are normal (it is regenerated by the submodule). Don't review it line-by-line.
- **Do not force-install old `pyOpenSSL` / `cryptography` versions after [requirements.txt](requirements.txt)** — doing so can invalidate the dependency set that pip just resolved. In August 2026, this caused Scrapy 2.18.0 to be combined with incompatible `pyOpenSSL==24.2.1` and `cryptography==43.0.3` versions. If constraints become necessary, resolve the complete dependency set in one install operation and verify it with `python -m pip check`.
- **Commit messages**: automatically generated data commits are literally `"News"` — set inside [main.yml](.github/workflows/main.yml) and `sub.yml` for CI, and by [tools/local_publish.py](tools/local_publish.py) for the local scheduled publisher. Human-authored commits, including a manual data refresh, use the `<type>: <subject>` form (`fix:`, `chore:`, `docs:` …) per the user's global commit convention. Keep the two distinct so `git log` stays readable.
