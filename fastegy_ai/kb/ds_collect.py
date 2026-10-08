"""Collect official Hikvision datasheets for the models in FastEgy's product list.

Runs on the server inside the fastegy-reader image, at night and slowly, so the search engines
the assistant also uses are not hammered. For each model (by default the ones with no description
in Odoo) it searches "<code> datasheet", reads the official PDFs found (assets.hikvision.com /
www.hikvision.com), keeps the first one whose text names this exact code (newest file first),
and stores its title, key features and specification text. products.py shows them in
lookup_product. Progress is saved after every model, so the job can stop and resume.

The search engines are the ones the assistant's web search uses, and SearXNG suspends an engine
for up to 24 hours after a CAPTCHA, "too many requests" or "access denied". So the job stops as
soon as any engine reports one of those, and the assistant keeps the engines that still answer.

Usage:
  python /app/ds_collect.py --carried /data/carried.json --out /data/ds/datasheets.json
         [--all] [--limit N] [--now] [--start 22] [--stop 7] [--sleep 45] [--day-sleep 60]
  --now        ignore the night window (for a short test with --limit)
  --day-sleep  also work outside the night window, this many seconds between searches

Version 1.1 — 2026-10-08: stops on an engine block; optional daytime pace.
"""
import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

sys.path.insert(0, "/app")

SEARCH = os.environ.get("SEARCH_URL", "http://fastegy-reader:3002/search")
OFFICIAL = re.compile(r"^https://(?:assets\.hikvision\.com|www\.hikvision\.com)/\S+\.pdf$", re.I)
CAIRO = ZoneInfo("Africa/Cairo")
SPEC_END = ("Available Model", "Physical Interface", "Accessor", "Typical Application")   # and a lone "Dimension"
BLOCK = ("captcha", "too many requests", "access denied")             # SearXNG's wording
PRIORITY = ("ip camera", "camera", "analog", "nvr", "dvr", "switch", "access", "face", "intercom",
            "monitor", "audio")


def code_regex(code):
    """The exact code, tolerating PDF spacing, not as the start of a longer code (LIU vs LIUF, -B)."""
    body = "".join(r"\s*-\s*" if ch == "-" else r"\s*/\s*" if ch == "/" else re.escape(ch)
                   for ch in code.upper())
    return re.compile(r"(?<![A-Z0-9])" + body + r"(?![A-Z0-9]|-[A-Z0-9])")


def clean(text):
    t = re.sub(r"[-]", "•", text)                 # PDF bullet glyphs
    t = re.sub(r"(\d) ,(\d{3})", r"\1,\2", t)                  # "100 ,000"
    t = re.sub(r" +,", ",", t)                                 # "IPv4 , IPv6"
    t = re.sub(r"([A-Za-z]) -([a-z])", r"\1-\2", t)            # "sub -stream", "Built -in"
    return re.sub(r"[ \t]{2,}", " ", t)


def parse(text, code):
    """{'title', 'features', 'spec'} from a datasheet's text, or None if it is not for this code."""
    t = clean(text)
    if not code_regex(code).search(t.upper()):
        return None
    lines = [line.strip() for line in t.splitlines() if line.strip()]
    start = next((i for i, line in enumerate(lines) if re.fullmatch(r"•?\s*Specifications?", line)), None)
    head = lines[:start] if start is not None else lines[:20]
    spec = lines[start + 1:] if start is not None else []
    end = next((i for i, line in enumerate(spec)
                if line.lstrip("• ") == "Dimension" or line.lstrip("• ").startswith(SPEC_END)), len(spec))
    spec = spec[:end]
    rx = code_regex(code)
    at = next((i for i, line in enumerate(head) if rx.search(line.upper())), None)
    title = head[at] if at is not None else code
    if at is not None and at + 1 < len(head):
        nxt = head[at + 1]
        if not nxt.startswith("•") and len(nxt) < 90 and not rx.search(nxt.upper()):
            title += " — " + nxt
    features = [line.lstrip("• ").strip() for line in head if line.startswith("•")][:10]
    return {"title": title, "features": features, "spec": "\n".join(spec)[:6000]}


def search(code):
    """SearXNG's JSON reply: "results", and "unresponsive_engines" as [engine, reason] pairs."""
    q = urllib.parse.urlencode({"q": f"{code} datasheet", "format": "json"})
    with urllib.request.urlopen(SEARCH + "?" + q, timeout=40) as r:
        return json.load(r)


def blocks(reply):
    """The engines that report a block ("CAPTCHA", "Suspended: too many requests", ...), not timeouts."""
    return [f"{e[0]}: {e[1]}" for e in reply.get("unresponsive_engines") or []
            if isinstance(e, list) and len(e) == 2 and any(word in str(e[1]).lower() for word in BLOCK)]


