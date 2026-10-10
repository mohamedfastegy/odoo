#!/usr/bin/env bash
# =============================================================================
# FastEgy AI — web search 1/2: start the two internal services
#
#   searxng         self-hosted search engine (no API key, no cost)
#   fastegy-reader  small page reader that speaks Firecrawl's /v2/scrape API,
#                   so LibreChat's web search can read result pages without a
#                   paid scraper. Python + trafilatura, ~60 MB RAM.
#
# Both join LibreChat's Docker network only: no published ports, nothing new
# is reachable from the internet. LibreChat is NOT touched by this script.
# Undo   : docker rm -f searxng fastegy-reader
# Run    : sudo bash web_1_services.sh
# Version: 1.1 — 2026-10-08 (reader v2 with search relay; yahoo/startpage/yandex on)
# =============================================================================
set -euo pipefail

LC=librechat
LABEL=fastegy.ai=web-search

docker inspect "$LC" >/dev/null 2>&1 || { echo "Container $LC not found"; exit 1; }
NET=$(docker inspect "$LC" -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' | awk '{print $1}')
[ -n "$NET" ] || { echo "Could not find LibreChat's Docker network"; exit 1; }
echo "LibreChat network: $NET"

# Replace a container only if this script created it (carries our label).
replace_ok() {
  docker inspect "$1" >/dev/null 2>&1 || return 0
  if [ "$(docker inspect "$1" -f '{{index .Config.Labels "fastegy.ai"}}')" = web-search ]; then
    docker rm -f "$1" >/dev/null
  else
    echo "A container named $1 already exists and was not created by this script; stopping."; exit 1
  fi
}

# ------------------------------------------------------------------ SearXNG
mkdir -p /root/searxng
chmod 777 /root/searxng   # the image runs as its own user; /root itself stays root-only
if [ ! -f /root/searxng/settings.yml ]; then
  SECRET=$(openssl rand -hex 32)
  cat >/root/searxng/settings.yml <<EOF
# FastEgy AI search engine. LibreChat asks for: google, bing, duckduckgo.
use_default_settings: true
server:
  secret_key: "$SECRET"
  limiter: false          # internal only, no bot protection needed
  image_proxy: false
  public_instance: false
search:
  safe_search: 0
  formats:
    - html
    - json                # LibreChat reads JSON
outgoing:
  request_timeout: 6.0
engines:
  - name: google
    disabled: false
  - name: bing
    disabled: false
  - name: duckduckgo
    disabled: false
  # these answer from this server (see web_4/web_5); the reader relays searches to them
  - name: yahoo
    disabled: false
    inactive: false
  - name: startpage
    disabled: false
    inactive: false
  - name: yandex
    disabled: false
    inactive: false
EOF
  chmod 644 /root/searxng/settings.yml
fi
replace_ok searxng
docker run -d --name searxng --restart unless-stopped --network "$NET" --label "$LABEL" \
  --memory 512m -v /root/searxng:/etc/searxng:rw searxng/searxng:latest >/dev/null
echo "searxng started"

# ------------------------------------------------------------- page reader
mkdir -p /root/fastegy-reader
cat >/root/fastegy-reader/reader.py <<'PY'
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
cat >/root/fastegy-reader/Dockerfile <<'EOF'
FROM python:3.12-slim
RUN pip install --no-cache-dir "trafilatura>=1.12,<3"
COPY reader.py /app/reader.py
USER nobody
EXPOSE 3002
CMD ["python", "-u", "/app/reader.py"]
EOF
echo "building fastegy-reader (about a minute)..."
docker build -q -t fastegy-reader:2 /root/fastegy-reader >/dev/null

KEYFILE=/root/fastegy-reader/.key
[ -s "$KEYFILE" ] || openssl rand -hex 24 >"$KEYFILE"
chmod 600 "$KEYFILE"
replace_ok fastegy-reader
docker run -d --name fastegy-reader --restart unless-stopped --network "$NET" --label "$LABEL" \
  --memory 384m -e READER_KEY="$(cat "$KEYFILE")" -e SEARCH_ENGINES=yahoo,startpage,yandex fastegy-reader:2 >/dev/null
echo "fastegy-reader started"

# ---------------------------------------------- tests from LibreChat's side
echo "== tests (run inside the LibreChat container, exactly as it will call them)"
SEARCH_JS='
const u = "http://searxng:8080/search?format=json&engines=google,bing,duckduckgo&q=" + encodeURIComponent("سعر الدولار في مصر");
fetch(u).then(r => r.json()).then(j => {
  console.log("searxng: results=" + j.results.length + " unresponsive=" + JSON.stringify(j.unresponsive_engines || []));
  if (j.results[0]) console.log("  first: " + j.results[0].title + " — " + j.results[0].url);
  process.exit(j.results.length > 0 ? 0 : 2);
}).catch(e => { console.log("searxng: not ready (" + e.message + ")"); process.exit(1); });'
READ_JS='
const call = (url) => fetch("http://fastegy-reader:3002/v2/scrape", { method: "POST",
  headers: { "Content-Type": "application/json", Authorization: "Bearer " + process.env.K },
  body: JSON.stringify({ url, formats: ["markdown"], timeout: 15000 }) }).then(r => r.json());
(async () => {
  const ok = await call("https://en.wikipedia.org/wiki/Egyptian_pound");
  console.log("reader: success=" + ok.success + " chars=" + ((ok.data && ok.data.markdown) || "").length + " " + (ok.error || ""));
  const blocked = await call("http://librechat:3080/api/config");
  console.log("reader guard (internal URL must be refused): success=" + blocked.success + " " + (blocked.error || ""));
  process.exit(ok.success && !blocked.success ? 0 : 2);
})().catch(e => { console.log("reader: not ready (" + e.message + ")"); process.exit(1); });'

s=1
for _ in $(seq 1 15); do
  set +e; docker exec "$LC" node -e "$SEARCH_JS"; s=$?; set -e
  [ "$s" = 1 ] || break
  sleep 3
done
r=1
for _ in $(seq 1 10); do
  set +e; docker exec -e K="$(cat "$KEYFILE")" "$LC" node -e "$READ_JS"; r=$?; set -e
  [ "$r" = 1 ] || break
  sleep 3
done

if [ "$s" != 0 ]; then
  echo "!! SearXNG returned no results. Last log lines:"; docker logs --tail 25 searxng 2>&1 | tail -25
fi
if [ "$r" != 0 ]; then
  echo "!! The page reader test failed. Last log lines:"; docker logs --tail 25 fastegy-reader 2>&1 | tail -25
fi
if [ "$s" = 0 ] && [ "$r" = 0 ]; then
  echo "DONE web 1/2: both services work. Next: web_2_enable.sh"
else
  echo "STOPPED: send this output to Claude. LibreChat was not changed."
  exit 1
fi
