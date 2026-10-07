#!/usr/bin/env bash
# =============================================================================
# FastEgy AI — web search 5: search through engines that work from this server
#
# Why    : LibreChat 0.8.6 always asks SearXNG for google,bing,duckduckgo. From
#          this VPS Google is denied, DuckDuckGo shows a CAPTCHA and Bing is weak
#          (1/4). Yahoo, Startpage and Yandex each scored 4/4 (web_4). Renaming
#          engines inside SearXNG crashes it (engine traits are looked up by
#          name), so instead fastegy-reader (v2) relays LibreChat's search to
#          SearXNG with engines=yahoo,startpage,yandex.
# Steps  : 1) SearXNG: switch on yahoo, startpage, yandex
#          2) fastegy-reader v2 (scraper unchanged + /search relay)
#          3) measure relay vs. today; stop and restore if not better
#          4) LibreChat: SEARXNG_INSTANCE_URL -> http://fastegy-reader:3002
#             (container recreated, ~30-60 s), with rollback
# Run    : sudo bash web_5_search_relay.sh
# Version: 1.0 — 2026-10-08
# =============================================================================
set -euo pipefail

LC=librechat
SX=searxng
RD=fastegy-reader
PYCTR=litellm
DIR=/root/librechat
COMPOSE=$DIR/docker-compose.yml
SXCFG=/root/searxng/settings.yml
RDDIR=/root/fastegy-reader
KEYFILE=$RDDIR/.key
ENGINES=yahoo,startpage,yandex
TS=$(date +%Y%m%d_%H%M%S)

for c in "$LC" "$SX" "$RD" "$PYCTR"; do
  [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = true ] || { echo "$c is not running; nothing changed."; exit 1; }
done
[ -s "$KEYFILE" ] || { echo "Missing $KEYFILE; run web_1_services.sh first. Nothing changed."; exit 1; }
IMG=$(docker inspect "$LC" -f '{{.Config.Image}}')
if [ "$(docker inspect "$LC" -f '{{.Image}}')" != "$(docker image inspect "$IMG" -f '{{.Id}}')" ]; then
  echo "The local image $IMG differs from the running LibreChat; recreating would upgrade it. Nothing changed."; exit 1
