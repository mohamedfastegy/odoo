#!/usr/bin/env bash
# =============================================================================
# FastEgy AI — brain 1: the link between Odoo and this assistant
#
# The Odoo AI module (fastegy_ai_assistant 18.0.4.1.0) will use the assistant
# exactly as it is set up on this server, so any change here (the rules in
# librechat.yaml, the model of the fast/smart option, the product list, the
# datasheets) reaches Odoo on its next question, with nothing to install
# there. This script:
#   - updates fastegy-reader to reader v6 (adds /brain: the rules, models and
#     catalog tools as one versioned "manifest", chat passed to LiteLLM with
#     this server's key, and the catalog tools); the image gets PyYAML;
#   - recreates the reader with librechat.yaml mounted read-only, a key of its
#     own for Odoo (kept in /root/fastegy-reader/brain.env, root only) and
#     port 3012 on 127.0.0.1 only;
#   - adds to Caddy, in the ai.fastegy.net site, the path /brain/* (and only
#     that path) to the reader: https://ai.fastegy.net/brain/
# LiteLLM's free fallback model is skipped for Odoo's requests, so customer
# data from Odoo never reaches it (if LiteLLM refuses that option, it stays
# on and the script says so). The LiteLLM key never leaves this server.
# Re-running keeps the same Odoo key. Backups; every step is tested and
# rolled back on failure. Prints no key.
# Run    : bash brain_1_gateway.sh
# Version: 1.0 — 2026-10-09
# =============================================================================
set -euo pipefail

SRC=https://raw.githubusercontent.com/mohamedfastegy/odoo/4cd67df6067b4941b1dd9d8da8d3c0e7048cc7e2/fastegy_ai
LC=librechat
LITE=litellm
RD=fastegy-reader
RDDIR=/root/fastegy-reader
DATA=$RDDIR/data
KEYFILE=$RDDIR/.key
BENV=$RDDIR/brain.env
CFG=/root/librechat/librechat.yaml
CF=/etc/caddy/Caddyfile
PORT=3012
ENGINES=yahoo,startpage,yandex
SITES="ai.fastegy.net status.fastegy.net go.fastegy.net scraper.fastegy.net n8n.fastegy.net mail.fastegy.net waha.fastegy.net chat.fastegy.net wa.fastegy.net"
TS=$(date +%Y%m%d_%H%M%S)

[ "$(id -u)" = 0 ] || { echo "Run as root."; exit 1; }
for c in "$LC" "$LITE" "$RD"; do
  [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = true ] || { echo "$c is not running; nothing changed."; exit 1; }
done
[ "$(docker inspect "$RD" -f '{{.Config.Image}}')" = fastegy-reader:4 ] || { echo "$RD is not running reader v4; nothing changed."; exit 1; }
[ -s "$KEYFILE" ] && [ -f "$DATA/carried.json" ] && [ -f "$RDDIR/Dockerfile" ] && [ -f "$RDDIR/reader.py" ] && [ -f "$CFG" ] ||
  { echo "Missing reader or LibreChat files; nothing changed."; exit 1; }
[ "$(systemctl is-active caddy 2>/dev/null)" = active ] && command -v caddy >/dev/null && [ -f "$CF" ] ||
  { echo "Caddy is not running as a service here; nothing changed."; exit 1; }
[ "$(grep -cE '^[[:space:]]*ai\.fastegy\.net[[:space:]]*\{[[:space:]]*$' "$CF" || true)" = 1 ] || { echo "No single 'ai.fastegy.net {' block in $CF; nothing changed."; exit 1; }
if ss -ltnH "( sport = :$PORT )" | grep -q . && ! docker port "$RD" 2>/dev/null | grep -q "127.0.0.1:$PORT"; then
  echo "Port $PORT is already used by something else; nothing changed."; exit 1
