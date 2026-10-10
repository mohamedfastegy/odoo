#!/usr/bin/env bash
# =============================================================================
# FastEgy AI — brain 3: Claude Haiku answers the customers
#
# The website chat (spec «fastegy-customer») moves from the free Groq model to
# Claude Haiku 5.5 (paid, Anthropic workspace «FastEgy AI Chat»). The rules,
# the catalog tools and the Odoo link do not change: only the engine.
#   - asks for the Anthropic key on the screen (hidden, never printed, never in
#     a command line) and tries it on api.anthropic.com before changing anything;
#   - puts it in /root/litellm/.env (mode 600) as ANTHROPIC_API_KEY;
#   - adds the LiteLLM model «fastegy-claude-haiku» = anthropic/claude-haiku-5-5,
#     thinking off (short chat answers, faster, cheaper; the tool loop stays
#     simple) and no temperature (Haiku 5.5 refuses anything but the default);
#   - recreates the litellm container (about 30 s) so it reads the new key;
#   - specs.json: «fastegy-customer» → fastegy-claude-haiku, and a copy of the
#     same rules on the old model as «fastegy-customer-backup» (Odoo 4.1.12
#     retries there when Claude does not answer).
# Tests: the key on Anthropic; the patch line; the key name in the container;
# two rounds of a catalog tool call through LiteLLM (what Odoo does); the
# manifest; one real answer from each spec. Any failure puts the old
# config.yaml, .env and specs.json back.
# Run    : bash brain_3_claude.sh            (install; asks for the key)
#          bash brain_3_claude.sh --back     (customers back on the old model now)
#          bash brain_3_claude.sh --claude   (customers on Claude again, no key asked)
#          bash brain_3_claude.sh --show     (which model each spec uses)
# Version: 1.0 — 2026-10-09
# =============================================================================
set -euo pipefail

DIR=/root/litellm
CTR=litellm
CFG=$DIR/config.yaml
ENVF=$DIR/.env
RDDIR=/root/fastegy-reader
SPECS=$RDDIR/data/brain/specs.json
BENV=$RDDIR/brain.env
B=http://127.0.0.1:3012/brain
L=http://127.0.0.1:4000
NEW=fastegy-claude-haiku
UPSTREAM=claude-haiku-5-5
CUST=fastegy-customer
BACKUP=fastegy-customer-backup
TS=$(date +%Y%m%d_%H%M%S)

[ "$(id -u)" = 0 ] || { echo "Run as root."; exit 1; }
[ -f "$BENV" ] && [ -f "$SPECS" ] || { echo "Run brain_1_gateway.sh and brain_2_customer.sh first; nothing changed."; exit 1; }
BKEY=$(grep -E '^BRAIN_KEY=' "$BENV" | head -1 | cut -d= -f2-)
LKEY=$(grep -E '^LLM_KEY=' "$BENV" | head -1 | cut -d= -f2-)
[ -n "$BKEY" ] && [ -n "$LKEY" ] || { echo "BRAIN_KEY or LLM_KEY missing in $BENV; nothing changed."; exit 1; }
[[ "$(curl -s -m 5 "$B/health" || true)" == *'"brain": true'* ]] || { echo "The brain link does not answer on $B; nothing changed."; exit 1; }
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
mget() { curl -s -m 20 -H "Authorization: Bearer $BKEY" "$B/manifest"; }

