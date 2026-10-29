"""Locked dependencies and git-ignored files (SPEC.md §21)."""

import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
REQUIREMENTS = ["numpy", "aiohttp", "tzdata", "ib_async==2.1.0", "pytest"]


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def test_requirements_in_lists_exactly_the_approved_packages():
    assert (REPO / "requirements.in").read_text(encoding="utf-8").split() == REQUIREMENTS


def test_the_lock_holds_a_hash_for_every_package():
    packages = tomllib.loads((REPO / "pylock.toml").read_text(encoding="utf-8"))["packages"]
    for package in packages:
        artifacts = package.get("wheels", []) + [package[kind] for kind in ("sdist", "archive") if kind in package]
        assert artifacts, package["name"]
        for artifact in artifacts:
            assert re.fullmatch(r"[0-9a-f]{64}", artifact["hashes"]["sha256"]), package["name"]
    versions = {normalized(package["name"]): package["version"] for package in packages}
    assert {normalized(requirement.split("==")[0]) for requirement in REQUIREMENTS} <= versions.keys()
    assert versions["ib-async"] == "2.1.0"


def test_gitignore_keeps_secrets_and_local_files_out_but_not_the_build_automation():
    lines = [line.strip() for line in (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()]
    required = {"config.toml", "data/", "logs/", ".venv/", "__pycache__/", ".pytest_cache/", ".claude-runs/", ".claude/settings.local.json"}
    assert required <= set(lines)
    for pattern in (line.lstrip("/") for line in lines):
        assert not pattern.startswith("buildctl"), pattern
        assert pattern not in {".claude", ".claude/", ".claude/*", ".claude/settings.json", "settings.json", "*.json"}, pattern
