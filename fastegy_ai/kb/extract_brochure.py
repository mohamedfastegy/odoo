"""Extract every product from the Hikvision "Hot-Selling Products" distribution brochure.

Usage:  python3 extract_brochure.py brochure.pdf out.json
Needs:  pdftotext (poppler-utils)

Each record is one product block as printed: one or more model codes that share the same
key-spec bullets, plus the page heading (category), the red series label (series), the page
number and whether the block is marked NEW. Codes keep the brochure notation, e.g.
"DS-2CD26X3G2-LIZS2U/SL(RB)" with "X=4/6/8 MP" in the specs; products.py turns that notation
into patterns for lookup.
"""
import json
import re
import subprocess
import sys

PREFIX = r"(?:i?DS|IDS|AE|HWN|HWD|HWT|HWP|HF|NP|DH|HM|HS|HK)"
# codes may carry lowercase letters ("/VPro", "-xxRB"), a hardware version ("V2") or a colour
CODE_RE = re.compile(r"\b" + PREFIX + r"[A-Za-z0-9]*-[A-Za-z0-9()/.\-]+(?:\([A-Za-z0-9/ ]+\))?"
                     r"(?: V\d+)?(?: ?\((?:B|Black|White|/BLACK|/WHITE)\))?")
# words allowed next to codes on a header line (kit labels, family names)
# (single \s, not \s+: a nested quantifier backtracks forever on lines that do not match)
REST_OK = re.compile(r"^(?:\s|Series|series|Serie|White|Black|Door Station:|Indoor Station:|Distributor:|Cat\d\w* –)*$")
CHUNK_RE = re.compile(r"\S(?:.*?\S)?(?=\s{3,}|$)")   # text separated by 3+ spaces = one column
BULLET = "·"


def is_code_line(line):
    return bool(CODE_RE.search(line)) and bool(REST_OK.match(CODE_RE.sub(" ", line)))


def code_chunks(line):
    """[(column start, [codes])] for every column of a header line that holds codes."""
    out = []
    for m in CHUNK_RE.finditer(line):
        codes = CODE_RE.findall(m.group(0))
        if codes:
            out.append((m.start(), [c.strip() for c in codes]))
    return out


def heading_kind(text):
    """'category' for ALL-CAPS page headings, 'series' for the red series labels, else None."""
    t = text.strip()
    if not t or t == "NEW" or BULLET in t or re.fullmatch(r"\d{1,3}", t) or CODE_RE.search(t):
        return None
    if len(re.split(r"\s{3,}", t)) > 1:   # several columns of free text: not a heading
        return None
    letters = re.sub(r"[^A-Za-z]", "", t)
    if letters and letters.isupper() and len(letters) >= 3:
        return "category"
    if 8 <= len(t) <= 90:
        return "series"
    return None


def parse_page(lines, page_no, state):
    products = []
    i = 0
    while i < len(lines):
        line = lines[i].replace("\x01", BULLET)
        if not is_code_line(line):
            kind = heading_kind(line)
            if kind == "category":
                state["category"], state["series"] = line.strip(), None
            elif kind == "series":
                state["series"] = line.strip()
            i += 1
            continue

        # header: one or more stacked code lines; column starts come from the first one
        first = code_chunks(line)
        starts = [s for s, _ in first]
        cols = {s: {"codes": list(c), "specs": [], "labels": []} for s, c in first}

        def add_labels(text_line):
            for m in CHUNK_RE.finditer(text_line):
                nearest = min(starts, key=lambda s: abs(s - m.start()))
                cols[nearest]["labels"].append(m.group(0).strip())

        # a descriptive line right above the codes ("UPS 1/2/3 KVA, with built-in battery")
        if i > 0 and lines[i - 1].strip() and BULLET not in lines[i - 1] and "NEW" not in lines[i - 1] \
                and len(CHUNK_RE.findall(lines[i - 1])) == len(starts):
            add_labels(lines[i - 1])
        j = i + 1
        while j < len(lines):
            if is_code_line(lines[j]):
                for start, codes in code_chunks(lines[j]):
                    nearest = min(starts, key=lambda s: abs(s - start))
                    cols[nearest]["codes"].extend(codes)
            elif (lines[j].strip() and BULLET not in lines[j] and j + 1 < len(lines)
                  and is_code_line(lines[j + 1])):
                add_labels(lines[j])   # e.g. "External battery pack" between code groups
            else:
                break
            j += 1
        # bullets (and their wrapped continuation lines), split by column
        while j < len(lines):
            raw = lines[j].replace("\x01", BULLET)
            if not raw.strip():
                if j + 1 < len(lines) and BULLET in lines[j + 1]:
                    j += 1
                    continue
                break
            if is_code_line(raw) or (BULLET not in raw and heading_kind(raw)):
                break
            for k, s in enumerate(starts):
                end = starts[k + 1] - 2 if k + 1 < len(starts) else len(raw)
                seg = raw[max(0, s - 2):end].strip()
                if not seg:
                    continue
                if seg.startswith(BULLET):
                    cols[s]["specs"].append(seg.lstrip(BULLET + " ").strip())
                elif cols[s]["specs"]:
                    cols[s]["specs"][-1] += " " + seg
            j += 1
        for k, s in enumerate(starts):
            # "NEW" badge sits above its own column
            end = starts[k + 1] if k + 1 < len(starts) else 10_000
            is_new = any("NEW" in l[max(0, s - 10):end] for l in lines[max(0, i - 6):i])
            products.append({
                "codes": cols[s]["codes"],
                "labels": cols[s]["labels"],
                "specs": [re.sub(r"\s+", " ", x) for x in cols[s]["specs"]],
                "category": state["category"],
                "series": state["series"],
                "page": page_no,
                "new": is_new,
            })
        i = j
    return products


def clean(products):
    """Drop spec lines that mix two columns (a bullet inside the text: only on the tightly packed
    SMB-solution pages) and repeat listings of a code already described on an earlier page."""
    seen, out = set(), []
    for p in products:
        p["specs"] = [x for x in p["specs"] if BULLET not in x]
        p["codes"] = [c for c in p["codes"] if c not in seen]
        if not p["codes"]:
            continue
        seen.update(p["codes"])
        out.append(p)
    return out


def main(pdf, out):
    text = subprocess.run(["pdftotext", "-layout", pdf, "-"], capture_output=True, text=True, check=True).stdout
    state = {"category": None, "series": None}
    products = []
    for page_no, page in enumerate(text.split("\f"), 1):
        products.extend(parse_page(page.split("\n"), page_no, state))
    products = clean(products)
    meta = {
        "source": "Hikvision Distribution Brochure for Hot-Selling Products, 2025 H2",
        "products": len(products),
        "codes": sum(len(p["codes"]) for p in products),
    }
    json.dump({"meta": meta, "products": products}, open(out, "w"), ensure_ascii=False, indent=1)
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
