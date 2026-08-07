Chaos run output lands here. Contents are gitignored; the directory is tracked.

Each run writes artifacts/<runid>/ and refreshes the artifacts/latest symlink:
  requests.csv       one row per request
  summary.json       windowed stats, assertion results, command_executed
  timeline.txt       the per-second ASCII table
  proxy-logs.jsonl   proxy logs for the run window
  report.md          standalone per-run report
