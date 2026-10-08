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
import os
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


RES = re.compile(r"(?:\bcamera\s*)?(\d+)\s*(?:mp\b|megapixels?\b|ميجا\S*)|\bcamera\s*(\d+)m\b")
AR = str.maketrans("ةأإآى", "هاااي")


# Arabic and English words for the same thing (the Odoo tags are English, the descriptions Arabic)
SYNONYMS = {
    "ch": ("ch", "channel", "channels", "قناه", "قنوات", "port", "ports", "بورت"),
    "camera": ("camera", "cameras", "كاميرا", "كاميره", "كاميرات"),
    "outdoor": ("outdoor", "external", "خارجيه", "خارجي"),
    "indoor": ("indoor", "internal", "داخليه", "داخلي"),
    "mic": ("mic", "microphone", "مايك", "ميكروفون", "ميك"),
    "speaker": ("speaker", "سبيكر"),
    "switch": ("switch", "switches", "سويتش"),
    "hybrid": ("hybrid", "hyprid", "هايبرد"),
    "colorvu": ("colorvu",),
}
SYN = {w: k for k, words in SYNONYMS.items() for w in words}
STOPWORDS = {"ds", "ids", "hikvision", "هيكفيجن", "هيك", "فيجن", "رشحلي", "عايز", "عاوز", "عندنا", "ايه"}


def search_tokens(text):
    """Lower-case word tokens; '4 MP', '4MP', '4 ميجا', 'Camera 4M' all become '4mp', and the
    Arabic / English words above become one word ('مايك' and 'Mic' -> 'mic')."""
    t = RES.sub(lambda m: f" {m.group(1) or m.group(2)}mp ", text.lower()).translate(AR)
    t = re.sub(r"(?:كلر|كولر)\s*فيو?|color\s*vu", " colorvu ", t)
    return [synonym(w) for w in re.findall(r"[^\W_]+", t)]


def synonym(word):
    """'بمايك', 'والمايك' -> 'mic': Arabic prefixes are dropped when the rest is a known word."""
    if word in SYN:
        return SYN[word]
    for prefix in ("بال", "وال", "لل", "ال", "ب", "و"):
        if word.startswith(prefix) and word[len(prefix):] in SYN:
            return SYN[word[len(prefix):]]
    return word