# ---------------------------------------------------------------- specs.json
# set_customer <model> [temperature]: the customer spec on that model; the backup spec keeps the
# rules and the previous (non-Claude) model. Prints one line.
set_customer() {
  python3 - "$SPECS" "$1" "${2:-}" "$NEW" <<'PY'
import json, os, sys
path, model, temp, claude = sys.argv[1:]
specs = json.load(open(path, encoding="utf-8")) or {}
cust = specs.get("fastegy-customer")
if not isinstance(cust, dict) or not cust.get("rules"):
    sys.exit("no fastegy-customer spec with rules in specs.json")
back = specs.get("fastegy-customer-backup") or {}
old = back.get("model") or (cust.get("model") if cust.get("model") != claude else "")
if not old:
    sys.exit("cannot tell which model the backup spec should use")
specs["fastegy-customer-backup"] = {"label": "العملاء (احتياطي)", "model": old,
                                    "temperature": back.get("temperature", 0.3) if back else 0.3,
                                    "rules": cust["rules"]}
if model == "BACKUP":
    model = old
cust = dict(cust, model=model, temperature=(float(temp) if temp else None))
specs["fastegy-customer"] = cust
tmp = path + ".tmp"
json.dump(specs, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
os.chmod(tmp, 0o644)
os.replace(tmp, path)
print("fastegy-customer -> %s (temperature %s) | fastegy-customer-backup -> %s" % (
    model, cust["temperature"], old))
PY
}
show() {
  mget | python3 -c '
import json, sys
m = json.load(sys.stdin)
print("manifest version:", m["version"])
for n in ("fastegy-customer", "fastegy-customer-backup", m["default_spec"]):
    s = m["specs"].get(n)
    print("  %-26s %s" % (n, ("%s (temperature %s)" % (s["model"], s.get("temperature"))) if s else "-"))'
}
manifest_has() {   # $1 = model expected on fastegy-customer
  sleep 1
  mget | python3 -c '
import json, sys
m = json.load(sys.stdin)
c, b = m["specs"].get("fastegy-customer") or {}, m["specs"].get("fastegy-customer-backup") or {}
ok = c.get("model") == sys.argv[1] and b.get("model") and b.get("rules") == c.get("rules")
sys.exit(0 if ok else 1)' "$1"
}

case "${1:-}" in
  --show) show; exit 0 ;;
  --back|--claude)
    [ "$1" = --back ] || grep -q "model_name: $NEW" "$CFG" ||
      { echo "$NEW is not in LiteLLM yet: run  bash brain_3_claude.sh  first. Nothing changed."; exit 1; }
    cp -p "$SPECS" "$SPECS.bak.$TS"
    if [ "$1" = --back ]; then set_customer BACKUP 0.3; WANT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["fastegy-customer-backup"]["model"])' "$SPECS")
    else set_customer "$NEW"; WANT=$NEW; fi
    manifest_has "$WANT" || { cp -p "$SPECS.bak.$TS" "$SPECS"; echo "!! the manifest does not show it; old specs.json back."; exit 1; }
    show; echo "DONE. The website chat uses it from its next question. Previous file: $SPECS.bak.$TS"; exit 0 ;;
  "") ;;
  *) echo "Unknown option $1 (use --show, --back or --claude)"; exit 1 ;;
esac

# ---------------------------------------------------------------- checks before any change
[ "$(docker inspect -f '{{.State.Running}}' "$CTR" 2>/dev/null)" = true ] || { echo "$CTR is not running; nothing changed."; exit 1; }
[ -f "$CFG" ] && [ -f "$ENVF" ] && grep -q 'env_file' "$DIR/docker-compose.yml" || { echo "Missing LiteLLM files or env_file; nothing changed."; exit 1; }
grep -q 'fastegy_litellm_patch' "$DIR/docker-compose.override.yml" 2>/dev/null ||
  { echo "The compose override does not mount the FastEgy patch; recreating would lose it. Nothing changed."; exit 1; }
(cd "$DIR" && docker compose config -q) || { echo "docker compose config fails; nothing changed."; exit 1; }

