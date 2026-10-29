"""Baton and lease of the btlab build, shared through git by the PC's runner and by cloud sessions.

buildctl/baton.json says who may build: "local" (the owner's PC) or "cloud".
buildctl/lease.json says which cloud session is building now, with a heartbeat.

Cloud sessions use it as a command (CLOUD.md); it makes every commit and push of a cloud session:
    python3 buildctl/cloud_lease.py take [--session ID]
    python3 buildctl/cloud_lease.py checkpoint "<step>"
    python3 buildctl/cloud_lease.py finish N
    python3 buildctl/cloud_lease.py beat
    python3 buildctl/cloud_lease.py release "<reason>"
    python3 buildctl/cloud_lease.py status
run_prompts.py on the PC imports the parsing helpers. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BATON = "buildctl/baton.json"
LEASE = "buildctl/lease.json"
BRANCH = "master"
REMOTE = "origin"
# A live session beats at least every 45 minutes; a lease silent for this long belongs to a session that is gone.
LEASE_STALE = timedelta(hours=4)
# Tolerated clock difference between the PC and cloud machines when comparing a heartbeat with a hand-over.
CLOCK_SKEW = timedelta(minutes=10)
# Inside .git: it lives as long as this clone and is never committed.
SESSION_FILE = Path(os.environ.get("BTLAB_SESSION_FILE") or ROOT / ".git" / "btlab-cloud-session")
NO_PROMPT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
PUSH_TRIES = 3

OK, STOP_BATON, DONE, LEASE_HELD, LEASE_LOST, GIT_ERROR, NO_SESSION = 0, 10, 11, 12, 13, 14, 15


class Exit(Exception):
    """Ends the command with an exit code and a message for the session."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


# ----- parsing helpers, also used by run_prompts.py -----

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_time(text: str | None) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def parse_json(text: str | None) -> dict | None:
    try:
        data = json.loads(text) if text else None
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def parse_baton(text: str | None) -> dict:
    # A repository without a baton file was never handed to the cloud.
    data = parse_json(text) or {}
    return {**data, "holder": "cloud" if data.get("holder") == "cloud" else "local"}


def parse_lease(text: str | None) -> dict | None:
    data = parse_json(text)
    return data if data and data.get("session") else None


def lease_age(lease: dict | None, now: datetime) -> timedelta | None:
    beat = parse_time(lease.get("heartbeat")) if lease else None
    return now - beat if beat else None


def lease_fresh(lease: dict | None, now: datetime) -> bool:
    age = lease_age(lease, now)
    return age is not None and age < LEASE_STALE


def lease_active(lease: dict | None, baton: dict, now: datetime) -> bool:
    """A fresh lease, unless the PC handed the build to the cloud again after that session last showed signs of life."""
    if not lease_fresh(lease, now):
        return False
    if baton.get("holder") != "cloud":
        return True
    since, beat = parse_time(baton.get("since")), parse_time(lease.get("heartbeat"))
    return not (since and beat and beat < since - CLOCK_SKEW)


def baton_record(holder: str, by: str, now: datetime) -> dict:
    return {"holder": holder, "since": iso(now), "by": by}


def prompt_titles(prompts_text: str) -> dict[int, str]:
    text = prompts_text.replace("\r\n", "\n")
    return {int(n): title.strip() for n, title in re.findall(r"^## Prompt (\d+): (.+?)\n\n```text\n", text, re.M)}


def committed_numbers(subjects: str) -> set[int]:
    return {int(n) for n in re.findall(r"^Prompt (\d+):", subjects, re.M)}


def next_prompt(prompts_text: str, subjects: str) -> int | None:
    done = committed_numbers(subjects)
    return next((n for n in sorted(prompt_titles(prompts_text)) if n not in done), None)


def dump(data: dict) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


# ----- git -----

def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, **NO_PROMPT_ENV},
    )
    if check and done.returncode != 0:
        raise Exit(GIT_ERROR, f"git {' '.join(args)} failed: {(done.stderr or done.stdout).strip()}")
    return done


def subjects() -> str:
    return git("log", "--format=%s", check=False).stdout


def read_file(relative: str) -> str | None:
    path = ROOT / relative
    return path.read_text(encoding="utf-8") if path.exists() else None


def write_file(relative: str, text: str | None) -> None:
    path = ROOT / relative
    if text is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(text, encoding="utf-8")


def fetch() -> None:
    for attempt in range(PUSH_TRIES):
        if git("fetch", "--quiet", REMOTE, BRANCH, check=False).returncode == 0:
            return
        time.sleep(10 * (attempt + 1))
    raise Exit(GIT_ERROR, "git fetch kept failing.")


def merge_remote() -> None:
    """Merges origin/master into HEAD. A conflict means two writers touched the same files: rescue and stop."""
    done = git("merge", "--no-edit", f"{REMOTE}/{BRANCH}", check=False)
    if done.returncode != 0:
        git("merge", "--abort", check=False)
        raise Exit(GIT_ERROR, f"Merging {REMOTE}/{BRANCH} failed: {(done.stderr or done.stdout).strip()}")


