#!/usr/bin/env bash
# =============================================================================
# FastEgy AI — web search 3: better results + correct dates
#
# A) Search quality. From this server Google answers "access denied",
#    DuckDuckGo a CAPTCHA, and Bing returns unrelated pages for Arabic queries.
#    Try routing SearXNG through the existing Cloudflare WARP container
#    (port 1080). Result quality is measured on the three engines LibreChat
#    queries (google, bing, duckduckgo) BEFORE and AFTER; the WARP route is kept
#    only if it scores higher, otherwise it is reverted automatically.
#    Touches only the searxng container (no LibreChat downtime).
# B) Dates. The model invented "22 October 2026". The rules block of both specs
#    now starts with today's date via LibreChat's {{current_date}} variable and
#    forbids invented dates. LibreChat restarts (~30 s).
# Run    : sudo bash web_3_quality.sh
# Version: 1.0 — 2026-10-08
# =============================================================================
set -euo pipefail

LC=librechat
PYCTR=litellm
SX=searxng
SXCFG=/root/searxng/settings.yml
CFG=/root/librechat/librechat.yaml
WARP=warp
TS=$(date +%Y%m%d_%H%M%S)

for c in "$LC" "$SX" "$PYCTR"; do
  [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = true ] || { echo "$c is not running; nothing changed."; exit 1; }
done

# ---------------------------------------------------------------- A) search
QUALITY_JS='
const qs = [["سعر الدولار في مصر اليوم", t => /[\u0600-\u06FF]|egp|exchange|dollar|usd/i.test(t)],
            ["Hikvision DS-2CD1043G2-LIUF datasheet", t => /1043/.test(t)]];
const es = ["google", "bing", "duckduckgo"];
(async () => {
  let score = 0;
  for (const [q, relevant] of qs) for (const e of es) {
    try {
      const j = await (await fetch("http://searxng:8080/search?format=json&engines=" + e + "&q=" + encodeURIComponent(q))).json();
      const rel = j.results.slice(0, 3).filter(r => relevant((r.title || "") + " " + (r.url || ""))).length;
      if (rel > 0) score++;
      const down = (j.unresponsive_engines || []).map(x => x[1]).join(", ");
      console.log("  [" + e + "] " + q.slice(0, 24) + ": " + j.results.length + " results, " + rel + "/3 relevant" + (down ? " (" + down + ")" : ""));
    } catch (err) { console.log("  [" + e + "] error: " + err.message); }
  }
  console.log("SCORE " + score);
})();'

measure() {  # prints the per-engine lines, sets SCORE
  local out
  out=$(docker exec "$LC" node -e "$QUALITY_JS" 2>&1 || true)
  printf '%s\n' "$out" | grep -v '^SCORE '
  SCORE=$(printf '%s\n' "$out" | sed -n 's/^SCORE //p')
  SCORE=${SCORE:-0}
}
wait_searxng() {
  for _ in $(seq 1 20); do
    docker exec "$LC" node -e 'fetch("http://searxng:8080/healthz").then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))' && return 0
    sleep 2
  done
  return 1
}

echo "== A) search quality now (direct from this server)"
measure; BEFORE=$SCORE
echo "   score: $BEFORE / 6"

if ! docker inspect "$WARP" >/dev/null 2>&1; then
  echo "   no $WARP container; keeping the direct route."
