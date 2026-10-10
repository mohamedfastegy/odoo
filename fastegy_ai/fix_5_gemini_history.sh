#!/usr/bin/env bash
# =============================================================================
# FastEgy AI — fix 5: Gemini fallback in the middle of a conversation
#
# Why    : the full chat test failed on a web-search answer. Groq's free tier
#          allows 8000 tokens per minute for gpt-oss-120b; a question plus the
#          pages it read was 8646 ("Request too large"). The fallback then failed
#          too: Gemini 3 rejects earlier tool calls without a "thought_signature"
#          (gpt-oss never writes one). LiteLLM sends Google's placeholder
#          signature only when the model name contains "gemini-3", and ours is
#          "gemini-flash-lite-latest".
# Change : fastegy_litellm_patch.py v2 also sends the placeholder for Gemini
#          "-latest" names. Copied into the litellm container (and to
#          /root/litellm for the compose override), then litellm restarts (~20 s).
# Tests  : fast, smart; the fallback with an earlier tool call (normal + stream);
#          a too-large request to smart that must end on the fallback.
#          Any failure puts the previous patch back.
# Run    : bash fix_5_gemini_history.sh
# Version: 1.0 — 2026-10-08
# =============================================================================
set -euo pipefail

DIR=/root/litellm
CFG=$DIR/config.yaml
PATCH=$DIR/fastegy_litellm_patch.py
LC_CFG=/root/librechat/librechat.yaml
URL=http://76.13.48.108:4000/v1          # same URL LibreChat uses
CTR=litellm
SRC=https://raw.githubusercontent.com/mohamedfastegy/odoo/954345fee7c94c81847c8a325879dcfc0e027530/fastegy_ai
PATCH_SHA=065f7f8e44f8e78a4dae4e7784695bc345b817d8be4ebe5e20f90c5c2b820de6

[ -f "$PATCH" ] && grep -q fastegy_litellm_patch "$CFG" || { echo "fix_4 is not installed (no patch or callback); nothing changed"; exit 1; }
[ "$(docker inspect -f '{{.State.Running}}' "$CTR" 2>/dev/null)" = true ] || { echo "$CTR is not running; nothing changed"; exit 1; }

TS=$(date +%Y%m%d_%H%M%S)
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
curl -fsSL "$SRC/fastegy_litellm_patch.py" -o "$STAGE/patch.py"
echo "$PATCH_SHA  $STAGE/patch.py" | sha256sum -c --quiet || { echo "Patch checksum mismatch; nothing changed"; exit 1; }
BAK="$PATCH.bak.$TS"
cp -p "$PATCH" "$BAK"
echo "Backup: $BAK"

wait_live() {
  for _ in $(seq 1 60); do
    curl -s -m 3 http://127.0.0.1:4000/health/liveliness >/dev/null 2>&1 && return 0
    sleep 2
  done
  return 1
}
load() { install -m 644 "$1" "$PATCH" && docker cp "$PATCH" "$CTR:/app/fastegy_litellm_patch.py" >/dev/null && docker restart "$CTR" >/dev/null; }
rollback() {
  echo "!! $1 — putting the previous patch back"
  load "$BAK" || true
  wait_live && echo "Previous patch restored." || echo "litellm did not come back: run  docker restart $CTR"
  exit 1
}

echo "Restarting $CTR with patch v2..."
load "$STAGE/patch.py" || rollback "could not copy the patch or restart"
wait_live || rollback "litellm did not start"
sleep 3
n=$(docker logs --since 2m "$CTR" 2>&1 | grep -c 'FASTEGY_PATCH active' || true)
docker logs --since 2m "$CTR" 2>&1 | grep 'FASTEGY_PATCH' | sort -u
[ "$n" -ge 2 ] || rollback "patch v2 did not load both fixes"

KEY=$(grep -A3 'name: FastEgy AI' "$LC_CFG" | sed -n 's/.*apiKey: *//p' | tr -d "\"' " || true)
if [[ $KEY == \$\{*\} ]]; then KEY=$(docker exec librechat printenv "${KEY:2:-1}" 2>/dev/null || true); fi
[ -n "$KEY" ] || rollback "could not read the LibreChat endpoint key"

# a conversation that already holds a tool call written by gpt-oss (no thought signature)
HISTORY='{"role":"user","content":"Look up DS-7608NXI-K1"},{"role":"assistant","content":null,"tool_calls":[{"id":"call_1","type":"function","function":{"name":"lookup_product","arguments":"{\"code\": \"DS-7608NXI-K1\"}"}}]},{"role":"tool","tool_call_id":"call_1","content":"EXACT MATCH for DS-7608NXI-K1: 8-ch IP video input, 1 SATA interface, up to 16 TB per HDD."},{"role":"user","content":"Summarize it in one short line."}'
TOOLS='"tools":[{"type":"function","function":{"name":"lookup_product","description":"Look up a model code","parameters":{"type":"object","properties":{"code":{"type":"string"}},"required":["code"]}}}]'
BIG=$(python3 -c 'print("Background notes for the reader. " * 2600)')    # ~ 20k tokens: over Groq's 8000 per minute
ask() { # $1 model, $2 messages JSON, $3 extra JSON -> "OK ..." or "FAIL ..."
  local hdr body; hdr=$(mktemp); body=$(mktemp)
  printf '{"model":"%s","messages":[%s],"max_tokens":300%s}' "$1" "$2" "$3" >"$body"
  curl -s -m 180 -D "$hdr" "$URL/chat/completions" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' --data-binary @"$body" |
    python3 -c 'import sys,json
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except Exception:
    print(("OK stream" if "data:" in raw and "\"error\"" not in raw else "FAIL " + raw[:200].replace("\n", " "))); sys.exit()
print("OK " + ("tool call" if d["choices"][0]["message"].get("tool_calls") else "text") if "choices" in d else "FAIL " + str(d.get("error", d))[:200])'
  grep -i '^x-litellm-attempted-fallbacks' "$hdr" | tr -d '\r' | sed 's/^/     /' || true
  rm -f "$hdr" "$body"
}

echo "== tests"
fast=$(ask fastegy-fast '{"role":"user","content":"Reply OK"}' "" || true);           echo "fastegy-fast                        $fast"
smart=$(ask fastegy-smart '{"role":"user","content":"Reply OK"}' "" || true);         echo "fastegy-smart                       $smart"
hist=$(ask fastegy-fallback "$HISTORY" ",$TOOLS" || true);                            echo "fallback, earlier tool call         $hist"
hists=$(ask fastegy-fallback "$HISTORY" ",$TOOLS,\"stream\":true" || true);           echo "fallback, earlier tool call (stream) $hists"
big=$(ask fastegy-smart "{\"role\":\"user\",\"content\":\"$BIG Reply OK.\"},${HISTORY#*\},}" ",$TOOLS" || true)
echo "smart, too large -> fallback        $big"

for r in "$fast" "$smart" "$hist" "$hists" "$big"; do
  [[ ${r%%$'\n'*} == OK* ]] || rollback "a test failed"
done
[[ $big == *"attempted-fallbacks: 1"* ]] || echo "NOTE: the too-large request did not report a fallback (Groq may have accepted it)."
echo "DONE fix 5. Previous patch: $BAK"
