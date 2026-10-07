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
