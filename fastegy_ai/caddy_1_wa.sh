#!/usr/bin/env bash
# =============================================================================
# FastEgy — wa.fastegy.net answers 502
#
# diag_3: in /etc/caddy/Caddyfile, wa.fastegy.net points to localhost:9000,
# where nothing listens. The WhatsApp gateway it most likely meant is
# Evolution API, published on 8081 (older Caddyfile backups point there).
#   --to-evolution  point wa.fastegy.net to localhost:8081 (Evolution API, over
#                   HTTPS; its API still needs its own key)
#   --remove        take wa.fastegy.net out of Caddy
# The new Caddyfile is validated before use; Caddy reloads without stopping;
# every site's answer is compared before/after, and anything other than the
# expected change on wa.fastegy.net puts the old Caddyfile back.
# Run    : bash caddy_1_wa.sh --to-evolution     (or --remove)
# Version: 1.0 — 2026-10-09
# =============================================================================
set -euo pipefail

CF=/etc/caddy/Caddyfile
MODE=${1:-}
SITES="ai.fastegy.net status.fastegy.net go.fastegy.net scraper.fastegy.net n8n.fastegy.net mail.fastegy.net waha.fastegy.net chat.fastegy.net wa.fastegy.net"
TS=$(date +%Y%m%d_%H%M%S)

[ "$MODE" = --to-evolution ] || [ "$MODE" = --remove ] || { echo "usage: bash caddy_1_wa.sh --to-evolution | --remove"; exit 1; }
[ "$(systemctl is-active caddy 2>/dev/null)" = active ] || { echo "Caddy is not running as a service here; nothing changed."; exit 1; }
[ -f "$CF" ] && command -v caddy >/dev/null || { echo "Missing $CF or the caddy command; nothing changed."; exit 1; }
if [ "$MODE" = --to-evolution ]; then
  curl -s -m 5 http://127.0.0.1:8081/ | grep -q 'Evolution API' || { echo "Evolution API does not answer on 8081; nothing changed."; exit 1; }
fi

NEW="$CF.new.$TS"
python3 - "$CF" "$NEW" "$MODE" <<'PY'
import re, sys
src, dst, mode = sys.argv[1:]
lines = open(src, encoding="utf-8").read().split("\n")
start = next((i for i, l in enumerate(lines) if re.match(r"^\s*wa\.fastegy\.net\s*\{\s*$", l)), None)
if start is None:
    sys.exit("no 'wa.fastegy.net {' block found")
depth, end = 0, None
for i in range(start, len(lines)):
    depth += lines[i].count("{") - lines[i].count("}")
    if depth == 0:
        end = i
        break
block = lines[start:end + 1]
targets = [l for l in block if re.match(r"^\s*reverse_proxy\s+localhost:9000\s*$", l)]
if len(targets) != 1:
    sys.exit("the block is not the expected one-line 'reverse_proxy localhost:9000'; nothing changed")
if mode == "--to-evolution":
    block = [re.sub(r"localhost:9000", "localhost:8081", l) for l in block]
    out = lines[:start] + block + lines[end + 1:]
else:
    out = lines[:start] + lines[end + 1:]
    while start < len(out) and start > 0 and out[start].strip() == "" and out[start - 1].strip() == "":
        del out[start]
open(dst, "w", encoding="utf-8").write("\n".join(out))
print("\n".join(block) if mode == "--to-evolution" else "block removed")
PY
caddy validate --config "$NEW" --adapter caddyfile >/dev/null 2>&1 || { rm -f "$NEW"; echo "The new Caddyfile does not validate; nothing changed."; exit 1; }
echo "new Caddyfile validated"

codes() { for s in $SITES; do printf '%s %s\n' "$s" "$(curl -s -o /dev/null -m 15 -w '%{http_code}' "https://$s/" || true)"; done; }
BEFORE=$(codes)
cp -p "$CF" "$CF.bak.$TS"
rollback() {
  echo "!! $1 — putting the old Caddyfile back"
  cp -p "$CF.bak.$TS" "$CF"
  systemctl reload caddy && echo "Old Caddyfile restored." || echo "Reload failed: cp $CF.bak.$TS $CF && systemctl reload caddy"
  exit 1
}
cat "$NEW" >"$CF"; rm -f "$NEW"
systemctl reload caddy || rollback "Caddy did not reload"
sleep 3
AFTER=$(codes)
echo "== sites (before -> after)"
paste -d' ' <(echo "$BEFORE") <(echo "$AFTER" | awk '{print $2}') | awk '{printf "  %-22s %s -> %s\n", $1, $2, $3}'
OTHERS_B=$(grep -v '^wa\.fastegy\.net ' <<<"$BEFORE"); OTHERS_A=$(grep -v '^wa\.fastegy\.net ' <<<"$AFTER")
[ "$OTHERS_B" = "$OTHERS_A" ] || { sleep 5; OTHERS_A=$(codes | grep -v '^wa\.fastegy\.net '); }
[ "$OTHERS_B" = "$OTHERS_A" ] || rollback "another site answers differently"
WA=$(awk '$1 == "wa.fastegy.net" {print $2}' <<<"$AFTER")
if [ "$MODE" = --to-evolution ]; then
  [ "$WA" = 200 ] && curl -s -m 10 https://wa.fastegy.net/ | grep -q 'Evolution API' || rollback "wa.fastegy.net does not show Evolution API"
  echo "wa.fastegy.net now: Evolution API over HTTPS"
else
  [ "$WA" != 502 ] || rollback "wa.fastegy.net still answers 502"
  echo "wa.fastegy.net is no longer served"
fi
echo "DONE caddy 1. Previous Caddyfile: $CF.bak.$TS"
