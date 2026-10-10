#!/usr/bin/env bash
# =============================================================================
# FastEgy AI — fix 4: make the Gemini fallback work with tools
#
# Why    : 1) fastegy-fallback was copied from the gemini-2.5-pro entry, including
#             its built-in googleSearch tool. Gemini rejects that together with
#             function tools (400 "include_server_side_tool_invocations"), and
#             since web search + the product catalog every chat carries tools,
#             so the fallback failed every time.
#          2) When gpt-oss writes a malformed tool call, Groq ends the stream with
#             code "tool_use_failed"; LiteLLM 1.82 crashes on int("tool_use_failed")
#             and the chat aborts instead of falling back (4 times last night).
# Change : - remove the googleSearch tool from fastegy-fallback only
#          - load fastegy_litellm_patch.py (maps Groq's text codes to 503, so the
#            router falls back to fastegy-fallback), via litellm_settings.callbacks:
#            copied into the running container, which is only restarted (not
#            recreated: compose would not rebuild it identically), plus a
#            docker-compose.override.yml that mounts it if the container is ever
#            recreated later
# Safety : backup first; automatic rollback (config, files, restart) if any test
#          fails. Keys are read from the existing files, never printed.
# Run    : sudo bash fix_4_fallback.sh
# Version: 1.1 — 2026-10-08 (1.0 recreated the container; its guard stopped it)
# =============================================================================
set -euo pipefail

DIR=/root/litellm
CFG=$DIR/config.yaml
OVR=$DIR/docker-compose.override.yml
PATCH=$DIR/fastegy_litellm_patch.py
LC_CFG=/root/librechat/librechat.yaml
URL=http://76.13.48.108:4000/v1          # same URL LibreChat uses
CTR=litellm
SRC=https://raw.githubusercontent.com/mohamedfastegy/odoo/cfc850bc71487e54de1f7c9b9aeb5ad3606e0d77/fastegy_ai
PATCH_SHA=4530f53a3c0afdcb272419020ea01fce5770066a4799b7f4d25e0b8dd9707b35
CB=fastegy_litellm_patch.proxy_handler_instance

[ -f "$CFG" ] && [ -f "$DIR/docker-compose.yml" ] || { echo "Missing $CFG or $DIR/docker-compose.yml"; exit 1; }
docker inspect "$CTR" >/dev/null 2>&1 || { echo "Container $CTR not found"; exit 1; }
[ "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$CTR")" = "$DIR" ] ||
  { echo "$CTR is not managed by $DIR/docker-compose.yml; nothing changed"; exit 1; }
if [ -e "$OVR" ] && ! grep -q fastegy_litellm_patch "$OVR"; then
  echo "$OVR already exists and is not ours; nothing changed"; exit 1
fi
TS=$(date +%Y%m%d_%H%M%S)
BAK="$CFG.bak.$TS"; [ -e "$BAK" ] && BAK="$BAK.$$"
TMP=$(mktemp); STAGE=$(mktemp -d)
trap 'rm -rf "$TMP" "$STAGE"' EXIT

curl -fsSL "$SRC/fastegy_litellm_patch.py" -o "$STAGE/patch.py"
echo "$PATCH_SHA  $STAGE/patch.py" | sha256sum -c --quiet || { echo "Patch checksum mismatch; nothing changed"; exit 1; }

cp -p "$CFG" "$BAK"
echo "Backup: $BAK"

PY=$(cat <<'PY'
import sys, yaml
cb = sys.argv[1]
cfg = yaml.safe_load(sys.stdin)
fb = [m for m in cfg.get("model_list") or [] if m.get("model_name") == "fastegy-fallback"]
if len(fb) != 1:
    sys.exit("ERROR: expected exactly one fastegy-fallback entry; nothing changed")
removed = fb[0]["litellm_params"].pop("tools", None)
ls = cfg.setdefault("litellm_settings", {}) or {}
cbs = ls.get("callbacks") or []
cbs = [cbs] if isinstance(cbs, str) else list(cbs)
if cb not in cbs:
    cbs.append(cb)
ls["callbacks"] = cbs
cfg["litellm_settings"] = ls
print("removed from fastegy-fallback: " + ("tools " + str(removed) if removed else "nothing (already clean)"), file=sys.stderr)
yaml.safe_dump(cfg, sys.stdout, sort_keys=False, allow_unicode=True, width=1000)
PY
)
if ! docker exec -i "$CTR" python3 -c "$PY" "$CB" <"$CFG" >"$TMP"; then
  docker exec -i "$CTR" python -c "$PY" "$CB" <"$CFG" >"$TMP"
