#!/usr/bin/env bash
# =============================================================================
# FastEgy AI — datasheets 6: retry the downloads that failed in ds_5
#
# ds_5 re-read 74 of 86 datasheets. Seven downloads got an HTML page from
# Hikvision's site instead of the PDF (too many in a row), so two switch
# tables (DS-3E1526P-SI, DS-TPE104) and five datasheets cut at 6000
# characters are still as before. This runs ds_relayout 1.3, which pauses
# 15 s between downloads, on whatever still needs it. Only the datasheets
# file changes; the previous one is kept and put back on any failure.
# Run    : bash ds_6_retry.sh
# Version: 1.0 — 2026-10-09
# =============================================================================
set -euo pipefail

SRC=https://raw.githubusercontent.com/mohamedfastegy/odoo/a95c22473aee197b5a511de4b47635aceabc46b7/fastegy_ai
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
[ -f "$DS" ] && [ -f "$RDDIR/ds_relayout.py" ] && [ -f "$RDDIR/ds_collect.py" ] || { echo "Run ds_5 first; nothing changed."; exit 1; }
[ "$(docker inspect -f '{{.State.Running}}' "$JOB" 2>/dev/null)" = true ] &&
  { echo "$JOB is running; nothing changed."; exit 1; }
NET=$(docker inspect "$LC" -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' | awk '{print $1}')
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
curl -fsSL "$SRC/kb/ds_relayout.py" -o "$STAGE/ds_relayout.py"
echo "16271bfd96e0b5fdb5fcee3de71b14d6d6d802de0a2e43a361ceb311ff5f8355  $STAGE/ds_relayout.py" | sha256sum -c --quiet || { echo "Downloaded file does not match; nothing changed."; exit 1; }
echo "ds_relayout 1.3 downloaded and verified"

restore() {
  echo "!! $1 — putting the previous datasheets file back"
  cp -p "$DS.bak.$TS" "$DS"
  cp -p "$RDDIR/ds_relayout.py.bak.$TS" "$RDDIR/ds_relayout.py"
  echo "Previous datasheets restored."; exit 1
}
cp -p "$DS" "$DS.bak.$TS"
cp -p "$RDDIR/ds_relayout.py" "$RDDIR/ds_relayout.py.bak.$TS"
install -m 644 "$STAGE/ds_relayout.py" "$RDDIR/ds_relayout.py"
R() { docker run --rm --network "$NET" -v "$DATA:/data" -v "$RDDIR/ds_collect.py:/app/ds_collect.py:ro" \
        -v "$RDDIR/ds_relayout.py:/app/ds_relayout.py:ro" fastegy-reader:4 python /app/ds_relayout.py "$@" 2>&1; }

echo "== still to re-read"
LIST=$(R --list) || restore "the list failed"
grep -v "Multiple definitions" <<<"$LIST"
echo "== re-reading them, 15 s apart"
OUT=$(R --apply --pause 15) || { grep -v "Multiple definitions" <<<"$OUT" | tail -5; restore "the re-read failed"; }
grep -v "Multiple definitions" <<<"$OUT" | grep -v "^invalid pdf header\|^EOF marker" || true
python3 - "$DS.bak.$TS" "$DS" <<'PY' || restore "the datasheets file does not check out"
import json, sys
old = {r["code"]: r for r in json.load(open(sys.argv[1], encoding="utf-8"))["datasheets"]}
new = {r["code"]: r for r in json.load(open(sys.argv[2], encoding="utf-8"))["datasheets"]}
assert old.keys() == new.keys(), "models differ"
moves = {(old[c]["status"], new[c]["status"]) for c in old if old[c]["status"] != new[c]["status"]}
assert moves <= {("found", "skipped")}, f"unexpected status change: {moves}"
changed = [c for c in old if old[c] != new[c]]
lost = [c for c in changed if new[c]["status"] == "found" and not new[c].get("spec", "").strip()]
assert not lost, f"empty spec after re-read: {lost}"
print(f"datasheets file OK: {len(new)} models, {sum(r['status'] == 'found' for r in new.values())} found, "
      f"{len(changed)} changed: {', '.join(changed) or 'none'}")
PY
echo "== left after this run"
R --list | grep -v "Multiple definitions" || true
echo "DONE ds 6. Previous datasheets file: $DS.bak.$TS"