fi
PROJ=$(docker inspect "$LC" -f '{{index .Config.Labels "com.docker.compose.project"}}')
SVC=$(docker inspect "$LC" -f '{{index .Config.Labels "com.docker.compose.service"}}')
[ -n "$PROJ" ] && [ -n "$SVC" ] || { echo "LibreChat is not managed by docker compose; nothing changed."; exit 1; }
NET=$(docker inspect "$LC" -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' | awk '{print $1}')
export SVC

wait_url() {  # wait_url URL : until it answers 2xx from inside LibreChat
  for _ in $(seq 1 20); do
    docker exec "$LC" node -e "fetch('$1').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" && return 0
    sleep 2
  done
  return 1
}

MEASURE_JS='
const qs = [
  ["USD to EGP exchange rate today", t => /egp|egypt|exchange|usd|dollar/i.test(t)],
  ["سعر الدولار في مصر اليوم", t => /[؀-ۿ]|egp|dollar/i.test(t)],
  ["Hikvision DS-2CD1043G2-LIUF datasheet", t => /1043/.test(t)],
  ["Hikvision ColorVu camera", t => /colorvu/i.test(t)],
];
(async () => {
  let score = 0; const notes = [];
  for (const [q, ok] of qs) {
    try {
      // exactly what LibreChat sends
      const u = process.env.BASE + "?format=json&pageno=1&categories=general&language=all&safesearch=1&engines=google,bing,duckduckgo&q=" + encodeURIComponent(q);
      const j = await (await fetch(u)).json();
      if (j.results.slice(0, 3).some(r => ok((r.title || "") + " " + (r.url || "")))) score++;
      for (const x of j.unresponsive_engines || []) notes.push(x[0] + ": " + x[1]);
    } catch (e) { notes.push(e.message); }
  }
  const why = [...new Set(notes)].join(", ");
  console.log("  " + process.env.LABEL.padEnd(34) + " " + score + "/4" + (why ? "   (" + why + ")" : ""));
  console.log("SCORE " + score);
})();'
measure() {  # measure LABEL BASEURL -> prints a line, sets SCORE
  local out
  out=$(docker exec -e LABEL="$1" -e BASE="$2" "$LC" node -e "$MEASURE_JS" 2>&1 || true)
  printf '%s\n' "$out" | grep -v '^SCORE '
  SCORE=$(printf '%s\n' "$out" | sed -n 's/^SCORE //p'); SCORE=${SCORE:-0}
}

echo "== search quality of LibreChat's request"
measure "today (google,bing,duckduckgo)" "http://searxng:8080/search"; BASE=$SCORE

# ------------------------------------------------- 1) SearXNG engines
cp -p "$SXCFG" "$SXCFG.bak.$TS"
PY_SX=$(cat <<'PY'
import sys, yaml
c = yaml.safe_load(sys.stdin)
want = ["yahoo", "startpage", "yandex"]
c["use_default_settings"] = True if not isinstance(c.get("use_default_settings"), dict) else c["use_default_settings"]
c["engines"] = [e for e in (c.get("engines") or []) if e.get("name") not in want] + \
               [{"name": n, "disabled": False, "inactive": False} for n in want]
yaml.safe_dump(c, sys.stdout, sort_keys=False, allow_unicode=True, width=1000)
PY
)
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
docker exec -i "$PYCTR" python3 -c "$PY_SX" <"$SXCFG.bak.$TS" >"$TMP"
grep -q '^engines:' "$TMP" || { echo "SearXNG rewrite failed; nothing changed"; exit 1; }
cat "$TMP" >"$SXCFG"
docker restart "$SX" >/dev/null

restore_services() {
  echo "!! $1 — restoring SearXNG and the reader"
  cat "$SXCFG.bak.$TS" >"$SXCFG"; docker restart "$SX" >/dev/null
  if [ -f "$RDDIR/reader.py.bak.$TS" ]; then
    cp "$RDDIR/reader.py.bak.$TS" "$RDDIR/reader.py"
    docker rm -f "$RD" >/dev/null 2>&1 || true
    docker run -d --name "$RD" --restart unless-stopped --network "$NET" --label fastegy.ai=web-search \
      --memory 384m -e READER_KEY="$(cat "$KEYFILE")" fastegy-reader:1 >/dev/null
  fi
  echo "LibreChat was not changed."; exit 1
}
wait_url "http://searxng:8080/healthz" || restore_services "SearXNG did not start"

# ------------------------------------------------- 2) reader v2
[ "$(docker inspect "$RD" -f '{{index .Config.Labels "fastegy.ai"}}')" = web-search ] ||
  restore_services "$RD was not created by web_1_services.sh"
cp -p "$RDDIR/reader.py" "$RDDIR/reader.py.bak.$TS"
cat >"$RDDIR/reader.py" <<'PY'
"""FastEgy reader: page reader + search relay for LibreChat web search.

POST /v2/scrape {"url": ..., "timeout": ms}
    Minimal Firecrawl-compatible scraper -> {"success": true, "data": {"markdown": ..., "metadata": {...}}}
    Only public http(s) URLs are fetched (private, loopback and link-local targets are refused,
    including on redirects), so the model cannot be steered into internal services.

GET /search?...
    Relay to SearXNG. LibreChat 0.8.6 always asks for engines=google,bing,duckduckgo, which are
    blocked from this server; the relay swaps in SEARCH_ENGINES (engines that answer from here)
    and passes every other parameter and the JSON reply through unchanged.

Version 2 — 2026-10-08
"""
import ipaddress
import json
import os
import socket
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import trafilatura

KEY = os.environ.get("READER_KEY", "")
MAX_CHARS = int(os.environ.get("READER_MAX_CHARS", "20000"))
SEARXNG = os.environ.get("SEARXNG_UPSTREAM", "http://searxng:8080/search")
ENGINES = os.environ.get("SEARCH_ENGINES", "yahoo,startpage,yandex")
MAX_BYTES = 3_000_000
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def check_public(url):
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("only public http(s) URLs are allowed")
    for info in socket.getaddrinfo(p.hostname, None):
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ValueError("URL not allowed (private address)")


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_public(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = urllib.request.build_opener(SafeRedirect)
# the SearXNG upstream is a fixed internal address: no proxy, no redirects to follow
UPSTREAM = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch(url, timeout):
    check_public(url)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ar,en;q=0.8"})
    with OPENER.open(req, timeout=timeout) as r:
        ctype = r.headers.get("Content-Type", "")
        if "html" not in ctype and "text" not in ctype:
            raise ValueError("unsupported content type: " + ctype)
        raw = r.read(MAX_BYTES)
        return raw.decode(r.headers.get_content_charset() or "utf-8", errors="replace"), r.status, r.geturl()


def to_markdown(html, url):
    text = trafilatura.extract(html, url=url, output_format="markdown", include_tables=True,
                               include_links=False, favor_recall=True)
    if not text:
        text = trafilatura.extract(html, url=url, output_format="txt", favor_recall=True)
    meta = trafilatura.extract_metadata(html, default_url=url)
    return (text or "")[:MAX_CHARS], (meta.title if meta else None)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        self._raw(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _raw(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urlparse(self.path)
        if p.path == "/health":
            return self._send(200, {"ok": True})
        if p.path.rstrip("/") != "/search":
            return self._send(404, {"ok": False})
        params = parse_qs(p.query, keep_blank_values=True)
        if ENGINES:
            params["engines"] = [ENGINES]
        req = urllib.request.Request(SEARXNG + "?" + urlencode(params, doseq=True),
                                     headers={"Accept": "application/json", "X-Real-IP": "127.0.0.1"})
        try:
            with UPSTREAM.open(req, timeout=20) as r:
                self._raw(r.status, r.read(), r.headers.get("Content-Type", "application/json"))
        except urllib.error.HTTPError as e:
            self._raw(e.code, e.read(), e.headers.get("Content-Type", "application/json"))
        except Exception as e:  # upstream down or timed out
            self._send(502, {"error": "search upstream failed: " + str(e)[:200]})

    def do_POST(self):
        if self.path.rstrip("/") not in ("/v1/scrape", "/v2/scrape"):
            return self._send(404, {"success": False, "error": "not found"})
        if KEY and self.headers.get("Authorization", "") != "Bearer " + KEY:
            return self._send(401, {"success": False, "error": "unauthorized"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            url = body["url"]
            # answer before the caller's own timeout runs out
            timeout = min(max(float(body.get("timeout") or 15000) / 1000 - 2, 3), 25)
            html, status, final = fetch(url, timeout)
            markdown, title = to_markdown(html, final)
            if not markdown.strip():
                return self._send(200, {"success": False, "error": "no readable content"})
            self._send(200, {"success": True, "data": {"markdown": markdown, "metadata": {
                "title": title, "sourceURL": url, "url": final, "statusCode": status}}})
        except Exception as e:  # report every failure to the caller as a failed scrape
            self._send(200, {"success": False, "error": str(e)[:300]})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 3002), Handler).serve_forever()
PY
echo "building fastegy-reader v2..."
docker build -q -t fastegy-reader:2 "$RDDIR" >/dev/null || restore_services "build failed"
docker rm -f "$RD" >/dev/null
docker run -d --name "$RD" --restart unless-stopped --network "$NET" --label fastegy.ai=web-search \
  --memory 384m -e READER_KEY="$(cat "$KEYFILE")" -e SEARCH_ENGINES="$ENGINES" fastegy-reader:2 >/dev/null ||
  restore_services "reader v2 failed to start"
wait_url "http://$RD:3002/health" || restore_services "reader v2 did not start"

# ------------------------------------------------- 3) measure
measure "through the relay ($ENGINES)" "http://$RD:3002/search"; RELAY=$SCORE
SCRAPE=$(docker exec -e K="$(cat "$KEYFILE")" "$LC" node -e '
fetch("http://fastegy-reader:3002/v2/scrape", { method: "POST",
  headers: { "Content-Type": "application/json", Authorization: "Bearer " + process.env.K },
  body: JSON.stringify({ url: "https://en.wikipedia.org/wiki/Egyptian_pound", formats: ["markdown"], timeout: 15000 }) })
  .then(r => r.json()).then(j => console.log(j.success ? "ok" : "fail " + j.error)).catch(e => console.log("fail " + e.message));' 2>&1 || true)
echo "  page reader: $SCRAPE"
[ "$RELAY" -gt "$BASE" ] || restore_services "the relay is not better ($RELAY vs $BASE)"
[ "$SCRAPE" = ok ] || restore_services "page reader check failed"

# ------------------------------------------------- 4) LibreChat
BAK_COMPOSE="$COMPOSE.bak.$TS"
cp -p "$COMPOSE" "$BAK_COMPOSE"
PY_C=$(cat <<'PY'
import copy, os, sys, yaml
old = yaml.safe_load(sys.stdin); new = copy.deepcopy(old)
svc = new["services"][os.environ["SVC"]]
env = svc.get("environment") or []
if isinstance(env, dict):
    env = [f"{k}={'' if v is None else v}" for k, v in env.items()]
env = [x for x in env if not str(x).startswith("SEARXNG_INSTANCE_URL=")] + ["SEARXNG_INSTANCE_URL=http://fastegy-reader:3002"]
svc["environment"] = env
a, b = copy.deepcopy(old), copy.deepcopy(new)
for x in (a, b):
    x["services"][os.environ["SVC"]]["environment"] = [e for e in (x["services"][os.environ["SVC"]].get("environment") or []) if not str(e).startswith("SEARXNG_INSTANCE_URL=")]
if a != b:
    sys.exit("ERROR: unexpected compose changes")
yaml.safe_dump(new, sys.stdout, sort_keys=False, allow_unicode=True, width=1000)
PY
)
TMP_C=$(mktemp "$DIR/.compose.XXXXXX.yml")
trap 'rm -f "$TMP" "$TMP_C"' EXIT
docker exec -i -e SVC "$PYCTR" python3 -c "$PY_C" <"$COMPOSE" >"$TMP_C" || restore_services "compose rewrite failed"
docker compose -p "$PROJ" --project-directory "$DIR" -f "$TMP_C" config -q || restore_services "new compose file did not validate"
cat "$TMP_C" >"$COMPOSE"

DC=(docker compose -p "$PROJ" --project-directory "$DIR" -f "$COMPOSE")
wait_up() {
  local code=000
  for _ in $(seq 1 60); do
    code=$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:3080/api/config || true)
    [ "$code" = 200 ] && return 0
    sleep 2
  done
  return 1
}
rollback() {
  echo "!! $1 — rolling back LibreChat"
  cat "$BAK_COMPOSE" >"$COMPOSE"
  "${DC[@]}" up -d --no-deps "$SVC" >/dev/null 2>&1 || docker restart "$LC" >/dev/null
  wait_up || true
  echo "LibreChat restored (it still searches the old way). The relay stays installed."; exit 1
}
echo "Recreating LibreChat (site down ~30-60 s)..."
"${DC[@]}" up -d --no-deps "$SVC" || rollback "docker compose up failed"
wait_up || rollback "LibreChat did not come back"
if docker logs --since 3m "$LC" 2>&1 | grep -i 'invalid custom config'; then rollback "LibreChat rejected the config"; fi
[ "$(docker exec "$LC" printenv SEARXNG_INSTANCE_URL)" = "http://fastegy-reader:3002" ] || rollback "the new setting did not reach LibreChat"
measure "LibreChat -> relay, after restart" "http://$RD:3002/search"
[ "$SCORE" -gt "$BASE" ] || rollback "search through the relay failed after the restart"

echo "DONE web 5. Backups: $SXCFG.bak.$TS  $RDDIR/reader.py.bak.$TS  $BAK_COMPOSE"
echo "Test: refresh (Cmd+Shift+R), NEW chat on the smart option: the dollar rate, and the specs of DS-2CD1043G2-LIUF."
