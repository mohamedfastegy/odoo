#!/usr/bin/env bash
# =============================================================================
# FastEgy AI — knowledge base 16: the fast option on the big model
#
# The test after kb_15: the fast option (gpt-oss-20b) still wrote "تتوفر" and
# "متاح", and once made up what /SL means. Mohamed chose: the fast option
# uses the big model (fastegy-smart = gpt-oss-120b, with the Gemini fallback)
# and stays without web search, so it is still the quicker one.
# Change : spec fastegy-fast -> preset model fastegy-smart. Nothing else:
#          its rules, tools and the chat titles (still on fastegy-fast) stay.
#          The fastegy-fast model stays in LiteLLM, so going back is one line.
#          LibreChat restarts (~30 s); rollback on failure.
# Trade-off: both options now share Groq's free limit for gpt-oss-120b; when
#          it is reached, answers come from Gemini (a little slower).
# Run    : bash kb_16_fast_model.sh
# Back   : bash kb_16_fast_model.sh --back   (the fast option on gpt-oss-20b again)
# Version: 1.0 — 2026-10-09 (same steps as kb_12)
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
import copy, os, sys, yaml
TARGET = os.environ.get("TARGET", "fastegy-smart")
old = yaml.safe_load(sys.stdin)
cfg = copy.deepcopy(old)
specs = {s.get("name"): s for s in cfg["modelSpecs"]["list"]}
fast, strong = specs.get("fastegy-fast"), specs.get("fastegy-strong")
if not fast or not strong:
    sys.exit("ERROR: specs fastegy-fast and fastegy-strong not found; nothing changed")
if fast.get("webSearch"):
    sys.exit("ERROR: the fast option has web search on; nothing changed")
if strong["preset"].get("model") != "fastegy-smart":
    sys.exit("ERROR: the smart option is not on fastegy-smart; nothing changed")
models = next(e for e in cfg["endpoints"]["custom"] if e.get("name") == "FastEgy AI")["models"]["default"]
if TARGET not in models:
    sys.exit(f"ERROR: {TARGET} is not in the endpoint's models; nothing changed")
if fast["preset"].get("model") == TARGET:
    sys.exit(3)
if fast["preset"].get("model") not in ("fastegy-fast", "fastegy-smart"):
    sys.exit("ERROR: unexpected model on the fast option; nothing changed")
fast["preset"]["model"] = TARGET
a, b = copy.deepcopy(old), copy.deepcopy(cfg)          # nothing else may change
for x in (a, b):
    for s in x["modelSpecs"]["list"]:
        if s.get("name") == "fastegy-fast":
            s["preset"].pop("model", None)
if a != b:
    sys.exit("ERROR: unexpected config changes; nothing changed")
yaml.safe_dump(cfg, sys.stdout, sort_keys=False, allow_unicode=True, width=1000)
PY
)
TARGET=fastegy-smart; [ "${1:-}" = --back ] && TARGET=fastegy-fast
TMP_Y=$(mktemp)
trap 'rm -f "$TMP_Y"' EXIT
rc=0; docker exec -i -e TARGET="$TARGET" "$PYCTR" python3 -c "$PY_CFG" <"$CFG" >"$TMP_Y" || rc=$?
[ "$rc" = 3 ] && { rm -f "$BAK"; echo "The fast option is already on $TARGET; nothing to do."; exit 0; }
[ "$rc" = 0 ] || { echo "Config rewrite failed; nothing changed."; exit 1; }
grep -q 'fastegy-strong' "$TMP_Y" || { echo "Rewrite produced unexpected output; nothing changed."; exit 1; }
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
docker exec "$LC" cat /app/librechat.yaml | cmp -s - "$CFG" || rollback "LibreChat does not see the new file"
echo "The fast option now runs on: $TARGET"
echo "DONE kb 16. Backup: $BAK"
echo "Check: ONLY=fastegy-fast bash /root/chat_test.sh"