def looks_like_code(text):
    """True for model codes (DS-2CD1043G2-LIU, iDS-7208HUHI-M2/S); False for keywords."""
    if re.search(r"[\u0600-\u06FF]", text):
        return False
    q = norm_query(text)
    return bool(re.search(r"[A-Z0-9]-[A-Z0-9]", q) and re.search(r"\d", q) and len(q) >= 6)


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
    def __init__(self, path, carried_path=None):
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
        # FastEgy's own product list (kb/odoo_products.py), optional
        self.carried, self.carried_index, self.carried_source = None, {}, None
        if carried_path and os.path.exists(carried_path):
            data = json.load(open(carried_path, encoding="utf-8"))
            self.carried_source = data.get("meta", {}).get("source", "FastEgy product list")
            self.carried = data["models"]
            self.carried_index = {norm(m["code"]): m for m in self.carried}

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
        if not looks_like_code(query):                # e.g. "ColorVu 4 ميجا": search instead
            return (f"NOT A MODEL CODE: '{query}' is a description, so here is the catalog search for it.\n"
                    + self.search(query))
        b_kind, b_text = self._brochure(q)
        if self.carried is None:                      # brochure only
            return self._answer(query, [(b_kind, "", b_text)])
        c_kind, c_text = self._carried(q)
        if b_kind == "exact" and c_kind in ("near", "none"):
            c_kind, c_text = "none", "Not in FastEgy's product list (the Odoo export)."
        parts = [(c_kind, "== FastEgy product list (Odoo) ==", c_text)]
        if not (c_kind == "exact" and b_kind in ("near", "none")):     # no brochure guesses next to a hit
            parts.append((b_kind, "== Hikvision brochure ==", b_text))
        return self._answer(query, parts)

    def _answer(self, query, parts):
        kinds = [k for k, _, _ in parts]
        body = "\n\n".join((h + "\n" + t).strip() for _, h, t in parts)
        if "exact" in kinds:
            return f"EXACT MATCH for {query}:\n\n{body}"
        web = ("If you have web search, look up the official datasheet of this exact code and give its specs "
               "with the link; otherwise ask which code the user means. Never present another code's specs as this one's.")
        if any(k in ("variant", "near") for k in kinds):
            return (f"NO EXACT MATCH for {query}: this exact code is not in our sources.\n\n{body}\n\n"
                    "Tell the user and show the closest codes above. " + web)
        where = "the " + self.source + (" or FastEgy's product list" if self.carried is not None else "")
        return f"NOT FOUND: {query} is not in {where}. Do not guess its specs. " + web

    def _brochure(self, q):
        """(kind, text): kind is exact / variant / near / none."""
        hits, seen = [], set()
        for n, pat, p in self.index:
            if pat.match(q) and id(p) not in seen:
                hits.append((n, p))
                seen.add(id(p))
        if hits:
            poe = [p for _, p in hits if any(re.search(r"/\d*P\s*:\s*PoE", x) for x in p["specs"])]
            if poe and not re.search(r"/\d*P\b", q):         # DS-7608NXI-K1 has no PoE; DS-7608NXI-K1/8P has
                poe_note = ("\n\nPoE: in this entry '/P: PoE' means only the /P versions (e.g. .../8P, .../16P) "
                            "have PoE ports. The code asked about has no /P, so it has no built-in PoE ports.")
            else:
                poe_note = ""
            return "exact", ("\n\n".join(self.describe(p, matched=n) for n, p in hits) + poe_note + "\n\n"
                             "Brochure notation: X = resolution digit listed in the specs; parts in brackets "
                             "like (/SL) or (RB) are optional variants; '/SL' = strobe light & audio alarm variant.\n"
                             "These are key specs only; for the full datasheet values, say so and do not invent them.")
        # the brochure may list only a suffixed variant of the code (…/SL, …/8P): say so explicitly
        variants, seen = [], set()
        for n, _, p in self.index:
            for base, tail in variant_bases(n):
                if id(p) not in seen and code_pattern(base, p["specs"]).match(q):
                    variants.append((n, tail))
                    seen.add(id(p))
                    break
        if variants:
            return "variant", ("The brochure lists only a variant of this code with an extra suffix:\n"
                               + "\n".join(f"  - {n}   (your code + \"{t}\")" for n, t in variants[:6])
                               + "\nA suffix marks a different variant ('/SL' = strobe light & audio alarm). "
                                 "You may look the variant up and give its specs, clearly labelled as the "
                                 "variant's specs, never as the plain code's.")
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
        if cands:
            return "near", "Closest codes in the brochure:\n" + "\n".join(f"  - {c}" for c in cands[:6])
        return "none", f"Not in the {self.source}."

    def _carried(self, q):
        """(kind, text) for FastEgy's own product list."""
        m = self.carried_index.get(q)
        if m:
            lines = [f"FastEgy carries {m['code']}: it is in FastEgy's product list ({self.carried_source})."]
            if m["names"] != [m["code"]]:
                lines.append("Product names in Odoo: " + "; ".join(m["names"][:12]))
            if m.get("lens"):
                lines.append("Lens options: " + ", ".join(m["lens"]))
            tags = [x for x in [m.get("category"), *m.get("tags", [])] if x]
            if tags:
                lines.append("Category / tags: " + ", ".join(tags))
            if m.get("ar"):
                lines.append("FastEgy description (Arabic, written by FastEgy): " + m["ar"])
            else:
                lines.append("FastEgy description: none in Odoo, so only the tags above are known.")
            lines.append("The list says nothing about stock or price: do not claim either.")
            return "exact", "\n".join(lines)
        related = []
        for code, mm in self.carried_index.items():          # the list has a suffixed variant of the code
            for base, tail in variant_bases(code):
                if base == q:
                    related.append(f"  - {mm['code']}   (your code + \"{tail}\")")
                    break
        for base, tail in variant_bases(q):                   # ...or the code without the user's suffix
            if base in self.carried_index:
                related.append(f"  - {self.carried_index[base]['code']}   (your code without \"{tail}\")")
        if related:
            return "variant", "FastEgy's product list has related codes, not this exact one:\n" + "\n".join(related[:6])
        scored = sorted(((difflib.SequenceMatcher(None, q, code).ratio(), mm["code"])
                         for code, mm in self.carried_index.items()), reverse=True)
        near = [c for r, c in scored[:5] if r >= 0.8]
        if near:
            return "near", "Closest codes in FastEgy's product list:\n" + "\n".join(f"  - {c}" for c in near)
        return "none", "Not in FastEgy's product list."

    def search(self, text, limit=8):
        """Keyword search (English or Arabic) over FastEgy's product list and the brochure."""
        terms = [t for t in dict.fromkeys(search_tokens(text)) if t not in STOPWORDS]
        if not terms:
            return "Give some keywords, e.g. 'ColorVu 4 MP turret', '16-ch NVR PoE' or 'كاميرا خارجية 4 ميجا'."

        def hits(blob):
            toks = set(search_tokens(blob))
            flat = " ".join(toks)
            return sum(1 for t in terms if t in toks or (len(t) >= 4 and t in flat))

        def best(items, blob_of):
            """Items with the most keywords; with 3+ keywords also those missing one, marked."""
            ranked = [(hits(blob_of(x)), x) for x in items]
            ranked = [(s, x) for s, x in ranked if s]
            if not ranked:
                return 0, []
            top = max(s for s, _ in ranked)
            floor = top - 1 if len(terms) >= 3 and top >= 2 else top
            rows = sorted(((s, x) for s, x in ranked if s >= floor), key=lambda t: -t[0])[:limit]
            return top, rows

        def mark(score, top):
            return "" if score == top else f"  [matches {score}/{len(terms)} keywords]"

        out = []
        if self.carried is not None:
            top, rows = best(self.carried, lambda m: " ".join(
                [m["code"], *m["names"], m.get("category") or "", *m.get("tags", []), m.get("ar") or ""]))
            if rows:
                out.append(f"FastEgy product list: {len(rows)} models matching '{text}' ({top}/{len(terms)} keywords):")
                for sc, m in rows:
                    out.append(f"- {m['code']} | tags: {', '.join(m.get('tags', []))} | FastEgy description: "
                               f"{m.get('ar') or 'none in Odoo (only the tags are known)'}{mark(sc, top)}")
        top, rows = best(self.products, lambda p: " ".join(
            [*p["codes"], *(p.get("labels") or []), p.get("category") or "", p.get("series") or "",
             *p["specs"]]))
        if out and rows:                      # brochure lines next to FastEgy's models got their specs mixed up
            out.append(f"Hikvision brochure: {len(rows)} entries also match; not listed here because FastEgy's own "
                       "models matched. Use lookup_product on a code for its official brochure specs.")
            rows = []
        if rows:
            out.append(f"Hikvision brochure: {len(rows)} entries matching '{text}' ({top}/{len(terms)} keywords):")
            for sc, p in rows:
                out.append(f"- {', '.join(p['codes'])} | {p.get('series') or p.get('category')} | "
                           + "; ".join(p["specs"][:8]) + f" (page {p['page']}){mark(sc, top)}")
        if not out:
            return f"No catalog entries match: {text}"
        out.append("These lines are everything known about these models here: do not add specs that are not "
                   "written above. Use lookup_product on a code for its full entry.")
        return "\n".join(out)