def rejected(done: subprocess.CompletedProcess[str]) -> bool:
    return bool(re.search(r"rejected|non-fast-forward|fetch first", done.stderr or "", re.I))


def push_once() -> bool:
    """True when pushed, False when rejected because master moved; raises on other failures after retries."""
    for attempt in range(PUSH_TRIES):
        done = git("push", "--quiet", REMOTE, f"HEAD:{BRANCH}", check=False)
        if done.returncode == 0:
            return True
        if rejected(done):
            return False
        time.sleep(10 * (attempt + 1))
    raise Exit(GIT_ERROR, f"git push kept failing: {(done.stderr or '').strip()}")


def rescue(session: str) -> str:
    branch = f"claude/btlab-rescue-{session}"
    git("push", "--quiet", REMOTE, f"HEAD:refs/heads/{branch}", check=False)
    return branch


def ensure_identity() -> None:
    # Cloud machines usually configure git; a commit must not fail where one does not.
    if not git("config", "user.email", check=False).stdout.strip():
        git("config", "user.name", "btlab cloud build")
        git("config", "user.email", "build@localhost")


def commit_all(subject: str, body: str | None = None) -> bool:
    git("add", "-A")
    if not git("status", "--porcelain").stdout.strip():
        return False
    git("commit", "-q", "-m", subject, *(["-m", body] if body else []))
    return True


# ----- the lease protocol -----

def load_session() -> str:
    session = SESSION_FILE.read_text(encoding="utf-8").strip() if SESSION_FILE.exists() else ""
    if not session:
        raise Exit(NO_SESSION, "No session id here. Run: python3 buildctl/cloud_lease.py take --session <your id>")
    return session


def save_session(session: str) -> None:
    SESSION_FILE.write_text(session, encoding="utf-8")


def prompts_text() -> str:
    return read_file("PROMPTS.md") or ""


def upcoming() -> int | None:
    return next_prompt(prompts_text(), subjects())


def write_lease(session: str, now: datetime, prompt: int | None) -> None:
    lease = parse_lease(read_file(LEASE)) or {}
    taken = lease.get("taken_at") if lease.get("session") == session else None
    write_file(LEASE, dump({"session": session, "taken_at": taken or iso(now), "heartbeat": iso(now), "prompt": prompt}))


def remote_lease() -> dict | None:
    done = git("show", f"{REMOTE}/{BRANCH}:{LEASE}", check=False)
    return parse_lease(done.stdout) if done.returncode == 0 else None


def remote_baton() -> dict:
    done = git("show", f"{REMOTE}/{BRANCH}:{BATON}", check=False)
    return parse_baton(done.stdout if done.returncode == 0 else None)


def lost(session: str) -> Exit:
    return Exit(LEASE_LOST, f"Another session took the lease. Your work is on branch {rescue(session)}. Stop now.")


def hand_back(session: str) -> Exit:
    """The PC took the baton back: push what this session has, release the lease, and tell it to stop."""
    commit_all(f"WIP prompt {upcoming()}: handed back to the PC")
    if (parse_lease(read_file(LEASE)) or {}).get("session") == session:
        write_file(LEASE, None)
        commit_all(f"Lease released by {session}: the PC holds the baton")
    publish(session, check_lease=False)
    return Exit(STOP_BATON, "The PC took the baton back. Your work is pushed and the lease released. Stop now.")


def check_holder(session: str) -> None:
    if parse_baton(read_file(BATON))["holder"] != "cloud":
        raise hand_back(session)
    if (parse_lease(read_file(LEASE)) or {}).get("session") != session:
        raise lost(session)


def merge_or_rescue(session: str) -> None:
    try:
        merge_remote()
    except Exit as failure:
        if (remote_lease() or {}).get("session") != session:
            raise lost(session) from None
        raise Exit(GIT_ERROR, f"{failure} Your work is on branch {rescue(session)}. Stop and report it.") from None


def publish(session: str, check_lease: bool = True) -> None:
    """Pushes HEAD to master. When master moved, merges it, re-checks the baton and the lease, and pushes again."""
    for _ in range(PUSH_TRIES):
        if push_once():
            return
        fetch()
        merge_or_rescue(session)
        if check_lease:
            check_holder(session)
    raise Exit(GIT_ERROR, f"master kept moving. Your work is on branch {rescue(session)}. Stop and report it.")


