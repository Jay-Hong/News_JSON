"""Offline tests for tools/local_publish.py.

Everything runs against a local bare repository and a fake crawler, so no
request is sent to Nate or GitHub. Run with:

    python3 -m unittest discover -s tools -p 'test_local_publish.py' -v
"""

import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import local_publish  # noqa: E402  (needs sys.path above)

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_VALIDATOR = REPO_ROOT / "nate_news" / "validate_result.py"

FAKE_CRAWLER = r'''
import json, os, sys
mode, seed = sys.argv[1], sys.argv[2]
marker = os.environ.get("FAKE_CRAWLER_MARKER")
if marker:
    open(marker, "a").write("ran\n")
if mode == "exit":
    sys.exit(2)
items, urls = [], []
for sect in ("sisa", "spo", "ent"):
    for i in range(100):
        url = f"https://m.news.nate.com/view/2026{seed}{sect}{i:03d}?sect={sect}&list=rank&cate=interest"
        urls.append(url)
        items.append({"title": f"{seed}-{sect}-{i}", "company": "c", "image": "", "newsURL": url})
pages = ["sisa-1", "sisa-2", "spo-1", "spo-2", "ent-1", "ent-2"]
metrics = {
    "close_reason": "article_transport_failures:3" if mode == "blocked" else "finished",
    "rank_pages_completed": pages,
    "discovered_by_page": {p: 50 for p in pages},
    "discovered_by_section": {"sisa": 100, "spo": 100, "ent": 100},
    "articles_discovered": 300, "discovered_urls": urls,
    "article_failures": 0, "article_items_yielded": 300,
    "item_scraped_count": 300, "item_dropped_count": 0, "item_error_count": 0,
}
json.dump(items, open(sys.argv[3], "w"), ensure_ascii=False, indent=4)
json.dump(metrics, open(sys.argv[4], "w"))
'''


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

    def crawl_command(self, result, metrics):
        return [sys.executable, str(self.crawler_script), self.mode, self.seed,
                str(result), str(metrics)]

    def check_dependencies(self):
        if self.real_dependency_check:
            super().check_dependencies()


class LocalPublishTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="news-publish-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old_env = dict(os.environ)
        self.addCleanup(self._restore_env)
        os.environ.update({
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        })

        self.origin = self.tmp / "origin.git"
        git(self.tmp, "init", "--quiet", "--bare", "--initial-branch=master", self.origin)
        self.seed = self.tmp / "seed"
        git(self.tmp, "clone", "--quiet", self.origin, self.seed)
        (self.seed / "nate_news" / "nate_news" / "spiders").mkdir(parents=True)
        shutil.copyfile(REAL_VALIDATOR, self.seed / "nate_news" / "validate_result.py")
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

        def crawl_then_someone_else_publishes(run_dir):
            outputs = original_crawl(run_dir)
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

        def remote_tip_then_plant_symlink():
            tip = original_remote_tip()
            calls.append(tip)
            if len(calls) == 2:  # the call inside publish()
                original_git = publisher.git

                def git_then_plant(cwd, *args, **kwargs):
                    out = original_git(cwd, *args, **kwargs)
                    if args[:1] == ("reset",):
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

    def test_history_and_last_success_are_recorded(self):
        self.run_publisher()
        self.mode, self.seed_name = "blocked", "b"
        self.run_publisher()
        state = json.loads((self.state / "state.json").read_text())
        self.assertEqual(state["last_run"]["outcome"], "failed")
        self.assertEqual(state["last_success"]["outcome"], "published")
        lines = (self.state / "history.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(line)["outcome"] for line in lines], ["published", "failed"])


if __name__ == "__main__":
    unittest.main()
