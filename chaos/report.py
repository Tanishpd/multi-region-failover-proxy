"""Regenerate the AUTOGEN blocks in docs/CHAOS-REPORT.md from run artifacts.

Only the marked regions are rewritten, so prose survives regeneration and no
measurement can be hand-authored. Two marker forms, with DISTINCT terminators so
a block-scanning regex can never pair across an inline span:

    <!-- AUTOGEN:BEGIN:name -->  ... block ...  <!-- AUTOGEN:END -->
    <!-- AUTOGEN:INLINE:name --> span <!-- AUTOGEN:INLINE:END -->

Usage:  python -m chaos.report            # newest run per scenario
"""

from __future__ import annotations

import json
import platform
import re
import subprocess
import sys
from datetime import UTC, datetime

from chaos.config import ARTIFACTS, ROOT

REPORT = ROOT / "docs" / "CHAOS-REPORT.md"

# Named groups throughout: positional indices here are a trap, because the
# `name` group is nested inside the opener and shifts every later index by one.
# Referencing the body where the terminator was meant drops the closing marker
# and leaves the stale text in place — which is exactly what happened once.
BLOCK = re.compile(
    r"(?P<open><!-- AUTOGEN:BEGIN:(?P<name>[\w-]+) -->)"
    r"(?P<body>.*?)"
    r"(?P<close><!-- AUTOGEN:END -->)",
    re.DOTALL,
)
INLINE = re.compile(
    r"(?P<open><!-- AUTOGEN:INLINE:(?P<name>[\w:-]+) -->)"
    r"(?P<body>.*?)"
    r"(?P<close><!-- AUTOGEN:INLINE:END -->)"
)


def newest_runs() -> dict[str, dict]:
    """The most recent summary.json per scenario."""
    runs: dict[str, dict] = {}
    for path in sorted(ARTIFACTS.glob("*/summary.json")):
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        data["_dir"] = path.parent
        runs[data["scenario"]] = data  # sorted order means later wins
    return runs


def runs_by_mode() -> dict[tuple[str, str], dict]:
    """Newest run per (scenario, mode). The mode-comparison needs both."""
    out: dict[tuple[str, str], dict] = {}
    for path in sorted(ARTIFACTS.glob("*/summary.json")):
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        data["_dir"] = path.parent
        out[(data["scenario"], data["notes"].get("mode", "literal"))] = data
    return out


def mode_comparison_block(by_mode: dict[tuple[str, str], dict]) -> str:
    """literal vs resilient on the SAME scenario — the breaker's cost/benefit."""
    scenario = "pause-us-east"
    lit = by_mode.get((scenario, "literal"))
    res = by_mode.get((scenario, "resilient"))
    if not lit or not res:
        return (
            "\n_needs `pause-us-east` run in both modes:_\n"
            "```\nmake chaos-pause-us-east\n"
            "MRFP_MODE=resilient docker compose up -d --force-recreate mrfp-proxy\n"
            "make chaos-pause-us-east\n```\n"
        )

    def ev(run: dict, name: str) -> int:
        return run["log_event_counts"].get(name, 0)

    rows = [
        "",
        f"Both rows are `{scenario}` on the same stack; only `MRFP_MODE` differs.",
        "",
        "| | literal (default) | resilient |",
        "|---|---|---|",
        f"| p50 during outage | {lit['by_window']['outage']['p50_ms']} ms |"
        f" **{res['by_window']['outage']['p50_ms']} ms** |",
        f"| p99 during outage | {lit['by_window']['outage']['p99_ms']} ms |"
        f" {res['by_window']['outage']['p99_ms']} ms |",
        f"| `upstream_failover` lines | **{ev(lit, 'upstream_failover')}** |"
        f" {ev(res, 'upstream_failover')} |",
        f"| `region_skipped_breaker_open` | {ev(lit, 'region_skipped_breaker_open')} |"
        f" {ev(res, 'region_skipped_breaker_open')} |",
        f"| `circuit_opened` | {ev(lit, 'circuit_opened')} | {ev(res, 'circuit_opened')} |",
        f"| errors surfaced | {lit['overall']['errors']} | {res['overall']['errors']} |",
        "",
    ]
    return "\n".join(rows)


def _fmt_verdict(run: dict | None) -> str:
    if run is None:
        return "_not run_"
    return "PASS" if run["passed"] else "**FAIL**"


def environment_block(runs: dict[str, dict]) -> str:
    def sh(*argv: str) -> str:
        try:
            return subprocess.run(argv, capture_output=True, text=True, check=True).stdout.strip()
        except Exception:
            return "unavailable"

    any_run = next(iter(runs.values()), None)
    generated = (
        datetime.fromtimestamp(any_run["generated_at"], tz=UTC).isoformat(timespec="seconds")
        if any_run
        else "n/a"
    )

    return "\n".join(
        [
            "",
            "| | |",
            "|---|---|",
            f"| Generated | {generated} |",
            f"| Host | {platform.system()} {platform.release()}, {platform.machine()} |",
            f"| Docker | {sh('docker', 'version', '--format', '{{.Server.Version}}')} |",
            f"| Compose | {sh('docker', 'compose', 'version', '--short')} |",
            f"| Proxy mode | {any_run['notes'].get('mode', 'literal') if any_run else 'n/a'} |",
            f"| Git commit | {sh('git', '-C', str(ROOT), 'rev-parse', '--short', 'HEAD')} |",
            "",
            "Read timeout 200 ms, connect 100 ms, pool 50 ms, total budget 900 ms.",
            "Rate limit: capacity 20, refill 20/s. Load: 50 rps, open-loop.",
            "",
        ]
    )