fi
# the reader runs as "nobody": it must be able to read librechat.yaml through the mount
MODE=$(stat -c %a "$CFG"); GROUP_ADD=()
if (( 8#$MODE & 4 )); then :
elif (( 8#$MODE & 040 )); then GROUP_ADD=(--group-add "$(stat -c %g "$CFG")")
else echo "librechat.yaml is mode $MODE: the reader could not read it. Nothing changed; tell Claude."; exit 1
fi
NET=$(docker inspect "$LC" -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' | awk '{print $1}')
KEY=$(cat "$KEYFILE")
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

curl -fsSL "$SRC/reader.py" -o "$STAGE/reader.py"
echo "6a549a84a4f465d0b4466c98820272c847b5a7ebb0a17d58cd1ecec910953282  $STAGE/reader.py" | sha256sum -c --quiet ||
  { echo "Downloaded file does not match; nothing changed."; exit 1; }
echo "reader v6 downloaded and verified"

# the LiteLLM key LibreChat uses (never printed)
REF=$(docker exec -i "$LITE" python3 -c '
import sys, yaml
c = yaml.safe_load(sys.stdin)
ep = [e for e in c["endpoints"]["custom"] if e.get("name") == "FastEgy AI"]
print(ep[0].get("apiKey", "") if ep else "")' <"$CFG")
if [[ "$REF" =~ ^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$ ]]; then
  LLM_KEY=$(docker exec "$LC" printenv "${BASH_REMATCH[1]}" 2>/dev/null || true)
else
  LLM_KEY=$REF
fi
unset REF
[ -n "$LLM_KEY" ] || { echo "Could not read the LiteLLM key LibreChat uses; nothing changed."; exit 1; }

# Odoo's key: kept when this already ran
NEW_ENV=yes
if [ -f "$BENV" ] && grep -qE '^BRAIN_KEY=.{32,}$' "$BENV"; then
  BRAIN_KEY=$(grep -E '^BRAIN_KEY=' "$BENV" | head -1 | cut -d= -f2-); NEW_ENV=no
  cp -p "$BENV" "$BENV.bak.$TS"
else
  BRAIN_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(24))')
fi
write_env() {   # $1 = BRAIN_NO_FALLBACK value
  ( umask 077; printf 'BRAIN_KEY=%s\nLLM_KEY=%s\nBRAIN_NO_FALLBACK=%s\n' "$BRAIN_KEY" "$LLM_KEY" "$1" >"$BENV" )
  chmod 600 "$BENV"
}

run_reader() {   # $1 = yes: with the brain link (v6); no: as kb_14 left it
  docker rm -f "$RD" >/dev/null 2>&1 || true
  local extra=()
  if [ "$1" = yes ]; then
    extra=(--env-file "$BENV" -e LC_CONFIG=/app/librechat.yaml -v "$CFG:/app/librechat.yaml:ro"
           -p "127.0.0.1:$PORT:3002" "${GROUP_ADD[@]}")
  fi
  docker run -d --name "$RD" --restart unless-stopped --network "$NET" --label fastegy.ai=web-search \
    --memory 384m -e READER_KEY="$KEY" -e SEARCH_ENGINES="$ENGINES" -e MCP_KEY="$KEY" \
    -e CATALOG_PATH=/app/catalog.json -v "$RDDIR/catalog.json:/app/catalog.json:ro" \
    -e CARRIED_PATH=/app/data/carried.json -e DATASHEETS_PATH=/app/data/ds/datasheets.json \
    -v "$DATA:/app/data:ro" "${extra[@]}" fastegy-reader:4 >/dev/null
}
restore() {
  echo "!! $1 — putting the previous reader back"
  cp -p "$RDDIR/reader.py.bak.$TS" "$RDDIR/reader.py"
  cp -p "$RDDIR/Dockerfile.bak.$TS" "$RDDIR/Dockerfile"
  if [ "$NEW_ENV" = yes ]; then rm -f "$BENV"; else cp -p "$BENV.bak.$TS" "$BENV"; fi
  docker tag fastegy-reader:4-prev fastegy-reader:4
  run_reader no
  echo "Previous reader restored."; exit 1
}

B=http://127.0.0.1:$PORT/brain
bget() { curl -s -m 20 -H "Authorization: Bearer $BRAIN_KEY" "$@"; }
code() { curl -s -o /dev/null -m 20 -w '%{http_code}' "$@" || true; }
chat_once() {   # prints "<http code> <fallbacks header>" and leaves the body in $STAGE/chat.json
  local model=$1
  curl -s -m 150 -D "$STAGE/chat.h" -o "$STAGE/chat.json" -H "Authorization: Bearer $BRAIN_KEY" -H 'Content-Type: application/json' \
    "$B/v1/chat/completions" -d "{\"model\":\"$model\",\"max_tokens\":200,\"messages\":[{\"role\":\"user\",\"content\":\"رد بكلمة واحدة: تمام\"}]}" >/dev/null || true
  tr -d '\r' <"$STAGE/chat.h" | awk 'NR == 1 {c = $2} tolower($1) == "x-brain-fallbacks:" {f = $2} END {print (c ? c : "000"), (f == "" ? 0 : f)}'
}

cp -p "$RDDIR/reader.py" "$RDDIR/reader.py.bak.$TS"
cp -p "$RDDIR/Dockerfile" "$RDDIR/Dockerfile.bak.$TS"
docker tag fastegy-reader:4 fastegy-reader:4-prev
install -m 644 "$STAGE/reader.py" "$RDDIR/reader.py"
python3 - "$RDDIR/Dockerfile" <<'PY'
import sys
p = sys.argv[1]
lines = open(p).read().split("\n")
if not any("PyYAML" in l for l in lines):
    i = next(i for i, l in enumerate(lines) if l.startswith("RUN pip install"))
    lines.insert(i + 1, 'RUN pip install --no-cache-dir "PyYAML>=6,<7"')
    open(p, "w").write("\n".join(lines))
PY
grep -q PyYAML "$RDDIR/Dockerfile" || restore "could not add PyYAML to the Dockerfile"
write_env 1
echo "building fastegy-reader v4 (reader v6)..."
docker build -q -t fastegy-reader:4 "$RDDIR" >/dev/null || restore "build failed"
run_reader yes || restore "reader did not start"

echo "== tests on the server"
for _ in $(seq 1 30); do [ "$(code "$B/health")" = 200 ] && break; sleep 1; done
[[ "$(curl -s -m 5 "$B/health" || true)" == *'"brain": true'* ]] || restore "the reader does not show the brain link"
[ "$(code "$B/manifest")" = 401 ] || restore "the manifest answers without a key"
bget "$B/manifest" -o "$STAGE/manifest.json"
read -r SPECS MODEL VERSION RULES < <(python3 - "$STAGE/manifest.json" <<'PY' || echo "- - - 0"
import json, sys
m = json.load(open(sys.argv[1]))
d = m["specs"][m["default_spec"]]
print(",".join(sorted(m["specs"])), d["model"], m["version"], len(d["rules"].splitlines()))
PY
)
echo "  manifest: specs $SPECS | default model $MODEL | version $VERSION | $RULES lines of rules"
[ "$MODEL" != - ] && [ "$RULES" -gt 0 ] && grep -q lookup_product "$STAGE/manifest.json" || restore "the manifest is incomplete"
[ "$(code -H "If-None-Match: \"$VERSION\"" -H "Authorization: Bearer $BRAIN_KEY" "$B/manifest")" = 304 ] || restore "the manifest version check does not answer 304"
CODE=$(python3 -c 'import json,sys; m=json.load(open(sys.argv[1]))["models"]; print(m[len(m)//2]["code"])' "$DATA/carried.json")
bget -H 'Content-Type: application/json' "$B/tool" -d "{\"name\":\"lookup_product\",\"arguments\":{\"code\":\"$CODE\"}}" -o "$STAGE/tool.json"
python3 -c 'import json,sys; t=json.load(open(sys.argv[1]))["text"]; print("  tool lookup_product " + sys.argv[2] + ": " + t.splitlines()[0][:70]); sys.exit(0 if t.startswith("EXACT MATCH") else 2)' \
  "$STAGE/tool.json" "$CODE" || restore "the catalog tool does not answer"

NOFB=1
read -r C F < <(chat_once "$MODEL")
if [ "$C" = 400 ] && grep -q disable_fallbacks "$STAGE/chat.json"; then
  echo "  LiteLLM does not take the no-fallback option: Odoo's requests can use the fallback model like LibreChat's"
  NOFB=0; write_env 0; run_reader yes || restore "reader did not start"
  for _ in $(seq 1 30); do [ "$(code "$B/health")" = 200 ] && break; sleep 1; done
  read -r C F < <(chat_once "$MODEL")
fi
for _ in 1 2; do   # a busy free tier answers 429: wait and ask again
  { [ "$C" = 429 ] || [ "$C" = 502 ] || [ "$C" = 503 ]; } || break
  sleep 20; read -r C F < <(chat_once "$MODEL")
done
if [ "$C" = 200 ] && grep -q '"choices"' "$STAGE/chat.json"; then
  echo "  chat through the link ($MODEL): 200, fallbacks used: $F"
elif [ "$C" = 429 ] || [ "$C" = 502 ] || [ "$C" = 503 ]; then
  echo "  chat through the link: the model server is busy ($C); the link itself works. Try the Odoo test button later."
else
  restore "chat through the link answered $C"
fi
[ "$(code -X POST -H 'Authorization: Bearer wrong' "$B/tool")" = 401 ] || restore "a wrong key is not refused"
MCPJS='
(async () => {
  const r = await fetch("http://fastegy-reader:3002/mcp", { method: "POST",
    headers: { "Content-Type": "application/json", Authorization: "Bearer " + process.env.K },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "lookup_product", arguments: { code: process.env.CODE } } }) });
  const t = (await r.json()).result.content[0].text;
  console.log("  LibreChat catalog tool (MCP): " + t.split("\n")[0].slice(0, 60));
  process.exit(t.startsWith("EXACT MATCH") ? 0 : 2);
})().catch(e => { console.log("  MCP error: " + e.message); process.exit(2); });'
docker exec -e K="$KEY" -e CODE="$CODE" "$LC" node -e "$MCPJS" || restore "LibreChat's catalog tool broke"

echo "== Caddy: https://ai.fastegy.net/brain/"
if awk '/^[[:space:]]*ai\.fastegy\.net[[:space:]]*\{[[:space:]]*$/ {inb=1} inb && /handle \/brain\/\*/ {f=1} inb && /^\}/ {inb=0} END {exit !f}' "$CF"; then
  echo "  the /brain route is already there"
else
  NEWCF="$CF.new.$TS"
  python3 - "$CF" "$NEWCF" "$PORT" <<'PY'
import re, sys
src, dst, port = sys.argv[1:]
lines = open(src, encoding="utf-8").read().split("\n")
i = next(i for i, l in enumerate(lines) if re.match(r"^\s*ai\.fastegy\.net\s*\{\s*$", l))
indent = re.match(r"^(\s*)", lines[i]).group(1) + "\t"
block = [indent + "handle /brain/* {", indent + "\treverse_proxy 127.0.0.1:%s" % port, indent + "}"]
open(dst, "w", encoding="utf-8").write("\n".join(lines[:i + 1] + block + lines[i + 1:]))
PY
  caddy validate --config "$NEWCF" --adapter caddyfile >/dev/null 2>&1 || { rm -f "$NEWCF"; echo "  !! the new Caddyfile does not validate; Caddy unchanged (the reader is updated)"; exit 1; }
  sites() { for s in $SITES; do printf '%s %s\n' "$s" "$(code "https://$s/")"; done; }
  BEFORE=$(sites)
  cp -p "$CF" "$CF.bak.$TS"
  caddy_back() {
    echo "  !! $1 — putting the old Caddyfile back"
    cp -p "$CF.bak.$TS" "$CF"; systemctl reload caddy || true
    echo "  Old Caddyfile restored (the reader keeps v6 on 127.0.0.1 only)."; exit 1
  }
  cat "$NEWCF" >"$CF"; rm -f "$NEWCF"
  systemctl reload caddy || caddy_back "Caddy did not reload"
  sleep 3
  AFTER=$(sites)
  [ "$BEFORE" = "$AFTER" ] || { sleep 5; AFTER=$(sites); }
  [ "$BEFORE" = "$AFTER" ] || caddy_back "a site answers differently: $(diff <(echo "$BEFORE") <(echo "$AFTER") | grep '^>' | tr '\n' ' ')"
fi
P=https://ai.fastegy.net/brain
[[ "$(curl -s -m 15 "$P/health" || true)" == *'"brain": true'* ]] || { [ -n "${NEWCF:-}" ] && caddy_back "https://ai.fastegy.net/brain/health does not reach the reader"; exit 1; }
[ "$(code "$P/manifest")" = 401 ] || { [ -n "${NEWCF:-}" ] && caddy_back "the public manifest answers without a key"; exit 1; }
[ "$(code -H "Authorization: Bearer $BRAIN_KEY" "$P/manifest")" = 200 ] || { [ -n "${NEWCF:-}" ] && caddy_back "the public manifest refuses the key"; exit 1; }
echo "  https://ai.fastegy.net/brain/: health ok, key required, manifest ok; the other sites answer as before"
unset LLM_KEY
echo "DONE brain 1."
echo "In Odoo (Settings → FastEgy AI → «سيرفر الذكاء»):"
echo "  URL : https://ai.fastegy.net/brain"
echo "  Key : run   grep BRAIN_KEY $BENV | cut -d= -f2   and paste it there"
[ "$NOFB" = 1 ] && echo "  (Odoo's requests skip LiteLLM's free fallback model)"
echo "Backups: $RDDIR/reader.py.bak.$TS, image fastegy-reader:4-prev$( [ -f "$CF.bak.$TS" ] && echo ", $CF.bak.$TS")"
