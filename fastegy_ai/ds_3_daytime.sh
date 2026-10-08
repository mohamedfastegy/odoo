#!/usr/bin/env bash
# =============================================================================
# FastEgy AI — datasheets 3: let the datasheet job work by day too, safely
#
# The job from ds_1 waits for 22:00. The server load is small; the only risk
# is the search engines, which the assistant's web search shares: SearXNG
# suspends an engine for up to 24 hours after a CAPTCHA or a rate limit. So:
#   - collector v1.2 stops at once when an engine reports a new block (CAPTCHA,
#     "too many requests", "access denied"), and the assistant keeps the
#     engines that still answer. Engines already blocked before it starts
#     (startpage: a permanent bot wall) are ignored;
#   - by day one search every 60 s; at night every 45 s, as before.
# The reader is not touched. Only the job container is replaced; the models
# already checked are kept. Any failure puts the night-only job back.
# Back to night-only any time:  bash ds_3_daytime.sh --night-only
# Run    : bash ds_3_daytime.sh
# Version: 1.1 — 2026-10-08 (1.0 stopped on startpage's permanent CAPTCHA)
# =============================================================================
set -euo pipefail

SRC=https://raw.githubusercontent.com/mohamedfastegy/odoo/61f578a235b9ef96a251fbd0cac2275b399579de/fastegy_ai
LC=librechat
RD=fastegy-reader
JOB=fastegy-datasheets
RDDIR=/root/fastegy-reader
DATA=$RDDIR/data
DS=$DATA/ds/datasheets.json
TS=$(date +%Y%m%d_%H%M%S)

for c in "$LC" "$RD"; do
  [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = true ] || { echo "$c is not running; nothing changed."; exit 1; }
done
docker image inspect fastegy-reader:4 >/dev/null 2>&1 || { echo "Image fastegy-reader:4 is missing; nothing changed."; exit 1; }
docker inspect "$JOB" >/dev/null 2>&1 || { echo "$JOB not found (run ds_1 first); nothing changed."; exit 1; }
[ -f "$RDDIR/ds_collect.py" ] && [ -f "$DS" ] || { echo "Missing collector files; nothing changed."; exit 1; }
NET=$(docker inspect "$LC" -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' | awk '{print $1}')

night_only() {  # the job as ds_1 started it, with the collector file on the server (v1.0, or v1.2 once installed)
  docker rm -f "$JOB" >/dev/null 2>&1 || true
  docker run -d --name "$JOB" --restart on-failure:3 --network "$NET" --label fastegy.ai=datasheets \
    --memory 256m -v "$DATA:/data" -v "$RDDIR/ds_collect.py:/app/ds_collect.py:ro" fastegy-reader:4 \
    python /app/ds_collect.py --start 22 --stop 7 --sleep 45 >/dev/null
}
if [ "${1:-}" = --night-only ]; then
  night_only
  echo "Night-only job started again (22:00-07:00 Cairo); progress kept."; exit 0
fi

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
curl -fsSL "$SRC/kb/ds_collect.py" -o "$STAGE/ds_collect.py"
(cd "$STAGE" && sha256sum -c --quiet) <<'SUMS' || { echo "Downloaded file does not match; nothing changed."; exit 1; }
8f58aea80fc346b2736f126434db64d40536f75f6f2c24420b713c05139c57a3  ds_collect.py
SUMS
echo "collector v1.2 downloaded and verified"

restore() {
  echo "!! $1 — putting the night-only job back"
  cp -p "$RDDIR/ds_collect.py.bak.$TS" "$RDDIR/ds_collect.py"
  night_only
  echo "Night-only job restored (starts 22:00 Cairo); progress kept."; exit 1
}
cp -p "$RDDIR/ds_collect.py" "$RDDIR/ds_collect.py.bak.$TS"
cp -p "$DS" "$DS.bak.$TS"
install -m 644 "$STAGE/ds_collect.py" "$RDDIR/ds_collect.py"
py() { docker run --rm --network "$NET" -v "$DATA:/data" -v "$RDDIR/ds_collect.py:/app/ds_collect.py:ro" fastegy-reader:4 "$@"; }

echo "== checks"
py python -c '
import sys; sys.path.insert(0, "/app"); import ds_collect as d
assert "Version 1.2" in d.__doc__
assert d.blocks({"unresponsive_engines": [["startpage", "CAPTCHA"], ["yahoo", "timeout"]]}) == [("startpage", "CAPTCHA")]
assert d.blocks({"unresponsive_engines": [["yandex", "Suspended: too many requests"]]}) == [("yandex", "Suspended: too many requests")]
print("block detection: ok")' || restore "collector v1.2 check failed"
rc=0
PRE=$(py python -c '
import sys; sys.path.insert(0, "/app"); import ds_collect as d
r = d.search("DS-2CD1027G2-L")
if "unresponsive_engines" not in r:
    print("search reply has no engine status"); sys.exit(2)
print("search now:", len(r.get("results", [])), "results; engines with problems:", r["unresponsive_engines"] or "none")
print("KNOWN=" + ",".join(e for e, _ in d.blocks(r)))
sys.exit(0 if r.get("results") else 3)') || rc=$?
grep -v "^KNOWN=" <<<"$PRE" || true
[ "$rc" = 3 ] && restore "the search returned no results right now"
[ "$rc" = 0 ] || restore "engine status check failed"
KNOWN=$(sed -n "s/^KNOWN=//p" <<<"$PRE")
echo "already blocked, so the job ignores them: ${KNOWN:-none}"

docker rm -f "$JOB" >/dev/null
echo "== live run: 2 models now"
OUT=$(py python /app/ds_collect.py --now --limit 2 --sleep 10 --ignore-blocked "$KNOWN") || restore "the live run failed"
echo "$OUT"
grep -q STOPPED <<<"$OUT" && restore "an engine reported a new block during the live run"
[ "$(grep -c '^\[[0-9]/2\]' <<<"$OUT")" = 2 ] || restore "the live run did not check 2 models"

echo "== starting $JOB: by day every 60 s, 22:00-07:00 Cairo every 45 s"
docker run -d --name "$JOB" --restart on-failure:3 --network "$NET" --label fastegy.ai=datasheets \
  --memory 256m -v "$DATA:/data" -v "$RDDIR/ds_collect.py:/app/ds_collect.py:ro" fastegy-reader:4 \
  python /app/ds_collect.py --start 22 --stop 7 --sleep 45 --day-sleep 60 --ignore-blocked "$KNOWN" >/dev/null
sleep 15
docker logs "$JOB" 2>&1 | tail -3
[ "$(docker inspect -f '{{.State.Running}}' "$JOB")" = true ] || restore "the job is not running"
docker logs "$JOB" 2>&1 | grep -q "models to check" || restore "the job did not start checking"

echo "== the assistant's web search"
docker exec "$LC" node -e '
fetch("http://fastegy-reader:3002/search?q=hikvision%20nvr&format=json").then(r => r.json()).then(j => {
  console.log("web search: " + (j.results || []).length + " results; engines with problems: " + JSON.stringify(j.unresponsive_engines || []));
  process.exit((j.results || []).length ? 0 : 2);
}).catch(e => { console.log("web search error: " + e.message); process.exit(2); });' || restore "web search returned nothing"

echo "DONE ds 3. Previous files: $RDDIR/ds_collect.py.bak.$TS, $DS.bak.$TS"
echo "Watch:  docker logs --tail 20 $JOB      Night-only again:  bash ds_3_daytime.sh --night-only"
