"""Phase 0 acceptance: the unit suite runs green with the Docker daemon stopped.

These tests assert the repository's own invariants rather than application
behaviour, which does not exist yet. They are not filler — two of them encode
traps that have already been verified to bite on this machine, and they will
keep biting as the code lands.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.unit


def test_region_registry_matches_the_brief() -> None:
    """The three container names and their order are fixed by the project brief.

    The chaos harness runs `docker stop us-east-1-endpoint` literally, so these
    are load-bearing strings. docs/CONTRACT.md section 5.
    """
    registry = yaml.safe_load((ROOT / "config" / "regions.yaml").read_text())
    regions = registry["regions"]

    assert [r["id"] for r in regions] == ["us-east-1", "eu-west-1", "ap-southeast-1"]
    assert [r["container"] for r in regions] == [
        "us-east-1-endpoint",
        "eu-west-1-endpoint",
        "ap-southeast-1-endpoint",
    ]
    assert [r["priority"] for r in regions] == [1, 2, 3]


def test_regions_are_addressed_by_container_name_not_localhost() -> None:
    """Inside the proxy container, localhost is the proxy itself.

    Host ports 9001-9003 exist only for manual curl; the proxy must dial the
    compose-network DNS name or every request loops back on itself.
    """
    registry = yaml.safe_load((ROOT / "config" / "regions.yaml").read_text())
    for region in registry["regions"]:
        assert region["base_url"] == f"http://{region['container']}:8080"
        assert "localhost" not in region["base_url"]
        assert "127.0.0.1" not in region["base_url"]


def test_no_makefile_recipe_contains_a_pipeline() -> None:
    """GNU Make 3.81 silently ignores .SHELLFLAGS, so pipelines swallow failures.

    Verified on this machine: a recipe containing `false | true` continued
    running and the target exited 0. A pipeline in a recipe therefore means
    `make chaos` can report success on a failed run. Pipelines belong in
    ops/scripts/*.sh, which carry their own `set -euo pipefail`. (ADR-016)
    """
    offenders = []
    for lineno, line in enumerate((ROOT / "Makefile").read_text().splitlines(), start=1):
        if not line.startswith("\t"):
            continue
        recipe = line.lstrip("\t").lstrip("@-")
        # `||` and `|&` are not pipelines; a bare `|` is.
        if re.search(r"(?<![|&])\|(?!\|)", recipe):
            offenders.append(f"Makefile:{lineno}: {recipe.strip()}")

    assert not offenders, "pipeline in a Make recipe:\n" + "\n".join(offenders)


def test_ops_scripts_are_strict() -> None:
    """Every executable script carries its own strict mode, since Make cannot.

    Sourced libraries are exempt: they run inside a caller that has already set
    it, and `set -e` in a sourced file leaks into that caller's shell. They must
    say so explicitly, so the exemption is a decision rather than an oversight.
    """
    for script in sorted((ROOT / "ops" / "scripts").glob("*.sh")):
        text = script.read_text()
        if "Source, don't execute" in text or "source, don't execute" in text:
            continue
        assert "set -euo pipefail" in text, f"{script.name} lacks `set -euo pipefail`"


def test_no_executable_code_calls_flushall() -> None:
    """A real Redis with real data sits one port away, so nothing may flush it.

    Matches *invocations*, not mentions: prose and help text are free to name
    the command in order to forbid it, which is exactly what they do.
    docs/CONTRACT.md section 10.
    """
    executable_suffixes = {".py", ".sh", ".lua", ".yml", ".yaml", ".toml"}
    # A call through redis-py, a redis-cli argument, or a raw command string.
    invocation = re.compile(
        r"""(?ix)
        \. \s* flushall \s* \(          # client.flushall()
        | redis-cli [^\n]* \b flushall \b   # redis-cli ... flushall
        | ["'] \s* flushall \s* ["']    # execute_command("FLUSHALL")
        """
    )

    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()

    offenders = []
    for name in tracked:
        path = ROOT / name
        if not path.is_file():
            continue
        if path.suffix not in executable_suffixes and path.name != "Makefile":
            continue
        # This test spells out the patterns it forbids, so it must skip itself.
        if path.resolve() == Path(__file__).resolve():
            continue
        try:
            content = path.read_text()
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(content.splitlines(), start=1):
            if invocation.search(line):
                offenders.append(f"{name}:{lineno}: {line.strip()}")

    assert not offenders, "FLUSHALL invoked in:\n" + "\n".join(offenders)


def test_env_example_has_no_default_redis_url_in_code_path() -> None:
    """MRFP_REDIS_URL must be explicit, and must never point at host 6379.

    An unset value has to raise at boot rather than silently connecting to the
    developer's own Redis. (ADR-004)
    """
    env = (ROOT / ".env.example").read_text()
    assert "MRFP_REDIS_URL=redis://redis:6379/0" in env, "in-network URL expected"
    assert "MRFP_REDIS_URL=redis://127.0.0.1:6379" not in env
    assert "MRFP_REDIS_URL_HOST=redis://127.0.0.1:6380/0" in env


# --- every command must be discoverable (the sixth reachability bug) --------


def test_every_make_target_appears_in_make_help() -> None:
    """`make help` is the discovery surface, and it had gone stale.

    Six targets — capacity, calibrate, calibrate-check, soak, and the four
    redis-ha-* — existed, worked, and were documented in ADRs while being
    invisible from the project's own front door. That is the same shape as the
    five plumbing bugs before it: the thing works, and nothing can reach it.

    This is the structural fix. A new target now fails the suite until it is
    listed, so discoverability stops depending on remembering.
    """
    import re

    makefile = (ROOT / "Makefile").read_text()
    help_text = (ROOT / "ops" / "scripts" / "help.sh").read_text()

    targets = set(re.findall(r"^([a-z][a-z0-9-]*):(?!=)", makefile, flags=re.M))
    # Pattern rules and internal plumbing are not user-facing commands.
    targets -= {"help", "venv", "chaos-%"}

    missing = sorted(t for t in targets if f"make {t}" not in help_text)
    assert not missing, f"targets missing from `make help`: {missing}"


def test_help_does_not_advertise_targets_that_do_not_exist() -> None:
    """The inverse: a help entry for a removed target sends people at a wall."""
    import re

    makefile = (ROOT / "Makefile").read_text()
    help_text = (ROOT / "ops" / "scripts" / "help.sh").read_text()

    targets = set(re.findall(r"^([a-z][a-z0-9-]*):(?!=)", makefile, flags=re.M))
    # Anchored to a help ENTRY, not to any prose containing the word "make" —
    # the first version matched "arch, make version" out of the doctor line.
    advertised = set(re.findall(r'^"\s+make ([a-z][a-z0-9-]*)', help_text, flags=re.M))

    # `chaos-NAME` / `chaos-pause-us-east` document the chaos-% pattern rule.
    phantom = sorted(
        a
        for a in advertised
        if a not in targets and not (a.startswith("chaos-") and "chaos-%:" in makefile)
    )
    assert not phantom, f"`make help` advertises non-existent targets: {phantom}"


def test_make_lint_runs_everything_ci_runs() -> None:
    """A local gate weaker than the remote one certifies broken pushes.

    `make lint` ran only `ruff check` while CI also ran `ruff format --check`.
    Five consecutive pushes went out green locally and red on CI, and nobody
    looked — the failures were invisible from the machine doing the pushing.

    Same shape as every reachability bug in this repo: two surfaces describing
    one thing, and only one of them tested.
    """
    import re

    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    makefile = (ROOT / "Makefile").read_text()

    lint_recipe = makefile[makefile.index("\nlint:") :]
    lint_recipe = lint_recipe[: lint_recipe.index("\n\n")]

    # Every `ruff <subcommand> <flags>` CI runs must appear in `make lint`.
    ci_ruff = {m.group(1).strip() for m in re.finditer(r"run: ruff (.+)", ci)}
    missing = sorted(
        cmd for cmd in ci_ruff if cmd.split()[0] not in lint_recipe or not _covers(lint_recipe, cmd)
    )
    assert not missing, f"`make lint` does not run what CI runs: {missing}"


def _covers(recipe: str, ci_cmd: str) -> bool:
    """True when the recipe invokes ruff with the same subcommand and flags."""
    want = [t for t in ci_cmd.split() if t != "."]
    return any(all(tok in line for tok in want) for line in recipe.splitlines() if "RUFF" in line)


# --- the docs must describe the system that exists --------------------------


def test_every_setting_is_documented_in_the_contract() -> None:
    """`docs-check` and the link checker are mechanical: they cannot tell
    whether a doc still describes the current system. 18 settings had accreted
    without reaching CONTRACT.md's env table, including several added the same
    day. Same shape as every other bug here — two surfaces, one of them tested.
    """
    import sys

    sys.path.insert(0, str(ROOT / "proxy"))
    from app.config import Settings

    contract = (ROOT / "docs" / "CONTRACT.md").read_text()
    missing = sorted(
        f"MRFP_{name.upper()}"
        for name in Settings.model_fields
        if f"MRFP_{name.upper()}" not in contract
    )
    assert not missing, f"settings missing from CONTRACT.md: {missing}"


def test_the_log_vocabulary_really_is_closed() -> None:
    """CONTRACT §8 and CLAUDE.md §6 both state the event vocabulary is closed
    and that the same strings are metric labels and doc terms. 25 events had
    been coined without being added — a rule with nothing enforcing it."""
    import re

    contract = (ROOT / "docs" / "CONTRACT.md").read_text()
    events: set[str] = set()
    for source in (ROOT / "proxy" / "app").rglob("*.py"):
        events |= set(re.findall(r'logx\(\s*"([a-z_]+)"', source.read_text()))

    missing = sorted(e for e in events if f"`{e}`" not in contract)
    assert not missing, f"log events missing from the CONTRACT §8 vocabulary: {missing}"


def test_every_make_target_is_in_the_agent_command_list() -> None:
    """CLAUDE.md §4 is what an agent reads before touching the repo. A target
    absent from it is a command that, for that reader, does not exist."""
    import re

    makefile = (ROOT / "Makefile").read_text()
    claude = (ROOT / "CLAUDE.md").read_text()
    targets = set(re.findall(r"^([a-z][a-z0-9-]*):(?!=)", makefile, flags=re.M))
    targets -= {"help", "venv", "chaos-%"}

    missing = sorted(t for t in targets if f"make {t}" not in claude)
    assert not missing, f"make targets missing from CLAUDE.md §4: {missing}"
