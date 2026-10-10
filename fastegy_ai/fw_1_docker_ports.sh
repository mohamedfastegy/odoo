#!/usr/bin/env bash
# =============================================================================
# FastEgy — close the Docker ports the firewall rule misses
#
# Why    : /etc/ufw/after.rules blocks outside traffic to the Docker ports with
#            -A DOCKER-USER -i eth0 -p tcp -m multiport --dports 3000,...,9443 -j DROP
#          but DOCKER-USER sees the port after Docker has translated it. Ports
#          published on a different container port slip through:
#            3003 -> 3001 (uptime-kuma)     8085 -> 8080 (gmaps-scraper: skips the
#            8890 -> 8080 (yourls)                         scraper.fastegy.net password)
#            8081 -> 8080 (evolution-api)
# Change : for every port of that list, one rule that matches the port the outside
#          client asked for (-m conntrack --ctorigdstport P --ctdir ORIGINAL).
#          8081 is left open unless --with-8081 is given.
#          The domains keep working: Caddy reaches these services from inside the
#          server, never through eth0.
# Safety : backup; rules validated before loading; every site's HTTP status is
#          compared before/after; automatic rollback on any difference.
# Run    : sudo bash fw_1_docker_ports.sh              (8081 stays open)
#          sudo bash fw_1_docker_ports.sh --with-8081  (8081 closed too)
# Version: 1.0 — 2026-10-08
# =============================================================================
set -euo pipefail

F=/etc/ufw/after.rules
SITES="ai.fastegy.net status.fastegy.net go.fastegy.net scraper.fastegy.net n8n.fastegy.net mail.fastegy.net waha.fastegy.net chat.fastegy.net wa.fastegy.net"
WITH_8081=no; [ "${1:-}" = "--with-8081" ] && WITH_8081=yes

[ "$(id -u)" = 0 ] || { echo "Run as root (sudo)"; exit 1; }
[ -f "$F" ] || { echo "Missing $F"; exit 1; }
ufw status | grep -q "Status: active" || { echo "ufw is not active; nothing changed"; exit 1; }

LINE=$(grep -E '^-A DOCKER-USER -i eth0 -p tcp -m multiport --dports [0-9,]+ -j DROP$' "$F" || true)
[ "$(printf '%s' "$LINE" | grep -c . || true)" = 1 ] || { echo "Expected exactly one DOCKER-USER multiport DROP line in $F; nothing changed"; exit 1; }
PORTS=$(sed -E 's/.*--dports ([0-9,]+) .*/\1/' <<<"$LINE" | tr , ' ')
[ "$WITH_8081" = yes ] || PORTS=$(tr ' ' '\n' <<<"$PORTS" | grep -vx 8081 | tr '\n' ' ')

NEW=()
for p in $PORTS; do
  r="-A DOCKER-USER -i eth0 -p tcp -m conntrack --ctorigdstport $p --ctdir ORIGINAL -j DROP"
  grep -qxF -- "$r" "$F" || NEW+=("$r")
done
if [ ${#NEW[@]} -eq 0 ]; then echo "All rules already present; nothing to do"; exit 0; fi
echo "Adding ${#NEW[@]} rule(s) for ports: $PORTS"

codes() { for s in $SITES; do printf '%s %s\n' "$s" "$(curl -s -o /dev/null -m 15 -w '%{http_code}' "https://$s/" || true)"; done; }
BEFORE=$(codes)

TS=$(date +%Y%m%d_%H%M%S)
BAK="$F.bak.$TS"; [ -e "$BAK" ] && BAK="$BAK.$$"
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
cp -p "$F" "$BAK"
echo "Backup: $BAK"

# the new rules go right after the multiport line
awk -v line="$LINE" -v add="$(printf '%s\n' "${NEW[@]}")" '{ print } $0 == line { printf "%s\n", add }' "$F" >"$TMP"
[ "$(wc -l <"$TMP")" -eq $(( $(wc -l <"$F") + ${#NEW[@]} )) ] || { echo "Edit produced unexpected output; nothing changed"; exit 1; }

# validate with iptables-restore when it can check the current file (it needs ufw's live chains)
if iptables-restore --test --noflush <"$F" >/dev/null 2>&1; then
  iptables-restore --test --noflush <"$TMP" || { echo "New rules do not validate; nothing changed"; exit 1; }
  echo "Rules validated."
else
  echo "Note: iptables-restore cannot test $F here; relying on the reload check."
fi

rollback() {
  echo "!! $1 — rolling back"
  cat "$BAK" >"$F"
  ufw reload >/dev/null && echo "Restored $BAK and reloaded ufw." || echo "ufw reload failed: run  cat $BAK > $F && ufw reload"
  exit 1
}

cat "$TMP" >"$F"
ufw reload >/dev/null || rollback "ufw reload failed"
ufw status | grep -q "Status: active" || rollback "ufw is not active after the reload"
LOADED=$(iptables -S DOCKER-USER | grep -c -- '--ctorigdstport' || true)
[ "$LOADED" -ge ${#NEW[@]} ] || rollback "the new rules are not loaded"
# the new rules must sit before the chain's final RETURN, or they never match
[ "$(iptables -S DOCKER-USER | grep -v '^-N' | tail -1)" = "-A DOCKER-USER -j RETURN" ] || rollback "unexpected rule order"

AFTER=$(codes)
if [ "$BEFORE" != "$AFTER" ]; then sleep 5; AFTER=$(codes); fi    # one re-check for a slow site
echo "== sites (before -> after)"
paste -d' ' <(echo "$BEFORE") <(echo "$AFTER" | awk '{print $2}') | awk '{printf "  %-22s %s -> %s\n", $1, $2, $3}'
[ "$BEFORE" = "$AFTER" ] || rollback "a site answers differently after the change"

echo "== DOCKER-USER now"
iptables -S DOCKER-USER | sed 's/^/  /'
echo "DONE. Backup kept at: $BAK"
echo "Undo later:  cat $BAK > $F && ufw reload"
echo "Check from your laptop (should time out now):  nc -vz -w 5 76.13.48.108 8085"
