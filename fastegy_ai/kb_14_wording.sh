#!/usr/bin/env bash
# =============================================================================
# FastEgy AI — kb 14: "موجود في قائمة منتجاتنا", never "متوفر"
#
# In the test after ds_6 the assistant wrote "متوفر في قائمة منتجات FastEgy":
# "متوفر" reads as "in stock", which the list does not say. products.py v4.9
# adds, at the end of lookup_product (when the model is in FastEgy's list)
# and of search_catalog, how to say it: «موجود في قائمة منتجاتنا», never
# «متوفر» or «متاح». When FastEgy's description and the official specs differ
# on a number (DS-7608NXI-K1: 10 vs 16 TB), it also says to give the official
# one and mention the difference. Rebuilds fastegy-reader:4 as ds_5 does
# (previous image kept as fastegy-reader:4-prev, restored on any failure).
# Run    : bash kb_14_wording.sh
# Version: 1.0 — 2026-10-09
# =============================================================================
set -euo pipefail

SRC=https://raw.githubusercontent.com/mohamedfastegy/odoo/0e5a7a2a811e73e9c520ed4e03355b157e1e4837/fastegy_ai
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
NET=$(docker inspect "$LC" -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' | awk '{print $1}')
KEY=$(cat "$KEYFILE")
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

curl -fsSL "$SRC/products.py" -o "$STAGE/products.py"
(cd "$STAGE" && sha256sum -c --quiet) <<'SUMS' || { echo "Downloaded file does not match; nothing changed."; exit 1; }
d1eb93900c4245df644a0c54ad313f47b45588f49fa987070f8f1c3786e635a5  products.py
SUMS
echo "products.py v4.9 downloaded and verified"

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
  console.log("stock wording note: " + (k1.includes("موجود في قائمة منتجاتنا") ? "yes" : "MISSING"));
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
                           && !lv.includes("Support on-board storage") && lv.includes("0.72 A")))
             && k1.includes("موجود في قائمة منتجاتنا") && nvr.includes("موجود في قائمة منتجاتنا");
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
echo "building fastegy-reader v4 (catalog v4.9)..."
docker build -q -t fastegy-reader:4 "$RDDIR" >/dev/null || restore "build failed"
run_reader || restore "reader did not start"
echo "== tests"
run_tests || restore "tests failed"
echo "DONE kb 14. Previous image: fastegy-reader:4-prev, previous file: $RDDIR/products.py.bak.$TS"