def timeline_block(runs: dict[str, dict], scenario: str) -> str:
    run = runs.get(scenario)
    if run is None:
        return "\n_not run_\n"
    timeline = (run["_dir"] / "timeline.txt").read_text().rstrip()
    return f"\n```\n{timeline}\n```\n"


def summary_block(runs: dict[str, dict], scenario: str) -> str:
    run = runs.get(scenario)
    if run is None:
        return "\n_not run_\n"

    rows = ["", "| Window | Requests | Success | Errors | p50 | p99 | Regions serving |", "|---|---|---|---|---|---|---|"]
    for window in ("baseline", "outage", "recovery"):
        w = run["by_window"][window]
        regions = ", ".join(f"{k} ×{v}" for k, v in w["by_region"].items() if k != "-") or "—"
        rows.append(
            f"| {window} | {w['count']} | {w['ok']} | {w['errors']} | "
            f"{w['p50_ms']} ms | {w['p99_ms']} ms | {regions} |"
        )
    rows.append("")
    return "\n".join(rows)


def assertions_block(runs: dict[str, dict], scenario: str) -> str:
    run = runs.get(scenario)
    if run is None:
        return "\n_not run_\n"

    rows = ["", "| id | Assertion | Result | Detail |", "|---|---|---|---|"]
    for a in run["assertions"]:
        verdict = "PASS" if a["passed"] else "**FAIL**"
        rows.append(f"| {a['id']} | {a['name']} | {verdict} | {a['detail']} |")
    rows.append("")
    return "\n".join(rows)


def commands_block(runs: dict[str, dict], scenario: str) -> str:
    run = runs.get(scenario)
    if run is None:
        return "\n_not run_\n"
    lines = ["", "```"]
    for action in run["commands_executed"]:
        lines.append(f"$ {action['command']}    # returned in {action['duration_ms']:.0f} ms")
    lines += ["```", ""]
    return "\n".join(lines)


def stop_vs_pause_block(runs: dict[str, dict]) -> str:
    stop, pause = runs.get("stop-us-east"), runs.get("pause-us-east")
    if not stop or not pause:
        return "\n_needs both stop-us-east and pause-us-east_\n"

    def row(label: str, fn) -> str:
        return f"| {label} | {fn(stop)} | {fn(pause)} |"

    def codes(run: dict) -> str:
        return ", ".join(f"`{k}` ×{v}" for k, v in run["detection"]["codes"].items()) or "—"

    return "\n".join(
        [
            "",
            "| | `docker stop` | `docker pause` |",
            "|---|---|---|",
            row("Exception class", codes),
            row("Detection p50", lambda r: f"**{r['detection']['p50_ms']} ms**"),
            row("Detection p99", lambda r: f"{r['detection']['p99_ms']} ms"),
            row("Detection min / max", lambda r: f"{r['detection']['min_ms']} / {r['detection']['max_ms']} ms"),
            row("Outage-window p50", lambda r: f"{r['by_window']['outage']['p50_ms']} ms"),
            row(
                "Read timeout exercised?",
                lambda r: "no — failed at DNS first"
                if "read_timeout" not in r["detection"]["codes"]
                else "**yes**",
            ),
            "",
            f"Ratio of detection p50: **{pause['detection']['p50_ms'] / max(stop['detection']['p50_ms'], 0.1):.0f}×**.",
            "",
        ]
    )


def matrix_rows(runs: dict[str, dict]) -> dict[str, str]:
    return {f"result:{name}": _fmt_verdict(runs.get(name)) for name in runs} | {
        f"result:{name}": _fmt_verdict(runs.get(name))
        for name in (
            "stop-us-east",
            "pause-us-east",
            "cascade-two",
            "total-blackout",
            "recover-us-east",
        )
    }


def build(runs: dict[str, dict], by_mode: dict[tuple[str, str], dict]) -> dict[str, str]:
    return {
        "mode-comparison": mode_comparison_block(by_mode),
        "environment": environment_block(runs),
        "timeline-stop-us-east": timeline_block(runs, "stop-us-east"),
        "timeline-pause-us-east": timeline_block(runs, "pause-us-east"),
        "summary-stop-us-east": summary_block(runs, "stop-us-east"),
        "assertions-stop-us-east": assertions_block(runs, "stop-us-east"),
        "commands-stop-us-east": commands_block(runs, "stop-us-east"),
        "stop-vs-pause": stop_vs_pause_block(runs),
    }


def main() -> int:
    runs = newest_runs()
    if not runs:
        print("report: no runs found in artifacts/ — run `make chaos` first", file=sys.stderr)
        return 2

    text = REPORT.read_text()
    blocks = build(runs, runs_by_mode())
    inlines = matrix_rows(runs)
    filled: list[str] = []

    def replace_block(match: re.Match) -> str:
        name = match.group("name")
        if name not in blocks:
            return match.group(0)
        filled.append(name)
        return f"{match.group('open')}{blocks[name]}{match.group('close')}"

    def replace_inline(match: re.Match) -> str:
        name = match.group("name")
        if name not in inlines:
            return match.group(0)
        filled.append(name)
        return f"{match.group('open')}{inlines[name]}{match.group('close')}"

    text = BLOCK.sub(replace_block, text)
    text = INLINE.sub(replace_inline, text)
    REPORT.write_text(text)

    print(f"report: filled {len(filled)} region(s) from {len(runs)} run(s): {', '.join(sorted(runs))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
