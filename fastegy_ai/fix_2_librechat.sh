#!/usr/bin/env bash
# =============================================================================
# FastEgy AI — fix 2/2: LibreChat options (run after fix_1_litellm.sh passed)
#
# Change : - "FastEgy AI" endpoint models -> [fastegy-fast, fastegy-smart]
#            (drops the dead groq models, gemini-2.5-pro and the paid gemini-image)
#          - chat titles + memory agent -> fastegy-fast
#          - spec fastegy-fast   -> fastegy-fast
#          - spec fastegy-strong -> fastegy-smart, label without "بحث في النت"
#            (no web search is configured)
#          - spec fastegy-vision removed (owner: image analysis not needed)
#          Spec names are kept so bookmarked ?spec= links still work.
# Safety : backup first; file rewritten in place (single-file bind mount);
#          automatic rollback if LibreChat does not come back healthy.
# Run    : sudo bash fix_2_librechat.sh
# Version: 1.0 — 2026-10-08
# =============================================================================
set -euo pipefail

CFG=/root/librechat/librechat.yaml
CTR=librechat
PYCTR=litellm            # has python3 + PyYAML; LibreChat's image is Node only

[ -f "$CFG" ] || { echo "Missing $CFG"; exit 1; }
docker inspect "$CTR" >/dev/null 2>&1 || { echo "Container $CTR not found"; exit 1; }

BAK="$CFG.bak.$(date +%Y%m%d_%H%M%S)"
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
cp -p "$CFG" "$BAK"
echo "Backup: $BAK"

PY=$(cat <<'PY'
import sys, yaml
cfg = yaml.safe_load(sys.stdin)
try:
    ep = next(e for e in cfg["endpoints"]["custom"] if e.get("name") == "FastEgy AI")
    specs = cfg["modelSpecs"]["list"]
except (KeyError, StopIteration, TypeError):
    sys.exit("ERROR: 'FastEgy AI' endpoint or modelSpecs not found; nothing changed")

ep["models"]["default"] = ["fastegy-fast", "fastegy-smart"]
ep["titleModel"] = "fastegy-fast"

kept = []
for s in specs:
    name = s.get("name")
    if name == "fastegy-vision":
        continue
    if name == "fastegy-fast":
        s["preset"]["model"] = "fastegy-fast"
        s["preset"]["maxContextTokens"] = 128000
    if name == "fastegy-strong":
        s["label"] = "FastEgy AI 💎 ذكي"
        s["description"] = "النموذج الأقوى — للأسئلة اللي محتاجة تفكير وتحليل"
        s["preset"]["model"] = "fastegy-smart"
        s["preset"]["maxContextTokens"] = 128000
    kept.append(s)
cfg["modelSpecs"]["list"] = kept

agent = (cfg.get("memory") or {}).get("agent")
if agent:
    agent["model"] = "fastegy-fast"
yaml.safe_dump(cfg, sys.stdout, sort_keys=False, allow_unicode=True, width=1000)
PY
)
if ! docker exec -i "$PYCTR" python3 -c "$PY" <"$CFG" >"$TMP"; then
  docker exec -i "$PYCTR" python -c "$PY" <"$CFG" >"$TMP"
fi
grep -q 'fastegy-smart' "$TMP" || { echo "Rewrite produced unexpected output; nothing changed"; exit 1; }

cat "$TMP" >"$CFG"        # in place: keeps the inode the container is bound to

rollback() {
  echo "!! $1 — rolling back"
  cat "$BAK" >"$CFG"
  docker restart "$CTR" >/dev/null
  echo "Restored $BAK and restarted $CTR (the site needs ~30 s)."
  exit 1
}

# the container must see the new file before we restart it
docker exec "$CTR" grep -q 'fastegy-smart' /app/librechat.yaml || rollback "container does not see the new file"

echo "Restarting $CTR (site down ~30 s)..."
docker restart "$CTR" >/dev/null || rollback "restart failed"
code=000
for _ in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:3080/api/config || true)
  [ "$code" = 200 ] && break
  sleep 2
done
[ "$code" = 200 ] || rollback "LibreChat did not come back (HTTP $code)"

# LibreChat logs "Invalid custom config file at ..." when the YAML fails validation
if docker logs --since 3m "$CTR" 2>&1 | grep -i 'invalid custom config'; then
  rollback "LibreChat rejected the new config (lines above)"
fi
curl -s -m 10 http://127.0.0.1:3080/api/config | grep -q 'fastegy-smart' ||
  echo "NOTE: /api/config does not list fastegy-smart; check the options in the browser."

echo "== options now offered"
grep -nE 'name: fastegy|label:|model: fastegy|titleModel' "$CFG"
echo "DONE fix 2/2. Backup kept at: $BAK"
echo "Now test in the browser: both options answer at ai.fastegy.net, and new chats get a title."
