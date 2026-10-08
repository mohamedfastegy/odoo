#!/usr/bin/env bash
# =============================================================================
# FastEgy AI — knowledge base 13: turn the memory agent off
#
# The memory agent ran on every message (fastegy-fast, free Groq quota) and kept
# overwriting "work_context" with the day's product request ("looking for a
# 16-channel PoE NVR..."), which is not what memory is for. Mohamed chose to
# turn it off.
# Change : memory.disabled = true in librechat.yaml; the other memory settings
#          stay, so it can be turned back on. Saved memories are not deleted;
#          they are simply no longer used. LibreChat restarts (~30 s);
#          rollback on failure.
# Run    : bash kb_13_memory_off.sh
# Version: 1.0 — 2026-10-08
# =============================================================================
set -euo pipefail

LC=librechat
PYCTR=litellm
CFG=/root/librechat/librechat.yaml
TS=$(date +%Y%m%d_%H%M%S)

for c in "$LC" "$PYCTR"; do
  [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = true ] || { echo "$c is not running; nothing changed."; exit 1; }
done
BAK="$CFG.bak.$TS"
cp -p "$CFG" "$BAK"
PY_CFG=$(cat <<'PY'
import copy, sys, yaml
old = yaml.safe_load(sys.stdin)
cfg = copy.deepcopy(old)
mem = cfg.get("memory")
if not isinstance(mem, dict):
    sys.exit("ERROR: no memory section; nothing changed")
mem["disabled"] = True          # the other memory settings stay, so it can be turned back on
a, b = copy.deepcopy(old), copy.deepcopy(cfg)          # nothing else may change
a["memory"].pop("disabled", None); b["memory"].pop("disabled", None)
if a != b:
    sys.exit("ERROR: unexpected config changes; nothing changed")
yaml.safe_dump(cfg, sys.stdout, sort_keys=False, allow_unicode=True, width=1000)
PY
)
TMP_Y=$(mktemp)
trap 'rm -f "$TMP_Y"' EXIT
docker exec -i "$PYCTR" python3 -c "$PY_CFG" <"$CFG" >"$TMP_Y" || { echo "Config rewrite failed; nothing changed."; exit 1; }
grep -q 'disabled: true' "$TMP_Y" || { echo "Rewrite produced unexpected output; nothing changed."; exit 1; }
cat "$TMP_Y" >"$CFG"

rollback() {
  echo "!! $1 — rolling back librechat.yaml"
  cat "$BAK" >"$CFG"; docker restart "$LC" >/dev/null
  echo "Restored $BAK and restarted LibreChat."; exit 1
}
echo "Restarting LibreChat (site down ~30 s)..."
docker restart "$LC" >/dev/null || rollback "restart failed"
code=000
for _ in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:3080/api/config || true)
  [ "$code" = 200 ] && break
  sleep 2
done
[ "$code" = 200 ] || rollback "LibreChat did not come back (HTTP $code)"
sleep 5
if docker logs --since 3m "$LC" 2>&1 | grep -i 'invalid custom config'; then rollback "LibreChat rejected the config"; fi
docker logs --since 3m "$LC" 2>&1 | grep -i 'fastegy-products' | grep -i 'tools:' | tail -1 || true
echo "DONE kb 13. Backup: $BAK"
echo "Check: bash /tmp/fastegy_check/dl/chat_test.sh   (or: bash chat_test.sh \"your question\")"
