#!/usr/bin/env bash
# =============================================================================
# FastEgy AI — web search 2/2: turn web search on in LibreChat
# (run after web_1_services.sh printed "DONE web 1/2")
#
# Change : docker-compose.yml (librechat service environment)
#            + SEARXNG_INSTANCE_URL / FIRECRAWL_API_URL / FIRECRAWL_API_KEY /
#              FIRECRAWL_VERSION  (this LibreChat version only reads web search
#              settings from environment variables)
#            ENDPOINTS=custom -> custom,agents (web search runs through agents)
#          librechat.yaml
#            + webSearch: searxng + fastegy-reader, no reranker
#            agents capabilities -> tools, web_search, artifacts (code runner and
#              file search are not installed yet, so their buttons are hidden)
#            spec fastegy-strong: web search on by default
#            rules block of both specs rewritten for web search
# Safety : refuses if recreating would also upgrade LibreChat; backups; checks
#          that nothing else changed; validates the compose file; recreates
#          only the librechat container; automatic rollback on failure.
# Run    : sudo bash web_2_enable.sh
# Version: 1.0 — 2026-10-08
# =============================================================================
set -euo pipefail

DIR=/root/librechat
COMPOSE=$DIR/docker-compose.yml
CFG=$DIR/librechat.yaml
CTR=librechat
PYCTR=litellm                      # has python3 + PyYAML
KEYFILE=/root/fastegy-reader/.key

for c in searxng fastegy-reader; do
  [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = true ] ||
    { echo "$c is not running; run web_1_services.sh first. Nothing changed."; exit 1; }
done
[ -s "$KEYFILE" ] || { echo "Missing $KEYFILE; run web_1_services.sh first. Nothing changed."; exit 1; }

# Recreating the container uses the local image. If someone pulled a newer
# "latest" since the container started, that would silently upgrade LibreChat.
IMG=$(docker inspect "$CTR" -f '{{.Config.Image}}')
if [ "$(docker inspect "$CTR" -f '{{.Image}}')" != "$(docker image inspect "$IMG" -f '{{.Id}}')" ]; then
  echo "The local image $IMG differs from the one LibreChat is running."
  echo "Recreating now would upgrade LibreChat as a side effect. Stopping; nothing changed."
  exit 1
fi
PROJ=$(docker inspect "$CTR" -f '{{index .Config.Labels "com.docker.compose.project"}}')
SVC=$(docker inspect "$CTR" -f '{{index .Config.Labels "com.docker.compose.service"}}')
[ -n "$PROJ" ] && [ -n "$SVC" ] || { echo "LibreChat is not managed by docker compose; nothing changed."; exit 1; }
DC=(docker compose -p "$PROJ" --project-directory "$DIR" -f "$COMPOSE")

TS=$(date +%Y%m%d_%H%M%S)
BAK_COMPOSE="$COMPOSE.bak.$TS"
BAK_CFG="$CFG.bak.$TS"
cp -p "$COMPOSE" "$BAK_COMPOSE"
cp -p "$CFG" "$BAK_CFG"
echo "Backups: $BAK_COMPOSE"
echo "         $BAK_CFG"
TMP_C=$(mktemp "$DIR/.compose.XXXXXX.yml")
TMP_Y=$(mktemp)
trap 'rm -f "$TMP_C" "$TMP_Y"' EXIT

READER_KEY=$(cat "$KEYFILE")
export READER_KEY SVC

pyrun() {  # pyrun CODE < in > out   (runs inside the litellm container)
  docker exec -i -e READER_KEY -e SVC "$PYCTR" python3 -c "$1"
}

# ---- docker-compose.yml: environment of the librechat service only
PY_COMPOSE=$(cat <<'PY'
import copy, os, sys, yaml
old = yaml.safe_load(sys.stdin)
new = copy.deepcopy(old)
svc = new["services"][os.environ["SVC"]]
env = svc.get("environment") or []
add = {
    "SEARXNG_INSTANCE_URL": "http://searxng:8080",
    "FIRECRAWL_API_URL": "http://fastegy-reader:3002",
    "FIRECRAWL_API_KEY": os.environ["READER_KEY"],
    "FIRECRAWL_VERSION": "v2",
}
if isinstance(env, dict):
    env = [f"{k}={'' if v is None else v}" for k, v in env.items()]
out = []
for item in env:
    key, _, val = str(item).partition("=")
    if key in add:
        continue
    if key == "ENDPOINTS" and "agents" not in [e.strip() for e in val.split(",")]:
        item = f"ENDPOINTS={val},agents"
    out.append(item)
out += [f"{k}={v}" for k, v in add.items()]
svc["environment"] = out
# nothing but that service's environment may differ
a, b = copy.deepcopy(old), copy.deepcopy(new)
a["services"][os.environ["SVC"]].pop("environment", None)
b["services"][os.environ["SVC"]].pop("environment", None)
if a != b:
    sys.exit("ERROR: unexpected compose changes; nothing changed")
yaml.safe_dump(new, sys.stdout, sort_keys=False, allow_unicode=True, width=1000)
PY
)