def take(session: str | None, now: datetime) -> str:
    if git("status", "--porcelain").stdout.strip():
        raise Exit(GIT_ERROR, "take runs only at the start of a session, on a clean clone. Stop and report it.")
    fetch()
    if git("merge", "--ff-only", "--quiet", f"{REMOTE}/{BRANCH}", check=False).returncode != 0:
        raise Exit(GIT_ERROR, f"This clone has diverged from {REMOTE}/{BRANCH}. Stop and report it.")
    for _ in range(PUSH_TRIES):
        if parse_baton(read_file(BATON))["holder"] != "cloud":
            raise Exit(STOP_BATON, "The PC holds the baton (buildctl/baton.json). Nothing to do here. Stop now.")
        if 1 not in committed_numbers(subjects()):
            raise Exit(STOP_BATON, "Prompt 1 must run on the PC first. Stop now.")
        prompt = upcoming()
        if prompt is None:
            raise Exit(DONE, "Every prompt is committed. Nothing to do. Stop now.")
        lease = parse_lease(read_file(LEASE))
        if lease and lease_active(lease, parse_baton(read_file(BATON)), now) and lease.get("session") != session:
            minutes = int(lease_age(lease, now).total_seconds() // 60)
            raise Exit(LEASE_HELD, f"Session {lease['session']} holds the lease (heartbeat {minutes} min ago). Stop now.")
        session = session or secrets.token_hex(4)
        write_lease(session, now, prompt)
        commit_all(f"Lease: cloud session {session} takes prompt {prompt}")
        if push_once():
            save_session(session)
            return f"OK. You hold the lease as session {session}. Continue with prompt {prompt}."
        # Someone pushed first. This clone's lease commit was never published, so it is dropped.
        fetch()
        git("reset", "--hard", "--quiet", f"{REMOTE}/{BRANCH}")
    raise Exit(GIT_ERROR, "master kept moving while taking the lease. Stop and report it.")


def checkpoint(step: str, now: datetime) -> str:
    session = load_session()
    write_lease(session, now, upcoming())
    commit_all(f"WIP prompt {upcoming()}: {step}")
    publish(session)
    return "Checkpoint pushed. Continue."


def beat(now: datetime) -> str:
    session = load_session()
    write_lease(session, now, upcoming())
    commit_all(f"Lease heartbeat ({session})")
    publish(session)
    return "Heartbeat pushed. Continue."


def finish(number: int, now: datetime) -> str:
    session = load_session()
    titles = prompt_titles(prompts_text())
    if number != upcoming():
        raise Exit(2, f"Prompt {number} is not the next prompt; the next is {upcoming()}. Nothing was committed.")
    done = committed_numbers(subjects()) | {number}
    later = next((n for n in sorted(titles) if n not in done), None)
    if later is None:
        write_file(LEASE, None)
    else:
        write_lease(session, now, later)
    git("add", "-A")
    git("commit", "-q", "--allow-empty", "-m", f"Prompt {number}: {titles[number]}",
        "-m", f"Built by Claude Code (cloud session {session}) following CLOUD.md.")
    publish(session, check_lease=later is not None)
    if later is None:
        raise Exit(DONE, f"Prompt {number} committed and pushed. Every prompt is committed. Stop now.")
    return f"Prompt {number} committed and pushed. Continue with prompt {later}."


def release(reason: str, now: datetime) -> str:
    session = load_session()
    commit_all(f"WIP prompt {upcoming()}: {reason}")
    fetch()
    merge_or_rescue(session)
    if (parse_lease(read_file(LEASE)) or {}).get("session") != session:
        raise lost(session)
    write_file(LEASE, None)
    commit_all(f"Lease released by {session}: {reason}")
    publish(session, check_lease=False)
    return "Lease released and work pushed. Stop now."


def status(now: datetime) -> str:
    fetch()
    lease = remote_lease()
    baton = remote_baton()
    log = git("log", "--format=%s", f"{REMOTE}/{BRANCH}", check=False).stdout
    titles = prompt_titles(prompts_text())
    done = committed_numbers(log) & set(titles)
    lines = [
        f"Prompts: {len(done)} of {len(titles)} committed; next: {next_prompt(prompts_text(), log)}",
        f"Baton: {baton['holder']} (since {baton.get('since', '?')}, by {baton.get('by', '?')})",
    ]
    if lease:
        age = int(lease_age(lease, now).total_seconds() // 60)
        state = "active" if lease_active(lease, baton, now) else "inactive"
        lines.append(f"Lease: session {lease['session']}, heartbeat {age} min ago ({state}), prompt {lease.get('prompt')}")
    else:
        lines.append("Lease: none")
    return "\n".join(lines)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Baton and lease of the btlab build (see CLOUD.md).")
    sub = parser.add_subparsers(dest="command", required=True)
    take_cmd = sub.add_parser("take")
    take_cmd.add_argument("--session", help="re-adopt a lease this session took before, e.g. after the VM was replaced")
    sub.add_parser("checkpoint").add_argument("step")
    sub.add_parser("finish").add_argument("number", type=int)
    sub.add_parser("beat")
    sub.add_parser("release").add_argument("reason")
    sub.add_parser("status")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    now = utc_now()
    commands = {
        "take": lambda: take(args.session, now),
        "checkpoint": lambda: checkpoint(args.step, now),
        "finish": lambda: finish(args.number, now),
        "beat": lambda: beat(now),
        "release": lambda: release(args.reason, now),
        "status": lambda: status(now),
    }
    try:
        if args.command != "status":
            ensure_identity()
        print(commands[args.command]())
        return OK
    except Exit as stop:
        print(f"[exit {stop.code}] {stop}")
        return stop.code


if __name__ == "__main__":
    sys.exit(main())
