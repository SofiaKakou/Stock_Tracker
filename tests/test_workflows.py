"""Checks on the GitHub workflows themselves (they run the bot, so mistakes there are bugs too)."""

import re
from pathlib import Path

import yaml

WORKFLOWS = sorted(Path(__file__).resolve().parents[1].glob(".github/workflows/*.yml"))


def steps():
    for f in WORKFLOWS:
        for job in yaml.safe_load(f.read_text())["jobs"].values():
            for step in job.get("steps", []):
                yield f.name, step


def test_every_database_restore_lists_the_same_files_as_the_save():
    # The cache is only found when the paths match exactly (a mismatch silently ran without the database).
    def paths(step):
        return sorted(p.strip() for p in str(step.get("with", {}).get("path", "")).split("\n") if p.strip())
    saves = {tuple(paths(s)) for _, s in steps() if "cache/save" in s.get("uses", "") and "market.db" in paths(s)}
    restores = {(f, tuple(paths(s))) for f, s in steps() if "cache/restore" in s.get("uses", "")
                and "market.db" in paths(s)}
    assert len(saves) == 1, saves
    assert all(p in saves for _, p in restores), restores


def test_actions_are_locked_to_exact_versions():
    for f, s in steps():
        if "uses" in s:
            assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", s["uses"]), f"{f}: {s['uses']} isn't pinned to a commit"


def test_every_workflow_has_least_privilege_permissions():
    for f in WORKFLOWS:
        assert "permissions" in yaml.safe_load(f.read_text()), f"{f.name} has no permissions block"


def test_the_demo_accounts_can_only_reach_the_paper_server():
    from trend_bot import paper
    src = Path(paper.__file__).read_text() + Path(paper.__file__).with_name("daytrade.py").read_text()
    assert paper.PAPER_URL == "https://paper-api.alpaca.markets/v2"
    assert "api.alpaca.markets" not in src.replace("paper-api.alpaca.markets", "").replace("data.alpaca.markets", "")