# ---------------------------------------------------------------- the key
OLDKEY=$(grep -E '^ANTHROPIC_API_KEY=' "$ENVF" | head -1 | cut -d= -f2- || true)
echo "Paste the Anthropic API key from the «FastEgy AI Chat» workspace and press Enter."
[ -n "$OLDKEY" ] && echo "(A key is already installed: press Enter alone to keep it.)"
read -rsp "Key (it will not show on the screen): " AKEY </dev/tty || { echo; echo "No terminal to read the key from; run it in an SSH session. Nothing changed."; exit 1; }
echo
AKEY=$(printf '%s' "$AKEY" | tr -d '[:space:]')
[ -z "$AKEY" ] && AKEY=$OLDKEY
[[ "$AKEY" == sk-ant-* ]] || { echo "That does not look like an Anthropic key (it starts with sk-ant-); nothing changed."; exit 1; }
echo "== the key on api.anthropic.com ($UPSTREAM)"
CODE=$(curl -s -m 60 -o "$STAGE/a.json" -w '%{http_code}' https://api.anthropic.com/v1/messages \
  -H @<(printf 'x-api-key: %s\nanthropic-version: 2023-06-01\ncontent-type: application/json\n' "$AKEY") \
  -d "{\"model\":\"$UPSTREAM\",\"max_tokens\":20,\"thinking\":{\"type\":\"disabled\"},\"messages\":[{\"role\":\"user\",\"content\":\"Say OK\"}]}" || true)
if [ "$CODE" != 200 ]; then
  MSG=$(python3 -c 'import json,sys
try: print(json.load(open(sys.argv[1]))["error"]["message"][:200])
except Exception: print("no answer")' "$STAGE/a.json")
  echo "  Anthropic answered $CODE: $MSG"
  case "$CODE" in
    401) echo "  The key is wrong or was deleted: create a new one and run again." ;;
    400) [[ "$MSG" == *credit* ]] && echo "  The organization has no credit: add funds in Plans & Billing, then run again." ;;
    403) echo "  The key has no access: check that the workspace is active and its spend limit is not 0." ;;
    429) echo "  Rate limit or spend limit reached: check the workspace limits." ;;
  esac
  echo "Nothing changed."; exit 1
fi
echo "  OK ($(python3 -c 'import json,sys; u=json.load(open(sys.argv[1]))["usage"]; print("%s in / %s out tokens" % (u["input_tokens"], u["output_tokens"]))' "$STAGE/a.json"))"

# ---------------------------------------------------------------- backups and edits
cp -p "$CFG" "$CFG.bak.$TS"; chmod 600 "$CFG.bak.$TS"
cp -p "$ENVF" "$ENVF.bak.$TS"; chmod 600 "$ENVF.bak.$TS"
cp -p "$SPECS" "$SPECS.bak.$TS"

live() { for _ in $(seq 1 45); do curl -sf -m 3 "$L/health/liveliness" >/dev/null && return 0; sleep 2; done; return 1; }
rollback() {
  echo "!! $1 — putting the old config.yaml, .env and specs.json back"
  cat "$CFG.bak.$TS" >"$CFG"          # in place: config.yaml is a single-file bind mount
  cp -p "$ENVF.bak.$TS" "$ENVF"
  cp -p "$SPECS.bak.$TS" "$SPECS"
  (cd "$DIR" && docker compose up -d >/dev/null 2>&1) || true
  live && echo "LiteLLM is back on the old settings; the website chat is on its old model." ||
    echo "LiteLLM did not come back: cd $DIR && docker compose up -d"
  exit 1
}

# .env: ANTHROPIC_API_KEY (the value goes through the environment, never a command line)
AKEY="$AKEY" python3 - "$ENVF" <<'PY'
import os, sys
path, key = sys.argv[1], os.environ["AKEY"]
lines = [l for l in open(path, encoding="utf-8").read().splitlines() if not l.startswith("ANTHROPIC_API_KEY=")]
lines.append("ANTHROPIC_API_KEY=" + key)
tmp = path + ".tmp"
with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
os.replace(tmp, path)
PY
chmod 600 "$ENVF"
unset AKEY OLDKEY

