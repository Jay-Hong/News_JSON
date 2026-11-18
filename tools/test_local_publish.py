"""Offline tests for tools/local_publish.py.

Everything runs against a local bare repository and a fake crawler, so no
request is sent to Nate or GitHub. Run with:

    python3 -m unittest discover -s tools -p 'test_local_publish.py' -v
"""

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import local_publish  # noqa: E402  (needs sys.path above)

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_VALIDATOR = REPO_ROOT / "nate_news" / "validate_result.py"
REAL_ASSEMBLER = REPO_ROOT / "nate_news" / "nate_news" / "fallback.py"
REAL_REUSE = REPO_ROOT / "nate_news" / "nate_news" / "reuse.py"
DAY = "20200102"

# Stands in for `scrapy crawl nate_news_fallback`: writes a raw snapshot that
# the real assembler (nate_news.fallback) and the real validator then process.
# Article ids are interleaved by rank across the sections, like the real plan.
FAKE_CRAWLER = r'''
import hashlib, json, os, sys
mode, seed, run_id, snapshot_path, previous_path, kst_day = sys.argv[1:7]
DAY = "20200102"
marker = os.environ.get("FAKE_CRAWLER_MARKER")
if marker:
    open(marker, "a").write("ran\n")
args_log = os.environ.get("FAKE_CRAWLER_ARGS")
if args_log:
    with open(args_log, "a") as handle:
        handle.write(json.dumps({
            "run_id": run_id, "snapshot": snapshot_path, "previous": previous_path,
            "previous_sha256": hashlib.sha256(open(previous_path, "rb").read()).hexdigest(),
            "kst_day": kst_day,
        }) + "\n")
# Like the real spider's from_crawler: a previous file that is not a JSON list
# is a configuration error before any request.
previous = json.loads(open(previous_path, encoding="utf-8").read())
if not isinstance(previous, list):
    sys.exit(1)
if mode == "exit":
    sys.exit(2)
if mode == "nosnapshot":
    sys.exit(0)
sections = ("sisa", "spo", "ent")
ranking = []
for index, sect in enumerate(sections):
    for position in range(1, 101):
        identifier = f"{DAY}n{index * 1000 + position:05d}"
        ranking.append({
            "id": identifier,
            "url": f"https://m.news.nate.com/view/{identifier}?sect={sect}&list=rank&cate=interest",
            "section": sect,
            "page": f"{sect}-{1 if position <= 50 else 2}",
            "page_position": position if position <= 50 else position - 50,
            "title": f"{seed}-{sect}-{position}",
        })
plan = [row["id"] for row in sorted(ranking, key=lambda row: (
    int(row["id"][-3:]), sections.index(row["section"])))]
if mode == "half":
    # Like the real plan: articles whose company the previous file (with any
    # reused local results) does not provide come first.
    known = {item["newsURL"].split("/view/")[1].split("?")[0] for item in previous
             if isinstance(item, dict) and item.get("company") and "/view/" in item.get("newsURL", "")}
    plan = [i for i in plan if i not in known] + [i for i in plan if i in known]
reached = {"ok": 300, "complete": 300, "blocked": 30, "cutoff": 285, "half": 150,
           "budget": 285, "reuse": 10, "rank_failed": 0, "wrong_run": 300}[mode]
close_reason = {
    "ok": "finished", "complete": "finished", "wrong_run": "finished",
    "blocked": "fallback_transport_failures:2", "cutoff": "fallback_transport_failures:2",
    "reuse": "fallback_transport_failures:2", "half": "fallback_transport_failures:2",
    "budget": "closespider_timeout", "rank_failed": "rank_page_transport_failed:sisa-1",
}[mode]
articles = {}
for count, identifier in enumerate(plan[:reached]):
    articles[identifier] = {
        "status": "ok", "title": f"{seed}-article-{identifier}", "company": "c",
        "image": f"https://img/{seed}/{identifier}.jpg" if count % 2 == 0 else "",
    }
if close_reason.startswith("fallback_transport_failures") and reached < len(plan):
    for identifier in plan[reached:reached + 2]:
        articles[identifier] = {"status": "failed", "reason": "TimeoutError()"}
rank_failed = mode == "rank_failed"
snapshot = {
    "schema": 2,
    "run_id": "another-run" if mode == "wrong_run" else run_id,
    "kst_day": DAY,
    "close_reason": close_reason,
    "spider_errors": 0,
    "rank_pages_completed": [] if rank_failed else
        ["sisa-1", "sisa-2", "spo-1", "spo-2", "ent-1", "ent-2"],
    "ranking": [] if rank_failed else ranking,
    # The PC stage ended on its first page, as when the PC ranking is empty.
    "pc_pages_completed": [],
    "pc_stop_reason": None if rank_failed else "no_rows:pc-sisa-1",
    "pc_rows": [],
    "plan_built": not rank_failed,
    "article_plan": [] if rank_failed else plan,
    "planned_before_cap": 0 if rank_failed else len(plan),
    "articles": articles,
    "requests": {"mobile_rank": 1 if rank_failed else 6, "pc_rank": 0 if rank_failed else 1,
                 "article": len(articles)},
}
json.dump(snapshot, open(snapshot_path, "w"))
'''


def previous_publish(company="old", image=""):
    """A published file covering the fake ranking, as a JSON text."""
    items = []
    for index, sect in enumerate(("sisa", "spo", "ent")):
        for position in range(1, 101):
            identifier = f"{DAY}n{index * 1000 + position:05d}"
            items.append({
                "title": f"old-{sect}-{position}", "company": company, "image": image,
                "newsURL": f"https://m.news.nate.com/view/{identifier}?sect={sect}&list=rank&cate=interest",
            })
    return json.dumps(items, ensure_ascii=False, indent=4) + "\n"


TEST_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    ).stdout.strip()


class FakeCrawlPublisher(local_publish.Publisher):
    """Publisher with the Nate crawl replaced by a local fake.

    Production code has no configuration switch for this; tests subclass.
    """

    crawler_script = None
    mode = "ok"
    seed = "a"
    real_dependency_check = False

    def crawl_command(self, run_id, snapshot, previous, kst_day):
        return [sys.executable, str(self.crawler_script), self.mode, self.seed,
                run_id, str(snapshot), str(previous), kst_day]

    def check_dependencies(self):
        if self.real_dependency_check:
            super().check_dependencies()

    def child_env(self):
        # Production strips GIT_* from the environment; the tests still need a
        # commit identity and isolation from the developer's own git config.
        environment = super().child_env()
        environment.update(TEST_GIT_ENV)
        return environment


class LocalPublishTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="news-publish-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old_env = dict(os.environ)
        self.addCleanup(self._restore_env)
        os.environ.update(TEST_GIT_ENV)

        self.origin = self.tmp / "origin.git"
        git(self.tmp, "init", "--quiet", "--bare", "--initial-branch=master", self.origin)
        self.seed = self.tmp / "seed"
        git(self.tmp, "clone", "--quiet", self.origin, self.seed)
        (self.seed / "nate_news" / "nate_news" / "spiders").mkdir(parents=True)
        shutil.copyfile(REAL_VALIDATOR, self.seed / "nate_news" / "validate_result.py")
        shutil.copyfile(REAL_ASSEMBLER, self.seed / "nate_news" / "nate_news" / "fallback.py")
        shutil.copyfile(REAL_REUSE, self.seed / "nate_news" / "nate_news" / "reuse.py")
        (self.seed / "nate_news" / "nate_news" / "__init__.py").write_text("")
        (self.seed / "nate_news" / "nate_news" / "spiders" / "spider.py").write_text("# v1\n")
        (self.seed / "nate_news" / "nate_news" / "config.json").write_text("{}\n")
        (self.seed / "nate_news" / "requirements.txt").write_text("")
        (self.seed / "nate_news" / "nate_result.json").write_text("[]\n")
        (self.seed / "newsURL.json").write_text("[]\n")
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "--quiet", "-m", "chore: seed")
        git(self.seed, "push", "--quiet", "origin", "HEAD:refs/heads/master")
        self.pin = git(self.seed, "rev-parse", "HEAD")

        self.state = self.tmp / "state"
        self.state.mkdir()
        (self.state / "runs").mkdir()
        git(self.tmp, "clone", "--quiet", self.origin, self.state / "code")
        git(self.state / "code", "checkout", "--quiet", "--detach", self.pin)
        git(self.tmp, "clone", "--quiet", self.origin, self.state / "publish")
        (self.state / "venv" / "bin").mkdir(parents=True)
        (self.state / "venv" / "bin" / "python").symlink_to(sys.executable)
        self.crawler = self.tmp / "fake_crawler.py"
        self.crawler.write_text(FAKE_CRAWLER)
        self.marker = self.tmp / "crawler_ran"
        os.environ["FAKE_CRAWLER_MARKER"] = str(self.marker)
        self.crawler_args = self.tmp / "crawler_args.jsonl"
        os.environ["FAKE_CRAWLER_ARGS"] = str(self.crawler_args)
        self.mode, self.seed_name = "ok", "a"
        self.write_config()

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._old_env)

    def write_config(self, **extra):
        config = {
            "pin_sha": self.pin, "repo_url": str(self.origin), "branch": "master",
            "freeze_sha256": "0" * 64, "notify": False,
        }
        config.update(extra)
        (self.state / "config.json").write_text(json.dumps(config))

    def make_publisher(self, **attributes):
        publisher = FakeCrawlPublisher(self.state)
        publisher.crawler_script = self.crawler
        publisher.mode, publisher.seed = self.mode, self.seed_name
        for name, value in attributes.items():
            setattr(publisher, name, value)
        return publisher

    def run_publisher(self, publisher=None):
        publisher = publisher or self.make_publisher()
        code = publisher.run()
        state = json.loads((self.state / "state.json").read_text())
        return code, state["last_run"]

    def origin_tip(self):
        return git(self.origin, "rev-parse", "refs/heads/master")

    def push_from_seed(self, relative, text, message):
        git(self.seed, "pull", "--quiet", "origin", "master")
        path = self.seed / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "--quiet", "-m", message)
        git(self.seed, "push", "--quiet", "origin", "HEAD:refs/heads/master")
        return git(self.seed, "rev-parse", "HEAD")

    # -- scenarios ---------------------------------------------------------

    def test_publishes_validated_data_as_one_fast_forward_commit(self):
        code, run = self.run_publisher()
        self.assertEqual((code, run["outcome"]), (0, "published"))
        tip = self.origin_tip()
        self.assertEqual(tip, run["published_commit"])
        self.assertEqual(git(self.origin, "rev-parse", f"{tip}^"), self.pin)
        self.assertEqual(git(self.origin, "log", "-1", "--format=%B", tip), "News")
        changed = git(self.origin, "diff", "--name-only", self.pin, tip).split()
        self.assertEqual(sorted(changed), sorted(local_publish.DATA_FILES))
        blobs = {git(self.origin, "rev-parse", f"{tip}:{p}") for p in local_publish.DATA_FILES}
        self.assertEqual(len(blobs), 1)
        published = json.loads(git(self.origin, "show", f"{tip}:newsURL.json"))
        self.assertEqual(len(published), 300)
        self.assertEqual(run["data_sha256"],
                         local_publish.sha256_file(self.state / "runs" / run["run_id"] / "nate_result.json"))
        self.assertEqual(run["crawl"], {
            "mode": "fallback", "stop_reason": "complete", "items": 300, "images": 150,
            "article_requests": 300, "article_pages_not_reached": 0,
        })
        # The spider got a byte copy of what the base tip publishes.
        pinned_publish = git(self.origin, "show", f"{self.pin}:newsURL.json") + "\n"
        expected = hashlib.sha256(pinned_publish.encode()).hexdigest()
        self.assertEqual((run["previous_sha256"], run["previous_usable"]), (expected, True))
        self.assertEqual(self.crawler_calls()[-1]["previous_sha256"], expected)
        self.assertEqual(self.crawler_calls()[-1]["run_id"], run["run_id"])

    def crawler_calls(self):
        return [json.loads(line) for line in self.crawler_args.read_text().splitlines()]

    def test_crawl_runs_the_incremental_spider_with_the_local_settings(self):
        publisher = local_publish.Publisher(self.state)
        snapshot, previous = self.tmp / "snap.json", self.tmp / "prev.json"
        crawl = local_publish.Publisher.crawl_command(publisher, "run-7", snapshot, previous,
                                                      "20261119")
        self.assertEqual(crawl[:5], [str(self.state / "venv" / "bin" / "python"), "-m", "scrapy",
                                     "crawl", "nate_news_fallback"])
        settings = {crawl[i + 1] for i, arg in enumerate(crawl) if arg == "-s"}
        self.assertEqual(settings, {
            "NATE_FALLBACK_RUN_ID=run-7", f"NATE_FALLBACK_SNAPSHOT_PATH={snapshot}",
            f"NATE_FALLBACK_PREVIOUS_PATH={previous}", "NATE_FALLBACK_KST_DAY=20261119",
            "DOWNLOAD_DELAY=2.0", "CLOSESPIDER_TIMEOUT=1200", "TELNETCONSOLE_ENABLED=False",
        })
        reuse = publisher.reuse_command(previous, "20261119", [Path("r2"), Path("r1")],
                                        Path("m.json"), Path("rep.json"))
        self.assertEqual(reuse[:3], [str(self.state / "venv" / "bin" / "python"), "-m",
                                     "nate_news.reuse"])
        for flag, value in (("--previous", str(previous)), ("--kst-day", "20261119"),
                            ("--out", "m.json"), ("--report", "rep.json")):
            self.assertEqual(reuse[reuse.index(flag) + 1], value)
        self.assertEqual([reuse[i + 1] for i, arg in enumerate(reuse) if arg == "--run"],
                         ["r2", "r1"])
        assemble = publisher.assemble_command("run-7", snapshot, previous, "out.json", "m.json")
        self.assertEqual(assemble[1:3], ["-m", "nate_news.fallback"])
        for flag, value in (("--snapshot", str(snapshot)), ("--previous", str(previous)),
                            ("--run-id", "run-7")):
            self.assertEqual(assemble[assemble.index(flag) + 1], value)
        self.assertGreater(local_publish.CRAWL_TIMEOUT_SECONDS, local_publish.CRAWL_BUDGET_SECONDS)

    def test_cut_off_crawl_that_validates_is_published(self):
        # 285 articles reached; the 15 without a company are left out. The
        # breaker run also records the two failed requests.
        for mode, stop_reason, attempted, not_reached in (
            ("cutoff", "transport_breaker", 287, 13),
            ("budget", "time_budget", 285, 15),
        ):
            with self.subTest(mode):
                self.mode, self.seed_name = mode, mode
                code, run = self.run_publisher()
                self.assertEqual((code, run["outcome"]), (0, "published"))
                self.assertEqual(run["crawl"], {
                    "mode": "fallback", "stop_reason": stop_reason, "items": 285, "images": 143,
                    "article_requests": attempted, "article_pages_not_reached": not_reached,
                })
                self.assertEqual(self.origin_tip(), run["published_commit"])

    def test_previous_publish_with_few_images_is_reused(self):
        text = previous_publish(company="old", image="")
        self.push_from_seed("newsURL.json", text, "News")
        self.mode = "reuse"
        code, run = self.run_publisher()
        self.assertEqual((code, run["outcome"]), (0, "published"))
        self.assertEqual(self.crawler_calls()[-1]["previous_sha256"],
                         hashlib.sha256(text.encode()).hexdigest())
        # Ten article pages were read; every other article keeps the company
        # of the previous publish and the title of the ranking list.
        self.assertEqual((run["crawl"]["items"], run["crawl"]["images"]), (300, 5))
        published = json.loads(git(self.origin, "show", f"{self.origin_tip()}:newsURL.json"))
        last = published[-1]
        self.assertEqual((last["title"], last["company"], last["image"]), ("a-ent-100", "old", ""))

    def test_unusable_previous_publish_is_replaced_by_an_empty_list(self):
        for index, broken in enumerate((b"not json\n", b"{}\n", b"null\n", b"\xff\xfe[]\n")):
            with self.subTest(broken=broken):
                git(self.seed, "pull", "--quiet", "origin", "master")
                (self.seed / "newsURL.json").write_bytes(broken)
                git(self.seed, "commit", "--quiet", "-am", "chore: break the data")
                git(self.seed, "push", "--quiet", "origin", "HEAD:refs/heads/master")
                self.seed_name = f"u{index}"
                code, run = self.run_publisher()
                self.assertEqual((code, run["outcome"]), (0, "published"))
                # History keeps the hash of what the tip published; the spider
                # got an empty list instead, followed by the results of the
                # earlier runs of this test.
                self.assertEqual((run["previous_sha256"], run["previous_usable"]),
                                 (hashlib.sha256(broken).hexdigest(), False))
                reused = run["reuse"]
                self.assertEqual(self.crawler_calls()[-1]["previous_sha256"],
                                 reused["input_sha256"])
                runs = self.state / "runs"
                if index == 0:
                    self.assertEqual(reused["accepted"], [])
                    self.assertEqual(reused["input_sha256"], hashlib.sha256(b"[]\n").hexdigest())
                else:
                    self.assertEqual(len(reused["accepted"]), index)
                    expected = []
                    for earlier in reused["accepted"]:
                        expected += json.loads(
                            (runs / earlier["run_id"] / "nate_result.json").read_text())
                    merged = runs / run["run_id"] / local_publish.MERGED_PREVIOUS_FILE
                    self.assertEqual(json.loads(merged.read_text()), expected)
                published = git(self.origin, "show", f"{self.origin_tip()}:newsURL.json")
                self.assertEqual(len(json.loads(published)), 300)

    def test_previous_copy_must_equal_the_tip_blob(self):
        publisher = self.make_publisher()
        real_git = publisher.git

        def git_then_rewrite(cwd, *args, **kwargs):
            out = real_git(cwd, *args, **kwargs)
            if args[:1] == ("clean",):  # first prepare_publish_clone, before the copy
                publisher.git = real_git
                (self.state / "publish" / "newsURL.json").write_text(previous_publish(company="tampered"))
            return out

        publisher.git = git_then_rewrite
        code, run = self.run_publisher(publisher)
        self.assertEqual((code, run["reason"]), (1, "previous_publish_mismatch"))
        self.assertFalse(self.marker.exists(), "must stop before crawling")
        # The optional reuse step never runs on, or hides, a bad copy.
        self.assertNotIn("reuse", run)
        self.assertFalse((self.state / "runs" / run["run_id"]
                          / local_publish.MERGED_PREVIOUS_FILE).exists())
        self.assertEqual(self.origin_tip(), self.pin)

    def test_stale_bytecode_is_never_used_as_the_assembler(self):
        # `python -m nate_news.fallback` would load a valid __pycache__ entry.
        # A forged one, same size and mtime as the source, must not run.
        import py_compile
        source = self.state / "code" / "nate_news" / "nate_news" / "fallback.py"
        size = source.stat().st_size
        forged_text = "print('FORGED ASSEMBLER RAN')\n"
        forged = self.tmp / "fallback.py"
        forged.write_text(forged_text + "#" * (size - len(forged_text) - 1) + "\n")
        self.assertEqual(forged.stat().st_size, size)
        stat = source.stat()
        os.utime(forged, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        cache = source.parent / "__pycache__"
        cache.mkdir()
        (self.state / "code" / ".git" / "info" / "exclude").write_text("__pycache__/\n")
        py_compile.compile(str(forged), cfile=str(cache / f"fallback.{sys.implementation.cache_tag}.pyc"))
        code, run = self.run_publisher()
        self.assertEqual((code, run["outcome"]), (0, "published"))
        log = (self.state / "runs" / run["run_id"] / "publish.log").read_text()
        self.assertNotIn("FORGED", log)
        self.assertIn("assemble: fallback result: 300 of 300", log)
        # The reuse step imports the same module and must not run it either.
        self.assertNotIn("error", run["reuse"])

    def test_symlink_planted_before_the_previous_copy_is_refused(self):
        outside = self.tmp / "outside.txt"
        outside.write_text("[]\n")
        publisher = self.make_publisher()
        real_git = publisher.git

        def git_then_plant(cwd, *args, **kwargs):
            out = real_git(cwd, *args, **kwargs)
            if args[:1] == ("clean",):  # first prepare_publish_clone, before the copy
                publisher.git = real_git
                target = self.state / "publish" / "newsURL.json"
                target.unlink()
                target.symlink_to(outside)
            return out

        publisher.git = git_then_plant
        code, run = self.run_publisher(publisher)
        self.assertEqual((code, run["reason"]), (1, "data_path_not_regular_file"))
        self.assertFalse(self.marker.exists(), "must stop before crawling")
        self.assertEqual(self.origin_tip(), self.pin)

    def test_cut_off_crawl_is_rescued_by_the_previous_publish(self):
        self.run_publisher()  # 300 articles with a company
        # Without the first run's local result, so only the tip can help.
        for path in (self.state / "runs").iterdir():
            shutil.rmtree(path)
        self.mode, self.seed_name = "blocked", "b"  # only 30 pages before the cut-off
        code, run = self.run_publisher()
        self.assertEqual((code, run["outcome"]), (0, "published"))
        self.assertEqual(run["reuse"]["accepted"], [])
        self.assertEqual((run["crawl"]["stop_reason"], run["crawl"]["items"]), ("transport_breaker", 300))

    # -- reuse of this machine's earlier results ---------------------------

    def fake_day_publisher(self):
        """A publisher whose run day is the fake ranking's day, so the reuse
        report counts the fake article ids as reusable."""
        return self.make_publisher(kst_day=lambda started: DAY)

    def test_cut_off_run_is_completed_by_the_next_run_with_its_results(self):
        # Each run is cut off after 150 article pages, and the tip publishes
        # nothing to reuse. The first run fails; the second asks only for the
        # other 150 articles and publishes all 300.
        self.mode, self.seed_name = "half", "a"
        code, first = self.run_publisher(self.fake_day_publisher())
        self.assertEqual((code, first["reason"]), (1, "validation_failed"))
        self.assertEqual(first["reuse"]["accepted"], [])
        self.seed_name = "b"
        code, second = self.run_publisher(self.fake_day_publisher())
        self.assertEqual((code, second["outcome"]), (0, "published"))
        self.assertEqual((second["crawl"]["items"], second["crawl"]["article_requests"]), (300, 152))
        earlier = self.state / "runs" / first["run_id"]
        reused = second["reuse"]
        self.assertEqual(reused["accepted"], [{
            "run_id": first["run_id"],
            "result_sha256": local_publish.sha256_file(earlier / "nate_result.json"),
            "metrics_sha256": local_publish.sha256_file(earlier / "crawl_metrics.json"),
        }])
        self.assertEqual((reused["added_ids"], reused["kst_day"], reused["considered"]),
                         (150, DAY, 1))
        merged = self.state / "runs" / second["run_id"] / local_publish.MERGED_PREVIOUS_FILE
        self.assertEqual(reused["input_sha256"], local_publish.sha256_file(merged))
        call = self.crawler_calls()[-1]
        self.assertEqual((call["previous"], call["previous_sha256"], call["kst_day"]),
                         (str(merged), reused["input_sha256"], DAY))
        # previous_sha256 still names what the tip published.
        self.assertEqual(second["previous_sha256"], hashlib.sha256(b"[]\n").hexdigest())
        published = json.loads(git(self.origin, "show", f"{self.origin_tip()}:newsURL.json"))
        self.assertEqual({item["company"] for item in published}, {"c"})

    def test_only_sound_recent_finished_runs_are_reused(self):
        self.mode, self.seed_name = "half", "a"
        code, first = self.run_publisher(self.fake_day_publisher())
        self.assertEqual(code, 1)
        runs = self.state / "runs"
        source = runs / first["run_id"]
        history = self.state / "history.jsonl"
        now = local_publish.utc_now()

        def clone(hours_ago, change=None, recorded=True, parent=runs):
            name = (now - timedelta(hours=hours_ago)).strftime(local_publish.RUN_ID_FORMAT)
            target = parent / name
            shutil.copytree(source, target)
            metrics = json.loads((target / "crawl_metrics.json").read_text())
            metrics["run_id"] = name
            (target / "crawl_metrics.json").write_text(json.dumps(metrics))
            if change:
                change(target)
            if recorded:
                with open(history, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"run_id": name, "outcome": "failed"}) + "\n")
            return name

        def metrics_change(**updates):
            def change(target):
                metrics = json.loads((target / "crawl_metrics.json").read_text())
                metrics.update(updates)
                for key in [key for key, value in updates.items() if value is None]:
                    del metrics[key]
                (target / "crawl_metrics.json").write_text(json.dumps(metrics))
            return change

        def empty_company(target):
            items = json.loads((target / "nate_result.json").read_text())
            items[5]["company"] = ""
            (target / "nate_result.json").write_text(json.dumps(items))

        def broken_result(target):
            (target / "nate_result.json").write_text("[")

        def linked_result(target):
            # A link to a sound result that stays in place: refused for being
            # a link, not for being missing.
            kept = self.tmp / "kept_result.json"
            shutil.copyfile(target / "nate_result.json", kept)
            (target / "nate_result.json").unlink()
            (target / "nate_result.json").symlink_to(kept)

        good_newer, good_older = clone(1), clone(2)
        clone(3, metrics_change(stop_reason="unexpected:fallback_chain_broken"))
        clone(4, metrics_change(spider_errors=1))
        clone(5, metrics_change(mode=None))  # metrics of a full crawl carry no mode
        clone(6, empty_company)
        clone(7, metrics_change(run_id="20200101T000000.000000Z"))
        clone(8, broken_result)
        clone(9, linked_result)
        clone(10, recorded=False)  # never reached record(): not in the history
        clone(49)  # older than the window
        clone(-1)  # in the future
        outside = self.tmp / "outside"
        outside.mkdir()
        linked = clone(11, parent=outside)
        (runs / linked).symlink_to(outside / linked)
        (runs / "notes").mkdir()
        shutil.rmtree(source)

        self.seed_name = "b"
        code, second = self.run_publisher(self.fake_day_publisher())
        self.assertEqual((code, second["outcome"]), (0, "published"))
        reused = second["reuse"]
        self.assertEqual([entry["run_id"] for entry in reused["accepted"]], [good_newer, good_older])
        self.assertEqual(reused["rejected"], {"unsound": 4, "run_id_mismatch": 1,
                                              "unreadable": 1, "not_regular_file": 1})
        self.assertEqual(reused["skipped"], {"not_in_history": 1, "outside_window": 2,
                                             "not_a_directory": 1, "not_a_run": 1})
        self.assertEqual(reused["considered"], 9)
        # The two copies of the same run add its 150 articles once.
        self.assertEqual(reused["added_ids"], 150)
        merged = json.loads((runs / second["run_id"] / local_publish.MERGED_PREVIOUS_FILE).read_text())
        good = json.loads((runs / good_newer / "nate_result.json").read_text())
        self.assertEqual(merged, good + good)

    def test_tip_entry_wins_and_an_invalid_tip_entry_does_not_hide_a_local_one(self):
        self.mode, self.seed_name = "half", "a"
        code, _ = self.run_publisher(self.fake_day_publisher())
        self.assertEqual(code, 1)
        # A manual data commit: every article with company "old", except one
        # with an empty company, which build_cache() does not accept. Both ids
        # are in the first run's result and outside the ten pages read below.
        hidden, kept = f"{DAY}n00030", f"{DAY}n00031"
        items = json.loads(previous_publish(company="old"))
        for item in items:
            if f"/{hidden}?" in item["newsURL"]:
                item["company"] = ""
        self.push_from_seed("newsURL.json", json.dumps(items, ensure_ascii=False) + "\n",
                            "chore: manual data")
        self.mode, self.seed_name = "reuse", "b"
        code, second = self.run_publisher(self.fake_day_publisher())
        self.assertEqual((code, second["outcome"]), (0, "published"))
        self.assertEqual(second["reuse"]["added_ids"], 1)
        published = {
            item["newsURL"].split("/view/")[1].split("?")[0]: item
            for item in json.loads(git(self.origin, "show", f"{self.origin_tip()}:newsURL.json"))
        }
        self.assertEqual(published[hidden]["company"], "c")  # the earlier run's result
        self.assertEqual(published[kept]["company"], "old")  # the tip's entry wins

    def test_reuse_failure_falls_back_to_the_previous_publish(self):
        self.push_from_seed("newsURL.json", previous_publish(company="old"), "News")
        report_ok = json.dumps({"accepted": [], "rejected_counts": {}, "added_ids": 0})
        scripts = {
            "exit with partial files": "open(merged, 'w').write('[1]'); "
                                       "open(report, 'w').write('{}'); raise SystemExit(1)",
            "no report": "open(merged, 'w').write('[]')",
            "corrupt report": "open(merged, 'w').write('[]'); open(report, 'w').write('{')",
            "report not an object": "open(merged, 'w').write('[]'); open(report, 'w').write('[]')",
            "merged not a list": f"open(merged, 'w').write('{{}}'); open(report, 'w').write({report_ok!r})",
            "no merged file": f"open(report, 'w').write({report_ok!r})",
            "report entry without a hash": "open(merged, 'w').write('[]'); open(report, 'w').write("
                                           "'{\"accepted\": [{\"run_id\": \"x\"}], "
                                           "\"rejected_counts\": {}, \"added_ids\": 0}')",
            "timeout": "import time; time.sleep(10)",
        }
        for index, (name, body) in enumerate(scripts.items()):
            with self.subTest(name), mock.patch.object(local_publish, "REUSE_TIMEOUT_SECONDS", 2):
                self.mode, self.seed_name = "reuse", f"f{index}"
                publisher = self.make_publisher()
                notices = []
                publisher.notify = lambda title, text: notices.append(title)

                def failing(previous, kst_day, runs, merged, report, body=body):
                    return [sys.executable, "-c",
                            "import sys\nmerged, report = sys.argv[1:3]\n" + body,
                            str(merged), str(report)]

                publisher.reuse_command = failing
                code, run = self.run_publisher(publisher)
                self.assertEqual((code, run["outcome"]), (0, "published"))
                self.assertIn("error", run["reuse"])
                self.assertNotIn("accepted", run["reuse"])
                self.assertEqual(run["reuse"]["input_sha256"], run["previous_sha256"])
                self.assertEqual(notices, ["News_JSON reuse failed"])
                call = self.crawler_calls()[-1]
                self.assertEqual(call["previous_sha256"], run["previous_sha256"])
                run_dir = self.state / "runs" / run["run_id"]
                self.assertEqual(call["previous"], str(run_dir / local_publish.PREVIOUS_FILE))
                self.assertFalse((run_dir / local_publish.MERGED_PREVIOUS_FILE).exists())
                self.assertFalse((run_dir / local_publish.REUSE_REPORT_FILE).exists())

    def test_failure_after_the_reuse_process_still_falls_back(self):
        # The reuse process succeeded and accepted a run, but hashing its
        # output fails: the run uses the tip copy and records no accepted runs.
        self.mode, self.seed_name = "half", "a"
        code, first = self.run_publisher(self.fake_day_publisher())
        self.assertEqual(code, 1)
        real_sha256 = local_publish.sha256_file

        def unreadable_merged(path):
            if Path(path).name == local_publish.MERGED_PREVIOUS_FILE:
                raise PermissionError(13, "Permission denied", str(path))
            return real_sha256(path)

        self.seed_name = "b"
        with mock.patch.object(local_publish, "sha256_file", side_effect=unreadable_merged):
            code, second = self.run_publisher(self.fake_day_publisher())
        # Without the earlier result, the second cut-off run is short again.
        self.assertEqual((code, second["reason"]), (1, "validation_failed"))
        self.assertIn("Permission denied", second["reuse"]["error"])
        self.assertNotIn("accepted", second["reuse"])
        self.assertEqual(second["reuse"]["input_sha256"], second["previous_sha256"])
        run_dir = self.state / "runs" / second["run_id"]
        self.assertEqual(self.crawler_calls()[-1]["previous"], str(run_dir / local_publish.PREVIOUS_FILE))
        self.assertFalse((run_dir / local_publish.MERGED_PREVIOUS_FILE).exists())

    def test_ctrl_c_during_the_reuse_step_is_not_absorbed(self):
        publisher = self.make_publisher()
        real_run_command = publisher.run_command

        def interrupt_reuse(args, **kwargs):
            if "nate_news.reuse" in map(str, args):
                raise KeyboardInterrupt
            return real_run_command(args, **kwargs)

        publisher.run_command = interrupt_reuse
        with self.assertRaises(KeyboardInterrupt):
            publisher.run()
        self.assertFalse(self.marker.exists(), "must not crawl after Ctrl-C")
        self.assertEqual(self.origin_tip(), self.pin)

    def test_reuse_and_spider_get_one_day_fixed_before_the_reuse_step(self):
        kst_day = local_publish.Publisher.kst_day
        self.assertEqual(kst_day(datetime(2020, 1, 2, 14, 59, 59, 999999, tzinfo=timezone.utc)),
                         "20200102")
        self.assertEqual(kst_day(datetime(2020, 1, 2, 15, 0, tzinfo=timezone.utc)), "20200103")
        # The run starts a moment before midnight KST (a date that can never
        # be today's); every later reading of the clock is after midnight.
        # The day is read once, after the preflight, and both the reuse step
        # and the spider get that same day.
        started = datetime(2020, 1, 2, 14, 59, 59, 900000, tzinfo=timezone.utc)
        clock = iter([started])
        with mock.patch.object(local_publish, "utc_now",
                               side_effect=lambda: next(clock, started + timedelta(seconds=1))):
            code, run = self.run_publisher()
        self.assertEqual(code, 0)
        self.assertTrue(run["run_id"].startswith("20200102T145959"))
        self.assertEqual(run["reuse"]["kst_day"], "20200103")
        self.assertEqual(self.crawler_calls()[-1]["kst_day"], "20200103")

    def test_suspension_during_the_reuse_step_is_not_published(self):
        # The day is fixed right before the reuse step, so a sleep from then
        # on (for example past midnight) must stop the run.
        publisher = self.make_publisher()
        real_add = publisher.add_local_results
        real_time = local_publish.time.time
        offset = [0.0]

        def add_while_machine_sleeps(*args):
            outputs = real_add(*args)
            offset[0] = 9 * 3600  # wall clock jumps, monotonic clock does not
            return outputs

        publisher.add_local_results = add_while_machine_sleeps
        with mock.patch.object(local_publish.time, "time", lambda: real_time() + offset[0]):
            code, run = self.run_publisher(publisher)
        self.assertEqual((code, run["reason"]), (1, "suspended_or_clock_jump"))
        self.assertEqual(self.origin_tip(), self.pin)

    def test_damaged_history_only_limits_reuse(self):
        self.mode, self.seed_name = "half", "a"
        code, first = self.run_publisher(self.fake_day_publisher())
        self.assertEqual(code, 1)
        history = self.state / "history.jsonl"
        good = history.read_bytes()
        # A torn append that ends inside a multi-byte character, then lines
        # that are not JSON objects, one nested too deeply to parse.
        history.write_bytes(good + b'{"run_id": "x", "detail": "\xea\xb0' + b'not json\n[1]\n'
                            + b"[" * 100000 + b"]" * 100000 + b"\n")
        self.seed_name = "b"
        code, second = self.run_publisher(self.fake_day_publisher())
        self.assertEqual((code, second["outcome"]), (0, "published"))
        self.assertEqual([entry["run_id"] for entry in second["reuse"]["accepted"]],
                         [first["run_id"]])
        # An unreadable history means nothing is offered, not a failed run.
        history.unlink()
        history.mkdir()
        self.seed_name = "c"
        code, third = self.run_publisher(self.fake_day_publisher())
        self.assertIn("error", third["reuse"])
        self.assertEqual(third["reuse"]["input_sha256"], third["previous_sha256"])

    def test_snapshot_problems_publish_nothing(self):
        for mode, reason in (("wrong_run", "assemble_failed"), ("nosnapshot", "crawl_failed"),
                             ("rank_failed", "validation_failed")):
            with self.subTest(mode):
                self.mode = mode
                code, run = self.run_publisher()
                self.assertEqual((code, run["outcome"], run["reason"]), (1, "failed", reason))
                self.assertNotIn("crawl", run)
                self.assertEqual(self.origin_tip(), self.pin)

    def test_identical_data_is_not_committed_again(self):
        self.run_publisher()
        tip = self.origin_tip()
        code, run = self.run_publisher()
        self.assertEqual((code, run["outcome"]), (0, "unchanged"))
        self.assertEqual(self.origin_tip(), tip)

    def test_validation_failure_publishes_nothing(self):
        self.mode = "blocked"
        code, run = self.run_publisher()
        self.assertEqual((code, run["outcome"], run["reason"]), (1, "failed", "validation_failed"))
        self.assertEqual(self.origin_tip(), self.pin)

    def test_crawler_failure_publishes_nothing(self):
        self.mode = "exit"
        code, run = self.run_publisher()
        self.assertEqual((code, run["reason"]), (1, "crawl_failed"))
        self.assertEqual(self.origin_tip(), self.pin)

    def test_remote_moved_during_crawl_is_not_overwritten(self):
        publisher = self.make_publisher()
        original_crawl = publisher.crawl

        def crawl_then_someone_else_publishes(*args):
            outputs = original_crawl(*args)
            self.newer = self.push_from_seed("newsURL.json", '["newer"]\n', "News")
            return outputs

        publisher.crawl = crawl_then_someone_else_publishes
        code, run = self.run_publisher(publisher)
        self.assertEqual((code, run["reason"]), (1, "remote_moved"))
        self.assertEqual(self.origin_tip(), self.newer)
        self.assertEqual(git(self.origin, "show", f"{self.newer}:newsURL.json"), '["newer"]')

    def test_unreviewed_remote_code_change_blocks_before_crawling(self):
        self.push_from_seed("nate_news/nate_news/spiders/spider.py", "# v2\n", "fix: change spider")
        tip = self.origin_tip()
        code, run = self.run_publisher()
        self.assertEqual((code, run["reason"]), (1, "remote_code_changed"))
        self.assertIn("spider.py", run["detail"])
        self.assertFalse(self.marker.exists(), "crawler must not run")
        self.assertEqual(self.origin_tip(), tip)

    def test_sub_yml_style_drift_is_allowed_and_preserved(self):
        synced = self.push_from_seed("nate_news/nate_news/config.json", '{"synced": 1}\n', "News")
        code, run = self.run_publisher()
        self.assertEqual((code, run["outcome"]), (0, "published"))
        tip = self.origin_tip()
        self.assertEqual(git(self.origin, "rev-parse", f"{tip}^"), synced)
        self.assertEqual(git(self.origin, "show", f"{tip}:nate_news/nate_news/config.json"), '{"synced": 1}')

    def test_rewritten_remote_history_blocks(self):
        other = self.tmp / "other"
        git(self.tmp, "init", "--quiet", "--initial-branch=master", other)
        (other / "x").write_text("x")
        git(other, "add", "-A")
        git(other, "commit", "--quiet", "-m", "unrelated")
        git(other, "push", "--quiet", "--force", str(self.origin), "HEAD:refs/heads/master")
        code, run = self.run_publisher()
        self.assertEqual((code, run["reason"]), (1, "remote_history_unexpected"))
        self.assertFalse(self.marker.exists())

    def test_code_checkout_must_be_clean_and_at_pin(self):
        (self.state / "code" / "nate_news" / "validate_result.py").write_text("# tampered\n")
        code, run = self.run_publisher()
        self.assertEqual((code, run["reason"]), (1, "code_checkout_dirty"))
        git(self.state / "code", "checkout", "--quiet", "--", ".")
        (self.state / "code" / "extra.py").write_text("# untracked\n")
        code, run = self.run_publisher()
        self.assertEqual((code, run["reason"]), (1, "code_checkout_dirty"))
        (self.state / "code" / "extra.py").unlink()
        self.write_config(pin_sha="0" * 40)
        code, run = self.run_publisher()
        self.assertEqual((code, run["reason"]), (1, "code_not_at_pin"))
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.origin_tip(), self.pin)

    def test_dependency_drift_blocks(self):
        code, run = self.run_publisher(self.make_publisher(real_dependency_check=True))
        self.assertEqual((code, run["reason"]), (1, "dependency_drift"))
        self.assertFalse(self.marker.exists())

    def test_missing_dependency_fingerprint_is_a_config_error(self):
        for bad in (None, "", "abc", "Z" * 64):
            self.write_config(freeze_sha256=bad)
            code, run = self.run_publisher(self.make_publisher(real_dependency_check=True))
            self.assertEqual((code, run["reason"]), (1, "config_invalid"), bad)
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.origin_tip(), self.pin)

    def test_symlinked_data_path_is_refused_without_writing_outside(self):
        outside = self.tmp / "outside.txt"
        outside.write_text("do not touch\n")
        git(self.seed, "pull", "--quiet", "origin", "master")
        (self.seed / "newsURL.json").unlink()
        (self.seed / "newsURL.json").symlink_to(outside)
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "--quiet", "-m", "News")
        git(self.seed, "push", "--quiet", "origin", "HEAD:refs/heads/master")
        tip = self.origin_tip()
        code, run = self.run_publisher()
        self.assertEqual((code, run["reason"]), (1, "data_path_not_regular_file"))
        self.assertFalse(self.marker.exists(), "must stop before crawling")
        self.assertEqual(outside.read_text(), "do not touch\n")
        self.assertEqual(self.origin_tip(), tip)

    def test_publish_refuses_symlink_in_worktree_even_if_tree_check_is_bypassed(self):
        outside = self.tmp / "outside.txt"
        outside.write_text("do not touch\n")
        publisher = self.make_publisher()
        original_remote_tip = publisher.remote_tip
        calls = []

        def remote_tip_then_plant_symlink(**kwargs):
            tip = original_remote_tip(**kwargs)
            calls.append(tip)
            if len(calls) == 2:  # the call inside publish()
                original_git = publisher.git

                def git_then_plant(cwd, *args, **kwargs):
                    out = original_git(cwd, *args, **kwargs)
                    if args[:1] == ("clean",):  # last step of prepare_publish_clone
                        target = self.state / "publish" / "newsURL.json"
                        target.unlink()
                        target.symlink_to(outside)
                    return out

                publisher.git = git_then_plant
            return tip

        publisher.remote_tip = remote_tip_then_plant_symlink
        code, run = self.run_publisher(publisher)
        self.assertEqual((code, run["reason"]), (1, "data_path_not_regular_file"))
        self.assertEqual(outside.read_text(), "do not touch\n")
        self.assertEqual(self.origin_tip(), self.pin)

    def test_repin_is_refused_while_a_run_holds_the_lock(self):
        target = self.push_from_seed("README.md", "x\n", "docs: x")
        with open(self.state / "lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(local_publish.Abort) as caught:
                local_publish.Publisher(self.state).repin(target, sys.executable)
        self.assertEqual(caught.exception.reason, "locked")
        self.assertEqual(git(self.state / "code", "rev-parse", "HEAD"), self.pin)
        self.assertEqual(json.loads((self.state / "config.json").read_text())["pin_sha"], self.pin)
        self.assertTrue((self.state / "venv" / "bin" / "python").exists())

    def test_failed_repin_keeps_previous_code_venv_and_config(self):
        bad = self.push_from_seed("nate_news/requirements.txt", "./no-such-package-dir\n", "chore: bad deps")
        before = (self.state / "config.json").read_text()
        with self.assertRaises(local_publish.Abort) as caught:
            local_publish.Publisher(self.state).repin(bad, sys.executable)
        self.assertEqual(caught.exception.reason, "pip_install_failed")
        self.assertEqual(git(self.state / "code", "rev-parse", "HEAD"), self.pin)
        self.assertEqual((self.state / "config.json").read_text(), before)
        self.assertTrue((self.state / "venv" / "bin" / "python").is_symlink(), "old venv kept")
        self.assertFalse((self.state / "venv.new").exists())

    def assert_previous_environment_intact(self, config_before):
        self.assertEqual(git(self.state / "code", "rev-parse", "HEAD"), self.pin)
        self.assertEqual((self.state / "config.json").read_text(), config_before)
        self.assertTrue((self.state / "venv" / "bin" / "python").is_symlink(), "old venv kept")
        self.assertFalse((self.state / "venv.new").exists())
        self.assertFalse((self.state / "venv.old").exists())
        self.assertFalse((self.state / "config.json.tmp").exists())

    def test_repin_restores_everything_when_a_late_step_fails(self):
        target = self.push_from_seed("README.md", "x\n", "docs: x")
        config_before = (self.state / "config.json").read_text()
        real_rename = Path.rename

        def fail_second_rename(path, destination):
            if path.name == "venv.new":
                raise OSError("simulated rename failure")
            return real_rename(path, destination)

        def fail_replace(path, destination):
            raise OSError("simulated config save failure")

        def fail_build(self_, base_python, venv_dir):
            raise OSError("simulated build failure")

        cases = (
            ("venv swap", mock.patch.object(Path, "rename", fail_second_rename)),
            ("config save", mock.patch.object(Path, "replace", fail_replace)),
            ("build OSError", mock.patch.object(local_publish.Publisher, "build_venv", fail_build)),
        )
        for label, patcher in cases:
            with self.subTest(label), patcher:
                with self.assertRaises(local_publish.Abort) as caught:
                    local_publish.Publisher(self.state).repin(target, sys.executable)
                self.assertEqual(caught.exception.reason, "repin_failed")
            with self.subTest(label + " state"):
                self.assert_previous_environment_intact(config_before)
        # After the failures a normal repin still succeeds.
        local_publish.Publisher(self.state).repin(target, sys.executable)
        self.assertEqual(json.loads((self.state / "config.json").read_text())["pin_sha"], target)

    def test_repin_moves_code_venv_and_fingerprint_together(self):
        target = self.push_from_seed("README.md", "x\n", "docs: x")
        local_publish.Publisher(self.state).repin(target, sys.executable)
        config = json.loads((self.state / "config.json").read_text())
        self.assertEqual(config["pin_sha"], target)
        self.assertEqual(git(self.state / "code", "rev-parse", "HEAD"), target)
        self.assertTrue(local_publish.is_hex(config["freeze_sha256"], 64))
        self.assertNotEqual(config["freeze_sha256"], "0" * 64)
        self.assertFalse((self.state / "venv.old").exists())
        self.assertFalse((self.state / "venv.new").exists())
        publisher = local_publish.Publisher(self.state)
        publisher.load_config()
        publisher.check_dependencies()  # real venv matches its recorded fingerprint

    def test_repin_refuses_abbreviated_sha_and_dirty_checkout(self):
        with self.assertRaises(local_publish.Abort) as caught:
            local_publish.Publisher(self.state).repin(self.pin[:12], sys.executable)
        self.assertEqual(caught.exception.reason, "pin_not_full_sha")
        (self.state / "code" / "extra.py").write_text("# untracked\n")
        with self.assertRaises(local_publish.Abort) as caught:
            local_publish.Publisher(self.state).repin(self.pin, sys.executable)
        self.assertEqual(caught.exception.reason, "code_checkout_dirty")

    def test_second_run_exits_while_lock_is_held(self):
        with open(self.state / "lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.make_publisher().run(), 3)
        self.assertFalse(self.marker.exists())
        self.assertFalse((self.state / "state.json").exists())

    def test_rejected_push_leaves_remote_untouched_and_next_run_recovers(self):
        hook = self.origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho denied >&2\nexit 1\n")
        hook.chmod(0o755)
        code, run = self.run_publisher()
        self.assertEqual((code, run["reason"]), (1, "push_failed"))
        self.assertEqual(self.origin_tip(), self.pin)
        hook.unlink()
        code, run = self.run_publisher()
        self.assertEqual((code, run["outcome"]), (0, "published"))
        self.assertEqual(git(self.origin, "rev-parse", f"{self.origin_tip()}^"), self.pin)

    def test_leftover_staged_data_does_not_block_later_runs(self):
        publisher = self.make_publisher()
        real_git = publisher.git

        def fail_commit_once(cwd, *args, **kwargs):
            if args[:1] == ("commit",):
                publisher.git = real_git
                raise local_publish.Abort("commit_failed", "simulated")
            return real_git(cwd, *args, **kwargs)

        publisher.git = fail_commit_once
        code, run = self.run_publisher(publisher)
        self.assertEqual((code, run["reason"]), (1, "commit_failed"))
        self.assertTrue(git(self.state / "publish", "status", "--porcelain"), "clone left dirty")
        other = self.push_from_seed("newsURL.json", '["someone else"]\n', "chore: manual refresh")
        self.seed_name = "b"
        code, run = self.run_publisher()
        self.assertEqual((code, run["outcome"]), (0, "published"))
        self.assertEqual(git(self.origin, "rev-parse", f"{self.origin_tip()}^"), other)

    def test_unusable_publish_clone_aborts_before_crawling(self):
        (self.state / "publish" / ".git" / "index.lock").write_text("")
        code, run = self.run_publisher()
        self.assertEqual((code, run["reason"]), (1, "publish_prepare_failed"))
        self.assertFalse(self.marker.exists(), "must stop before crawling")

    def test_push_auth_failure_aborts_before_crawling(self):
        publisher = self.make_publisher()
        real_git = publisher.git

        def deny_dry_run(cwd, *args, **kwargs):
            if "--dry-run" in args:
                raise local_publish.Abort(kwargs["reason"], "simulated auth failure")
            return real_git(cwd, *args, **kwargs)

        publisher.git = deny_dry_run
        code, run = self.run_publisher(publisher)
        self.assertEqual((code, run["reason"]), (1, "push_auth_failed"))
        self.assertFalse(self.marker.exists())

    def test_push_error_after_remote_accepted_counts_as_published(self):
        publisher = self.make_publisher()
        real_git = publisher.git

        def push_then_report_failure(cwd, *args, **kwargs):
            out = real_git(cwd, *args, **kwargs)
            if args[:1] == ("push",) and "--dry-run" not in args:
                raise local_publish.Abort("push_failed", "simulated client timeout")
            return out

        publisher.git = push_then_report_failure
        code, run = self.run_publisher(publisher)
        self.assertEqual((code, run["outcome"]), (0, "published"))
        self.assertEqual(self.origin_tip(), run["published_commit"])

    def test_crawl_suspended_by_sleep_is_not_published(self):
        publisher = self.make_publisher()
        original_crawl = publisher.crawl
        real_time = local_publish.time.time
        offset = [0.0]

        def crawl_while_machine_sleeps(*args):
            outputs = original_crawl(*args)
            offset[0] = 3 * 3600  # wall clock jumps, monotonic clock does not
            return outputs

        publisher.crawl = crawl_while_machine_sleeps
        with mock.patch.object(local_publish.time, "time", lambda: real_time() + offset[0]):
            code, run = self.run_publisher(publisher)
        self.assertEqual((code, run["reason"]), (1, "suspended_or_clock_jump"))
        self.assertEqual(self.origin_tip(), self.pin)

    def test_suspension_while_preparing_the_publish_is_not_pushed(self):
        publisher = self.make_publisher()
        real_git = publisher.git
        real_time = local_publish.time.time
        offset = [0.0]

        def sleep_during_commit(cwd, *args, **kwargs):
            out = real_git(cwd, *args, **kwargs)
            if args[:1] == ("commit",):
                offset[0] = 3 * 3600  # lid closed after the first check passed
            return out

        publisher.git = sleep_during_commit
        with mock.patch.object(local_publish.time, "time", lambda: real_time() + offset[0]):
            code, run = self.run_publisher(publisher)
        self.assertEqual((code, run["reason"]), (1, "suspended_or_clock_jump"))
        self.assertEqual(self.origin_tip(), self.pin, "nothing was pushed")
        code, run = self.run_publisher()  # the leftover local commit does not block the next run
        self.assertEqual((code, run["outcome"]), (0, "published"))

    def test_ctrl_c_during_the_final_repin_swap_is_deferred_until_consistent(self):
        import signal
        real_rename, real_replace = Path.rename, Path.replace

        def interrupt_after(method, name):
            def wrapper(path, destination):
                result = method(path, destination)
                if path.name == name:
                    os.kill(os.getpid(), signal.SIGINT)
                return result
            return wrapper

        cases = (
            ("after first rename", mock.patch.object(Path, "rename", interrupt_after(real_rename, "venv"))),
            ("after config save", mock.patch.object(Path, "replace", interrupt_after(real_replace, "config.json.tmp"))),
        )
        expected_pin = self.pin
        for index, (label, patcher) in enumerate(cases):
            target = self.push_from_seed("README.md", f"{index}\n", f"docs: {index}")
            with self.subTest(label):
                with patcher, self.assertRaises(KeyboardInterrupt):
                    local_publish.Publisher(self.state).repin(target, sys.executable)
                expected_pin = target  # the swap completed before the interrupt surfaced
                config = json.loads((self.state / "config.json").read_text())
                self.assertEqual(config["pin_sha"], expected_pin)
                self.assertEqual(git(self.state / "code", "rev-parse", "HEAD"), expected_pin)
                self.assertTrue((self.state / "venv" / "bin" / "python").exists())
                self.assertFalse((self.state / "venv.new").exists())
                publisher = local_publish.Publisher(self.state)
                publisher.load_config()
                publisher.check_code_pin()
                publisher.check_dependencies()

    def test_ctrl_c_while_building_the_new_venv_restores_the_old_environment(self):
        target = self.push_from_seed("README.md", "x\n", "docs: x")
        config_before = (self.state / "config.json").read_text()

        def interrupted_build(self_, base_python, venv_dir):
            venv_dir.mkdir()
            raise KeyboardInterrupt

        with mock.patch.object(local_publish.Publisher, "build_venv", interrupted_build):
            with self.assertRaises(KeyboardInterrupt):
                local_publish.Publisher(self.state).repin(target, sys.executable)
        self.assert_previous_environment_intact(config_before)

    def test_unexpected_exception_is_recorded_and_lock_released(self):
        publisher = self.make_publisher()
        notices = []
        publisher.notify = lambda title, text: notices.append(text)

        def explode(*args):
            raise OSError(28, "No space left on device")

        publisher.crawl = explode
        code, run = self.run_publisher(publisher)
        self.assertEqual((code, run["outcome"], run["reason"]), (1, "failed", "internal_error"))
        self.assertIn("No space left", run["detail"])
        self.assertEqual(notices, ["internal_error"])
        log = (self.state / "runs" / run["run_id"] / "publish.log").read_text()
        self.assertIn("Traceback", log)
        code, run = self.run_publisher()  # lock was released, next run works
        self.assertEqual((code, run["outcome"]), (0, "published"))

    def test_damaged_state_file_does_not_break_a_run(self):
        for damaged in (b"[]", b"\xff\xfe not json", b"{broken"):
            (self.state / "state.json").write_bytes(damaged)
            self.seed_name = self.seed_name + "x"
            code, run = self.run_publisher()
            self.assertEqual((code, run["outcome"]), (0, "published"), damaged)

    def test_single_changed_data_file_is_published(self):
        self.run_publisher()
        current = git(self.origin, "show", f"{self.origin_tip()}:newsURL.json")
        self.push_from_seed("nate_news/nate_result.json", "[]\n", "chore: break one copy")
        code, run = self.run_publisher()
        self.assertEqual((code, run["outcome"]), (0, "published"))
        tip = self.origin_tip()
        self.assertEqual(git(self.origin, "diff", "--name-only", f"{tip}^", tip), "nate_news/nate_result.json")
        self.assertEqual(git(self.origin, "show", f"{tip}:nate_news/nate_result.json"), current)

    def test_fingerprint_covers_the_base_interpreter(self):
        publisher = local_publish.Publisher(self.state)
        listing, digest = publisher.freeze_fingerprint(sys.executable)
        self.assertIn("# interpreter", listing)
        self.assertIn(sys.version.splitlines()[0], listing)
        self.assertTrue(local_publish.is_hex(digest, 64))

    def test_failed_init_removes_what_it_created_and_can_be_rerun(self):
        fresh = self.tmp / "fresh-state"
        publisher = local_publish.Publisher(fresh)
        with self.assertRaises(local_publish.Abort) as caught:
            publisher.init("f" * 40, str(self.origin), "master", sys.executable)
        self.assertEqual(caught.exception.reason, "pin_checkout_failed")
        for name in ("code", "publish", "venv", "config.json"):
            self.assertFalse((fresh / name).exists(), name)
        local_publish.Publisher(fresh).init(self.pin, str(self.origin), "master", sys.executable)
        self.assertEqual(json.loads((fresh / "config.json").read_text())["pin_sha"], self.pin)
        with self.assertRaises(local_publish.Abort) as caught:
            local_publish.Publisher(fresh).init(self.pin, str(self.origin), "master", sys.executable)
        self.assertEqual(caught.exception.reason, "already_initialized")

    def test_run_command_refuses_an_unpinned_script_or_interpreter(self):
        code = local_publish.main(["--state-dir", str(self.state), "run"])
        self.assertEqual(code, 1)
        self.assertFalse(self.marker.exists())
        self.assertFalse((self.state / "state.json").exists())

    def test_inherited_tool_variables_are_not_passed_to_children(self):
        with mock.patch.dict(os.environ, {"PYTHONPATH": "/evil", "PIP_INDEX_URL": "http://evil",
                                           "SCRAPY_SETTINGS_MODULE": "evil", "GIT_DIR": "/evil"}):
            environment = local_publish.Publisher(self.state).child_env()
        for name in ("PYTHONPATH", "PIP_INDEX_URL", "SCRAPY_SETTINGS_MODULE", "GIT_DIR"):
            self.assertNotIn(name, environment)
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
        # pip compiles into PYTHONPYCACHEPREFIX, so only pinned-code processes get it.
        self.assertNotIn("PYTHONPYCACHEPREFIX", environment)
        run_dir = self.state / "runs" / "x"
        pinned = local_publish.Publisher(self.state).pinned_code_env(run_dir)
        self.assertEqual(Path(pinned["PYTHONPYCACHEPREFIX"]).parent, run_dir)

    def test_stale_bytecode_in_the_checkout_is_never_imported(self):
        # A git-ignored __pycache__ survives the cleanliness check; make sure
        # the validator still runs from source. The planted .pyc would exit 0.
        import py_compile
        cache = self.state / "code" / "nate_news" / "__pycache__"
        cache.mkdir()
        (self.state / "code" / ".git" / "info" / "exclude").write_text("__pycache__/\n")
        evil = self.tmp / "validate_result.py"
        evil.write_text("print('Validation passed: forged')\n")
        source = self.state / "code" / "nate_news" / "validate_result.py"
        stat = source.stat()
        os.utime(evil, (stat.st_atime, stat.st_mtime))
        tag = sys.implementation.cache_tag
        py_compile.compile(str(evil), cfile=str(cache / f"validate_result.{tag}.pyc"))
        self.mode = "blocked"
        code, run = self.run_publisher()
        self.assertEqual((code, run["reason"]), (1, "validation_failed"))
        self.assertEqual(self.origin_tip(), self.pin)
        for run_dir in (self.state / "runs").iterdir():
            self.assertFalse((run_dir / "pycache-never-written").exists())

    def test_history_and_last_success_are_recorded(self):
        self.run_publisher()
        # Not "blocked": with the first publish to reuse, a cut-off run can
        # still validate. A lost ranking page never does.
        self.mode, self.seed_name = "rank_failed", "b"
        self.run_publisher()
        state = json.loads((self.state / "state.json").read_text())
        self.assertEqual(state["last_run"]["outcome"], "failed")
        self.assertEqual(state["last_success"]["outcome"], "published")
        lines = (self.state / "history.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(line)["outcome"] for line in lines], ["published", "failed"])


if __name__ == "__main__":
    unittest.main()