else
  WARP_NET=$(docker inspect "$WARP" -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' | awk '{print $1}')
  GOST=$(docker inspect "$WARP" -f '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^GOST_ARGS=//p')
  CRED=$(printf '%s' "$GOST" | grep -oE '//[^@ ]+@' | sed 's#^//##; s#@$##' || true)   # never printed
  cp -p "$SXCFG" "$SXCFG.bak.$TS"
  CONNECTED=no
  if ! docker inspect "$SX" -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' | grep -qw "$WARP_NET"; then
    docker network connect "$WARP_NET" "$SX"; CONNECTED=yes
  fi

  PY_SX=$(cat <<'PY'
import os, sys, yaml
c = yaml.safe_load(sys.stdin)
c.setdefault("outgoing", {})["proxies"] = {"all://": [os.environ["PROXY"]]}
yaml.safe_dump(c, sys.stdout, sort_keys=False, allow_unicode=True, width=1000)
PY
)
  BEST=$BEFORE; BEST_FILE=""; BEST_SCHEME=""
  # WARP's proxy (gost) usually accepts both; try each and keep the best
  for scheme in socks5h http; do
    PROXY="$scheme://${CRED:+$CRED@}$WARP:1080"; export PROXY
    TMP_SX=$(mktemp)
    docker exec -i -e PROXY "$PYCTR" python3 -c "$PY_SX" <"$SXCFG.bak.$TS" >"$TMP_SX"
    cat "$TMP_SX" >"$SXCFG"; rm -f "$TMP_SX"
    docker restart "$SX" >/dev/null
    if wait_searxng; then
      echo "== A) search quality through WARP ($scheme)"
      measure
      echo "   score: $SCORE / 6"
      if [ "$SCORE" -gt "$BEST" ]; then
        BEST=$SCORE; BEST_SCHEME=$scheme
        [ -n "$BEST_FILE" ] || BEST_FILE=$(mktemp)
        cp "$SXCFG" "$BEST_FILE"
      fi
    else
      echo "   searxng did not come back through WARP ($scheme)"
    fi
  done

  if [ -n "$BEST_FILE" ]; then
    cat "$BEST_FILE" >"$SXCFG"; rm -f "$BEST_FILE"
    docker restart "$SX" >/dev/null; wait_searxng || true
    echo "   -> keeping the WARP route ($BEST_SCHEME), score $BEST vs $BEFORE direct. Backup: $SXCFG.bak.$TS"
  else
    cat "$SXCFG.bak.$TS" >"$SXCFG"
    if [ "$CONNECTED" = yes ]; then docker network disconnect "$WARP_NET" "$SX" >/dev/null 2>&1 || true; fi
    docker restart "$SX" >/dev/null; wait_searxng || true
    echo "   -> WARP is not better; reverted to the direct route."
  fi
fi

# ---------------------------------------------------------------- B) dates
echo "== B) today's date in the instructions"
BAK="$CFG.bak.$TS"
cp -p "$CFG" "$BAK"
PY_DATE=$(cat <<'PY'
import sys, yaml
DATE_RULES = """- تاريخ النهارده: {{current_date}}. أي تاريخ تكتبه في الرد لازم يكون ده، أو التاريخ المكتوب فعلًا في المصدر. ممنوع تألّف تاريخ.
- لما تنقل رقم من مصدر، اكتب جنبه تاريخه زي ما هو في المصدر، ولو المصدر مفيهوش تاريخ قول كده."""
cfg = yaml.safe_load(sys.stdin)
done = 0
for s in cfg["modelSpecs"]["list"]:
    if s.get("name") in ("fastegy-fast", "fastegy-strong"):
        p = s["preset"]["promptPrefix"]
        head, sep, rules = p.partition("قواعد ثابتة:\n")
        if not sep:
            sys.exit("ERROR: rules block not found; nothing changed")
        rules = "\n".join(l for l in rules.split("\n") if "تاريخ النهارده" not in l and "اكتب جنبه تاريخه" not in l)
        s["preset"]["promptPrefix"] = head + sep + DATE_RULES + "\n" + rules
        done += 1
if done != 2:
    sys.exit("ERROR: expected specs fastegy-fast and fastegy-strong; nothing changed")
yaml.safe_dump(cfg, sys.stdout, sort_keys=False, allow_unicode=True, width=1000)
PY
)
TMP_Y=$(mktemp)
trap 'rm -f "$TMP_Y"' EXIT
docker exec -i "$PYCTR" python3 -c "$PY_DATE" <"$CFG" >"$TMP_Y"
[ "$(grep -c 'current_date' "$TMP_Y")" -eq 2 ] || { echo "Rewrite produced unexpected output; LibreChat config unchanged"; exit 1; }
cat "$TMP_Y" >"$CFG"

rollback() {
  echo "!! $1 — rolling back the date change"
  cat "$BAK" >"$CFG"; docker restart "$LC" >/dev/null
  echo "Restored $BAK and restarted LibreChat."; exit 1
}
docker exec "$LC" grep -q 'current_date' /app/librechat.yaml || rollback "container does not see the new file"
echo "Restarting LibreChat (site down ~30 s)..."
docker restart "$LC" >/dev/null || rollback "restart failed"
code=000
for _ in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:3080/api/config || true)
  [ "$code" = 200 ] && break
  sleep 2
done
[ "$code" = 200 ] || rollback "LibreChat did not come back (HTTP $code)"
if docker logs --since 3m "$LC" 2>&1 | grep -i 'invalid custom config'; then
  rollback "LibreChat rejected the new config (lines above)"
fi
echo "DONE web 3. Backup: $BAK"
echo "Test: refresh (Cmd+Shift+R), NEW chat on the smart option, ask today's dollar rate; check the date."
