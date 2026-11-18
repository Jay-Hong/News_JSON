"""Crawl, validate, and publish Nate news from a local machine.

GitHub-hosted runners lose access to Nate mid-crawl (see CLAUDE.md), so this
script lets a scheduled local job (launchd) be the primary collector. Since
2026-11-17 this machine's full crawls were cut off the same way after four to
eight minutes, so it runs the incremental pipeline that main.yml uses as its
fallback (spider nate_news_fallback, then nate_news.fallback): ranking pages,
the previous publish, and article pages only where a company or an image is
still missing, at the full crawl's request spacing. It only uses the standard
library and is meant to be run from a pinned checkout:

    <state>/venv/bin/python <state>/code/tools/local_publish.py run

State lives outside any working repository (default
``~/Library/Application Support/NewsJSON``):

    config.json   pinned commit, repository URL, dependency fingerprint
    code/         detached checkout of the pinned commit (crawler + this script)
    publish/      clean clone used only to create and push the data commit
    venv/         interpreter with nate_news/requirements.txt installed
    runs/<id>/    per-run crawl output, metrics, and log
    state.json    last run / last success summary
    history.jsonl one line per run

Safety rules, in the order they are enforced by ``run``:

1. One run at a time (file lock shared by launchd and manual runs).
2. The code checkout must be exactly the pinned commit with no local changes,
   and the venv must match the recorded ``pip freeze`` fingerprint. Nothing is
   pulled or installed during a run. The fingerprint detects a changed package
   set; it does not prove file integrity of the installed packages.
3. Compared with the pinned commit, the remote tip may differ only in data
   and sub.yml-synced paths, and the two data paths must be regular files.
   Any other net difference means code changed upstream, so a person has to
   review it and bump the pin first.
4. Only output that passes ``validate_result.py --mode fallback`` is
   published. The previous publish it builds on is a byte copy of what the
   base tip publishes, taken before crawling; if that copy is not a JSON
   list, an empty list takes its place and the run says so. The results of
   this machine's own earlier incremental runs of the last 48 hours,
   including runs that were cut off and failed validation, are added after
   it (see nate_news/nate_news/reuse.py), so a company such a run read for an
   article the tip does not cover is not requested again; if that step
   fails, the run uses the previous publish alone.
5. If the remote branch moved while crawling, the run stops instead of
   rebuilding on top of it; a newer publish must never be overwritten by an
   older crawl. The push is fast-forward only.
"""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path


DEFAULT_STATE_DIR = Path.home() / "Library" / "Application Support" / "NewsJSON"
DEFAULT_REPO_URL = "https://github.com/Jay-Hong/News_JSON.git"
DEFAULT_BRANCH = "master"
# The scheduled publisher stands in for main.yml, whose commits are "News".
DEFAULT_COMMIT_MESSAGE = "News"

DATA_FILES = ("nate_news/nate_result.json", "newsURL.json")
# Paths sub.yml rewrites from the `news` submodule. They are not part of the
# reviewed crawler code, so they may drift from the pinned commit.
SYNCED_PATHS = ("news", "requirements.txt", "nate_news/nate_news/config.json")
ALLOWED_DRIFT = frozenset(DATA_FILES + SYNCED_PATHS)

CRAWL_SPIDER = "nate_news_fallback"
VALIDATION_MODE = "fallback"
# The spacing (randomized 1-3 s) of the full crawl that ran 14 times without a
# cut-off until 2026-11-17. The spider's own 0.5 s is for the runner fallback.
CRAWL_DOWNLOAD_DELAY = 2.0
# Up to 312 requests at about 2 s each do not fit the spider's 10 minutes.
CRAWL_BUDGET_SECONDS = 20 * 60
# Above the crawl budget, with room for startup and shutdown.
CRAWL_TIMEOUT_SECONDS = 40 * 60
ASSEMBLE_TIMEOUT_SECONDS = 120
PREVIOUS_FILE = "previous_newsURL.json"
SNAPSHOT_FILE = "fallback_snapshot.json"
# Earlier runs whose results may be added to the previous publish (see
# nate_news/nate_news/reuse.py). Article ids are limited to today and
# yesterday (KST) anyway, so older runs could add nothing.
REUSE_WINDOW_SECONDS = 48 * 60 * 60
REUSE_TIMEOUT_SECONDS = 120
MERGED_PREVIOUS_FILE = "previous_merged.json"
REUSE_REPORT_FILE = "reuse_report.json"
RUN_ID_FORMAT = "%Y%m%dT%H%M%S.%fZ"
KST = timezone(timedelta(hours=9))
GIT_TIMEOUT_SECONDS = 120
RUNS_TO_KEEP = 40
# A run right after wake can start before the network is back.
PREFLIGHT_FETCH_ATTEMPTS = 3
PREFLIGHT_FETCH_WAIT_SECONDS = 20
# time.monotonic() stops while a Mac sleeps; time.time() does not. A gap this
# large between the two since the ranking day was fixed (right before the
# reuse step and the crawl) means the machine was suspended
# (or the system clock was moved), so the ranking may be stale.
MAX_SUSPENDED_SECONDS = 120
# Inherited variables that could redirect git, Python, pip, or Scrapy away
# from the pinned checkout and venv.
STRIPPED_ENV_PREFIXES = ("GIT_", "PYTHON", "PIP_", "SCRAPY_")