fi
grep -q "$CB" "$TMP" && grep -q 'fastegy-fallback' "$TMP" || { echo "Rewrite produced unexpected output; nothing changed"; exit 1; }

load_patch() {   # the running container reads /app/fastegy_litellm_patch.py after a restart
  docker cp "$PATCH" "$CTR:/app/fastegy_litellm_patch.py" && docker restart "$CTR" >/dev/null
}
wait_live() {
  for _ in $(seq 1 60); do
    curl -s -m 3 http://127.0.0.1:4000/health/liveliness >/dev/null 2>&1 && return 0
    sleep 2
  done
  return 1
}
rollback() {
  echo "!! $1 — rolling back"
  cat "$BAK" >"$CFG"
  if ! grep -q "$CB" "$BAK"; then      # a re-run's backup still needs the patch file
    rm -f "$OVR" "$PATCH"
    docker exec "$CTR" rm -f /app/fastegy_litellm_patch.py 2>/dev/null || true
  fi
  docker restart "$CTR" >/dev/null || true
  wait_live && echo "Restored $BAK; litellm is back as before." || echo "litellm did not come back: run  docker restart $CTR"
  exit 1
}

cat "$TMP" >"$CFG"        # in place: keeps the inode the container is bound to
install -m 644 "$STAGE/patch.py" "$PATCH"
cat >"$OVR" <<'YML'
# FastEgy: mounts fastegy_litellm_patch.py (loaded by litellm_settings.callbacks in config.yaml)
services:
  litellm:
    volumes:
      - ./fastegy_litellm_patch.py:/app/fastegy_litellm_patch.py:ro
YML

echo "Restarting $CTR with the patch..."
load_patch || rollback "could not copy the patch or restart"
wait_live || rollback "litellm did not start"
sleep 3
docker logs --since 5m "$CTR" 2>&1 | grep -m1 FASTEGY_PATCH || echo "WARNING: no FASTEGY_PATCH line in the logs"

# LibreChat's key for this endpoint (read, never printed); resolves a ${VAR} reference
KEY=$(grep -A3 'name: FastEgy AI' "$LC_CFG" | sed -n 's/.*apiKey: *//p' | tr -d "\"' " || true)
if [[ $KEY == \$\{*\} ]]; then KEY=$(docker exec librechat printenv "${KEY:2:-1}" 2>/dev/null || true); fi
[ -n "$KEY" ] || rollback "could not read the LibreChat endpoint key"

TOOLS='"tools":[{"type":"function","function":{"name":"lookup_product","description":"Look up a model code","parameters":{"type":"object","properties":{"code":{"type":"string"}},"required":["code"]}}}]'
ask() { # $1 model, $2 extra JSON -> "OK <how>" or "FAIL <reason>"
  local hdr; hdr=$(mktemp)
  curl -s -m 120 -D "$hdr" "$URL/chat/completions" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$1\",\"messages\":[{\"role\":\"user\",\"content\":\"Look up DS-7608NXI-K1\"}],\"max_tokens\":400$2}" |
    python3 -c 'import sys,json
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except Exception:
    ok = "data:" in raw and "\"error\"" not in raw
    print(("OK stream" if ok else "FAIL " + raw[:200].replace("\n", " "))); sys.exit()
if "choices" not in d:
    print("FAIL " + str(d.get("error", d))[:200]); sys.exit()
m = d["choices"][0]["message"]
print("OK " + ("tool call " + m["tool_calls"][0]["function"]["name"] if m.get("tool_calls") else "text"))'
  grep -i '^x-litellm-attempted-fallbacks' "$hdr" | tr -d '\r' | sed 's/^/     /' || true
  rm -f "$hdr"
}

echo "== tests"
fast=$(ask fastegy-fast "" || true);                              echo "fastegy-fast                  $fast"
smart=$(ask fastegy-smart "" || true);                            echo "fastegy-smart                 $smart"
fbk=$(ask fastegy-fallback ",$TOOLS" || true);                    echo "fallback + tools              $fbk"
fbs=$(ask fastegy-fallback ",$TOOLS,\"stream\":true" || true);     echo "fallback + tools (stream)     $fbs"
mock=$(ask fastegy-smart ",$TOOLS,\"mock_testing_fallbacks\":true" || true); echo "smart -> fallback + tools     $mock"

for r in "$fast" "$smart" "$fbk" "$fbs" "$mock"; do
  [[ ${r%%$'\n'*} == OK* ]] || rollback "a test failed"
done
echo "DONE fix 4. Backup kept at: $BAK"
echo "Undo later:  cat $BAK > $CFG && rm $OVR $PATCH && docker restart $CTR"
