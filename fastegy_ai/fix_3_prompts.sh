#!/usr/bin/env bash
# =============================================================================
# FastEgy AI — fix 3: stop the models from inventing live data
#
# Why    : with no internet access, both options answered "today's dollar rate"
#          with a pre-2024 number, and the smart one even claimed "reliable
#          sources, 7 Oct 2026". Sales could quote wrong prices from that.
# Change : appends a fixed rules block to the promptPrefix of the two specs
#          (fastegy-fast, fastegy-strong): no internet/live data, never give
#          rates/prices/stock/news numbers, never cite sources or dates it
#          does not have, say "not sure" instead of guessing.
# Safety : 1) tests the new instructions on both models through LiteLLM FIRST;
#             if a model still answers with a rate, nothing is changed.
#          2) backup, in-place rewrite, restart, automatic rollback on failure.
# Run    : sudo bash fix_3_prompts.sh
# Version: 1.0 — 2026-10-08
# =============================================================================
set -euo pipefail

CFG=/root/librechat/librechat.yaml
CTR=librechat
PYCTR=litellm
URL=${LITELLM_URL:-http://76.13.48.108:4000/v1}

[ -f "$CFG" ] || { echo "Missing $CFG"; exit 1; }

# The quotes inside RULES are part of the Arabic text, not shell syntax.
# shellcheck disable=SC2089
RULES='قواعد ثابتة:
- معندكش اتصال بالإنترنت ولا بأي بيانات لحظية، ومعلوماتك العامة ممكن تكون قديمة.
- ممنوع تدّي أرقام عن أسعار العملات أو أسعار المنتجات أو المخزون أو الأخبار أو أي حاجة بتتغير، حتى لو "تقريبًا".
- ممنوع تقول إن معلومة جاية من مصدر معين أو محدّثة بتاريخ معين، لأنك مش متصل بأي مصدر.
- لو اتسألت عن حاجة زي كده: قول بوضوح إن المعلومة اللحظية مش متاحة عندك، ووجّه السائل للمصدر الصح (البنك المركزي المصري لسعر الصرف، وأودو للأسعار والمخزون).
- لو مش متأكد من معلومة فنية عن منتج أو كود، قول إنك مش متأكد بدل ما تخمّن.'
# shellcheck disable=SC2090
export RULES

KEY=$(grep -A3 'name: FastEgy AI' "$CFG" | sed -n 's/.*apiKey: *//p' | tr -d "\"' " || true)
[ -n "$KEY" ] || { echo "Could not read the LibreChat endpoint key; nothing changed"; exit 1; }
export KEY URL

# ---- 1) test the instructions on both models before touching anything ----
echo "== testing the new instructions (question: سعر الدولار النهارده كام؟)"
if ! python3 - <<'PY'
import json, os, re, sys, urllib.request
base = {
    "fastegy-fast": "انت المساعد الذكي لشركة FastEgy. اللغة: عربي مصري. كن مختصر.",
    "fastegy-smart": "انت المساعد الذكي لشركة FastEgy. اللغة: عربي مصري. كن مختصر وعملي.",
}
rate = re.compile(r"[0-9٠-٩]{2}\s*[.,٫]\s*[0-9٠-٩]{1,2}|[0-9٠-٩]{2}\s*(جنيه|ج\.?\s?م|EGP)")
bad = False
for model, prefix in base.items():
    body = {"model": model, "max_tokens": 1500, "messages": [
        {"role": "system", "content": prefix + "\n\n" + os.environ["RULES"]},
        {"role": "user", "content": "سعر الدولار في مصر النهارده كام؟"}]}
    req = urllib.request.Request(os.environ["URL"] + "/chat/completions", json.dumps(body).encode(),
                                 {"Authorization": "Bearer " + os.environ["KEY"], "Content-Type": "application/json"})
    try:
        answer = json.load(urllib.request.urlopen(req, timeout=180))["choices"][0]["message"]["content"] or ""
    except Exception as e:
        print(f"{model}: FAIL {e}"); bad = True; continue
    verdict = "FAIL (still gives a rate)" if rate.search(answer) else "OK"
    bad |= verdict != "OK"
    print(f"--- {model}: {verdict}\n{answer.strip()[:600]}\n")
sys.exit(1 if bad else 0)
PY
then
  echo "The instructions did not hold on every model (answers above). NOTHING was changed; send this output to Claude."
  exit 1
fi
echo "Instructions work on both models."

# ---- 2) apply to librechat.yaml ----
BAK="$CFG.bak.$(date +%Y%m%d_%H%M%S)"
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
cp -p "$CFG" "$BAK"
echo "Backup: $BAK"

PY=$(cat <<'PY'
import os, sys, yaml
cfg = yaml.safe_load(sys.stdin)
rules = os.environ["RULES"]
done = 0
for s in cfg["modelSpecs"]["list"]:
    if s.get("name") in ("fastegy-fast", "fastegy-strong"):
        base = (s["preset"].get("promptPrefix") or "").split("\n\nقواعد ثابتة:")[0].strip()
        s["preset"]["promptPrefix"] = base + "\n\n" + rules
        done += 1
if done != 2:
    sys.exit("ERROR: expected specs fastegy-fast and fastegy-strong; nothing changed")
yaml.safe_dump(cfg, sys.stdout, sort_keys=False, allow_unicode=True, width=1000)
PY
)
if ! docker exec -i -e RULES "$PYCTR" python3 -c "$PY" <"$CFG" >"$TMP"; then
  docker exec -i -e RULES "$PYCTR" python -c "$PY" <"$CFG" >"$TMP"
fi
[ "$(grep -c 'قواعد ثابتة' "$TMP")" -eq 2 ] || { echo "Rewrite produced unexpected output; nothing changed"; exit 1; }

cat "$TMP" >"$CFG"        # in place: keeps the inode the container is bound to

rollback() {
  echo "!! $1 — rolling back"
  cat "$BAK" >"$CFG"
  docker restart "$CTR" >/dev/null
  echo "Restored $BAK and restarted $CTR (the site needs ~30 s)."
  exit 1
}

docker exec "$CTR" grep -q 'قواعد ثابتة' /app/librechat.yaml || rollback "container does not see the new file"

echo "Restarting $CTR (site down ~30 s)..."
docker restart "$CTR" >/dev/null || rollback "restart failed"
code=000
for _ in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:3080/api/config || true)
  [ "$code" = 200 ] && break
  sleep 2
done
[ "$code" = 200 ] || rollback "LibreChat did not come back (HTTP $code)"
if docker logs --since 3m "$CTR" 2>&1 | grep -i 'invalid custom config'; then
  rollback "LibreChat rejected the new config (lines above)"
fi

echo "DONE fix 3. Backup kept at: $BAK"
echo "Test in the browser: open a NEW chat on each option and ask about today's dollar rate."
