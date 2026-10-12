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
    Sources: the Hikvision brochure (CATALOG_PATH), FastEgy's own product list from Odoo
    (CARRIED_PATH, built by kb/odoo_products.py) and the official datasheets collected at night
    (DATASHEETS_PATH, kb/ds_collect.py). Each reloads when its file is replaced.

/brain/... (only when BRAIN_KEY is set; Caddy publishes this path, and only this path, on
    https://ai.fastegy.net/brain/) — the link for FastEgy's Odoo modules, so they always use the
    assistant exactly as it is configured here:
    GET  /brain/health                 no key: {"ok": true, "brain": true|false}
    GET  /brain/manifest               the rules (promptPrefix) and model of each FastEgy model spec in
                                       librechat.yaml (plus data/brain/specs.json if present), the
                                       catalog tools, and a version that changes whenever one of them,
                                       the product list or the datasheets change. Sends an ETag and
                                       answers 304 to If-None-Match when nothing changed.
    POST /brain/v1/chat/completions    OpenAI-style chat, passed to LiteLLM with this server's key.
                                       Only the spec models; no streaming; max_tokens capped; LiteLLM's
                                       fallback model is skipped (BRAIN_NO_FALLBACK=1, the default) so
                                       Odoo data never reaches the free fallback tier.
    POST /brain/tool                   {"name": "lookup_product"|"search_catalog", "arguments": {...}}
    Every /brain answer carries X-Brain-Version. Requests need "Authorization: Bearer <BRAIN_KEY>";
    BRAIN_RATE requests a minute at most. librechat.yaml is read again whenever it changes.

Version 6 — 2026-10-09 (MCP serverInfo still says "4": the kb_2 update test checks it)
"""
import datetime
import hashlib
import hmac
import ipaddress
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import io

import trafilatura
from pypdf import PdfReader

import products

try:
    import yaml
except ImportError:              # the brain stays off without PyYAML
    yaml = None

KEY = os.environ.get("READER_KEY", "")
MAX_CHARS = int(os.environ.get("READER_MAX_CHARS", "20000"))
SEARXNG = os.environ.get("SEARXNG_UPSTREAM", "http://searxng:8080/search")
ENGINES = os.environ.get("SEARCH_ENGINES", "yahoo,startpage,yandex")
CATALOG_PATH = os.environ.get("CATALOG_PATH", "/app/catalog.json")
CARRIED_PATH = os.environ.get("CARRIED_PATH", "/app/carried.json")
DATASHEETS_PATH = os.environ.get("DATASHEETS_PATH", "/app/data/ds/datasheets.json")
MCP_KEY = os.environ.get("MCP_KEY", "")
BRAIN_KEY = os.environ.get("BRAIN_KEY", "")
LC_CONFIG = os.environ.get("LC_CONFIG", "/app/librechat.yaml")
BRAIN_SPECS_FILE = os.environ.get("BRAIN_SPECS_FILE", "/app/data/brain/specs.json")
BRAIN_SPECS = [s.strip() for s in os.environ.get("BRAIN_SPECS", "fastegy-strong,fastegy-fast").split(",") if s.strip()]
BRAIN_ENDPOINT = os.environ.get("BRAIN_ENDPOINT", "FastEgy AI")   # the librechat.yaml endpoint that holds the LiteLLM URL
LLM_BASE = os.environ.get("LLM_BASE", "")                          # empty: that endpoint's baseURL
LLM_KEY = os.environ.get("LLM_KEY", "")
BRAIN_NO_FALLBACK = os.environ.get("BRAIN_NO_FALLBACK", "1") == "1"
BRAIN_MAX_TOKENS = int(os.environ.get("BRAIN_MAX_TOKENS", "4000"))
BRAIN_RATE = int(os.environ.get("BRAIN_RATE", "60"))              # requests a minute, all callers together
BRAIN_TIMEOUT = int(os.environ.get("BRAIN_TIMEOUT", "120"))
BRAIN_MAX_BODY = 2_000_000
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
    paths = (CATALOG_PATH, CARRIED_PATH, DATASHEETS_PATH)
    mtime = tuple(os.path.getmtime(p) if os.path.exists(p) else None for p in paths)
    if _catalog["mtime"] != mtime:
        _catalog["obj"], _catalog["mtime"] = products.Catalog(*paths), mtime
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


# ---------------------------------------------------------------- brain: the link for Odoo
_brain = {"key": None, "manifest": None, "llm": None}
_brain_lock = threading.Lock()
_rate = {"window": [], "lock": threading.Lock()}
CHAT_FIELDS = ("model", "messages", "tools", "tool_choice", "temperature", "max_tokens", "top_p",
               "stop", "seed", "response_format", "parallel_tool_calls")


def _stamp(path):
    try:
        st = os.stat(path)
        return st.st_mtime_ns, st.st_size
    except OSError:
        return None


def _iso(ns):
    return datetime.datetime.fromtimestamp(ns / 1e9, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_manifest():
    """(manifest, llm) from librechat.yaml, data/brain/specs.json and the catalog files."""
    if yaml is None:
        raise RuntimeError("PyYAML is missing")
    with open(LC_CONFIG, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    eps = [e for e in ((cfg.get("endpoints") or {}).get("custom") or []) if e.get("name") == BRAIN_ENDPOINT]
    base = (LLM_BASE or (eps[0].get("baseURL") if eps else "") or "").rstrip("/")
    specs = {}
    for s in ((cfg.get("modelSpecs") or {}).get("list") or []):
        if s.get("name") not in BRAIN_SPECS:
            continue
        p = s.get("preset") or {}
        specs[s["name"]] = {"label": s.get("label") or s["name"], "model": p.get("model"),
                            "temperature": p.get("temperature"), "rules": p.get("promptPrefix") or "",
                            "web_search": bool(s.get("webSearch")), "source": "librechat"}
    if os.path.exists(BRAIN_SPECS_FILE):        # extra specs kept on this server (e.g. a customer persona)
        with open(BRAIN_SPECS_FILE, encoding="utf-8") as f:
            for name, s in (json.load(f) or {}).items():
                if name not in specs and isinstance(s, dict) and s.get("model"):
                    specs[name] = {"label": s.get("label") or name, "model": s["model"],
                                   "temperature": s.get("temperature"), "rules": s.get("rules") or "",
                                   "web_search": False, "source": "brain"}
    if not specs:
        raise RuntimeError("no FastEgy model spec found in " + LC_CONFIG)
    tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                               "parameters": t["inputSchema"]}} for t in MCP_TOOLS]
    stamps = {"product_list": _stamp(CARRIED_PATH), "datasheets": _stamp(DATASHEETS_PATH),
              "brochure": _stamp(CATALOG_PATH)}
    body = {"specs": specs, "default_spec": next((n for n in BRAIN_SPECS if n in specs), next(iter(specs))),
            "tools": tools, "models": sorted({s["model"] for s in specs.values() if s.get("model")}),
            "catalog": {k: (_iso(v[0]) if v else None) for k, v in stamps.items()}, "reader_version": "6"}
    version = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
    changed = [x[0] for x in [_stamp(LC_CONFIG), _stamp(BRAIN_SPECS_FILE)] + list(stamps.values()) if x]
    manifest = dict(body, version=version, updated_at=_iso(max(changed)))
    return manifest, {"base": base, "models": set(body["models"])}


def brain_state():
    """The manifest, rebuilt when librechat.yaml, specs.json or a catalog file changes. A file
    caught half-written keeps the previous manifest until the next request."""
    key = tuple(_stamp(p) for p in (LC_CONFIG, BRAIN_SPECS_FILE, CATALOG_PATH, CARRIED_PATH, DATASHEETS_PATH))
    with _brain_lock:
        if _brain["key"] != key:
            try:
                _brain["manifest"], _brain["llm"] = build_manifest()
                _brain["key"] = key
            except Exception:
                if _brain["manifest"] is None:
                    raise
        return _brain["manifest"], _brain["llm"]


def brain_allowed(headers):
    got = headers.get("Authorization", "")
    return bool(BRAIN_KEY) and hmac.compare_digest(got.encode(), ("Bearer " + BRAIN_KEY).encode())


def brain_rate_ok():
    now = time.monotonic()
    with _rate["lock"]:
        _rate["window"] = [t for t in _rate["window"] if now - t < 60]
        if len(_rate["window"]) >= BRAIN_RATE:
            return False
        _rate["window"].append(now)
        return True


def brain_chat(body):
    """Pass one chat request to LiteLLM: (status, json bytes, fallbacks header, model)."""
    manifest, llm = brain_state()
    model = body.get("model") or manifest["specs"][manifest["default_spec"]]["model"]
    if model not in llm["models"]:
        return 400, json.dumps({"error": {"message": "model not allowed: %s" % model}}).encode(), None, model
    if not llm["base"] or not LLM_KEY:
        return 503, json.dumps({"error": {"message": "the model server is not configured"}}).encode(), None, model
    out = {k: body[k] for k in CHAT_FIELDS if k in body}
    out["model"], out["stream"] = model, False
    try:
        out["max_tokens"] = max(1, min(int(out.get("max_tokens") or 2000), BRAIN_MAX_TOKENS))
    except (TypeError, ValueError):
        out["max_tokens"] = 2000
    if BRAIN_NO_FALLBACK:
        out["disable_fallbacks"] = True
    req = urllib.request.Request(llm["base"] + "/chat/completions", data=json.dumps(out).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + LLM_KEY, "Content-Type": "application/json"})
    try:
        with UPSTREAM.open(req, timeout=BRAIN_TIMEOUT) as r:
            return r.status, r.read(), r.headers.get("x-litellm-attempted-fallbacks"), model
    except urllib.error.HTTPError as e:
        return e.code, e.read(), None, model
    except Exception as e:  # model server down or too slow
        return 502, json.dumps({"error": {"message": "model server: " + str(e)[:200]}}).encode(), None, model


def brain_tool(body):
    name, args = body.get("name"), body.get("arguments") or {}
    if name == "lookup_product":
        return catalog().lookup(str(args.get("code", "")))
    if name == "search_catalog":
        return catalog().search(str(args.get("keywords", "")))
    return None


def brain_log(path, status, started, model=""):
    print(json.dumps({"brain": path, "status": status, "ms": int((time.monotonic() - started) * 1000),
                      "model": model}), flush=True)


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
        if p.path.startswith("/brain/"):
            return self.handle_brain_get(p.path.rstrip("/"))
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
        if self.path.startswith("/brain/"):
            return self.handle_brain_post(urlparse(self.path).path.rstrip("/"))
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

    def _brain_send(self, code, body, version="", extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if version:
            self.send_header("X-Brain-Version", version)
            self.send_header("ETag", '"%s"' % version)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def handle_brain_get(self, path):
        started = time.monotonic()
        if path == "/brain/health":
            return self._brain_send(200, {"ok": True, "brain": bool(BRAIN_KEY) and yaml is not None})
        if not brain_allowed(self.headers):
            brain_log(path, 401, started)
            return self._brain_send(401, {"ok": False, "error": "unauthorized"})
        if path != "/brain/manifest":
            return self._brain_send(404, {"ok": False, "error": "not found"})
        try:
            manifest, _ = brain_state()
        except Exception as e:
            brain_log(path, 503, started)
            return self._brain_send(503, {"ok": False, "error": "manifest: " + str(e)[:200]})
        version = manifest["version"]
        if self.headers.get("If-None-Match", "").strip('"') == version:
            self.send_response(304)
            self.send_header("ETag", '"%s"' % version)
            self.send_header("X-Brain-Version", version)
            self.end_headers()
            return brain_log(path, 304, started)
        brain_log(path, 200, started)
        return self._brain_send(200, manifest, version)

    def handle_brain_post(self, path):
        started = time.monotonic()
        if not brain_allowed(self.headers):
            brain_log(path, 401, started)
            return self._brain_send(401, {"ok": False, "error": "unauthorized"})
        if path not in ("/brain/v1/chat/completions", "/brain/tool"):
            return self._brain_send(404, {"ok": False, "error": "not found"})
        if not brain_rate_ok():
            brain_log(path, 429, started)
            return self._brain_send(429, {"ok": False, "error": "too many requests"}, extra={"Retry-After": "10"})
        size = int(self.headers.get("Content-Length") or 0)
        if size > BRAIN_MAX_BODY:
            return self._brain_send(413, {"ok": False, "error": "request too large"})
        try:
            body = json.loads(self.rfile.read(size) or b"{}")
            assert isinstance(body, dict)
            manifest, _ = brain_state()
        except Exception as e:
            brain_log(path, 400, started)
            return self._brain_send(400, {"ok": False, "error": "bad request: " + str(e)[:120]})
        version = manifest["version"]
        if path == "/brain/tool":
            try:
                text = brain_tool(body)
            except Exception as e:  # the model gets the failure as the tool result
                text = "catalog error: " + str(e)[:200]
            if text is None:
                brain_log(path, 404, started)
                return self._brain_send(404, {"ok": False, "error": "unknown tool"}, version)
            brain_log(path, 200, started, body.get("name", ""))
            return self._brain_send(200, {"ok": True, "text": text}, version)
        code, data, fallbacks, model = brain_chat(body)
        brain_log(path, code, started, model)
        return self._brain_send(code, data, version,
                                {"X-Brain-Fallbacks": fallbacks} if fallbacks else None)

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
