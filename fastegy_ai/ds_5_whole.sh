#!/usr/bin/env bash
# =============================================================================
# FastEgy AI — datasheets 5: whole datasheets; switch tables kept row by row
#
# The morning check found two gaps:
#   - lookup_product showed only the first 3200 characters of a datasheet, and
#     69 datasheets were stored cut at 6000, so power and size lines could be
#     missing and the model might fill them in;
#   - table-style datasheets (11, mostly switches) were stored with labels and
#     values on separate lines and words split ("Metal m" / "aterial").
# This installs:
#   - products.py v4.8: up to 4500 characters, then the version, power and size
#     lines from the rest, and a note that the rest was cut. Rebuilds
#     fastegy-reader:4 (previous image kept as fastegy-reader:4-prev).
#   - collector v1.4 (stores up to 12000; skips internal SKU exports) and
#     kb/ds_relayout.py, which re-reads those datasheets from their
#     saved links: tables with layout extraction ("Label | Value"), cut ones
#     in full. The few internal SKU exports (values per SKU, named in Chinese)
#     are searched again for an ordinary datasheet and hidden if there is none.
#     A record changes only when the re-read is clearly better.
# Any failure puts the previous reader or the previous datasheets file back.
# Run    : bash ds_5_whole.sh
# Version: 1.0 — 2026-10-09 (reader steps as ds_4)
# =============================================================================
set -euo pipefail

SRC=https://raw.githubusercontent.com/mohamedfastegy/odoo/2c33191d06e4ca5e09e8308d5fd1eda3230fd355/fastegy_ai
LC=librechat
RD=fastegy-reader
JOB=fastegy-datasheets
RDDIR=/root/fastegy-reader
DATA=$RDDIR/data
DS=$DATA/ds/datasheets.json
KEYFILE=$RDDIR/.key
ENGINES=yahoo,startpage,yandex
TS=$(date +%Y%m%d_%H%M%S)

for c in "$LC" "$RD"; do
  [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = true ] || { echo "$c is not running; nothing changed."; exit 1; }
done
[ "$(docker inspect "$RD" -f '{{.Config.Image}}')" = fastegy-reader:4 ] || { echo "$RD is not running reader v4 (run kb_2 first); nothing changed."; exit 1; }
[ -s "$KEYFILE" ] && [ -f "$DATA/carried.json" ] && [ -f "$RDDIR/Dockerfile" ] && [ -f "$DS" ] || { echo "Missing reader files; nothing changed."; exit 1; }
[ "$(docker inspect -f '{{.State.Running}}' "$JOB" 2>/dev/null)" = true ] &&
  { echo "$JOB is still running; it must have finished before its file is rewritten. Nothing changed."; exit 1; }
NET=$(docker inspect "$LC" -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' | awk '{print $1}')
KEY=$(cat "$KEYFILE")
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

curl -fsSL "$SRC/products.py" -o "$STAGE/products.py"
curl -fsSL "$SRC/kb/ds_collect.py" -o "$STAGE/ds_collect.py"
curl -fsSL "$SRC/kb/ds_relayout.py" -o "$STAGE/ds_relayout.py"
(cd "$STAGE" && sha256sum -c --quiet) <<'SUMS' || { echo "Downloaded files do not match; nothing changed."; exit 1; }
acde93b9386a6e9278fd7c9158c27d4c4b8fe7a8c2ff8dcbec435109036549ae  products.py
6b60aae676623cfb3bcd18c69082ade11e61cbf619759d75206e5643d9cd1be3  ds_collect.py
d5d4a118be678c7981b5bfd601a91d8d5df11a4a8012252af554fea47f7ea872  ds_relayout.py
SUMS
echo "products.py v4.8, collector v1.4 and ds_relayout downloaded and verified"

run_reader() {  # same settings as kb_2
  docker rm -f "$RD" >/dev/null 2>&1 || true
  docker run -d --name "$RD" --restart unless-stopped --network "$NET" --label fastegy.ai=web-search \
    --memory 384m -e READER_KEY="$KEY" -e SEARCH_ENGINES="$ENGINES" -e MCP_KEY="$KEY" \
    -e CATALOG_PATH=/app/catalog.json -v "$RDDIR/catalog.json:/app/catalog.json:ro" \
    -e CARRIED_PATH=/app/data/carried.json -e DATASHEETS_PATH=/app/data/ds/datasheets.json \
    -v "$DATA:/app/data:ro" fastegy-reader:4 >/dev/null
}
restore() {
  echo "!! $1 — putting the previous reader back"
  cp -p "$RDDIR/products.py.bak.$TS" "$RDDIR/products.py"
  docker tag fastegy-reader:4-prev fastegy-reader:4
  run_reader
  echo "Previous reader restored."; exit 1
}

TEST_JS='
const base = "http://fastegy-reader:3002";
const rpc = (id, method, params) => fetch(base + "/mcp", { method: "POST",
  headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream",
             Authorization: "Bearer " + process.env.K },
  body: JSON.stringify({ jsonrpc: "2.0", id, method, params }) }).then(r => r.json());