# ---- librechat.yaml
PY_CONFIG=$(cat <<'PY'
import copy, sys, yaml
RULES = """قواعد ثابتة:
- معلوماتك العامة ممكن تكون قديمة. أي معلومة بتتغير (سعر عملة، أخبار، أسعار في السوق، مواعيد) لازم تيجي من البحث في النت، مش من ذاكرتك.
- لو أداة البحث في النت متاحة ليك: استخدمها، وابني ردك على النتايج اللي رجعت بس، واذكر اسم المصدر.
- لو أداة البحث مش متاحة أو ما جابتش نتيجة: قول بوضوح إن المعلومة اللحظية مش متاحة، ومتدّيش أي رقم ولا "تقريبًا". ولو البحث مش متاح، اقترح على المستخدم يشغّل زرار البحث في النت أو يستخدم الخيار الذكي.
- ممنوع تقول إن معلومة جاية من مصدر أو بتاريخ معين غير لو ده ظاهر فعلًا في نتايج البحث.
- أسعار منتجات FastEgy والمخزون والعروض مش من النت: دي من أودو.
- لو مش متأكد من معلومة فنية عن منتج أو كود، قول إنك مش متأكد بدل ما تخمّن."""
old = yaml.safe_load(sys.stdin)
cfg = copy.deepcopy(old)
cfg["webSearch"] = {
    "searchProvider": "searxng",
    "searxngInstanceUrl": "${SEARXNG_INSTANCE_URL}",
    "scraperProvider": "firecrawl",
    "firecrawlApiKey": "${FIRECRAWL_API_KEY}",
    "firecrawlApiUrl": "${FIRECRAWL_API_URL}",
    "firecrawlVersion": "${FIRECRAWL_VERSION}",
    "rerankerType": "none",
    "scraperTimeout": 15000,
}
agents = cfg.setdefault("endpoints", {}).setdefault("agents", {})
agents["capabilities"] = ["tools", "web_search", "artifacts"]
done = 0
for s in cfg["modelSpecs"]["list"]:
    if s.get("name") in ("fastegy-fast", "fastegy-strong"):
        base = (s["preset"].get("promptPrefix") or "").split("\n\nقواعد ثابتة:")[0].strip()
        s["preset"]["promptPrefix"] = base + "\n\n" + RULES
        done += 1
    if s.get("name") == "fastegy-strong":
        s["webSearch"] = True
if done != 2:
    sys.exit("ERROR: expected specs fastegy-fast and fastegy-strong; nothing changed")
# nothing outside webSearch / agents capabilities / the two specs may differ
a, b = copy.deepcopy(old), copy.deepcopy(cfg)
for x in (a, b):
    x.pop("webSearch", None)
    x.get("endpoints", {}).get("agents", {}).pop("capabilities", None)
    x["modelSpecs"]["list"] = [s for s in x["modelSpecs"]["list"] if s.get("name") not in ("fastegy-fast", "fastegy-strong")]
if a != b:
    sys.exit("ERROR: unexpected config changes; nothing changed")
yaml.safe_dump(cfg, sys.stdout, sort_keys=False, allow_unicode=True, width=1000)
PY
)

pyrun "$PY_COMPOSE" <"$COMPOSE" >"$TMP_C"
pyrun "$PY_CONFIG" <"$CFG" >"$TMP_Y"
grep -q 'SEARXNG_INSTANCE_URL' "$TMP_C" && grep -q 'searxng' "$TMP_Y" ||
  { echo "Rewrite produced unexpected output; nothing changed"; exit 1; }
docker compose -p "$PROJ" --project-directory "$DIR" -f "$TMP_C" config -q ||
  { echo "New compose file did not validate; nothing changed"; exit 1; }

cat "$TMP_C" >"$COMPOSE"
cat "$TMP_Y" >"$CFG"          # in place: keeps the inode the container is bound to

wait_up() {
  local code=000
  for _ in $(seq 1 60); do
    code=$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:3080/api/config || true)
    [ "$code" = 200 ] && return 0
    sleep 2
  done
  echo "HTTP $code"; return 1
}
rollback() {
  echo "!! $1 — rolling back"
  cat "$BAK_COMPOSE" >"$COMPOSE"
  cat "$BAK_CFG" >"$CFG"
  "${DC[@]}" up -d --no-deps "$SVC" >/dev/null 2>&1 || docker restart "$CTR" >/dev/null
  wait_up || true
  echo "Restored the backups and restarted LibreChat."
  exit 1
}

echo "Recreating LibreChat with the new settings (site down ~30-60 s)..."
"${DC[@]}" up -d --no-deps "$SVC" || rollback "docker compose up failed"
wait_up || rollback "LibreChat did not come back"
if docker logs --since 3m "$CTR" 2>&1 | grep -i 'invalid custom config'; then
  rollback "LibreChat rejected the new config (lines above)"
fi
[ "$(docker exec "$CTR" printenv SEARXNG_INSTANCE_URL)" = http://searxng:8080 ] ||
  rollback "the new environment did not reach the container"
docker exec "$CTR" node -e 'fetch("http://searxng:8080/healthz").then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))' ||
  rollback "LibreChat cannot reach searxng after the restart"

echo "DONE web 2/2. Backups kept at:"
echo "  $BAK_COMPOSE"
echo "  $BAK_CFG"
echo "Test: refresh the browser (Cmd+Shift+R), open a NEW chat on the smart option, ask about today's dollar rate."
