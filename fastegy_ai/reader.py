"""FastEgy reader: page reader + search relay for LibreChat web search.

POST /v2/scrape {"url": ..., "timeout": ms}
    Minimal Firecrawl-compatible scraper -> {"success": true, "data": {"markdown": ..., "metadata": {...}}}
    Only public http(s) URLs are fetched (private, loopback and link-local targets are refused,
    including on redirects), so the model cannot be steered into internal services.

GET /search?...
    Relay to SearXNG. LibreChat 0.8.6 always asks for engines=google,bing,duckduckgo, which are
    blocked from this server; the relay swaps in SEARCH_ENGINES (engines that answer from here)
    and passes every other parameter and the JSON reply through unchanged.

POST /mcp
    Minimal MCP server (streamable HTTP, JSON responses) exposing the product catalog to
    LibreChat: lookup_product(code) and search_catalog(keywords). See products.py.
    Two sources: the Hikvision brochure (CATALOG_PATH) and FastEgy's own product list from
    Odoo (CARRIED_PATH, optional, built by kb/odoo_products.py). Both reload when replaced.

Version 4 — 2026-10-08
"""
import ipaddress
import json
import os
import socket
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import io

import trafilatura
from pypdf import PdfReader

import products

KEY = os.environ.get("READER_KEY", "")
MAX_CHARS = int(os.environ.get("READER_MAX_CHARS", "20000"))
SEARXNG = os.environ.get("SEARXNG_UPSTREAM", "http://searxng:8080/search")
ENGINES = os.environ.get("SEARCH_ENGINES", "yahoo,startpage,yandex")
CATALOG_PATH = os.environ.get("CATALOG_PATH", "/app/catalog.json")
CARRIED_PATH = os.environ.get("CARRIED_PATH", "/app/carried.json")
MCP_KEY = os.environ.get("MCP_KEY", "")
MAX_BYTES = 3_000_000
MAX_PDF_BYTES = 15_000_000
MAX_PDF_PAGES = 12          # datasheets are 3-8 pages; brochures are cut to the first pages
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
        if "pdf" in ctype or r.geturl().lower().split("?")[0].endswith(".pdf"):
            return r.read(MAX_PDF_BYTES), r.status, r.geturl()       # bytes: handled by to_markdown
        if "html" not in ctype and "text" not in ctype:
            raise ValueError("unsupported content type: " + ctype)
        raw = r.read(MAX_BYTES)
        return raw.decode(r.headers.get_content_charset() or "utf-8", errors="replace"), r.status, r.geturl()


def pdf_to_text(data):
    """Text of the first pages of a PDF (official datasheets are PDFs)."""
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages[:MAX_PDF_PAGES]:
        text = page.extract_text() or ""
        pages.append("\n".join(line.rstrip() for line in text.splitlines() if line.strip()))
    title = (reader.metadata.title if reader.metadata else None) or None
    return "\n\n".join(pages)[:MAX_CHARS], title


def to_markdown(html, url):
    if isinstance(html, bytes):
        return pdf_to_text(html)
    text = trafilatura.extract(html, url=url, output_format="markdown", include_tables=True,
                               include_links=False, favor_recall=True)
    if not text:
        text = trafilatura.extract(html, url=url, output_format="txt", favor_recall=True)
    meta = trafilatura.extract_metadata(html, default_url=url)
    return (text or "")[:MAX_CHARS], (meta.title if meta else None)


_catalog = {"mtime": None, "obj": None}


def catalog():
    """Load the catalog files, reloading when one of them changes (no restart needed)."""
    mtime = tuple(os.path.getmtime(p) if os.path.exists(p) else None for p in (CATALOG_PATH, CARRIED_PATH))
    if _catalog["mtime"] != mtime:
        _catalog["obj"], _catalog["mtime"] = products.Catalog(CATALOG_PATH, CARRIED_PATH), mtime
    return _catalog["obj"]