def pdf_text(url):
    import reader                                 # the assistant's own safe fetcher and PDF reader
    data, _, _ = reader.fetch(url, 30)
    if not isinstance(data, bytes):
        raise ValueError("not a PDF")
    return reader.pdf_to_text(data)[0]


def candidates(code, results):
    """Official PDF links, the ones naming the code in the file name first, newest first."""
    norm = re.sub(r"[^A-Z0-9]", "", code.upper())
    seen, out = set(), []
    for r in results:
        url = r.get("url") or ""
        if not OFFICIAL.match(url) or url in seen:
            continue
        seen.add(url)
        named = norm in re.sub(r"[^A-Z0-9]", "", url.upper())
        date = max(re.findall(r"(20\d{6})", url) or ["0"])
        out.append((named, date, url))
    named = sorted([c for c in out if c[0]], key=lambda t: t[1], reverse=True)
    other = sorted([c for c in out if not c[0]], key=lambda t: t[1], reverse=True)
    return [u for _, _, u in named + other]


def collect(code, results, max_pdfs=3):
    tried = []
    for url in candidates(code, results)[:max_pdfs]:
        tried.append(url)
        try:
            found = parse(pdf_text(url), code)
        except Exception as e:                    # a broken or blocked PDF: try the next one
            found = None
            tried[-1] += f" ({str(e)[:60]})"
        if found:
            return {"code": code, "status": "found", "url": url, **found}
    return {"code": code, "status": "not_found", "results": len(results), "tried": tried}


def priority(model):
    tags = " ".join(model.get("tags", []) + [model.get("category") or ""]).lower()
    return next((i for i, word in enumerate(PRIORITY) if word in tags), len(PRIORITY))


def in_window(start, stop):
    hour = datetime.datetime.now(CAIRO).hour
    return (start <= hour or hour < stop) if start > stop else (start <= hour < stop)


def save(path, records):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"meta": {"source": "Official Hikvision datasheets, read automatically",
                            "updated": datetime.datetime.now(CAIRO).isoformat(timespec="seconds")},
                   "datasheets": records}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--carried", default="/data/carried.json")
    ap.add_argument("--out", default="/data/ds/datasheets.json")
    ap.add_argument("--all", action="store_true", help="also models that have a description")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--now", action="store_true", help="ignore the night window")
    ap.add_argument("--start", type=int, default=22, help="Cairo hour the window opens")
    ap.add_argument("--stop", type=int, default=7, help="Cairo hour the window closes")
    ap.add_argument("--sleep", type=float, default=45, help="seconds between searches")
    ap.add_argument("--day-sleep", type=float, default=0,
                    help="also work outside the window, this many seconds between searches (0: wait)")
    a = ap.parse_args()

    models = json.load(open(a.carried, encoding="utf-8"))["models"]
    records = {}
    if os.path.exists(a.out):
        records = {r["code"]: r for r in json.load(open(a.out, encoding="utf-8"))["datasheets"]}
    todo = [m["code"] for m in sorted(models, key=priority)
            if (a.all or not m.get("ar")) and records.get(m["code"], {}).get("status") not in ("found", "not_found")]
    if a.limit:
        todo = todo[:a.limit]
    print(f"{len(todo)} models to check; {sum(r['status'] == 'found' for r in records.values())} datasheets already kept",
          flush=True)
    errors, blocked = 0, []
    for n, code in enumerate(todo, 1):
        while not a.now and not a.day_sleep and not in_window(a.start, a.stop):
            print(f"outside {a.start}:00-{a.stop}:00 Cairo; waiting", flush=True)
            time.sleep(600)
        night, blocked = a.now or in_window(a.start, a.stop), []
        try:
            reply = search(code)
            blocked = blocks(reply)
            rec = collect(code, reply.get("results", []))
            errors = 0
        except Exception as e:                    # search down or blocked: keep going, slower
            rec = {"code": code, "status": "error", "error": str(e)[:200]}
            errors += 1
        if not blocked or rec["status"] == "found":   # a miss while an engine is blocked is checked again later
            rec["checked"] = datetime.datetime.now(CAIRO).isoformat(timespec="seconds")
            records[code] = rec
            save(a.out, list(records.values()))
        print(f"[{n}/{len(todo)}] {code}: {rec['status']} {rec.get('url', '')}", flush=True)
        if blocked:
            print(f"STOPPED: {'; '.join(blocked)}. Stopped so the assistant's web search keeps its other engines;"
                  " start the job again later and it resumes here.", flush=True)
            break
        if errors >= 5:
            print("5 search errors in a row; pausing 20 minutes", flush=True)
            time.sleep(1200)
            errors = 0
        if n < len(todo):
            time.sleep(a.sleep if night else a.day_sleep)
    found = sum(r["status"] == "found" for r in records.values())
    print(f"{'stopped' if blocked else 'done'}: {found} datasheets kept of {len(records)} models checked", flush=True)


if __name__ == "__main__":
    main()
