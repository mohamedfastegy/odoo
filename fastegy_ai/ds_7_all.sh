#!/usr/bin/env bash
# =============================================================================
# FastEgy AI — datasheets 7: official datasheets for the models with a FastEgy description too
#
# ds_1..ds_6 searched official Hikvision datasheets only for the 646 models with
# no Arabic description in Odoo (449 found). The staging chat of 12 Oct asked
# about models that do have one (DS-2CD1021G0-I, DS-2CD1023G0-IU,
# DS-7104HUHI-K1, DS-3E0105D-E): the assistant had only FastEgy's short Arabic
# lines, which miss values (night range, PoE) or differ from the official ones.
# This runs the same collector (v1.4, already on the server) with --all: the
# models already checked are skipped, so only those with a description are
# searched. Pace and safety as ds_3: by day one search every 60 s, 22:00-07:00
# Cairo every 45 s, and it stops at once on a new search-engine block.
# The reader is not touched: lookup_product shows each datasheet as soon as it
# is saved. The datasheets file is backed up first and put back on failure.
# Run    : bash ds_7_all.sh
# Watch  : docker logs --tail 20 fastegy-datasheets
# Stop   : docker rm -f fastegy-datasheets   (progress kept; run again to resume)
# Version: 1.0 — 2026-10-12
# =============================================================================
set -euo pipefail

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
[ -f "$RDDIR/ds_collect.py" ] && [ -f "$DS" ] && [ -f "$DATA/carried.json" ] || { echo "Missing collector files (run ds_1..ds_5 first); nothing changed."; exit 1; }
grep -q "Version 1.4" "$RDDIR/ds_collect.py" || { echo "The collector on the server is not v1.4 (run ds_5 first); nothing changed."; exit 1; }
NET=$(docker inspect "$LC" -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' | awk '{print $1}')
py() { docker run --rm --network "$NET" -v "$DATA:/data" -v "$RDDIR/ds_collect.py:/app/ds_collect.py:ro" fastegy-reader:4 "$@"; }

if [ "$(docker inspect -f '{{.State.Running}}' "$JOB" 2>/dev/null)" = true ]; then
  echo "$JOB is running (models without a description); it is replaced by the --all job, progress kept"
fi
docker rm -f "$JOB" >/dev/null 2>&1 || true

cp -p "$DS" "$DS.bak.$TS"
restore() {
  echo "!! $1 — putting the previous datasheets file back"
  docker rm -f "$JOB" >/dev/null 2>&1 || true
  cp -p "$DS.bak.$TS" "$DS"
  echo "Previous datasheets file restored; no job running. Run this script again later."; exit 1
}

echo "== before"
python3 - "$DATA/carried.json" "$DS" <<'PY'
import json, sys
models = json.load(open(sys.argv[1], encoding="utf-8"))["models"]
recs = {r["code"]: r for r in json.load(open(sys.argv[2], encoding="utf-8"))["datasheets"]}
done = lambda c: recs.get(c, {}).get("status") in ("found", "not_found")
with_ar = [m["code"] for m in models if m.get("ar")]
todo = [c for c in with_ar if not done(c)]
print(f"models in FastEgy's list: {len(models)}; official datasheets kept: {sum(r['status'] == 'found' for r in recs.values())}")
print(f"models with a FastEgy description: {len(with_ar)}; still to search: {len(todo)} "
      f"(about {len(todo) // 60 + 1} hours at one a minute)")
for c in ("DS-2CD1021G0-I", "DS-2CD1023G0-IU", "DS-7104HUHI-K1", "DS-3E0105D-E", "DS-7604NXI-K1/4P", "DS-7608NI-K2/8P"):
    print(f"  {c}: {recs.get(c, {}).get('status', 'not searched yet')}")
PY

echo "== search engines now"
rc=0
PRE=$(py python -c '
import sys; sys.path.insert(0, "/app"); import ds_collect as d
r = d.search("DS-2CD1021G0-I")
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

echo "== live run: 2 models now"
OUT=$(py python /app/ds_collect.py --all --now --limit 2 --sleep 10 --ignore-blocked "$KNOWN") || restore "the live run failed"
echo "$OUT"
grep -q STOPPED <<<"$OUT" && restore "an engine reported a new block during the live run"
[ "$(grep -c '^\[[0-9]/2\]' <<<"$OUT")" = 2 ] || restore "the live run did not check 2 models"

echo "== starting $JOB --all: by day every 60 s, 22:00-07:00 Cairo every 45 s"
docker run -d --name "$JOB" --restart on-failure:3 --network "$NET" --label fastegy.ai=datasheets \
  --memory 256m -v "$DATA:/data" -v "$RDDIR/ds_collect.py:/app/ds_collect.py:ro" fastegy-reader:4 \
  python /app/ds_collect.py --all --start 22 --stop 7 --sleep 45 --day-sleep 60 --ignore-blocked "$KNOWN" >/dev/null
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

echo "DONE ds 7. Previous datasheets file: $DS.bak.$TS"
echo "Watch:  docker logs --tail 20 $JOB      Stop (progress kept):  docker rm -f $JOB"
