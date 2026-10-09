"""Re-read datasheets that were stored badly: table-style ones, and ones cut at 6000 characters.

For some Hikvision datasheets (switches especially) pypdf's default text extraction puts every
table cell on its own line ("Switching Capacity" / "40 Gbps") and splits words ("Metal m" /
"aterial"), so the assistant could pair a label with the wrong value. Layout extraction keeps a
row together; the gaps between cells become " | ", so tables that list several models side by
side keep their columns.

Records stored before collector v1.4 were cut at 6000 characters, which lost the end of long
datasheets (power, size). Those are read again the normal way and stored up to 12000.

A record is replaced only when the re-read is clearly better and still names the exact code.

Usage (inside the fastegy-reader image, data mounted at /data):
  python /app/ds_relayout.py --probe CODE [CODE ...]    read-only: old vs new for these codes
  python /app/ds_relayout.py --list                     read-only: the records that would be re-read
  python /app/ds_relayout.py --apply                    re-read them and keep the better ones
Version 1.0 — 2026-10-09
"""
import argparse
import datetime
import io
import json
import re
import sys

sys.path.insert(0, "/app")

import ds_collect                                # parse(), save(), CAIRO
import reader                                    # the assistant's own safe fetcher

OUT = "/data/ds/datasheets.json"
MIN_SHORT = 0.15          # table-style: at least this share of spec lines is 3 characters or fewer
CUT = 5900                # cut: stored spec this long or longer (the old cap was 6000)
GAP = re.compile(r" {2,}")


def short_share(spec):
    lines = [line.strip() for line in spec.split("\n") if line.strip()]
    return sum(len(line) <= 3 for line in lines) / len(lines) if lines else 0.0


def layout_text(url):
    """The PDF's text with each visual row on one line and cells separated by ' | '."""
    data, _, _ = reader.fetch(url, 30)
    if not isinstance(data, bytes):
        raise ValueError("not a PDF")
    pdf = reader.PdfReader(io.BytesIO(data))
    rows = []
    for page in pdf.pages[:reader.MAX_PDF_PAGES]:
        for line in (page.extract_text(extraction_mode="layout") or "").splitlines():
            line = ds_collect.clean(GAP.sub(" | ", line.strip()))     # cells first, then bullet glyphs etc.
            if not line:
                continue
            if " | •" in line or " | " in line and line.startswith("•"):   # two columns of bullets
                rows.extend(part.strip() for part in line.split(" | ") if part.strip())
            else:
                rows.append(line)
    return "\n".join(rows)


def kind(rec, min_short=MIN_SHORT):
    spec = rec.get("spec", "")
    return "table" if short_share(spec) >= min_short else "cut" if len(spec) >= CUT else None


def reread(rec, how):
    """(re-read record or None, keep it?, why). how: "table" (layout read) or "cut" (normal read, longer)."""
    text = layout_text(rec["url"]) if how == "table" else ds_collect.pdf_text(rec["url"])
    found = ds_collect.parse(text, rec["code"])
    if not found:
        return None, False, "the re-read does not name the exact code"
    found["title"] = found["title"].replace(" | ", " — ")
    new_rec = {**rec, **found, "checked": datetime.datetime.now(ds_collect.CAIRO).isoformat(timespec="seconds")}
    if how == "table":
        new_rec["layout"] = True
    old_spec, new_spec = rec.get("spec", ""), found["spec"]
    if not new_spec.strip():
        return new_rec, False, "the re-read has no Specification section"
    if how == "table":
        old, new = short_share(old_spec), short_share(new_spec)
        if new > old - 0.10:
            return new_rec, False, f"not clearly better (short lines {old:.2f} -> {new:.2f})"
        return new_rec, True, f"table rows kept together (short lines {old:.2f} -> {new:.2f})"
    if len(new_spec) <= len(old_spec) or new_spec.split("\n")[0] != old_spec.split("\n")[0]:
        return new_rec, False, f"not longer or starts differently ({len(old_spec)} -> {len(new_spec)} chars)"
    return new_rec, True, f"no longer cut ({len(old_spec)} -> {len(new_spec)} chars)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--min-short", type=float, default=MIN_SHORT)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--probe", nargs="+", metavar="CODE")
    group.add_argument("--list", action="store_true")
    group.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    records = json.load(open(a.out, encoding="utf-8"))["datasheets"]
    by_code = {r["code"]: r for r in records}
    if a.probe:
        for code in a.probe:
            rec = by_code.get(code)
            if not rec or rec.get("status") != "found":
                print(f"== {code}: no found record")
                continue
            how = kind(rec, a.min_short) or "table"          # no problem found: show the layout read anyway
            new, keep, why = reread(rec, how)
            print(f"== {code} [{kind(rec, a.min_short) or 'looks fine'}]: {'would re-read' if keep else 'would keep'} "
                  f"({why})\nurl: {rec['url']}\nold title: {rec.get('title')}")
            if new:
                lines = new["spec"].split("\n")
                print(f"new title: {new['title']}\nnew features: {new['features'][:6]}\n"
                      f"new spec ({len(new['spec'])} chars, {len(lines)} lines), first 60 and last 12 lines:")
                print("\n".join(lines[:60]))
                if len(lines) > 60:
                    print("   ...\n" + "\n".join(lines[-12:]))
            print(flush=True)
        return
    todo = [r for r in records if r.get("status") == "found" and kind(r, a.min_short)]
    tables = sum(kind(r, a.min_short) == "table" for r in todo)
    print(f"{len(todo)} records to re-read: {tables} table-style, {len(todo) - tables} cut at 6000 characters",
          flush=True)
    if a.list:
        for r in todo:
            print(f"  {r['code']} | {kind(r, a.min_short)} | short {short_share(r.get('spec', '')):.2f} | "
                  f"{len(r.get('spec', ''))} chars")
        return
    changed = 0
    for n, rec in enumerate(todo, 1):
        try:
            new, keep, why = reread(rec, kind(rec, a.min_short))
        except Exception as e:                    # a broken or blocked PDF: keep the stored record
            new, keep, why = None, False, f"error: {str(e)[:80]}"
        if keep:
            by_code[rec["code"]] = new
            changed += 1
        print(f"[{n}/{len(todo)}] {rec['code']}: {'re-read' if keep else 'kept'} ({why})", flush=True)
    if changed:
        ds_collect.save(a.out, [by_code[r["code"]] for r in records])
    print(f"done: {changed} of {len(todo)} records re-read", flush=True)


if __name__ == "__main__":
    main()