const call = async (id, name, args) => (await rpc(id, "tools/call", { name, arguments: args })).result.content[0].text;
(async () => {
  for (let i = 0; i < 15; i++) { try { if ((await fetch(base + "/health")).ok) break; } catch {} await new Promise(r => setTimeout(r, 2000)); }
  const init = await rpc(1, "initialize", { protocolVersion: "2025-03-26", capabilities: {}, clientInfo: { name: "kb2-test", version: "1" } });
  const listed = await call(2, "lookup_product", { code: process.env.CODE });       // a code from the new list
  const miss = await call(3, "lookup_product", { code: "DS-2CD9999G9-XYZ" });
  const ar = await call(4, "search_catalog", { keywords: "كاميرا" });
  const words = await call(7, "lookup_product", { code: "ColorVu 4 ميجا" });
  const nvr = await call(8, "search_catalog", { keywords: "16-ch NVR PoE" });
  console.log("lookup of words: " + words.split("\n")[0].slice(0, 70));
  console.log("16-ch NVR PoE: " + nvr.split("\n")[0]);
  const k1 = await call(9, "lookup_product", { code: "DS-7608NXI-K1" });
  console.log("DS-7608NXI-K1 PoE note: " + (k1.includes("PoE: in this entry") ? "yes" : "MISSING"));
  const v = await call(10, "lookup_product", { code: "DS-2CD1027G2-L" });
  console.log("DS-2CD1027G2-L datasheet + versions note: " + (v.includes("Official Hikvision datasheet") && v.includes("covers several versions") ? "yes" : "MISSING"));
  const lv = await call(11, "lookup_product", { code: "DS-2CD1067G3-LIU/SL" });
  const lvDs = lv.includes("Official Hikvision datasheet");
  console.log("DS-2CD1067G3-LIU/SL: " + (!lvDs ? "no datasheet yet" : (lv.includes("covers several versions") ? "versions note" : "NO NOTE")
    + (lv.includes("Key features") ? " | KEY FEATURES SHOWN" : " | key features left out")
    + (lv.includes("Support on-board storage") ? " | SD SLOT CLAIM" : "")
    + (lv.includes("[other versions, NOT DS-2CD1067G3-LIU/SL]") ? " | other-version lines marked" : "")
    + (lv.includes("0.72 A") ? " | its power line shown" : " | POWER LINE MISSING")));
  console.log("server version: " + init.result.serverInfo.version);
  console.log("lookup " + process.env.CODE + ": " + listed.split("\n")[0] + (listed.includes("FastEgy carries") ? " + FastEgy list" : " (FastEgy list missing)"));
  console.log("lookup unknown code: " + miss.split("\n")[0].slice(0, 60));
  console.log("arabic search: " + ar.split("\n")[0]);
  for (const [i, code] of [[5, "DS-7608NXI-K1"], [6, "DS-2CD1043G2-LIUF"]]) {
    const t = await call(i, "lookup_product", { code });
    console.log("  example " + code + ": " + t.split("\n")[0] + (t.includes("FastEgy carries") ? " | FastEgy list" : "") + (t.includes("Key specs") ? " | brochure specs" : "") + (t.includes("/SL") ? " | offers the /SL variant" : ""));
  }
  const ok = init.result.serverInfo.version === "4" && listed.startsWith("EXACT MATCH") && listed.includes("FastEgy carries")
             && miss.startsWith("NOT FOUND") && miss.includes("official datasheet") && ar.startsWith("FastEgy product list")
             && words.startsWith("NOT A MODEL CODE") && words.includes("FastEgy product list")
             && (nvr.match(/\n- DS-7/g) || []).length >= 3 && k1.includes("PoE: in this entry")
             && nvr.includes("not listed here because FastEgy")
             && v.includes("Official Hikvision datasheet") && v.includes("covers several versions")
             && (!lvDs || (lv.includes("covers several versions") && !lv.includes("Key features")
                           && !lv.includes("Support on-board storage") && lv.includes("0.72 A")));
  process.exit(ok ? 0 : 2);
})().catch(e => { console.log("test error: " + e.message); process.exit(2); });'
run_tests() {
  local code
  code=$(python3 -c 'import json,sys; m=json.load(open(sys.argv[1]))["models"]; print(m[len(m)//2]["code"])' "$DATA/carried.json")
  docker exec -e K="$KEY" -e CODE="$code" "$LC" node -e "$TEST_JS"
}

