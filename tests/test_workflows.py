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


def test_a_refused_sec_download_is_retried_from_a_fresh_machine_with_the_same_nights_database():
    nightly = yaml.safe_load((WORKFLOWS[0].parent / "nightly.yml").read_text())["jobs"]
    retry = nightly["sec-retry"]
    assert retry["needs"] == "run" and "needs.run.outputs.sec_refused == '1'" in retry["if"]
    assert nightly["run"]["outputs"]["sec_refused"] == "${{ steps.sec.outputs.refused }}"
    restore = next(s for s in retry["steps"] if "cache/restore" in s.get("uses", ""))
    assert restore["with"]["restore-keys"] == "market-db-${{ github.run_id }}"     # tonight's, never an older one
    save = next(s for s in retry["steps"] if "cache/save" in s.get("uses", ""))
    assert save["if"] == "steps.retry.outputs.tried == '1'"                          # nothing saved unless it ran


def test_the_morning_part_starts_the_afternoon_part_itself():
    job = yaml.safe_load((WORKFLOWS[0].parent / "daytrade.yml").read_text())["jobs"]["trade"]
    step = next(s for s in job["steps"] if s.get("name") == "Start the afternoon part")
    assert job["permissions"] == {"contents": "read", "actions": "write"}
    assert "gh workflow run daytrade.yml" in step["run"] and "'afternoon' not in ran" in step["run"]
    code = step["run"].split('python -c "', 1)[1].split('")', 1)[0]
    compile(code, "step", "exec")                                   # the embedded Python is valid
