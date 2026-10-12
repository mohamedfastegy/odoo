#!/usr/bin/env bash
# =============================================================================
# FastEgy AI — fix 1/2: LiteLLM model mapping
#
# Why    : Groq decommissioned every model LiteLLM pointed at (llama-3.1-8b,
#          llama-3.3-70b, qwen-qwq-32b, llama-4-scout). Only gemini-2.5-pro
#          still answers, so the "fast" option, titles and memory are down.
# Change : - remove the 4 dead groq aliases
#          - fastegy-fast     -> groq/openai/gpt-oss-20b    (free tier)
#          - fastegy-smart    -> groq/openai/gpt-oss-120b   (free tier)
#          - fastegy-fallback -> gemini/gemini-flash-lite-latest (cheapest Gemini)
#          - fastegy-fast / fastegy-smart fall back to fastegy-fallback on errors
#            or when the Groq daily limit is reached
#          gemini-2.5-pro, gemini-image, dall-e-3 stay untouched.
# Safety : backup first; file rewritten in place (single-file bind mount);
#          automatic rollback if fast/smart do not answer after the restart.
#          API keys are reused as-is from the existing entries, never printed.
# Run    : sudo bash fix_1_litellm.sh
# Version: 1.0 — 2026-10-08
# =============================================================================
set -euo pipefail

CFG=/root/litellm/config.yaml
LC_CFG=/root/librechat/librechat.yaml
URL=http://76.13.48.108:4000/v1          # same URL LibreChat uses
CTR=litellm

[ -f "$CFG" ] || { echo "Missing $CFG"; exit 1; }
docker inspect "$CTR" >/dev/null 2>&1 || { echo "Container $CTR not found"; exit 1; }

BAK="$CFG.bak.$(date +%Y%m%d_%H%M%S)"
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
cp -p "$CFG" "$BAK"
echo "Backup: $BAK"

# Rewrite with PyYAML inside the litellm container (always installed there).
PY=$(cat <<'PY'
import sys, copy, yaml
cfg = yaml.safe_load(sys.stdin)
ml = cfg.get("model_list") or []
groq = next((m for m in ml if str(m.get("litellm_params", {}).get("model", "")).startswith("groq/")), None)
gem = next((m for m in ml if m.get("model_name") == "gemini-2.5-pro"), None)
if groq is None or gem is None:
    sys.exit("ERROR: expected a groq/... entry and the gemini-2.5-pro entry; nothing changed")

def entry(name, src, model):
    params = copy.deepcopy(src["litellm_params"])   # keeps the provider's api_key reference
    params["model"] = model
    return {"model_name": name, "litellm_params": params}

dead = {"llama-3.3-70b", "qwen-32b", "llama-3.1-8b", "llama-4-scout-vision"}
ours = {"fastegy-fast", "fastegy-smart", "fastegy-fallback"}
cfg["model_list"] = [m for m in ml if m.get("model_name") not in dead | ours] + [
    entry("fastegy-fast", groq, "groq/openai/gpt-oss-20b"),
    entry("fastegy-smart", groq, "groq/openai/gpt-oss-120b"),
    entry("fastegy-fallback", gem, "gemini/gemini-flash-lite-latest"),
]
ls = cfg.get("litellm_settings") or {}
fb = [f for f in (ls.get("fallbacks") or []) if not (set(f) & {"fastegy-fast", "fastegy-smart"})]
ls["fallbacks"] = fb + [{"fastegy-fast": ["fastegy-fallback"]}, {"fastegy-smart": ["fastegy-fallback"]}]
cfg["litellm_settings"] = ls
yaml.safe_dump(cfg, sys.stdout, sort_keys=False, allow_unicode=True, width=1000)
PY
)
if ! docker exec -i "$CTR" python3 -c "$PY" <"$CFG" >"$TMP"; then
  docker exec -i "$CTR" python -c "$PY" <"$CFG" >"$TMP"
fi
grep -q 'fastegy-fallback' "$TMP" || { echo "Rewrite produced unexpected output; nothing changed"; exit 1; }

cat "$TMP" >"$CFG"        # in place: keeps the inode the container is bound to
echo "New aliases:"; grep -nE 'model_name|model:' "$CFG" | grep -v -i key

rollback() {
  echo "!! $1 — rolling back"
  cat "$BAK" >"$CFG"
  docker restart "$CTR" >/dev/null
  echo "Restored $BAK and restarted $CTR."
  exit 1
}

echo "Restarting $CTR..."
docker restart "$CTR" >/dev/null || rollback "restart failed"
for _ in $(seq 1 45); do
  curl -s -m 3 http://127.0.0.1:4000/health/liveliness >/dev/null 2>&1 && break
  sleep 2
done

# LibreChat's key for this endpoint (read, never printed)
KEY=$(grep -A3 'name: FastEgy AI' "$LC_CFG" | sed -n 's/.*apiKey: *//p' | tr -d "\"' " || true)
[ -n "$KEY" ] || rollback "could not read the LibreChat endpoint key"

ask() { # $1 model, $2 extra JSON fields -> prints "OK <served model>" or "FAIL <reason>"
  curl -s -m 120 "$URL/chat/completions" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$1\",\"messages\":[{\"role\":\"user\",\"content\":\"رد بكلمة واحدة: تمام\"}],\"max_tokens\":400$2}" |
    python3 -c 'import sys,json
try:
    d = json.load(sys.stdin)
except Exception as e:
    print("FAIL bad response:", e); sys.exit()
print("OK " + str(d.get("model")) if "choices" in d else "FAIL " + str(d.get("error", d))[:200])'
}

echo "== tests"
fast=$(ask fastegy-fast "" || true);      echo "fastegy-fast      $fast"
smart=$(ask fastegy-smart "" || true);    echo "fastegy-smart     $smart"
fbk=$(ask fastegy-fallback "" || true);   echo "fastegy-fallback  $fbk"
mock=$(ask fastegy-fast ',"mock_testing_fallbacks":true' || true); echo "fallback test     $mock"
pro=$(ask gemini-2.5-pro "" || true);     echo "gemini-2.5-pro    $pro"

# anything that is not "OK ..." (including an empty reply) counts as a failure
[[ $fast == OK* && $smart == OK* ]] || rollback "fast/smart did not answer"
[[ $fbk == OK* && $mock == OK* ]] || echo "WARNING: fast/smart work, but the Gemini fallback did not pass. Keeping the change; send this output to Claude."
echo "DONE fix 1/2. Backup kept at: $BAK"