cp -p "$RDDIR/products.py" "$RDDIR/products.py.bak.$TS"
docker tag fastegy-reader:4 fastegy-reader:4-prev
cp "$STAGE/products.py" "$RDDIR/products.py"
echo "building fastegy-reader v4 (catalog v4.8)..."
docker build -q -t fastegy-reader:4 "$RDDIR" >/dev/null || restore "build failed"
run_reader || restore "reader did not start"
echo "== tests"
run_tests || restore "tests failed"

echo "== re-reading the table-style and cut datasheets from their saved links"
restore_ds() {
  echo "!! $1 — putting the previous datasheets file and collector back (the new reader stays)"
  cp -p "$DS.bak.$TS" "$DS"
  cp -p "$RDDIR/ds_collect.py.bak.$TS" "$RDDIR/ds_collect.py"
  rm -f "$RDDIR/ds_relayout.py"
  echo "Previous datasheets restored."; exit 1
}
cp -p "$RDDIR/ds_collect.py" "$RDDIR/ds_collect.py.bak.$TS"
cp -p "$DS" "$DS.bak.$TS"
install -m 644 "$STAGE/ds_collect.py" "$RDDIR/ds_collect.py"
install -m 644 "$STAGE/ds_relayout.py" "$RDDIR/ds_relayout.py"
OUT=$(docker run --rm --network "$NET" -v "$DATA:/data" -v "$RDDIR/ds_collect.py:/app/ds_collect.py:ro" \
  -v "$RDDIR/ds_relayout.py:/app/ds_relayout.py:ro" fastegy-reader:4 python /app/ds_relayout.py --apply 2>&1) ||
  { grep -v "Multiple definitions" <<<"$OUT" | tail -5; restore_ds "the re-read failed"; }
OUT=$(grep -v "Multiple definitions" <<<"$OUT")
grep -v '^\[' <<<"$OUT" || true
echo "re-read: $(grep -c 're-read (' <<<"$OUT" || true)"
grep '^\[' <<<"$OUT" | grep -v 're-read (' | cut -c1-160 | sed 's/^/  kept as it was: /' || true
python3 - "$DS.bak.$TS" "$DS" <<'PY' || restore_ds "the datasheets file does not check out"
import json, sys
old = {r["code"]: r for r in json.load(open(sys.argv[1], encoding="utf-8"))["datasheets"]}
new = {r["code"]: r for r in json.load(open(sys.argv[2], encoding="utf-8"))["datasheets"]}
assert old.keys() == new.keys(), "models differ"
moves = {(old[c]["status"], new[c]["status"]) for c in old if old[c]["status"] != new[c]["status"]}
assert moves <= {("found", "skipped")}, f"unexpected status change: {moves}"
changed = [c for c in old if old[c] != new[c]]
lost = [c for c in changed if new[c]["status"] == "found" and not new[c].get("spec", "").strip()]
assert not lost, f"empty spec after re-read: {lost}"
hidden = [c for c in old if new[c]["status"] == "skipped" and old[c]["status"] == "found"]
assert len(hidden) <= 10, f"too many hidden: {hidden}"
print(f"datasheets file OK: {len(new)} models, {sum(r['status'] == 'found' for r in new.values())} found, "
      f"{len(changed)} changed; hidden (only an internal SKU export): {', '.join(hidden) or 'none'}")
PY
CHECK_JS='
(async () => {
  const r = await fetch("http://fastegy-reader:3002/mcp", { method: "POST",
    headers: { "Content-Type": "application/json", Authorization: "Bearer " + process.env.K },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "lookup_product", arguments: { code: "DS-3E0520HP-E" } } }) });
  const t = (await r.json()).result.content[0].text;
  const spec = t.split("Specification:")[1] || "";
  console.log("DS-3E0520HP-E now:\n" + spec.split("\n").slice(1, 9).join("\n"));
  process.exit(spec.includes(" | ") && !/\n\s*DS\n\s*-\n/.test(spec) ? 0 : 2);
})().catch(e => { console.log("error: " + e.message); process.exit(2); });'
sleep 2
docker exec -e K="$KEY" "$LC" node -e "$CHECK_JS" || restore_ds "DS-3E0520HP-E does not show table rows"
echo "DONE ds 5. Previous image: fastegy-reader:4-prev; previous files: $RDDIR/products.py.bak.$TS, $RDDIR/ds_collect.py.bak.$TS, $DS.bak.$TS"
