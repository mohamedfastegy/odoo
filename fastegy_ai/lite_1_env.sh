#!/usr/bin/env bash
# =============================================================================
# FastEgy AI — LiteLLM 1: keys out of config.yaml into the protected .env
#
# diag_3 found two api_key values (Groq) and the master_key written in plain
# text in /root/litellm/config.yaml (mode 644), and in its old backups. This:
#   - moves each literal key into /root/litellm/.env (mode 600) under a name
#     (GROQ_API_KEY, GROQ_API_KEY_2 …, LITELLM_MASTER_KEY) and writes
#     os.environ/NAME in config.yaml, as GEMINI_API_KEY already is;
#   - makes config.yaml and its backups readable by root only (if the
#     container runs as root, which it must for that);
#   - recreates the litellm container (about 30 s without the assistant) so it
#     reads the new .env; the FastEgy patch comes back through the compose
#     override, which mounts it.
# The key values are never printed. LibreChat keeps working: the master key
# value does not change.
# Tests: the patch line, the keys present (names), /v1/models with the master
# key, a wrong key refused, one short answer from fastegy-fast and from
# fastegy-smart. Any failure puts the old config.yaml and .env back.
# Run    : bash lite_1_env.sh
# Version: 1.0 — 2026-10-09
# =============================================================================
set -euo pipefail

DIR=/root/litellm
CTR=litellm
CFG=$DIR/config.yaml
ENVF=$DIR/.env
TS=$(date +%Y%m%d_%H%M%S)

[ "$(docker inspect -f '{{.State.Running}}' "$CTR" 2>/dev/null)" = true ] || { echo "$CTR is not running; nothing changed."; exit 1; }
[ -f "$CFG" ] && [ -f "$ENVF" ] && [ -f "$DIR/docker-compose.yml" ] || { echo "Missing LiteLLM files; nothing changed."; exit 1; }
grep -q 'env_file' "$DIR/docker-compose.yml" || { echo "docker-compose.yml has no env_file; nothing changed."; exit 1; }
[ -f "$DIR/docker-compose.override.yml" ] && grep -q 'fastegy_litellm_patch' "$DIR/docker-compose.override.yml" ||
  { echo "The compose override does not mount the FastEgy patch; recreating would lose it. Nothing changed."; exit 1; }
[ -f "$DIR/fastegy_litellm_patch.py" ] || { echo "Missing $DIR/fastegy_litellm_patch.py; nothing changed."; exit 1; }
(cd "$DIR" && docker compose config -q) || { echo "docker compose config fails; nothing changed."; exit 1; }
ROOT_USER=$(docker exec "$CTR" id -u 2>/dev/null || echo "?")

cp -p "$CFG" "$CFG.bak.$TS"; chmod 600 "$CFG.bak.$TS"
cp -p "$ENVF" "$ENVF.bak.$TS"; chmod 600 "$ENVF.bak.$TS"

# the edit: literal keys -> .env names; prints names only
PLAN=$(python3 - "$CFG" "$ENVF" <<'PY'
import os, re, sys
cfg_path, env_path = sys.argv[1:]
cfg = open(cfg_path, encoding="utf-8").read().split("\n")
env_text = open(env_path, encoding="utf-8").read()
env = dict(l.split("=", 1) for l in env_text.splitlines() if "=" in l and not l.startswith("#"))
by_value, new_env, changed = {}, {}, []
rx = re.compile(r"^(\s*)(api_key|master_key)(\s*:\s*)([\"']?)([^\"'\s#]+)\4(\s*(?:#.*)?)$")
for i, line in enumerate(cfg):
    m = rx.match(line)
    if not m or m.group(5).startswith("os.environ/"):
        continue
    value = m.group(5)
    if value in by_value:
        name = by_value[value]
    else:
        existing = [k for k, v in {**env, **new_env}.items() if v == value]
        if existing:
            name = existing[0]
        else:
            base = "LITELLM_MASTER_KEY" if m.group(2) == "master_key" else \
                   "GROQ_API_KEY" if value.startswith("gsk_") else "PROVIDER_API_KEY"
            name, n = base, 2
            while name in env or name in new_env:
                name, n = f"{base}_{n}", n + 1
            new_env[name] = value
        by_value[value] = name
    cfg[i] = f"{m.group(1)}{m.group(2)}{m.group(3)}os.environ/{name}{m.group(6)}"
    changed.append(f"line {i + 1}: {m.group(2)} -> os.environ/{name}")
if not changed:
    print("NOTHING")
    sys.exit(0)
tmp = cfg_path + ".tmp"
open(tmp, "w", encoding="utf-8").write("\n".join(cfg))
os.chmod(tmp, os.stat(cfg_path).st_mode & 0o777)
os.replace(tmp, cfg_path)
if new_env:
    with open(env_path, "a", encoding="utf-8") as f:
        if env_text and not env_text.endswith("\n"):
            f.write("\n")
        for k, v in new_env.items():
            f.write(f"{k}={v}\n")
for c in changed:
    print(c)
print("new names in .env: " + (", ".join(new_env) or "none"))
PY
)
if [ "$PLAN" = NOTHING ]; then
  rm -f "$CFG.bak.$TS" "$ENVF.bak.$TS"
  echo "config.yaml has no literal keys any more; nothing to do."; exit 0
