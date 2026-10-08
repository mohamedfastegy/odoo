#!/usr/bin/env bash
# =============================================================================
# FastEgy AI — knowledge base 8: catalog v4.3
#
# From the kb_7 regression: the fast option gave DS-7608NXI-K1 PoE ports (only
# its /8P version has them), and both options gave specs to models whose Odoo
# description is empty. products.py v4.3: a PoE note when the code asked about
# has no /P, and "none in Odoo, only the tags are known" for empty descriptions.
# Change : rebuild fastegy-reader:4 with the new products.py and recreate the
#          container with the same settings. The previous image is kept as
#          fastegy-reader:4-prev; any failure puts it back. LibreChat is not touched.
# Run    : bash kb_5_reader.sh
# Version: 1.0 — 2026-10-08 (same steps as kb_5)
# =============================================================================
set -euo pipefail

SRC=https://raw.githubusercontent.com/mohamedfastegy/odoo/d6462a21d0094b2e7ba1846938fdbc9b708d6ac9/fastegy_ai
LC=librechat
RD=fastegy-reader
RDDIR=/root/fastegy-reader
DATA=$RDDIR/data
KEYFILE=$RDDIR/.key
ENGINES=yahoo,startpage,yandex
TS=$(date +%Y%m%d_%H%M%S)

for c in "$LC" "$RD"; do
  [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = true ] || { echo "$c is not running; nothing changed."; exit 1; }
done
[ "$(docker inspect "$RD" -f '{{.Config.Image}}')" = fastegy-reader:4 ] || { echo "$RD is not running reader v4 (run kb_2 first); nothing changed."; exit 1; }
[ -s "$KEYFILE" ] && [ -f "$DATA/carried.json" ] && [ -f "$RDDIR/Dockerfile" ] || { echo "Missing reader files; nothing changed."; exit 1; }
NET=$(docker inspect "$LC" -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' | awk '{print $1}')
KEY=$(cat "$KEYFILE")
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

curl -fsSL "$SRC/products.py" -o "$STAGE/products.py"
echo "f6a394fc6e6289c7058af905ea6b5c1f1fffc598531f97ca07ffae95e1cbf878  $STAGE/products.py" | sha256sum -c --quiet ||
  { echo "Downloaded file does not match; nothing changed."; exit 1; }
echo "products.py downloaded and verified"

run_reader() {  # same settings as kb_2
  docker rm -f "$RD" >/dev/null 2>&1 || true
  docker run -d --name "$RD" --restart unless-stopped --network "$NET" --label fastegy.ai=web-search \
    --memory 384m -e READER_KEY="$KEY" -e SEARCH_ENGINES="$ENGINES" -e MCP_KEY="$KEY" \
    -e CATALOG_PATH=/app/catalog.json -v "$RDDIR/catalog.json:/app/catalog.json:ro" \
    -e CARRIED_PATH=/app/data/carried.json -v "$DATA:/app/data:ro" fastegy-reader:4 >/dev/null
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
             && (nvr.match(/\n- DS-7/g) || []).length >= 3 && k1.includes("PoE: in this entry");
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
echo "building fastegy-reader v4 (catalog v4.3)..."
docker build -q -t fastegy-reader:4 "$RDDIR" >/dev/null || restore "build failed"
run_reader || restore "reader did not start"
echo "== tests"
run_tests || restore "tests failed"
echo "DONE kb 8. Previous image: fastegy-reader:4-prev, previous file: $RDDIR/products.py.bak.$TS"
