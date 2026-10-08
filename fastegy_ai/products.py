"""FastEgy product catalog lookup (used by fastegy-reader's MCP tools).

The catalog JSON comes from kb/extract_brochure.py. Codes keep Hikvision's brochure notation,
so each printed code is turned into a pattern that also matches the concrete codes it stands for:
    X followed by a digit  -> the resolution digits listed in the specs ("X=4/6/8 MP")
    2(4)                   -> 2 or 4                  (alternative digit)
    (/SL) (RB) (C) (/8P)   -> optional parts
    -El                    -> -EI                     (brochure misprint)
Lookup never guesses: it reports an exact match, or the closest catalog codes as candidates.
"""
import difflib
import json
import re

SPACES = re.compile(r"\s+")
LENS = re.compile(r"\(?\d+(?:\.\d+)?\s*MM\)?")      # "(2.8mm)" ordering suffix in queries


def norm(code):
    """Upper case, no spaces, plain dashes."""
    code = code.replace("–", "-").replace("—", "-")
    code = re.sub(r"(?<=E)l\b", "I", code)          # brochure misprint "-El" for "-EI"
    return SPACES.sub("", code).upper().strip(".,;:")


def norm_query(code):
    return LENS.sub("", norm(code))


def x_digits(specs):
    for s in specs:
        m = re.search(r"\bX\s*=\s*([\d/]+)", s)
        if m:
            return [d for d in m.group(1).split("/") if d]
    return None


def code_pattern(code, specs):
    """Regex for a printed brochure code (already normalized)."""
    digits = x_digits(specs)
    out, i = [], 0
    while i < len(code):
        ch = code[i]
        if ch == "(":
            j = code.find(")", i)
            if j == -1:
                out.append(re.escape(code[i:]))
                break
            inner = code[i + 1:j]
            if out and out[-1].isdigit() and inner.isdigit():       # 2(4) -> 2 or 4
                prev = out.pop()
                out.append("(?:%s|%s)" % (prev, inner))
            else:                                                    # (/SL), (RB), (C) -> optional
                out.append("(?:%s)?" % re.escape(inner))
            i = j + 1
            continue
        if ch == "X" and i + 1 < len(code) and code[i + 1].isdigit() and i > 0 and code[i - 1].isalnum():
            out.append("[%s]" % ("".join(digits) if digits else "0-9"))
            i += 1
            continue
        out.append(re.escape(ch) if not ch.isalnum() else ch)
        i += 1
    pattern = "".join(out)
    pattern = re.sub(r"(V\d+)$", r"(?:\1)?", pattern)                # hardware version optional
    return re.compile(pattern + r"$")


def literal(code):
    """Code with optional parts dropped, for fuzzy comparison."""
    return re.sub(r"\([^)]*\)", "", code)


VARIANT_TAIL = re.compile(r"(?:/[A-Z0-9]+|\([^)]*\))$")


def variant_bases(code):
    """(base, suffix) for the code with its trailing variant parts removed one at a time:
    DS-2CD2043G2-LIZ2UY/SL(RB) -> (…/SL, (RB)), (DS-2CD2043G2-LIZ2UY, /SL(RB))."""
    base, tail = code, ""
    while True:
        m = VARIANT_TAIL.search(base)
        if not m or m.start() == 0:
            return
        base, tail = base[:m.start()], m.group(0) + tail
        yield base, tail


