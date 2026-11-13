"""Crawl, validate, and publish Nate news from a local machine.

GitHub-hosted runners lose access to Nate mid-crawl (see CLAUDE.md), so this
script lets a scheduled local job (launchd) do what main.yml does. It only uses
the standard library and is meant to be run from a pinned checkout:

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
4. Only output that passes validate_result.py is published.
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
import subprocess
import sys
from datetime import datetime, timezone
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

# settings.py closes the spider after 35 minutes; leave room for startup.
CRAWL_TIMEOUT_SECONDS = 40 * 60
GIT_TIMEOUT_SECONDS = 120
RUNS_TO_KEEP = 40


class Abort(Exception):
    """Stop the run without publishing. ``reason`` is a stable short code."""

    def __init__(self, reason, detail=""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def utc_now():
    return datetime.now(timezone.utc)


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

    # -- small helpers -----------------------------------------------------

    def log(self, message):
        line = f"{utc_now().strftime('%Y-%m-%dT%H:%M:%SZ')} {message}"
        print(line, flush=True)
        if self._log_handle is not None:
            self._log_handle.write(line + "\n")
            self._log_handle.flush()

    def run_command(self, args, *, cwd, timeout, reason, env=None, check=True):
        """Run a command, logging its output; raise Abort(reason) on failure."""
        full_env = dict(os.environ)
        # Never block an unattended run on a credential or passphrase prompt.
        full_env["GIT_TERMINAL_PROMPT"] = "0"
        full_env["PYTHONDONTWRITEBYTECODE"] = "1"
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
            raise Abort("dependency_drift", f"pip freeze {actual} != {expected}")

    def remote_tip(self):
        branch = self.config["branch"]
        self.git(
            self.publish_dir,
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            "origin",
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
            reason="fetch_failed",
        )
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

    # -- crawl and validate ------------------------------------------------

    def crawl_command(self, result, metrics):
        feeds = json.dumps(
            {
                str(result): {
                    "format": "json",
                    "encoding": "utf-8",
                    "indent": 4,
                    "overwrite": True,
                }
            }
        )
        return [
            str(self.python),
            "-m",
            "scrapy",
            "crawl",
            "nate_news_rank",
            "-s",
            f"FEEDS={feeds}",
            "-s",
            f"NATE_CRAWL_METRICS_PATH={metrics}",
            "-s",
            "TELNETCONSOLE_ENABLED=False",
        ]

    def crawl(self, run_dir):
        result = run_dir / "nate_result.json"
        metrics = run_dir / "crawl_metrics.json"
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        # Stream to the file so a timed-out crawl still leaves its log behind.
        with open(run_dir / "spider.log", "w", encoding="utf-8") as spider_log:
            try:
                completed = subprocess.run(
                    self.crawl_command(result, metrics),
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
        if not result.is_file() or not metrics.is_file():
            raise Abort("crawl_failed", "crawler produced no result or metrics file")
        return result, metrics

    def validate(self, result, metrics):
        completed = self.run_command(
            [self.python, "validate_result.py", result, metrics],
            cwd=self.code_dir / "nate_news",
            timeout=120,
            reason="validation_failed",
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

        self.git(self.publish_dir, "checkout", "--quiet", "--detach", tip, reason="publish_prepare_failed")
        self.git(self.publish_dir, "reset", "--quiet", "--hard", tip, reason="publish_prepare_failed")
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
        if sorted(staged) != sorted(DATA_FILES):
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
        # No leading '+': a non-fast-forward update is rejected by the remote.
        self.git(
            self.publish_dir,
            "push",
            "origin",
            f"{commit}:refs/heads/{branch}",
            reason="push_failed",
        )
        return commit

    # -- bookkeeping -------------------------------------------------------

    def record(self, entry):
        state = {}
        if self.state_path.is_file():
            try:
                state = json.loads(self.state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                state = {}
        state["last_run"] = entry
        if entry["outcome"] in ("published", "unchanged"):
            state["last_success"] = entry
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.state_path)
        with open(self.history_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

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
        run_id = started.strftime("%Y%m%dT%H%M%S.%fZ")
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
                base_tip = self.remote_tip()
                entry["remote_tip"] = base_tip
                self.check_remote_matches_pin(base_tip)
                self.log(f"preflight ok: pin {self.config['pin_sha'][:8]}, remote {base_tip[:8]}")

                result, metrics = self.crawl(run_dir)
                self.validate(result, metrics)
                entry["data_sha256"] = sha256_file(result)

                commit = self.publish(result, base_tip)
                entry["outcome"] = "published" if commit else "unchanged"
                entry["published_commit"] = commit
                self.log(f"outcome: {entry['outcome']} {commit or ''}".rstrip())
            except Abort as abort:
                entry["reason"] = abort.reason
                entry["detail"] = abort.detail[:500]
                self.log(f"ABORT {abort.reason}: {abort.detail[:500]}")
                self.notify("News_JSON publish failed", abort.reason)
            finally:
                entry["finished"] = utc_now().isoformat()
                self.record(entry)
                self.prune_runs()
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
            if self.config_path.exists():
                raise Abort("already_initialized", str(self.config_path))
            self.runs_dir.mkdir(exist_ok=True)
            self.config = {"pin_sha": sha, "repo_url": repo_url, "branch": branch}
            for target in (self.code_dir, self.publish_dir):
                self.git(self.state_dir, "clone", "--quiet", "--no-recurse-submodules",
                         repo_url, target, reason="clone_failed")
            self.git(self.code_dir, "checkout", "--quiet", "--detach", sha,
                     reason="pin_checkout_failed")
            self.config["freeze_sha256"] = self.build_venv(base_python, self.venv_dir)
            self.write_config()
            self.log(f"initialized {self.state_dir} at pin {sha}")

    def repin(self, sha, base_python):
        """Move the pin to another reviewed commit.

        Code, venv, and config switch together. If any step raises, the
        previous combination is restored before the error is reported. A
        process killed mid-switch is not covered; rerun repin in that case.
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
            old_venv_retired = False
            try:
                self.git(self.code_dir, "fetch", "--quiet", "--no-tags", "origin",
                         reason="fetch_failed")
                self.git(self.code_dir, "checkout", "--quiet", "--detach", sha,
                         reason="pin_checkout_failed")
                fingerprint = self.build_venv(base_python, staging)
                self.venv_dir.rename(retired)
                old_venv_retired = True
                # Only `python -m ...` is used from the venv, so renaming is safe.
                staging.rename(self.venv_dir)
                self.config = dict(previous_config, pin_sha=sha, freeze_sha256=fingerprint)
                self.write_config()  # atomic: the last step that can fail
            except Exception as error:
                self.config = previous_config
                if old_venv_retired:
                    shutil.rmtree(self.venv_dir, ignore_errors=True)
                    retired.rename(self.venv_dir)
                shutil.rmtree(staging, ignore_errors=True)
                self.git(self.code_dir, "checkout", "--quiet", "--detach", previous,
                         reason="pin_restore_failed")
                if isinstance(error, Abort):
                    raise
                raise Abort("repin_failed", f"{type(error).__name__}: {error}") from error
            shutil.rmtree(retired, ignore_errors=True)
            self.log(f"pin moved {previous[:8]} -> {sha}")

    def freeze_fingerprint(self, python):
        frozen = self.run_command([python, "-m", "pip", "freeze", "--all"],
                                  cwd=self.state_dir, timeout=120,
                                  reason="dependency_check_failed").stdout
        return frozen, hashlib.sha256(frozen.encode("utf-8")).hexdigest()

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
        self.git(self.publish_dir, "push", "--dry-run", "origin",
                 f"{tip}:refs/heads/{self.config['branch']}", reason="push_auth_failed")
        self.log(f"dry-run push authentication ok (remote {tip[:8]})")

    def status(self):
        self.load_config()
        state = {}
        if self.state_path.is_file():
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
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
