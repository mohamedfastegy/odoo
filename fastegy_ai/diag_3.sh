#!/usr/bin/env bash
# =============================================================================
# FastEgy — read-only diagnosis for three items
#   A) wa.fastegy.net answers 502
#   B) LiteLLM: which credentials are written in plain text
#   C) port 8081 (Evolution API) is open to the internet: who uses it
# Changes nothing. Secret values are never printed: they show as
# <redacted, N chars>; references such as os.environ/X or ${X} are shown.
# Run    : bash diag_3.sh
# Version: 1.0 — 2026-10-09
# =============================================================================
set -uo pipefail

mask() {
  python3 -c '
import re, sys
KEY = re.compile(r"(?i)([\w.-]*(?:key|secret|password|passwd|token|auth)[\w.-]*\s*[\"\x27]?\s*[:=]\s*[\"\x27]?)([^\"\x27\s,}#]+)")
def hide(m):
    v = m.group(2)
    return m.group(0) if v.startswith(("os.environ/", "${", "$")) else f"{m.group(1)}<redacted, {len(v)} chars>"
for line in sys.stdin:
    line = KEY.sub(hide, line)
    line = re.sub(r"(://[^:/@\s]+:)[^@\s]+@", r"\1<redacted>@", line)
    line = re.sub(r"\b(sk-|gsk_|AIza)[A-Za-z0-9_\-]{6,}", lambda x: x.group(1) + "<redacted>", line)
    line = re.sub(r"\$2[aby]\$\d+\$\S+", "<password hash>", line)
    sys.stdout.write(line)'
}
block() {   # $1 = site name: print its Caddyfile block from stdin
  awk -v site="$1" '
    !inside && index($0, site) && $0 ~ /\{[[:space:]]*$/ { inside = 1; depth = 0 }
    inside { print; n = gsub(/\{/, "{"); m = gsub(/\}/, "}"); depth += n - m; if (depth <= 0) { inside = 0; print "" } }'
}

echo "################ A) wa.fastegy.net"
echo "-- listening on 80/443:"; ss -ltnpH '( sport = :80 or sport = :443 )' | awk '{print "  " $4, $6}'
CADDY=$(docker ps --format '{{.Names}} {{.Image}}' | awk 'tolower($2) ~ /caddy/ {print $1; exit}')
if [ -n "$CADDY" ]; then
  echo "-- proxy: container $CADDY"
  CF=$(docker exec "$CADDY" sh -c 'cat /etc/caddy/Caddyfile 2>/dev/null')
elif [ -f /etc/caddy/Caddyfile ]; then
  echo "-- proxy: Caddy on the host"; CF=$(cat /etc/caddy/Caddyfile)
else
  echo "-- proxy: no Caddy found"; CF=""
fi
echo "-- its block for wa.fastegy.net (and waha.fastegy.net, which works, to compare):"
printf '%s\n' "$CF" | block wa.fastegy.net | mask | sed 's/^/  /'
printf '%s\n' "$CF" | block waha.fastegy.net | mask | sed 's/^/  /'
UP=$(printf '%s\n' "$CF" | block wa.fastegy.net | grep -m1 -oE 'reverse_proxy[[:space:]]+[^[:space:]{]+' | awk '{print $2}')
echo "-- upstream: ${UP:-not found}"
if [ -n "$UP" ] && [ -n "$CADDY" ]; then
  echo "-- from the proxy to the upstream:"
  docker exec "$CADDY" sh -c "wget -S -q -O /dev/null -T 5 http://${UP#http://}/ 2>&1 | head -3 || echo '  no answer'" | sed 's/^/  /'
fi
echo "-- WhatsApp-like containers (all states):"
docker ps -a --format '{{.Names}}\t{{.Status}}\t{{.Image}}\t{{.Ports}}' | grep -iE 'evolution|wa|whats|baileys' | sed 's/^/  /'
for c in $(docker ps -a --format '{{.Names}} {{.Image}}' | awk 'tolower($0) ~ /evolution/ {print $1}'); do
  echo "-- $c: restarts $(docker inspect -f '{{.RestartCount}}' "$c"), started $(docker inspect -f '{{.State.StartedAt}}' "$c"), health $(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$c")"
  echo "   networks: $(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$c")"
  echo "   last 25 log lines:"; docker logs --tail 25 "$c" 2>&1 | cut -c1-220 | mask | sed 's/^/     /'
