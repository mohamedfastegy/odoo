#!/usr/bin/env bash
# =============================================================================
# FastEgy AI — knowledge base 3: two answer rules from the first chat test
#
# The chat test (chat_test.sh) showed:
#   - DS-7608NXI-K1: the brochure says up to 16 TB per HDD, FastEgy's Arabic
#     description says 10 TB; the fast option gave 10 TB and credited the brochure.
#   - both options added small claims that were in neither source.
# Change : two lines added to the catalog rules of both options. Nothing else.
#          LibreChat restarts (~30 s); rollback on failure.
# Run    : bash kb_3_rules.sh
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
NEW_RULES = [
    "- لو المصدرين اختلفوا في رقم (زي سعة الهارد): اعتمد على كتالوج هيك فيجن، وقول إن وصف FastEgy مكتوب فيه رقم مختلف.",
    "- ما تضيفش مميزات ولا مقارنات مش مكتوبة في نتيجة الأداة، وانسب كل معلومة لمصدرها الصح.",
]
old = yaml.safe_load(sys.stdin)
cfg = copy.deepcopy(old)
done = 0
for s in cfg["modelSpecs"]["list"]:
    if s.get("name") in ("fastegy-fast", "fastegy-strong"):
        p = s["preset"]["promptPrefix"]
        if "lookup_product" not in p:
            sys.exit("ERROR: the catalog rules are missing (run kb_2 first); nothing changed")
        lines = [l for l in p.split("\n") if l not in NEW_RULES]
        s["preset"]["promptPrefix"] = "\n".join(lines).rstrip() + "\n" + "\n".join(NEW_RULES)
        done += 1
if done != 2:
    sys.exit("ERROR: expected specs fastegy-fast and fastegy-strong; nothing changed")
a, b = copy.deepcopy(old), copy.deepcopy(cfg)          # nothing else may change
for x in (a, b):
    for s in x["modelSpecs"]["list"]:
        if s.get("name") in ("fastegy-fast", "fastegy-strong"):
            s["preset"].pop("promptPrefix", None)
if a != b:
    sys.exit("ERROR: unexpected config changes; nothing changed")
yaml.safe_dump(cfg, sys.stdout, sort_keys=False, allow_unicode=True, width=1000)
PY
)
TMP_Y=$(mktemp)
trap 'rm -f "$TMP_Y"' EXIT
docker exec -i "$PYCTR" python3 -c "$PY_CFG" <"$CFG" >"$TMP_Y" || { echo "Config rewrite failed; nothing changed."; exit 1; }
grep -q 'وانسب كل معلومة' "$TMP_Y" || { echo "Rewrite produced unexpected output; nothing changed."; exit 1; }
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
echo "DONE kb 3. Backup: $BAK"
echo "Check: bash /tmp/fastegy_check/dl/chat_test.sh   (or: bash chat_test.sh \"your question\")"