class Abort(Exception):
    """Stop the run without publishing. ``reason`` is a stable short code."""

    def __init__(self, reason, detail=""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def utc_now():
    return datetime.now(timezone.utc)


@contextlib.contextmanager
def deferred_interrupt():
    """Hold Ctrl-C back until a multi-step switch has finished or rolled back."""
    pending = []
    try:
        previous = signal.signal(signal.SIGINT, lambda signum, frame: pending.append(signum))
    except ValueError:  # signals can only be handled from the main thread
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
        if pending:
            raise KeyboardInterrupt


def is_hex(value, length):
    return len(value) == length and all(char in "0123456789abcdef" for char in value)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Publisher:
    def __init__(self, state_dir, log=None):
        self.state_dir = Path(state_dir)
        self.code_dir = self.state_dir / "code"
        self.publish_dir = self.state_dir / "publish"
        self.venv_dir = self.state_dir / "venv"
        self.runs_dir = self.state_dir / "runs"
        self.config_path = self.state_dir / "config.json"
        self.state_path = self.state_dir / "state.json"
        self.history_path = self.state_dir / "history.jsonl"
        self.lock_path = self.state_dir / "lock"
        self._log_handle = log
        self.config = {}
        self._crawl_clock = None  # (wall, monotonic) when the ranking day is fixed

    # -- small helpers -----------------------------------------------------

    def log(self, message):
        line = f"{utc_now().strftime('%Y-%m-%dT%H:%M:%SZ')} {message}"
        print(line, flush=True)
        if self._log_handle is not None:
            self._log_handle.write(line + "\n")
            self._log_handle.flush()

    def run_command(self, args, *, cwd, timeout, reason, env=None, check=True):
        """Run a command, logging its output; raise Abort(reason) on failure."""
        full_env = self.child_env()
        if env:
            full_env.update(env)
        try:
            completed = subprocess.run(
                [str(arg) for arg in args],
                cwd=str(cwd),
                env=full_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise Abort(reason, f"timed out after {timeout}s: {args[0]}") from error
        except OSError as error:
            raise Abort(reason, f"cannot run {args[0]}: {error}") from error
        if check and completed.returncode != 0:
            tail = completed.stdout.strip().splitlines()[-15:]
            raise Abort(
                reason,
                f"exit {completed.returncode} from {' '.join(map(str, args[:4]))}: "
                + " | ".join(tail),
            )
        return completed

    def child_env(self):
        environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith(STRIPPED_ENV_PREFIXES)
        }
        # Never block an unattended run on a credential or passphrase prompt.
        environment["GIT_TERMINAL_PROMPT"] = "0"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return environment

    def pinned_code_env(self, run_dir):
        """Environment for processes that import the pinned checkout.

        Bytecode lookups go to a path inside this run's directory that is
        never created, so a stale (git-ignored) __pycache__ in the checkout
        cannot be imported. This is not applied to pip, which compiles the
        packages it installs into whatever prefix is set.
        """
        environment = self.child_env()
        environment["PYTHONPYCACHEPREFIX"] = str(run_dir / "pycache-never-written")
        return environment

    def git(self, cwd, *args, reason, check=True):
        return self.run_command(
            ["git", *args],
            cwd=cwd,
            timeout=GIT_TIMEOUT_SECONDS,
            reason=reason,
            check=check,
        )

    def load_config(self):
        try:
            self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise Abort("config_unreadable", str(error)) from error
        for key in ("pin_sha", "repo_url", "branch"):
            if not isinstance(self.config.get(key), str) or not self.config[key]:
                raise Abort("config_invalid", f"missing {key}")
        fingerprint = self.config.get("freeze_sha256")
        if not isinstance(fingerprint, str) or not is_hex(fingerprint, 64):
            # A missing fingerprint must not silently disable the check.
            raise Abort("config_invalid", "freeze_sha256 must be a SHA-256 hex digest")
        return self.config

    @contextlib.contextmanager
    def exclusive(self):
        """Hold the lock shared by run, init, and repin; fail fast if taken."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Abort("locked", "another run, init, or repin holds the lock") from None
            yield

    @property
    def python(self):
        return self.venv_dir / "bin" / "python"

    # -- preflight ---------------------------------------------------------

    def check_code_pin(self):
        pin = self.config["pin_sha"]
        head = self.git(
            self.code_dir, "rev-parse", "HEAD", reason="code_checkout_unreadable"
        ).stdout.strip()
        if head != pin:
            raise Abort("code_not_at_pin", f"HEAD {head} != pin {pin}")
        self.check_code_clean()

    def check_code_clean(self):
        dirty = self.git(
            self.code_dir,
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--ignore-submodules=all",
            reason="code_checkout_unreadable",
        ).stdout.strip()
        if dirty:
            raise Abort("code_checkout_dirty", dirty.splitlines()[0])

    def check_dependencies(self):
        expected = self.config["freeze_sha256"]
        actual = self.freeze_fingerprint(self.python)[1]
        if actual != expected:
            raise Abort("dependency_drift", f"fingerprint {actual} != {expected}")

    def remote_tip(self, attempts=1):
        branch = self.config["branch"]
        for attempt in range(1, attempts + 1):
            try:
                self.git(
                    self.publish_dir,
                    "fetch",
                    "--no-tags",
                    "--no-recurse-submodules",
                    "origin",
                    f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
                    reason="fetch_failed",
                )
                break
            except Abort as abort:
                if attempt == attempts:
                    raise
                self.log(f"fetch attempt {attempt} failed, retrying: {abort.detail[:160]}")
                time.sleep(PREFLIGHT_FETCH_WAIT_SECONDS)
        return self.git(
            self.publish_dir,
            "rev-parse",
            f"refs/remotes/origin/{branch}",
            reason="fetch_failed",
        ).stdout.strip()

    def check_remote_matches_pin(self, tip):
        """Refuse to publish next to code that nobody pinned (rule 3)."""
        pin = self.config["pin_sha"]
        ancestor = self.git(
            self.publish_dir,
            "merge-base",
            "--is-ancestor",
            pin,
            tip,
            reason="remote_history_unexpected",
            check=False,
        )
        if ancestor.returncode != 0:
            raise Abort(
                "remote_history_unexpected", f"pin {pin} is not an ancestor of {tip}"
            )
        changed = self.git(
            self.publish_dir,
            "diff",
            "--name-only",
            "--ignore-submodules=none",
            pin,
            tip,
            reason="remote_history_unexpected",
        ).stdout.split("\n")
        unexpected = sorted(path for path in changed if path and path not in ALLOWED_DRIFT)
        if unexpected:
            raise Abort(
                "remote_code_changed",
                "bump the pin after review; changed: " + ", ".join(unexpected[:8]),
            )
        self.check_data_paths_are_regular_files(tip)

    def check_data_paths_are_regular_files(self, tip):
        """A data path turned into a symlink would redirect our write."""
        for relative in DATA_FILES:
            listing = self.git(
                self.publish_dir, "ls-tree", tip, "--", relative,
                reason="remote_history_unexpected",
            ).stdout.strip()
            mode = listing.split(" ", 1)[0] if listing else "missing"
            if mode != "100644":
                raise Abort("data_path_not_regular_file", f"{relative} has mode {mode}")

    def check_not_suspended(self):
        """Abort if the wall clock ran ahead of the awake clock since the day was fixed."""
        if self._crawl_clock is None:
            return
        wall_start, awake_start = self._crawl_clock
        gap = (time.time() - wall_start) - (time.monotonic() - awake_start)
        if gap > MAX_SUSPENDED_SECONDS:
            raise Abort("suspended_or_clock_jump",
                        f"wall clock ran {int(gap)}s ahead of awake time since the day was fixed")

    def prepare_publish_clone(self, tip):
        """Put the publish clone exactly at tip, discarding leftovers.

        --force matters: staged files from an interrupted publish would
        otherwise block the checkout on every later run.
        """
        for args in (
            ("checkout", "--quiet", "--force", "--detach", tip),
            ("reset", "--quiet", "--hard", tip),
            ("clean", "--quiet", "-fd"),
        ):
            self.git(self.publish_dir, *args, reason="publish_prepare_failed")

    def check_push_auth(self, tip):
        """Dry-run push: fail before crawling if this context cannot authenticate."""
        self.git(self.publish_dir, "push", "--dry-run", "origin",
                 f"{tip}:refs/heads/{self.config['branch']}", reason="push_auth_failed")

    # -- crawl and validate ------------------------------------------------

    def copy_previous_publish(self, run_dir, tip):
        """Freeze what the base tip publishes, for the spider and the assembler.

        Called after the publish clone was reset to the base tip and the
        dry-run push, before crawling. The tree-mode check covers the tip's
        tree; this refuses a symlink on disk, and the blob comparison below is
        the real guard that the copy is exactly the tip's file.

        Returns (path given to the spider and the assembler, sha256 of the
        copy, whether the copy could be reused).
        """
        relative = "newsURL.json"
        source = self.publish_dir / relative
        if source.is_symlink() or not source.is_file():
            raise Abort("data_path_not_regular_file", relative)
        target = run_dir / PREVIOUS_FILE
        shutil.copyfile(source, target, follow_symlinks=False)
        copied = self.git(
            self.publish_dir, "hash-object", "--no-filters", "--", target,
            reason="publish_prepare_failed",
        ).stdout.strip()
        expected = self.git(
            self.publish_dir, "rev-parse", f"{tip}:{relative}", reason="publish_prepare_failed"
        ).stdout.strip()
        if copied != expected:
            raise Abort("previous_publish_mismatch", f"{copied} != {expected}")
        digest = sha256_file(target)
        try:
            usable = isinstance(json.loads(target.read_text(encoding="utf-8")), list)
        except ValueError:  # also covers UnicodeDecodeError
            usable = False
        if usable:
            return target, digest, True
        # The previous publish only decides which article pages can be skipped
        # and which fields can be reused. Without it every page is requested and
        # validation still guards the output; refusing to run would instead keep
        # a broken file published.
        self.log("previous publish is not a JSON list; crawling without it")
        self.notify("News_JSON previous publish unusable", "crawling without it")
        empty = run_dir / "previous_unusable.json"
        empty.write_text("[]\n", encoding="utf-8")
        return empty, digest, False

    @staticmethod
    def kst_day(moment):
        """The KST day of a UTC moment, as the spider's YYYYMMDD setting.

        A run fixes it once, right before the reuse step, and gives the same
        day to the reuse step and the spider.
        """
        return moment.astimezone(KST).strftime("%Y%m%d")

    def history_run_ids(self):
        """Run ids that reached record(), i.e. runs that finished.

        Lines that cannot be decoded or parsed (a torn append, a hand edit)
        are skipped, so a damaged history only means less reuse.
        """
        try:
            lines = self.history_path.read_bytes().splitlines()
        except FileNotFoundError:
            return set()
        run_ids = set()
        for line in lines:
            try:
                entry = json.loads(line.decode("utf-8"))
            # ValueError also covers UnicodeDecodeError; RecursionError is a
            # line nested too deeply to parse.
            except (ValueError, RecursionError):
                continue
            if isinstance(entry, dict) and isinstance(entry.get("run_id"), str):
                run_ids.add(entry["run_id"])
        return run_ids

    def reuse_candidates(self, run_id, started):
        """Earlier finished runs at most REUSE_WINDOW_SECONDS old, newest first.

        Returns (paths, counts of directories passed over, by reason).
        """
        finished = self.history_run_ids()
        candidates = []
        skipped = {}
        for path in self.runs_dir.iterdir():
            if path.name == run_id:
                continue
            try:
                when = datetime.strptime(path.name, RUN_ID_FORMAT).replace(tzinfo=timezone.utc)
            except ValueError:
                reason = "not_a_run"
            else:
                age = (started - when).total_seconds()
                if path.is_symlink() or not path.is_dir():
                    reason = "not_a_directory"
                elif path.name not in finished:
                    reason = "not_in_history"
                elif not 0 <= age <= REUSE_WINDOW_SECONDS:
                    reason = "outside_window"
                else:
                    candidates.append((when, path))
                    continue
            skipped[reason] = skipped.get(reason, 0) + 1
        return [path for _, path in sorted(candidates, reverse=True)], skipped

    def reuse_command(self, previous, kst_day, runs, merged, report):
        command = [
            str(self.python), "-m", "nate_news.reuse",
            "--previous", str(previous), "--kst-day", kst_day,
            "--out", str(merged), "--report", str(report),
        ]
        for path in runs:
            command += ["--run", str(path)]
        return command

    def add_local_results(self, run_dir, run_id, started, previous, kst_day):
        """Add recent local results to the previous publish (nate_news.reuse).

        Returns (path to give the spider and the assembler, record for the
        history). This step only adds optional reuse: if it fails, the run
        continues with the previous publish alone, which is exactly how runs
        worked before it existed, and validation still guards the output.
        Only a failure of this step (choosing the earlier runs, the reuse
        process, or its output) is absorbed.
        """
        merged = run_dir / MERGED_PREVIOUS_FILE
        report_path = run_dir / REUSE_REPORT_FILE
        record = {"kst_day": kst_day}
        try:
            runs, skipped = self.reuse_candidates(run_id, started)
            completed = self.run_command(
                self.reuse_command(previous, kst_day, runs, merged, report_path),
                cwd=self.code_dir / "nate_news",
                timeout=REUSE_TIMEOUT_SECONDS,
                reason="reuse_failed",
                env=self.pinned_code_env(run_dir),
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if not isinstance(json.loads(merged.read_text(encoding="utf-8")), list):
                raise ValueError("merged previous publish is not a JSON list")
            details = {
                "considered": len(runs),
                "skipped": skipped,
                "accepted": [
                    {key: entry[key] for key in ("run_id", "result_sha256", "metrics_sha256")}
                    for entry in report["accepted"]
                ],
                "rejected": dict(report["rejected_counts"]),
                "added_ids": report["added_ids"],
            }
            if details["accepted"]:
                used = merged
            else:
                # Nothing to add: the spider gets the previous publish itself.
                merged.unlink(missing_ok=True)
                used = previous
            details["input_sha256"] = sha256_file(used)
        except (Abort, OSError, ValueError, KeyError, TypeError) as error:
            detail = getattr(error, "detail", "") or str(error)
            self.log(f"reuse failed, using the previous publish alone: {detail[:300]}")
            self.notify("News_JSON reuse failed", "crawling with the previous publish alone")
            for partial in (merged, report_path):
                with contextlib.suppress(OSError):
                    partial.unlink()
            record["error"] = detail[:300]
            record["input_sha256"] = sha256_file(previous)
            return previous, record
        record.update(details)
        summary = completed.stdout.strip().splitlines()[-1:] or [""]
        self.log(f"reuse: {summary[0][:300]}")
        return used, record

    def crawl_command(self, run_id, snapshot, previous, kst_day):
        return [
            str(self.python),
            "-m",
            "scrapy",
            "crawl",
            CRAWL_SPIDER,
            "-s",
            f"NATE_FALLBACK_RUN_ID={run_id}",
            "-s",
            f"NATE_FALLBACK_SNAPSHOT_PATH={snapshot}",
            "-s",
            f"NATE_FALLBACK_PREVIOUS_PATH={previous}",
            "-s",
            f"NATE_FALLBACK_KST_DAY={kst_day}",
            # Command-line settings take priority over the spider's own.
            "-s",
            f"DOWNLOAD_DELAY={CRAWL_DOWNLOAD_DELAY}",
            "-s",
            f"CLOSESPIDER_TIMEOUT={CRAWL_BUDGET_SECONDS}",
            "-s",
            "TELNETCONSOLE_ENABLED=False",
        ]

    def assemble_command(self, run_id, snapshot, previous, result, metrics):
        return [
            str(self.python),
            "-m",
            "nate_news.fallback",
            "--snapshot",
            str(snapshot),
            "--previous",
            str(previous),
            "--run-id",
            run_id,
            "--out",
            str(result),
            "--metrics",
            str(metrics),
        ]

    def crawl(self, run_dir, run_id, previous, kst_day):
        snapshot = run_dir / SNAPSHOT_FILE
        environment = self.pinned_code_env(run_dir)
        # Stream to the file so a timed-out crawl still leaves its log behind.
        with open(run_dir / "spider.log", "w", encoding="utf-8") as spider_log:
            try:
                completed = subprocess.run(
                    self.crawl_command(run_id, snapshot, previous, kst_day),
                    cwd=str(self.code_dir / "nate_news"),
                    env=environment,
                    stdout=spider_log,
                    stderr=subprocess.STDOUT,
                    timeout=CRAWL_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired as error:
                raise Abort("crawl_failed", f"timed out after {CRAWL_TIMEOUT_SECONDS}s") from error
            except OSError as error:
                raise Abort("crawl_failed", f"cannot start crawler: {error}") from error
        if completed.returncode != 0:
            raise Abort("crawl_failed", f"crawler exit {completed.returncode}")
        if not snapshot.is_file():
            raise Abort("crawl_failed", "crawler wrote no snapshot")
        return self.assemble(run_dir, run_id, snapshot, previous)

    def assemble(self, run_dir, run_id, snapshot, previous):
        """Turn the raw snapshot into nate_result.json and crawl_metrics.json."""
        result = run_dir / "nate_result.json"
        metrics = run_dir / "crawl_metrics.json"
        completed = self.run_command(
            self.assemble_command(run_id, snapshot, previous, result, metrics),
            cwd=self.code_dir / "nate_news",
            timeout=ASSEMBLE_TIMEOUT_SECONDS,
            reason="assemble_failed",
            env=self.pinned_code_env(run_dir),
            check=False,
        )
        lines = completed.stdout.strip().splitlines()
        for line in lines[-12:]:
            self.log(f"assemble: {line[:300]}")
        if completed.returncode != 0:
            raise Abort("assemble_failed", (lines[-1:] or [f"exit {completed.returncode}"])[0][:300])
        if not result.is_file() or not metrics.is_file():
            raise Abort("assemble_failed", "assembler produced no result or metrics file")
        return result, metrics

    @staticmethod
    def summarize(result, metrics):
        """What the validated output holds, for history and status.

        A "complete" stop reason does not mean every article page was read:
        fields of articles that were already published, or that earlier local
        runs read, are reused.
        """
        items = json.loads(Path(result).read_text(encoding="utf-8"))
        stats = json.loads(Path(metrics).read_text(encoding="utf-8"))
        requests = stats.get("article_requests", {})
        return {
            "mode": VALIDATION_MODE,
            "stop_reason": stats.get("stop_reason"),
            "items": len(items),
            "images": sum(1 for item in items if item.get("image")),
            "article_requests": requests.get("attempted"),
            "article_pages_not_reached": requests.get("not_requested"),
        }

    def validate(self, result, metrics):
        completed = self.run_command(
            [self.python, "validate_result.py", "--mode", VALIDATION_MODE, result, metrics],
            cwd=self.code_dir / "nate_news",
            timeout=120,
            reason="validation_failed",
            env=self.pinned_code_env(result.parent),
            check=False,
        )
        summary = completed.stdout.strip().splitlines()[-1:] or [""]
        self.log(f"validate: {summary[0][:300]}")
        if completed.returncode != 0:
            raise Abort("validation_failed", summary[0][:300])

    # -- publish -----------------------------------------------------------

    def publish(self, result, base_tip):
        branch = self.config["branch"]
        tip = self.remote_tip()
        if tip != base_tip:
            # Rule 5: someone published while we crawled. Do not rebuild.
            raise Abort("remote_moved", f"{base_tip} -> {tip}")

        self.prepare_publish_clone(tip)
        root = self.publish_dir.resolve()
        for relative in DATA_FILES:
            target = self.publish_dir / relative
            # Defense in depth behind the tree-mode check: never write through
            # a symlink or outside the publish clone.
            if (
                target.is_symlink()
                or not target.is_file()
                or target.parent.resolve() != (root / relative).parent
            ):
                raise Abort("data_path_not_regular_file", relative)
            shutil.copyfile(result, target, follow_symlinks=False)
        self.git(self.publish_dir, "add", "--", *DATA_FILES, reason="publish_prepare_failed")

        staged = self.git(
            self.publish_dir,
            "diff",
            "--cached",
            "--name-only",
            "--ignore-submodules=all",
            reason="publish_prepare_failed",
        ).stdout.split()
        if not staged:
            self.log("publish: data identical to remote; nothing to commit")
            return None
        # One of the two may already match (for example after a manual fix);
        # the blob comparison below still proves both equal the validated file.
        if not set(staged) <= set(DATA_FILES):
            raise Abort("unexpected_staged_paths", ", ".join(staged))
        message = self.config.get("commit_message", DEFAULT_COMMIT_MESSAGE)
        self.git(self.publish_dir, "commit", "--quiet", "-m", message, reason="commit_failed")
        commit = self.git(self.publish_dir, "rev-parse", "HEAD", reason="commit_failed").stdout.strip()
        parent = self.git(self.publish_dir, "rev-parse", "HEAD^", reason="commit_failed").stdout.strip()
        if parent != tip:
            raise Abort("commit_parent_unexpected", f"{parent} != {tip}")
        # Compare the committed blobs themselves with the validated file.
        expected_blob = self.git(
            self.publish_dir, "hash-object", "--no-filters", "--", result, reason="commit_failed"
        ).stdout.strip()
        for relative in DATA_FILES:
            blob = self.git(
                self.publish_dir, "rev-parse", f"{commit}:{relative}", reason="commit_failed"
            ).stdout.strip()
            if blob != expected_blob:
                raise Abort("published_blob_mismatch", relative)
        # Last check before the data leaves the machine: fetch, reset, and
        # commit above take time, and the lid may have been closed meanwhile.
        self.check_not_suspended()
        # No leading '+': a non-fast-forward update is rejected by the remote.
        try:
            self.git(
                self.publish_dir,
                "push",
                "origin",
                f"{commit}:refs/heads/{branch}",
                reason="push_failed",
            )
        except Abort:
            # The client can fail (timeout, dropped connection) after the
            # server accepted the push. Ask the remote before calling it failed.
            if self.remote_tip() != commit:
                raise
            self.log("push reported an error but the remote has the commit")
        return commit

    # -- bookkeeping -------------------------------------------------------

    def record(self, entry):
        state = self.read_state()
        state["last_run"] = entry
        if entry["outcome"] in ("published", "unchanged"):
            state["last_success"] = entry
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.state_path)
        with open(self.history_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def read_state(self):
        """Return state.json as a dict; a damaged file must not break a run."""
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return state if isinstance(state, dict) else {}

    def prune_runs(self):
        runs = sorted(path for path in self.runs_dir.iterdir() if path.is_dir())
        for stale in runs[:-RUNS_TO_KEEP]:
            shutil.rmtree(stale, ignore_errors=True)

    def notify(self, title, text):
        if sys.platform != "darwin" or self.config.get("notify") is False:
            return
        script = f"display notification {json.dumps(text)} with title {json.dumps(title)}"
        try:
            subprocess.run(["osascript", "-e", script], timeout=10, check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            pass  # A missing notification must never change the run outcome.

    # -- commands ----------------------------------------------------------

    def run(self):
        try:
            with self.exclusive():
                return self._run_locked()
        except Abort as abort:
            if abort.reason != "locked":
                raise
            print("another run holds the lock; exiting", flush=True)
            return 3

    def _run_locked(self):
        started = utc_now()
        run_id = started.strftime(RUN_ID_FORMAT)
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        entry = {"run_id": run_id, "started": started.isoformat(), "outcome": "failed"}
        with open(run_dir / "publish.log", "a", encoding="utf-8") as handle:
            self._log_handle = handle
            try:
                self.load_config()
                entry["code_sha"] = self.config["pin_sha"]
                self.check_code_pin()
                self.check_dependencies()
                base_tip = self.remote_tip(attempts=PREFLIGHT_FETCH_ATTEMPTS)
                entry["remote_tip"] = base_tip
                self.check_remote_matches_pin(base_tip)
                # Everything that can fail without Nate is checked before crawling.
                self.prepare_publish_clone(base_tip)
                self.check_push_auth(base_tip)
                previous, entry["previous_sha256"], entry["previous_usable"] = (
                    self.copy_previous_publish(run_dir, base_tip)
                )
                self.log(f"preflight ok: pin {self.config['pin_sha'][:8]}, remote {base_tip[:8]},"
                         f" previous publish {entry['previous_sha256'][:12]}"
                         + ("" if entry["previous_usable"] else " (unusable, replaced by an empty list)"))

                # The ranking day is fixed here, and the suspension check covers
                # everything from this moment to the push, so a machine that
                # sleeps past midnight cannot publish the previous day's ranking.
                self._crawl_clock = (time.time(), time.monotonic())
                kst_day = self.kst_day(utc_now())
                previous, entry["reuse"] = self.add_local_results(
                    run_dir, run_id, started, previous, kst_day
                )
                result, metrics = self.crawl(run_dir, run_id, previous, kst_day)
                self.validate(result, metrics)
                entry["data_sha256"] = sha256_file(result)
                entry["crawl"] = self.summarize(result, metrics)
                self.log(f"crawl: {json.dumps(entry['crawl'])}")
                self.check_not_suspended()  # checked again right before the push

                commit = self.publish(result, base_tip)
                entry["outcome"] = "published" if commit else "unchanged"
                entry["published_commit"] = commit
                self.log(f"outcome: {entry['outcome']} {commit or ''}".rstrip())
            except Abort as abort:
                entry["reason"] = abort.reason
                entry["detail"] = abort.detail[:500]
                self.log(f"ABORT {abort.reason}: {abort.detail[:500]}")
                self.notify("News_JSON publish failed", abort.reason)
            except Exception as error:  # a bug or an unexpected OS error
                entry["reason"] = "internal_error"
                entry["detail"] = f"{type(error).__name__}: {error}"[:500]
                self.log(f"ABORT internal_error: {entry['detail']}")
                self.log(traceback.format_exc())
                self.notify("News_JSON publish failed", "internal_error")
            finally:
                entry["finished"] = utc_now().isoformat()
                try:
                    self.record(entry)
                    self.prune_runs()
                except Exception as error:
                    # Bookkeeping must not hide or change the run outcome.
                    self.log(f"bookkeeping failed: {type(error).__name__}: {error}")
                self._log_handle = None
        return 0 if entry["outcome"] in ("published", "unchanged") else 1

    @staticmethod
    def require_full_sha(sha):
        # An abbreviated SHA or a branch name could resolve to something else
        # later; the pin must name exactly one reviewed commit.
        if not is_hex(sha, 40):
            raise Abort("pin_not_full_sha", sha)

    def init(self, sha, repo_url, branch, base_python):
        self.require_full_sha(sha)
        with self.exclusive():
            created = (self.code_dir, self.publish_dir, self.venv_dir)
            existing = [str(path) for path in (*created, self.config_path) if path.exists()]
            if existing:
                raise Abort("already_initialized", ", ".join(existing))
            self.runs_dir.mkdir(exist_ok=True)
            self.config = {"pin_sha": sha, "repo_url": repo_url, "branch": branch}
            try:
                for target in (self.code_dir, self.publish_dir):
                    self.git(self.state_dir, "clone", "--quiet", "--no-recurse-submodules",
                             repo_url, target, reason="clone_failed")
                self.git(self.code_dir, "checkout", "--quiet", "--detach", sha,
                         reason="pin_checkout_failed")
                self.config["freeze_sha256"] = self.build_venv(base_python, self.venv_dir)
                self.write_config()
            except BaseException:
                # Remove only what this call created, so init can be rerun.
                for path in created:
                    shutil.rmtree(path, ignore_errors=True)
                raise
            self.log(f"initialized {self.state_dir} at pin {sha}")

    def repin(self, sha, base_python):
        """Move the pin to another reviewed commit.

        Code, venv, and config switch together. An error or Ctrl-C while the
        new venv is being built restores the previous combination. During the
        final swap Ctrl-C is held back until the swap has completed or been
        rolled back. A process killed outright mid-switch is not covered: keep the
        state directory as it is and inspect code/, venv*/, and config.json
        before repairing by hand, because run and repin both refuse a checkout
        that does not match the pin.
        """
        self.require_full_sha(sha)
        with self.exclusive():  # never swap code or venv under a running crawl
            self.load_config()
            previous_config = dict(self.config)
            previous = previous_config["pin_sha"]
            self.check_code_pin()
            staging = self.state_dir / "venv.new"
            retired = self.state_dir / "venv.old"
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(retired, ignore_errors=True)

            def restore_checkout():
                shutil.rmtree(staging, ignore_errors=True)
                self.git(self.code_dir, "checkout", "--quiet", "--detach", previous,
                         reason="pin_restore_failed")

            # Phase 1 (interruptible): only the checkout and a side directory
            # change, so undoing it is always the same two steps.
            try:
                self.git(self.code_dir, "fetch", "--quiet", "--no-tags", "origin",
                         reason="fetch_failed")
                self.git(self.code_dir, "checkout", "--quiet", "--detach", sha,
                         reason="pin_checkout_failed")
                fingerprint = self.build_venv(base_python, staging)
            except BaseException as error:
                restore_checkout()
                if not isinstance(error, Exception) or isinstance(error, Abort):
                    raise
                raise Abort("repin_failed", f"{type(error).__name__}: {error}") from error

            # Phase 2: swap the venv and save the config. Ctrl-C is held back
            # so the switch either completes or is rolled back as a whole; the
            # atomic config save is the commit point.
            with deferred_interrupt():
                old_venv_retired = False
                try:
                    self.venv_dir.rename(retired)
                    old_venv_retired = True
                    # Only `python -m ...` is used from the venv, so renaming is safe.
                    staging.rename(self.venv_dir)
                    self.config = dict(previous_config, pin_sha=sha, freeze_sha256=fingerprint)
                    self.write_config()
                except Exception as error:
                    self.config = previous_config
                    if old_venv_retired:
                        shutil.rmtree(self.venv_dir, ignore_errors=True)
                        retired.rename(self.venv_dir)
                    restore_checkout()
                    if isinstance(error, Abort):
                        raise
                    raise Abort("repin_failed", f"{type(error).__name__}: {error}") from error
            shutil.rmtree(retired, ignore_errors=True)
            self.log(f"pin moved {previous[:8]} -> {sha}")

    def freeze_fingerprint(self, python):
        frozen = self.run_command([python, "-m", "pip", "freeze", "--all"],
                                  cwd=self.state_dir, timeout=120,
                                  reason="dependency_check_failed").stdout
        # The venv's python is a link to a base interpreter that can be
        # upgraded behind its back, so identify that interpreter as well.
        interpreter = self.run_command(
            [python, "-c",
             "import os, sys; print(sys.version); print(os.path.realpath(sys.base_prefix))"],
            cwd=self.state_dir, timeout=60, reason="dependency_check_failed").stdout
        listing = frozen + "# interpreter\n" + interpreter
        return listing, hashlib.sha256(listing.encode("utf-8")).hexdigest()

    def build_venv(self, base_python, venv_dir):
        """Create venv_dir from the checked-out requirements; return its fingerprint."""
        self.run_command([base_python, "-m", "venv", venv_dir],
                         cwd=self.state_dir, timeout=300, reason="venv_failed")
        python = venv_dir / "bin" / "python"
        self.run_command(
            [python, "-m", "pip", "install", "--quiet", "-r",
             self.code_dir / "nate_news" / "requirements.txt"],
            cwd=self.state_dir, timeout=900, reason="pip_install_failed")
        self.run_command([python, "-m", "pip", "check"],
                         cwd=self.state_dir, timeout=120, reason="pip_check_failed")
        frozen, fingerprint = self.freeze_fingerprint(python)
        # Kept inside the venv so the listing always travels with it.
        (venv_dir / "requirements.lock.txt").write_text(frozen, encoding="utf-8")
        return fingerprint

    def write_config(self):
        # Write-then-rename so a failed save leaves the previous config intact.
        temporary = self.config_path.with_suffix(".json.tmp")
        try:
            temporary.write_text(
                json.dumps(self.config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.config_path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise

    def check_auth(self):
        """Dry-run push: checks authentication only, not server-side push rules."""
        self.load_config()
        tip = self.remote_tip()
        self.check_push_auth(tip)
        self.log(f"dry-run push authentication ok (remote {tip[:8]})")

    def status(self):
        self.load_config()
        state = self.read_state()
        print(json.dumps({"pin_sha": self.config["pin_sha"], **state}, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.environ.get("NEWS_JSON_STATE_DIR", DEFAULT_STATE_DIR)),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="crawl, validate, and publish once")
    commands.add_parser("check-auth", help="dry-run push to verify credentials")
    commands.add_parser("status", help="print pin and last run summary")
    for name in ("init", "repin"):
        sub = commands.add_parser(name, help=f"{name} the pinned checkout and venv")
        sub.add_argument("--sha", required=True, help="reviewed commit to pin (full SHA)")
        sub.add_argument("--python", default=sys.executable, help="interpreter for the venv")
        if name == "init":
            sub.add_argument("--repo-url", default=DEFAULT_REPO_URL)
            sub.add_argument("--branch", default=DEFAULT_BRANCH)
    args = parser.parse_args(argv)

    publisher = Publisher(args.state_dir)
    try:
        if args.command == "run":
            # A scheduled run must be the pinned script under the pinned venv;
            # otherwise the recorded code_sha would not describe what ran.
            script = (publisher.code_dir / "tools" / "local_publish.py").resolve()
            interpreter_dir = (publisher.venv_dir / "bin").resolve()
            if (Path(__file__).resolve() != script
                    or Path(sys.executable).parent.resolve() != interpreter_dir):
                raise Abort("not_pinned_invocation",
                            f"run {publisher.venv_dir}/bin/python {script} instead")
            return publisher.run()
        if args.command == "init":
            publisher.init(args.sha, args.repo_url, args.branch, args.python)
        elif args.command == "repin":
            publisher.repin(args.sha, args.python)
        elif args.command == "check-auth":
            publisher.check_auth()
        elif args.command == "status":
            publisher.status()
    except Abort as abort:
        print(f"ABORT {abort.reason}: {abort.detail}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