done

echo; echo "################ B) LiteLLM credentials"
ls -la /root/litellm 2>/dev/null | sed 's/^/  /'
for f in /root/litellm/*.yml /root/litellm/*.yaml /root/litellm/.env /root/litellm/*.env; do
  [ -f "$f" ] || continue
  echo "-- $f ($(stat -c '%a %U' "$f")):"
  grep -nEi 'key|secret|password|token|database_url|env_file|environment|os\.environ|\$\{' "$f" | mask | sed 's/^/  /'
done
echo "-- LiteLLM container environment (names only):"
docker inspect litellm -f '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | cut -d= -f1 | grep -vE '^(PATH|HOME|LANG|HOSTNAME|PYTHON.*|GPG_KEY)$' | tr '\n' ' '; echo
echo "-- LibreChat side (names / masked):"
grep -nEi 'litellm|baseURL|apiKey' /root/librechat/librechat.yaml 2>/dev/null | mask | sed 's/^/  /'
grep -E '^[A-Z0-9_]+=' /root/librechat/.env 2>/dev/null | cut -d= -f1 | grep -iE 'litellm|openai|groq|gemini|key' | tr '\n' ' '; echo

echo; echo "################ C) port 8081"
ss -ltnpH '( sport = :8081 )' | awk '{print "  listening: " $4, $6}'
docker ps --filter publish=8081 --format '  container: {{.Names}} ({{.Image}}) {{.Ports}}'
echo "  local answer: $(curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8081/) $(curl -s -m 5 http://127.0.0.1:8081/ | head -c 160 | tr '\n' ' ')"
echo "  firewall: DOCKER-USER $(iptables -S DOCKER-USER | grep -c 8081) rules for 8081; ufw: $(ufw status 2>/dev/null | grep -c 8081) lines"
echo "  connections from outside since boot (Docker NAT counter):"
iptables -t nat -L DOCKER -v -n -x 2>/dev/null | awk '/dpt:8081/ {print "    " $1 " packets, " $2 " bytes"}'
echo "  uptime: $(uptime -p)"
echo "  config files that mention 8081 (not these scripts):"
grep -rIln --exclude='*.sh' --exclude='*.bak*' -e ':8081' /etc /root /opt 2>/dev/null | grep -v '/root/reboot_check' | head -10 | sed 's/^/    /'
EVO=$(docker ps --format '{{.Names}} {{.Image}}' | awk 'tolower($0) ~ /evolution/ {print $1; exit}')
if [ -n "$EVO" ]; then
  K=$(docker exec "$EVO" printenv AUTHENTICATION_API_KEY 2>/dev/null)
  if [ -n "$K" ]; then
    echo "  Evolution instances (name, state, webhook host), read with its own key:"
    curl -s -m 10 -H "apikey: $K" http://127.0.0.1:8081/instance/fetchInstances | python3 -c '
import json, sys, urllib.parse
try:
    data = json.load(sys.stdin)
except Exception as e:
    print("    could not read:", e); sys.exit()
items = data if isinstance(data, list) else data.get("response", data.get("instances", []))
for i in items or []:
    inst = i.get("instance", i)
    name = inst.get("instanceName") or inst.get("name")
    state = inst.get("connectionStatus") or inst.get("status") or inst.get("state")
    hook = (i.get("webhook") or inst.get("webhook") or {})
    url = hook.get("url") if isinstance(hook, dict) else hook
    host = urllib.parse.urlparse(url).netloc if url else "-"
    print(f"    {name}: {state}; webhook to {host}")'
  fi
fi
echo; echo "DONE diag 3 (nothing was changed)"
