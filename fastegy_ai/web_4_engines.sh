#!/usr/bin/env bash
# =============================================================================
# FastEgy AI — web search 4: use the search engines that work from this server
#
# Why    : LibreChat 0.8.6 always asks SearXNG for "google,bing,duckduckgo".
#          From this VPS Google is denied and DuckDuckGo shows a CAPTCHA, so only
#          Bing answers, and Bing returns unrelated pages for Arabic queries.
# A) Test 11 engines from this server with 4 queries (English/Arabic dollar
#    rate, a Hikvision model code, ColorVu). The best two that score >= 2/4 are
#    served under the names "google" and "duckduckgo" (SearXNG allows an entry
#    with any name to use any engine), so LibreChat's fixed request reaches
#    engines that work. Kept only if LibreChat's own request scores better than
#    before; otherwise SearXNG is restored. No LibreChat downtime for this part.
# B) Rules: write search queries in English (answer in Arabic); search model
#    codes exactly with "datasheet". LibreChat restarts (~30 s).
# Run    : sudo bash web_4_engines.sh
# Version: 1.0 — 2026-10-08
# =============================================================================
set -euo pipefail

LC=librechat
SX=searxng
PYCTR=litellm
SXCFG=/root/searxng/settings.yml
CFG=/root/librechat/librechat.yaml
TS=$(date +%Y%m%d_%H%M%S)
CANDS="yahoo mojeek startpage qwant yandex dogpile yep brave bing duckduckgo google"
export CANDS

for c in "$LC" "$SX" "$PYCTR"; do
  [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = true ] || { echo "$c is not running; nothing changed."; exit 1; }
done

MEASURE_JS='
const qs = [
  ["USD to EGP exchange rate today", t => /egp|egypt|exchange|usd|dollar/i.test(t)],
  ["سعر الدولار في مصر اليوم", t => /[؀-ۿ]|egp|dollar/i.test(t)],
  ["Hikvision DS-2CD1043G2-LIUF datasheet", t => /1043/.test(t)],
  ["Hikvision ColorVu camera", t => /colorvu/i.test(t)],
];
(async () => {
  for (const set of process.env.SETS.split(" ")) {
    let score = 0; const notes = [];
    for (const [q, ok] of qs) {
      try {
        const j = await (await fetch("http://searxng:8080/search?format=json&engines=" + set + "&q=" + encodeURIComponent(q))).json();
        const hit = j.results.slice(0, 3).some(r => ok((r.title || "") + " " + (r.url || "")));
        if (hit) score++;
        for (const u of j.unresponsive_engines || []) notes.push(u[1]);
      } catch (e) { notes.push(e.message); }
    }
    const why = [...new Set(notes)].join(", ");
    console.log("  " + set.padEnd(26) + score + "/4" + (why ? "   (" + why + ")" : ""));
    console.log("SCORE " + set + " " + score);
  }
})();'

declare -A SC
measure() {  # measure "set1 set2 ..." -> prints table, fills SC[set]
  local out
  out=$(docker exec -e SETS="$1" "$LC" node -e "$MEASURE_JS" 2>&1 || true)
  printf '%s\n' "$out" | grep -v '^SCORE '
  while read -r _ name n; do SC[$name]=$n; done < <(printf '%s\n' "$out" | grep '^SCORE ')
}
wait_searxng() {
  for _ in $(seq 1 20); do
    docker exec "$LC" node -e 'fetch("http://searxng:8080/healthz").then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))' && return 0
    sleep 2
  done
  return 1
}
apply_sx() {  # apply_sx PYCODE: rewrite settings from the backup, restart searxng
  local tmp; tmp=$(mktemp)
  docker exec -i -e CANDS -e ALIAS_GOOGLE -e ALIAS_DDG "$PYCTR" python3 -c "$1" <"$SXCFG.bak.$TS" >"$tmp" || true
  grep -q '^engines:' "$tmp" || { rm -f "$tmp"; return 1; }
  cat "$tmp" >"$SXCFG"; rm -f "$tmp"
  docker restart "$SX" >/dev/null
  wait_searxng
}
restore_sx() {
  cat "$SXCFG.bak.$TS" >"$SXCFG"; docker restart "$SX" >/dev/null; wait_searxng || true
}

cp -p "$SXCFG" "$SXCFG.bak.$TS"
echo "== A) LibreChat's search request today (google,bing,duckduckgo)"
measure "google,bing,duckduckgo"; BASE=${SC[google,bing,duckduckgo]:-0}

# Phase 1: switch every candidate on and score each one alone
PY_ENABLE=$(cat <<'PY'
import os, sys, yaml
c = yaml.safe_load(sys.stdin)
cands = os.environ["CANDS"].split()
c["use_default_settings"] = True
c["engines"] = [e for e in (c.get("engines") or []) if e.get("name") not in cands] + \
               [{"name": n, "disabled": False, "inactive": False} for n in cands]
yaml.safe_dump(c, sys.stdout, sort_keys=False, allow_unicode=True, width=1000)
PY
)
echo "== A) testing each engine from this server (about a minute)"
export ALIAS_GOOGLE="" ALIAS_DDG=""
apply_sx "$PY_ENABLE" || { restore_sx; echo "searxng did not start with the test settings; restored, nothing changed."; exit 1; }
# shellcheck disable=SC2086
measure "$(echo $CANDS)"

# Phase 2: pick the best two (score >= 2) to stand in for google / duckduckgo
mapfile -t RANKED < <(for c in yahoo mojeek startpage qwant yandex dogpile yep brave; do
  s=${SC[$c]:-0}; [ "$s" -ge 2 ] && echo "$s $c"; done | sort -s -k1,1nr | awk '{print $2}')
