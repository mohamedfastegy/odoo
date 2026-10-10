#!/usr/bin/env bash
# =============================================================================
# FastEgy server — checks around a reboot (the "System restart required" one)
#
#   bash reboot_check.sh before          read-only: saves what runs now and
#                                        warns about anything that will not
#                                        start again by itself
#   reboot                               (Mohamed runs it; ~2 minutes)
#   bash reboot_check.sh after           read-only: compares with "before"
#   bash reboot_check.sh start-missing   starts the containers that ran before
#                                        and do not run now (only if "after"
#                                        says so)
#
# Nothing is changed except by start-missing, which only runs `docker start`.
# Version: 1.0 — 2026-10-09
# =============================================================================
set -uo pipefail

DIR=/root/reboot_check
SITES="ai.fastegy.net status.fastegy.net go.fastegy.net scraper.fastegy.net n8n.fastegy.net mail.fastegy.net waha.fastegy.net chat.fastegy.net wa.fastegy.net"
mkdir -p "$DIR"

snapshot() {   # $1 = directory to write
  local d=$1
  mkdir -p "$d"
  uname -r >"$d/kernel"
  docker ps --format '{{.Names}}' | sort >"$d/containers"
  : >"$d/policies"
  for c in $(cat "$d/containers"); do
    echo "$c $(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$c")" >>"$d/policies"
  done
  ss -ltnH | awk '{print $4}' | sed 's/.*://' | sort -u >"$d/ports"
  systemctl list-units --type=service --state=running --no-legend --plain | awk '{print $1}' | sort >"$d/services"
  for s in $SITES; do
    printf '%s %s\n' "$s" "$(curl -s -o /dev/null -m 15 -w '%{http_code}' "https://$s/" || true)"
  done | sort >"$d/sites"
  iptables -S DOCKER-USER 2>/dev/null | grep -c -- '--ctorigdstport' >"$d/fw_rules" || true
}

case "${1:-}" in
before)
  rm -rf "$DIR/before"
  snapshot "$DIR/before"
  B=$DIR/before
  echo "== saved in $B ($(date -u +%H:%M) UTC)"
  echo "kernel now: $(cat "$B/kernel")"
  echo "containers running: $(wc -l <"$B/containers")"
  echo "listening ports: $(tr '\n' ' ' <"$B/ports")"
  echo "firewall rules for Docker ports: $(cat "$B/fw_rules")"
  echo "sites:"; sed 's/^/  /' "$B/sites"
  echo "== will they come back by themselves?"
  [ "$(systemctl is-enabled docker 2>/dev/null)" = enabled ] && echo "  docker starts at boot: yes" || echo "  !! docker is NOT enabled at boot (systemctl enable docker)"
  NO=$(awk '$2 == "no" || $2 == "" {print $1}' "$B/policies")
  if [ -n "$NO" ]; then
    echo "  !! these run now but have no restart policy, so they will NOT start by themselves:"
    echo "$NO" | sed 's/^/       /'
    echo "     That's fine: after the reboot, 'bash reboot_check.sh start-missing' starts them."
  else
    echo "  every running container restarts by itself: yes"
  fi
  echo "READY. Next: reboot   (then, about 2 minutes later: ssh in again and run  bash reboot_check.sh after)"
  ;;
after)
  [ -d "$DIR/before" ] || { echo "No 'before' snapshot: run it before rebooting."; exit 1; }
  rm -rf "$DIR/after"
  snapshot "$DIR/after"
  B=$DIR/before A=$DIR/after PROBLEMS=0
  echo "== kernel: $(cat "$B/kernel") -> $(cat "$A/kernel")"
  MISSING=$(comm -23 "$B/containers" "$A/containers")
  if [ -n "$MISSING" ]; then
    PROBLEMS=1; echo "!! containers that ran before and do not run now:"; echo "$MISSING" | sed 's/^/     /'
  else
    echo "containers: all $(wc -l <"$B/containers") that ran before run again"
  fi
  RESTARTING=$(docker ps -a --filter status=restarting --format '{{.Names}}')
  [ -n "$RESTARTING" ] && { PROBLEMS=1; echo "!! restarting in a loop: $RESTARTING"; }
  PORTS=$(comm -23 "$B/ports" "$A/ports" | tr '\n' ' ')
  [ -n "$PORTS" ] && { PROBLEMS=1; echo "!! ports that listened before and not now: $PORTS"; } || echo "ports: all listen again"
  SVC=$(comm -23 "$B/services" "$A/services" | tr '\n' ' ')
  [ -n "$SVC" ] && echo "note: services that ran before and not now (often one-off ones): $SVC"
  echo "sites (before -> after):"
  join "$B/sites" "$A/sites" | while read -r s b a; do
    flag=""; [ "$b" != "$a" ] && flag="   <- changed"
    echo "  $s $b -> $a$flag"
  done
  join "$B/sites" "$A/sites" | awk '$2 != $3' | grep -q . && PROBLEMS=1
  if [ "$(cat "$A/fw_rules")" -ge "$(cat "$B/fw_rules")" ] 2>/dev/null; then
    echo "firewall: $(cat "$A/fw_rules") Docker-port rules loaded (before: $(cat "$B/fw_rules"))"
  else
    PROBLEMS=1; echo "!! firewall: $(cat "$A/fw_rules") Docker-port rules loaded, before: $(cat "$B/fw_rules")"
  fi
  if docker ps --format '{{.Names}}' | grep -qx litellm; then
    START=$(docker inspect -f '{{.State.StartedAt}}' litellm)
    if docker logs --since "$START" litellm 2>&1 | grep -q 'FASTEGY_PATCH active'; then
      echo "LiteLLM: running, FastEgy patch loaded"
    else
      PROBLEMS=1; echo "!! LiteLLM runs but the FastEgy patch line is missing from its logs"
    fi
  fi
  if docker ps --format '{{.Names}}' | grep -qx librechat; then
    docker exec librechat node -e '
      Promise.all([
        fetch("http://fastegy-reader:3002/health").then(r => r.ok),
        fetch("http://fastegy-reader:3002/search?q=hikvision%20nvr&format=json").then(r => r.json()).then(j => (j.results || []).length)
      ]).then(([ok, n]) => { console.log("assistant tools: reader " + (ok ? "ok" : "DOWN") + ", web search " + n + " results"); process.exit(ok && n ? 0 : 2); })
        .catch(e => { console.log("assistant tools: error " + e.message); process.exit(2); });' || PROBLEMS=1
  fi
  if [ "$PROBLEMS" = 0 ]; then
    echo "ALL GOOD: everything that ran before the reboot runs again."
  else
    echo "SOME PROBLEMS above. Containers not running: bash reboot_check.sh start-missing"
  fi
  ;;
start-missing)
  [ -f "$DIR/before/containers" ] || { echo "No 'before' snapshot."; exit 1; }
  MISSING=$(comm -23 "$DIR/before/containers" <(docker ps --format '{{.Names}}' | sort))
  [ -z "$MISSING" ] && { echo "Nothing to start: everything from before runs."; exit 0; }
  for c in $MISSING; do
    printf 'starting %s ... ' "$c"; docker start "$c" >/dev/null && echo ok || echo FAILED
  done
  echo "Now run: bash reboot_check.sh after"
  ;;
*)
  echo "usage: bash reboot_check.sh before | after | start-missing"; exit 1 ;;
esac
