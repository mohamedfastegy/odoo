"""Turn FastEgy's Odoo product export (Excel) into the catalog of models FastEgy carries.

Usage:  python3 odoo_products.py export.xlsx out.json
Needs:  openpyxl

The export has one product per "Name" row, with Tags and Product Attributes continued on
the rows below it. Products are grouped by model code: lens options ("(2.8mm)") and stock
variants ("(without box)", "(2)", "showroom_") fold into one record. Prices are left out on
purpose: they change, and a stale price in the assistant's catalog is worse than none.
Rows without a model code (merchandise, services) are skipped. The website descriptions are
left out too: they are generated text and some values are wrong (DS-7608NXI-K1 "4 Channels").
"""
import datetime
import json
import re
import sys

import openpyxl

CODE = re.compile(r"(?<![\w-])((?:i?DS|IDS|AE|HM|DP|HC|HS|HK|NP|HWN|HWD|HWT|HF)-[A-Za-z0-9/.+()-]*[A-Za-z0-9)])")
LENS = re.compile(r"\(?\b(\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?)\s*mm\)?", re.I)
STOCK = re.compile(r"^\s*(?:\(2\))?\s*(?:\(without box\))?\s*(?:showroom_)?\s*", re.I)


def code_of(name):
    m = CODE.search(name)
    if not m:
        return None
    code = LENS.sub("", m.group(1)).strip(" -/.")
    return code.replace("()", "")


def main(xlsx, out):
    ws = openpyxl.load_workbook(xlsx, read_only=True).worksheets[0]
    rows = ws.iter_rows(values_only=True)
    head = [str(c or "").strip() for c in next(rows)]
    missing = [h for h in ("Name", "Tags") if h not in head]
    if missing:
        sys.exit("Export needs the columns Name and Tags (Odoo in English); missing: " + ", ".join(missing))
    idx = {name: head.index(name) for name in head}
    col = lambda r, name: r[idx[name]] if name in idx else None   # noqa: E731  optional columns
    products, cur = [], None
    for r in rows:
        if col(r, "Name") is not None:
            cur = {"name": str(col(r, "Name")).strip(), "category": col(r, "Category"),
                   "ar": col(r, "وصف الصنف"), "tags": []}
            products.append(cur)
        if cur is not None and col(r, "Tags"):
            cur["tags"].append(str(col(r, "Tags")).strip())

    models, skipped = {}, 0
    for p in products:
        code = code_of(p["name"])
        if not code:
            skipped += 1
            continue
        m = models.setdefault(code.upper(), {"code": code, "names": [], "lens": [], "category": None,
                                             "tags": [], "ar": None})
        stock_variant = bool(STOCK.match(p["name"]).group(0).strip())
        if not stock_variant:
            m["names"].append(p["name"])
        for lens in LENS.findall(p["name"]):
            lens = re.sub(r"\s+", "", lens) + "mm"
            if lens not in m["lens"]:
                m["lens"].append(lens)
        m["category"] = m["category"] or p["category"]
        m["tags"] += [t for t in p["tags"] if t not in m["tags"] and not re.fullmatch(r"Line \d", t)]
        if p["ar"] and not m["ar"]:
            m["ar"] = re.sub(r"\s*\n\s*", " | ", str(p["ar"]).strip())
    for m in models.values():
        m["names"] = m["names"] or [m["code"]]       # only stock variants (open box) in Odoo
    recs = sorted(models.values(), key=lambda m: m["code"])
    meta = {"source": f"FastEgy product list (Odoo export, {datetime.date.today()})", "models": len(recs),
            "odoo_products": len(products), "skipped_without_code": skipped}
    json.dump({"meta": meta, "models": recs}, open(out, "w"), ensure_ascii=False, indent=1)
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
