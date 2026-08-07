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
    for lineno, line in enumerate(
        (ROOT / "Makefile").read_text().splitlines(), start=1
    ):
        if not line.startswith("\t"):
            continue
        recipe = line.lstrip("\t").lstrip("@-")
        # `||` and `|&` are not pipelines; a bare `|` is.
        if re.search(r"(?<![|&])\|(?!\|)", recipe):
            offenders.append(f"Makefile:{lineno}: {recipe.strip()}")

    assert not offenders, "pipeline in a Make recipe:\n" + "\n".join(offenders)


def test_ops_scripts_are_strict() -> None:
    """Every script carries its own strict mode, since the Makefile cannot."""
    for script in sorted((ROOT / "ops" / "scripts").glob("*.sh")):
        text = script.read_text()
        assert "set -euo pipefail" in text, f"{script.name} lacks `set -euo pipefail`"


def test_no_executable_code_calls_flushall() -> None:
    """A real Redis with real data sits one port away, so nothing may flush it.

    Scoped to code and scripts: prose is free to *forbid* the command by name,
    which is why the docs mention it. docs/CONTRACT.md section 10.
    """
    executable_suffixes = {".py", ".sh", ".lua", ".yml", ".yaml", ".toml"}
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
        # This test names the command it forbids, so it must skip itself.
        if path.resolve() == Path(__file__).resolve():
            continue
        try:
            content = path.read_text()
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(content.splitlines(), start=1):
            if "flushall" in line.lower():
                offenders.append(f"{name}:{lineno}")

    assert not offenders, f"FLUSHALL invoked in: {offenders}"


def test_env_example_has_no_default_redis_url_in_code_path() -> None:
    """MRFP_REDIS_URL must be explicit, and must never point at host 6379.

    An unset value has to raise at boot rather than silently connecting to the
    developer's own Redis. (ADR-004)
    """
    env = (ROOT / ".env.example").read_text()
    assert "MRFP_REDIS_URL=redis://redis:6379/0" in env, "in-network URL expected"
    assert "MRFP_REDIS_URL=redis://127.0.0.1:6379" not in env
    assert "MRFP_REDIS_URL_HOST=redis://127.0.0.1:6380/0" in env