MCP_TOOLS = [
    {"name": "lookup_product",
     "description": ("Look up a Hikvision / EZVIZ model code. Call this FIRST whenever the user mentions "
                     "or asks about a model code, before searching the web. Answers from two sources: "
                     "FastEgy's own product list from Odoo (whether FastEgy sells the model, lens options, "
                     "FastEgy's Arabic description) and the Hikvision brochure (official key specs). Returns "
                     "the exact entries, or the closest codes when there is no exact match. Never state specs "
                     "for a code this tool did not match exactly."),
     "inputSchema": {"type": "object", "properties": {"code": {"type": "string",
                     "description": "Model code as the user wrote it, e.g. DS-2CD2043G2-LIZ2UY"}},
                     "required": ["code"]}},
    {"name": "search_catalog",
     "description": ("Search the products FastEgy sells (Odoo list, Arabic descriptions) and the Hikvision "
                     "brochure by keywords in English or Arabic, e.g. 'ColorVu 4 MP', '16-ch NVR PoE', "
                     "'كاميرا خارجية 4 ميجا مايك' or 'سويتش 8 بورت PoE'. Use it to recommend or compare "
                     "products we carry."),
     "inputSchema": {"type": "object", "properties": {"keywords": {"type": "string"}},
                     "required": ["keywords"]}},
]


def mcp_dispatch(msg):
    """Handle one JSON-RPC message; returns the response dict, or None for notifications."""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:                                   # notification (e.g. notifications/initialized)
        return None
    try:
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion", "2025-03-26"),
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "fastegy-products", "version": "4"},
                      "instructions": "FastEgy product catalog: use lookup_product for any model code."}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": MCP_TOOLS}
        elif method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            if name == "lookup_product":
                text = catalog().lookup(str(args.get("code", "")))
            elif name == "search_catalog":
                text = catalog().search(str(args.get("keywords", "")))
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool"}}
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}}
    except Exception as e:  # tool failures go back to the model as an error result
        result = {"content": [{"type": "text", "text": "catalog error: " + str(e)[:200]}], "isError": True}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


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
        if p.path.rstrip("/") == "/mcp":              # no server-initiated stream
            self.send_response(405)
            self.send_header("Allow", "POST")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if p.path.rstrip("/") != "/search":
            return self._send(404, {"ok": False})
        params = parse_qs(p.query, keep_blank_values=True)
        if ENGINES:
            params["engines"] = [ENGINES]
            params.pop("categories", None)   # otherwise SearXNG also queries every engine of the category
        req = urllib.request.Request(SEARXNG + "?" + urlencode(params, doseq=True),
                                     headers={"Accept": "application/json", "X-Real-IP": "127.0.0.1"})
        try:
            with UPSTREAM.open(req, timeout=20) as r:
                self._raw(r.status, r.read(), r.headers.get("Content-Type", "application/json"))
        except urllib.error.HTTPError as e:
            self._raw(e.code, e.read(), e.headers.get("Content-Type", "application/json"))
        except Exception as e:  # upstream down or timed out
            self._send(502, {"error": "search upstream failed: " + str(e)[:200]})

    def do_DELETE(self):
        self._raw(200, b"", "text/plain")             # session end: nothing to clean up

    def do_POST(self):
        if self.path.rstrip("/") == "/mcp":
            return self.handle_mcp()
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

    def handle_mcp(self):
        if MCP_KEY and self.headers.get("Authorization", "") != "Bearer " + MCP_KEY:
            return self._send(401, {"error": "unauthorized"})
        try:
            msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null")
        except ValueError:
            return self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
        batch = msg if isinstance(msg, list) else [msg]
        out = [r for r in (mcp_dispatch(m) for m in batch if isinstance(m, dict)) if r is not None]
        if not out:
            return self._raw(202, b"", "text/plain")   # only notifications
        self._send(200, out if isinstance(msg, list) else out[0])

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 3002), Handler).serve_forever()