fi
echo "== moved to .env (names only):"; echo "$PLAN" | sed 's/^/  /'
chmod 600 "$ENVF"
LEFT=$(grep -E '^\s*(api_key|master_key)\s*:' "$CFG" | grep -vc 'os.environ/' || true)

live() { for i in $(seq 1 45); do curl -sf -m 3 http://127.0.0.1:4000/health/liveliness >/dev/null && return 0; sleep 2; done; return 1; }
rollback() {
  echo "!! $1 — putting the old config.yaml and .env back"
  cp -p "$CFG.bak.$TS" "$CFG"; chmod 644 "$CFG"
  cp -p "$ENVF.bak.$TS" "$ENVF"
  (cd "$DIR" && docker compose up -d >/dev/null 2>&1)
  live && echo "LiteLLM is back on the old settings." || echo "LiteLLM did not come back: cd $DIR && docker compose up -d"
  exit 1
}

[ "$LEFT" = 0 ] || rollback "$LEFT literal key line(s) are still in config.yaml"
echo "== recreating $CTR (about 30 s)"
START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
(cd "$DIR" && docker compose up -d) >/dev/null 2>&1 || rollback "docker compose up failed"
live || rollback "LiteLLM did not answer within 90 s"
sleep 3

echo "== tests"
docker logs --since "$START" "$CTR" 2>&1 | grep -q 'FASTEGY_PATCH active' && echo "  FastEgy patch: loaded" || rollback "the FastEgy patch line is missing"
NAMES=$(docker inspect "$CTR" -f '{{range .Config.Env}}{{println .}}{{end}}' | cut -d= -f1 | grep -E 'API_KEY|MASTER_KEY' | tr '\n' ' ')
echo "  keys in the container (names): $NAMES"
MK=$(grep -E '^LITELLM_MASTER_KEY=' "$ENVF" | head -1 | cut -d= -f2-)
[ -n "$MK" ] || MK=$(python3 -c 'import re,sys; m=re.search(r"^\s*master_key\s*:\s*[\"\x27]?([^\"\x27\s#]+)", open(sys.argv[1]).read(), re.M); print(m.group(1) if m else "")' "$CFG.bak.$TS")
code() { curl -s -o /dev/null -m 60 -w '%{http_code}' "$@"; }
C=$(code -H "Authorization: Bearer $MK" http://127.0.0.1:4000/v1/models)
echo "  /v1/models with the master key: $C"; [ "$C" = 200 ] || rollback "the master key is refused"
C=$(code -H "Authorization: Bearer wrong-key-test" http://127.0.0.1:4000/v1/models)
echo "  /v1/models with a wrong key: $C (must be refused)"; [ "$C" = 401 ] || [ "$C" = 403 ] || [ "$C" = 400 ] || rollback "a wrong key is not refused"
ask() {   # $1 = model: prints "<http code> <fallbacks attempted>"
  curl -s -o /dev/null -D - -m 60 -H "Authorization: Bearer $MK" -H 'Content-Type: application/json' \
    http://127.0.0.1:4000/v1/chat/completions \
    -d "{\"model\":\"$1\",\"max_tokens\":5,\"messages\":[{\"role\":\"user\",\"content\":\"Say OK\"}]}" |
    tr -d '\r' | awk 'NR == 1 {c = $2} tolower($1) == "x-litellm-attempted-fallbacks:" {f = $2} END {print c, (f == "" ? 0 : f)}'
}
for model in fastegy-fast fastegy-smart; do
  read -r C F < <(ask "$model")
  [ "$C" = 200 ] && [ "$F" != 0 ] && { sleep 5; read -r C F < <(ask "$model"); }
  echo "  short answer from $model: $C, fallbacks used: $F"
  [ "$C" = 200 ] || rollback "$model does not answer"
  [ "$F" = 0 ] || rollback "$model answered only through the fallback: its Groq key does not work"
done
unset MK

chmod 600 "$DIR"/config.yaml.bak.* 2>/dev/null || true      # old copies still hold the keys
echo "  config.yaml backups: root only"
if [ "$ROOT_USER" = 0 ]; then
  chmod 600 "$CFG"
  docker exec "$CTR" test -r /app/config.yaml || { chmod 644 "$CFG"; echo "  (config.yaml left readable: the container needs it)"; }
  echo "  config.yaml: root only"
else
  echo "  the container runs as user $ROOT_USER, so config.yaml stays readable (its keys are gone anyway)"
fi
echo "DONE lite 1. Previous files: $CFG.bak.$TS, $ENVF.bak.$TS (both root only)"