# config.yaml: the new model (edited with the container's own python + yaml, like fix 4)
PY=$(cat <<'PY'
import sys, yaml
name, upstream = sys.argv[1:]
cfg = yaml.safe_load(sys.stdin)
models = [m for m in cfg.get("model_list") or [] if m.get("model_name") != name]
models.append({"model_name": name, "litellm_params": {
    "model": "anthropic/" + upstream, "api_key": "os.environ/ANTHROPIC_API_KEY",
    "thinking": {"type": "disabled"},
    "input_cost_per_token": 1.0e-07, "output_cost_per_token": 5.0e-07}})
cfg["model_list"] = models
mp = (cfg.get("litellm_settings") or {}).get("modify_params")
print("modify_params: %s" % mp, file=sys.stderr)
yaml.safe_dump(cfg, sys.stdout, sort_keys=False, allow_unicode=True, width=1000)
PY
)
TMP=$STAGE/config.yaml
if ! docker exec -i "$CTR" python3 -c "$PY" "$NEW" "$UPSTREAM" <"$CFG" >"$TMP" 2>"$STAGE/py.err"; then
  docker exec -i "$CTR" python -c "$PY" "$NEW" "$UPSTREAM" <"$CFG" >"$TMP" 2>"$STAGE/py.err" || rollback "could not edit config.yaml: $(tail -1 "$STAGE/py.err")"
fi
grep -q "model_name: $NEW" "$TMP" && grep -q 'fastegy-smart' "$TMP" && grep -q 'fastegy_litellm_patch' "$TMP" ||
  rollback "the edited config.yaml looks wrong"
MODIFY=$(sed -n 's/^modify_params: //p' "$STAGE/py.err")
cat "$TMP" >"$CFG"
echo "== config.yaml: model $NEW added (thinking off, no temperature)"
[ "$MODIFY" = True ] && echo "  note: litellm_settings.modify_params is on; the two-round test below shows if it matters"

echo "== recreating $CTR (about 30 s)"
START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
(cd "$DIR" && docker compose up -d) >/dev/null 2>&1 || rollback "docker compose up failed"
live || rollback "LiteLLM did not answer within 90 s"
if ! docker exec "$CTR" sh -c 'test -n "$ANTHROPIC_API_KEY"'; then
  echo "  the container did not get the key; recreating it explicitly"
  START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  (cd "$DIR" && docker compose up -d --force-recreate --no-deps litellm) >/dev/null 2>&1 || rollback "docker compose up failed"
  live || rollback "LiteLLM did not answer within 90 s"
  docker exec "$CTR" sh -c 'test -n "$ANTHROPIC_API_KEY"' || rollback "the container still has no ANTHROPIC_API_KEY"
fi
sleep 3

echo "== tests"
docker logs --since "$START" "$CTR" 2>&1 | grep -q 'FASTEGY_PATCH active' && echo "  FastEgy patch: loaded" || rollback "the FastEgy patch line is missing"
echo "  ANTHROPIC_API_KEY: in the container (value not shown)"

