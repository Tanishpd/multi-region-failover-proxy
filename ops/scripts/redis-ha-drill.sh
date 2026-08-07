#!/usr/bin/env bash
# Kill the CURRENT Redis primary under continuous load and measure the blip.
#
# The first time I ran this by hand I killed the container named
# `mrfp-redis-primary` and measured a perfect zero-error failover — because an
# earlier run had already promoted the other node, so I had killed a replica.
# Hence: ask Sentinel which node is primary, map that address back to a
# container, and kill THAT. A drill that assumes the topology it is testing is
# not a drill.
set -euo pipefail

cd "$(dirname "$0")/../.."

HA_FILE=deploy/compose.redis-ha.yml
SENTINELS=sentinel-1:26379,sentinel-2:26379,sentinel-3:26379
KILL_AT_S="${KILL_AT_S:-7}"
export PROBE_DURATION_S="${PROBE_DURATION_S:-25}"

if ! docker ps --format '{{.Names}}' | grep -q '^mrfp-sentinel-1$'; then
  echo "FAIL: the HA topology is not running. Start it with: make redis-ha-up" >&2
  exit 2
fi

# Sentinel can only promote if a SYNCED replica exists. Run this drill twice in
# a row without waiting and the second run kills the master while the previously
# killed node is still resyncing — Sentinel has nothing to promote, the client
# correctly never recovers, and the drill reports a client bug that is not
# there. Three back-to-back runs gave PASS / no-promotion / PASS before this
# check existed. A drill that does not assert its own preconditions measures the
# weather.
echo "== waiting for a healthy topology (one master + one in-sync replica) =="
for attempt in $(seq 1 60); do
  REPLICAS=$(docker exec mrfp-sentinel-1 redis-cli -p 26379 \
    sentinel replicas mrfp-master 2>/dev/null | tr -d '\r' || true)
  LINK=$(echo "$REPLICAS" | grep -A1 '^master-link-status$' | tail -1)
  FLAGS=$(echo "$REPLICAS" | grep -A1 '^flags$' | tail -1)
  if [ "$LINK" = "ok" ] && [ "$FLAGS" = "slave" ]; then
    echo "topology healthy after ${attempt}s (replica link=ok, flags=slave)"
    break
  fi
  if [ "$attempt" = 60 ]; then
    echo "FAIL: no in-sync replica after 60s (link=${LINK:-none} flags=${FLAGS:-none})." >&2
    echo "      Sentinel would have nothing to promote, so the drill cannot run." >&2
    exit 2
  fi
  sleep 1
done

echo "== asking Sentinel which node is primary =="
# `mapfile` would be the obvious read here, but macOS ships bash 3.2 and it
# arrived in 4.0 — this script has to run on the machine it documents.
ADDR=$(docker exec mrfp-sentinel-1 redis-cli -p 26379 \
  sentinel get-master-addr-by-name mrfp-master | tr -d '\r' | tr '\n' ' ')
MASTER_IP=$(echo "$ADDR" | awk '{print $1}')
MASTER_PORT=$(echo "$ADDR" | awk '{print $2}')
echo "primary address: ${MASTER_IP}:${MASTER_PORT}"

TARGET=""
for candidate in mrfp-redis-primary mrfp-redis-replica; do
  ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' \
    "$candidate" 2>/dev/null || true)
  [ "$ip" = "$MASTER_IP" ] && TARGET="$candidate"
done

if [ -z "$TARGET" ]; then
  echo "FAIL: no container owns ${MASTER_IP}; the topology drifted" >&2
  exit 2
fi
echo "primary is container: ${TARGET}"

# Kill it partway through the probe. Backgrounded so the probe is already
# hammering when the node disappears — a promotion measured from an idle client
# hides the reconnect cost that real traffic pays.
(
  sleep "$KILL_AT_S"
  echo ">> docker stop ${TARGET}" >&2
  docker stop "$TARGET" >/dev/null
) &
KILLER=$!

echo "== probing for ${PROBE_DURATION_S}s =="
RESULT=$(docker run --rm --network mrfp-net \
  -v "$PWD/ops/drills:/drills:ro" \
  -e PROBE_SENTINELS="$SENTINELS" \
  -e PROBE_DURATION_S="$PROBE_DURATION_S" \
  -e PROBE_MASTER_NAME=mrfp-master \
  mrfp/proxy:dev python /drills/redis_probe.py)
wait "$KILLER"

echo "$RESULT" | python3 -m json.tool

# Restore before asserting, so a failure still leaves the topology usable.
docker compose -f "$HA_FILE" start "${TARGET#mrfp-}" >/dev/null 2>&1 \
  || docker start "$TARGET" >/dev/null

python3 - "$RESULT" <<'PY'
import json, sys
r = json.loads(sys.argv[1])
fails = []

# The promotion must actually have happened, or the drill proved nothing.
if not r["promoted"]:
    fails.append(f"no promotion: master stayed {r['started_master']}")

# Some failure is EXPECTED and healthy — it is the window between the primary
# dying and Sentinel promoting. A run with zero errors means the load did not
# overlap the outage, so the measurement is not evidence of anything.
if r["errors"] == 0:
    fails.append("zero errors: the probe did not overlap the outage, so nothing was measured")

# The client must come back on its own. This is the assertion that matters:
# without Sentinel support in the client, it never would.
if r["errors"] and not r["blip_s"]:
    fails.append("never recovered inside the run window")
elif r["blip_s"] > 15.0:
    fails.append(f"blip {r['blip_s']}s exceeds the 15s ceiling")

for f in fails:
    print(f"FAIL: {f}")
if fails:
    sys.exit(1)
print(f"PASS: promoted {r['started_master']} -> {r['ended_master']}, "
      f"blip {r['blip_s']}s, {r['errors']} failed / {r['ok']} ok")
PY