class Catalog:
    def __init__(self, path):
        data = json.load(open(path, encoding="utf-8"))
        self.source = data.get("meta", {}).get("source", "catalog")
        self.products = data["products"]
        self.index = []      # (normalized printed code, pattern, product)
        for p in self.products:
            names = list(p["codes"])
            for label in p.get("labels", []):                       # kit codes printed as labels
                names += re.findall(r"\b(?:DS-)?K[A-Z]{1,3}\d[\w()/-]*", label)
            for c in names:
                n = norm(c if c.startswith(("DS", "IDS", "AE", "HF", "HW")) else "DS-" + c)
                self.index.append((n, code_pattern(n, p["specs"]), p))

    # ------------------------------------------------------------- formatting
    def describe(self, p, matched=None):
        lines = []
        head = ", ".join(p["codes"])
        lines.append(f"Catalog entry: {head}" + ("  [NEW]" if p.get("new") else ""))
        if matched:
            lines.append(f"Matched your code against: {matched}")
        where = " / ".join(x for x in (p.get("category"), p.get("series")) if x)
        if where:
            lines.append(f"Category: {where}")
        if p.get("labels"):
            lines.append("Notes on this entry: " + "; ".join(p["labels"]))
        if p["specs"]:
            lines.append("Key specs (as printed in the brochure):")
            lines += [f"  - {s}" for s in p["specs"]]
        else:
            lines.append("Key specs: none printed in the brochure for this entry.")
        lines.append(f"Source: {self.source}, page {p['page']}.")
        return "\n".join(lines)

    # ------------------------------------------------------------- lookups
    def lookup(self, query):
        q = norm_query(query)
        if not q:
            return "Give a model code, e.g. DS-2CD2043G2-LIZ2UY."
        hits, seen = [], set()
        for n, pat, p in self.index:
            if pat.match(q) and id(p) not in seen:
                hits.append((n, p))
                seen.add(id(p))
        notation = ("Brochure notation: X = resolution digit listed in the specs; "
                    "parts in brackets like (/SL) or (RB) are optional variants; "
                    "'/SL' = strobe light & audio alarm variant.")
        if hits:
            body = "\n\n".join(self.describe(p, matched=n) for n, p in hits)
            return (f"EXACT MATCH for {query}:\n\n{body}\n\n{notation}\n"
                    "These are key specs only; for the full datasheet values, say so and do not invent them.")
        # the catalog may list only a suffixed variant of the code (…/SL, …/8P): say so explicitly
        variants, seen = [], set()
        for n, _, p in self.index:
            for base, tail in variant_bases(n):
                if id(p) not in seen and code_pattern(base, p["specs"]).match(q):
                    variants.append((n, tail))
                    seen.add(id(p))
                    break
        if variants:
            return (f"NO EXACT MATCH for {query}. The catalog lists only a variant of this code "
                    "with an extra suffix:\n"
                    + "\n".join(f"  - {n}   (your code + \"{t}\")" for n, t in variants[:6])
                    + "\nA suffix marks a different variant ('/SL' = strobe light & audio alarm). "
                      "Tell the user the catalog carries that variant; you may look it up and give its "
                      "specs, clearly labelled as the variant's specs, never as the plain code's.")
        # family / prefix matches, then fuzzy
        family = [(n, p) for n, _, p in self.index if literal(n).startswith(q) or q.startswith(literal(n))]
        scored = sorted(((difflib.SequenceMatcher(None, q, literal(n)).ratio(), n, p) for n, _, p in self.index),
                        key=lambda t: -t[0])
        cands, seen = [], set()
        for n, p in family:
            if id(p) not in seen:
                cands.append(n); seen.add(id(p))
        for r, n, p in scored:
            if r >= 0.72 and id(p) not in seen and len(cands) < 6:
                cands.append(n); seen.add(id(p))
        if not cands:
            return (f"NOT FOUND: {query} is not in the {self.source}. "
                    "Do not guess its specs; say it is not in the catalog.")
        return (f"NO EXACT MATCH for {query}. Closest codes in the catalog:\n"
                + "\n".join(f"  - {c}" for c in cands[:6])
                + "\nAsk the user which one they mean, or look up one of these codes. "
                  "Do not give specs for a code that did not match exactly.")

    def search(self, text, limit=10):
        """Keyword search over codes, series, category and specs (e.g. 'ColorVu 8 MP bullet')."""
        terms = [t for t in re.split(r"[\s,]+", text.lower()) if t]
        if not terms:
            return "Give some keywords, e.g. 'ColorVu 4 MP turret' or '16-ch NVR PoE'."
        ranked = []
        for p in self.products:
            blob = " ".join([*p["codes"], *(p.get("labels") or []), p.get("category") or "",
                             p.get("series") or "", *p["specs"]]).lower()
            score = sum(1 for t in terms if t in blob)
            if score:
                ranked.append((score, p))
        ranked.sort(key=lambda t: (-t[0], t[1]["page"]))
        if not ranked:
            return f"No catalog entries match: {text}"
        best = ranked[0][0]
        rows = [p for s, p in ranked if s == best][:limit]
        out = [f"{len(rows)} catalog entries matching '{text}' ({best}/{len(terms)} keywords):"]
        for p in rows:
            out.append(f"- {', '.join(p['codes'])} | {p.get('series') or p.get('category')} | "
                       + "; ".join(p["specs"][:4]) + f" (page {p['page']})")
        out.append("Use lookup_product on a code for its full entry.")
        return "\n".join(out)