# what Odoo does: round 1 with the catalog tool, round 2 with the result and tool_choice none
python3 - "$L/v1/chat/completions" "$NEW" "$STAGE" <<'PY' || rollback "Claude does not answer through LiteLLM"
import json, os, sys, time, urllib.request, urllib.error
url, model, stage = sys.argv[1:]
key = [l.split("=", 1)[1].strip() for l in open("/root/fastegy-reader/brain.env") if l.startswith("LLM_KEY=")][0]
def post(body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    t = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read()), time.monotonic() - t
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode(errors="replace")[:300]}, time.monotonic() - t
tools = [{"type": "function", "function": {"name": "lookup_product", "description": "Look up a Hikvision/EZVIZ model code",
          "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}}}]
msgs = [{"role": "system", "content": "You help customers of a CCTV distributor. Use lookup_product for any model code."},
        {"role": "user", "content": "What is DS-2CD1043G2-LIU?"}]
c, d, s = post({"model": model, "messages": msgs, "max_tokens": 300, "tools": tools, "disable_fallbacks": True})
if c != 200:
    print("  round 1 (with tools): %s %s" % (c, d.get("error", ""))); sys.exit(1)
m = d["choices"][0]["message"]
calls = m.get("tool_calls") or []
print("  round 1 (with tools): 200 in %.1f s, %s" % (s, "tool call " + calls[0]["function"]["name"] if calls else "answered in text"))
if calls:
    msgs.append({"role": "assistant", "content": m.get("content") or None, "tool_calls": calls})
    for x in calls:
        msgs.append({"role": "tool", "tool_call_id": x.get("id"),
                     "content": "DS-2CD1043G2-LIU: 4MP fixed bullet IP camera, smart hybrid light, built-in mic, IP67."})
    c, d, s = post({"model": model, "messages": msgs, "max_tokens": 300, "tools": tools, "tool_choice": "none",
                    "disable_fallbacks": True})
    if c != 200 or not (d["choices"][0]["message"].get("content") or "").strip():
        print("  round 2 (tool result): %s %s" % (c, d.get("error", "empty answer"))); sys.exit(1)
    print("  round 2 (tool result): 200 in %.1f s, answered in text" % s)
u = d.get("usage") or {}
print("  tokens of the last round: %s in / %s out" % (u.get("prompt_tokens"), u.get("completion_tokens")))
PY

python3 - "$SPECS" <<'PY' >/dev/null || rollback "specs.json has no fastegy-customer rules"
import json, sys
sys.exit(0 if (json.load(open(sys.argv[1])).get("fastegy-customer") or {}).get("rules") else 1)
PY
echo "== specs.json"
set_customer "$NEW" | sed 's/^/  /' || rollback "could not write specs.json"
manifest_has "$NEW" || rollback "the manifest does not show the new model"
show | sed 's/^/  /'

echo "== one real answer per spec, through the brain link (the website adds its name, prices and tools)"
mget >"$STAGE/m.json"
for spec in "$CUST" "$BACKUP"; do
  python3 - "$STAGE/m.json" "$spec" >"$STAGE/req.json" <<'PY'
import json, sys
s = json.load(open(sys.argv[1]))["specs"][sys.argv[2]]
body = {"model": s["model"], "max_tokens": 400,
        "messages": [{"role": "system", "content": s["rules"]}, {"role": "user", "content": "عاوز كاميرات للبيت"}]}
if s.get("temperature") is not None:
    body["temperature"] = s["temperature"]
print(json.dumps(body, ensure_ascii=False))
PY
  T0=$(date +%s.%N)
  CODE=$(curl -s -m 120 -o "$STAGE/ans.json" -w '%{http_code}' -H "Authorization: Bearer $BKEY" \
    -H 'Content-Type: application/json' "$B/v1/chat/completions" --data-binary @"$STAGE/req.json" || true)
  SECS=$(python3 -c "import sys; print('%.1f' % (float(sys.argv[2]) - float(sys.argv[1])))" "$T0" "$(date +%s.%N)")
  echo "  [$spec] $CODE in $SECS s — عميل: عاوز كاميرات للبيت"
  if [ "$CODE" = 200 ]; then
    python3 -c 'import json,sys; print("\n".join("     " + l for l in json.load(open(sys.argv[1]))["choices"][0]["message"]["content"].strip().splitlines()))' "$STAGE/ans.json"
  elif [ "$spec" = "$CUST" ]; then
    rollback "the customer spec does not answer through the brain link"
  else
    echo "     (the backup model answered $CODE — on the free Groq tier this is usually its daily limit; Claude is unaffected)"
  fi
done

unset BKEY LKEY
echo "DONE brain 3. The website chat answers with Claude Haiku from its next question."
echo "Back to the old model at any time:  bash brain_3_claude.sh --back"
echo "Previous files: $CFG.bak.$TS, $ENVF.bak.$TS (root only), $SPECS.bak.$TS"