ALIAS_GOOGLE=""; ALIAS_DDG=""; i=0
if [ "${SC[google]:-0}" -lt 2 ] && [ "${#RANKED[@]}" -gt $i ]; then ALIAS_GOOGLE=${RANKED[$i]}; i=$((i+1)); fi
if [ "${SC[duckduckgo]:-0}" -lt 2 ] && [ "${#RANKED[@]}" -gt $i ]; then ALIAS_DDG=${RANKED[$i]}; fi
export ALIAS_GOOGLE ALIAS_DDG

PY_FINAL=$(cat <<'PY'
import copy, os, sys, yaml
# default SearXNG definitions of the candidate engines (module + required params)
T = {
  "yahoo": {"engine": "yahoo"},
  "mojeek": {"engine": "mojeek", "categories": ["general", "web"]},
  "startpage": {"engine": "startpage", "startpage_categ": "web", "categories": ["general", "web"]},
  "qwant": {"engine": "qwant", "qwant_categ": "web", "categories": ["general", "web"]},
  "yandex": {"engine": "yandex", "categories": "general", "search_type": "web"},
  "dogpile": {"engine": "dogpile", "dogpile_categ": "search", "categories": "general"},
  "yep": {"engine": "yep", "categories": "general"},
  "brave": {"engine": "brave", "time_range_support": True, "paging": True,
            "categories": ["general", "web"], "brave_category": "search"},
}
c = yaml.safe_load(sys.stdin)
alias = {k: v for k, v in (("google", os.environ.get("ALIAS_GOOGLE")), ("duckduckgo", os.environ.get("ALIAS_DDG"))) if v}
base = [e for e in (c.get("engines") or []) if e.get("name") not in ("google", "bing", "duckduckgo")]
base.append({"name": "bing", "disabled": False})
for name in ("google", "duckduckgo"):
    if name in alias:
        e = copy.deepcopy(T[alias[name]])
        e.update({"name": name, "shortcut": "fx" + name[:2], "disabled": False, "inactive": False})
    else:
        e = {"name": name, "disabled": False}
    base.append(e)
c["engines"] = base
c["use_default_settings"] = {"engines": {"remove": sorted(alias)}} if alias else True
yaml.safe_dump(c, sys.stdout, sort_keys=False, allow_unicode=True, width=1000)
PY
)

if [ -z "$ALIAS_GOOGLE$ALIAS_DDG" ]; then
  restore_sx
  echo "   -> no engine scored 2/4 or better; search settings restored unchanged."
else
  echo "== A) serving: google -> ${ALIAS_GOOGLE:-google itself}, duckduckgo -> ${ALIAS_DDG:-duckduckgo itself}"
  if apply_sx "$PY_FINAL"; then
    measure "google,bing,duckduckgo"; FINAL=${SC[google,bing,duckduckgo]:-0}
  else
    FINAL=-1
  fi
  if [ "$FINAL" -gt "$BASE" ]; then
    echo "   -> kept: LibreChat's search now scores $FINAL/4 (was $BASE/4). Backup: $SXCFG.bak.$TS"
  else
    restore_sx
    echo "   -> not better ($FINAL vs $BASE); search settings restored unchanged."
  fi
fi

# ---------------------------------------------------------------- B) rules
echo "== B) search rules in the instructions"
BAK="$CFG.bak.$TS"
cp -p "$CFG" "$BAK"
PY_RULES=$(cat <<'PY'
import sys, yaml
ADD = ["- لما تستخدم أداة البحث: اكتب جملة البحث بالإنجليزي حتى لو السؤال بالعربي، وبعدين رد بالعربي.",
       "- لو بتدوّر على كود منتج: ابحث بالكود بالظبط وجنبه كلمة datasheet. ولو ما لقيتش صفحة للكود نفسه، قول إنك مش متأكد من مواصفاته."]
cfg = yaml.safe_load(sys.stdin)
done = 0
for s in cfg["modelSpecs"]["list"]:
    if s.get("name") in ("fastegy-fast", "fastegy-strong"):
        p = s["preset"]["promptPrefix"]
        if "قواعد ثابتة:" not in p:
            sys.exit("ERROR: rules block not found; nothing changed")
        lines = [l for l in p.split("\n") if "جملة البحث بالإنجليزي" not in l and "ابحث بالكود بالظبط" not in l]
        s["preset"]["promptPrefix"] = "\n".join(lines).rstrip() + "\n" + "\n".join(ADD)
        done += 1
if done != 2:
    sys.exit("ERROR: expected specs fastegy-fast and fastegy-strong; nothing changed")
yaml.safe_dump(cfg, sys.stdout, sort_keys=False, allow_unicode=True, width=1000)
PY
)
TMP_Y=$(mktemp)
trap 'rm -f "$TMP_Y"' EXIT
docker exec -i "$PYCTR" python3 -c "$PY_RULES" <"$CFG" >"$TMP_Y"
[ "$(grep -c 'جملة البحث بالإنجليزي' "$TMP_Y")" -eq 2 ] || { echo "Rewrite produced unexpected output; LibreChat config unchanged"; exit 1; }
cat "$TMP_Y" >"$CFG"

rollback() {
  echo "!! $1 — rolling back the rules change"
  cat "$BAK" >"$CFG"; docker restart "$LC" >/dev/null
  echo "Restored $BAK and restarted LibreChat."; exit 1
}
docker exec "$LC" grep -q 'datasheet' /app/librechat.yaml || rollback "container does not see the new file"
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
echo "DONE web 4. Backup: $BAK"
echo "Test: refresh (Cmd+Shift+R), NEW chat on the smart option, ask about the dollar rate and about a model code."
