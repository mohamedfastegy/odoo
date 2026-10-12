#!/usr/bin/env bash
# =============================================================================
# FastEgy AI — datasheets 1: official datasheets for models with no description
#
# 646 of FastEgy's 1099 models have no description in Odoo, so the assistant
# knows only their code. This installs:
#   - reader v5 (catalog v4.5): lookup_product shows a model's official
#     datasheet (title, key features, specification) when one was collected;
#   - kb/ds_collect.py in the image, and runs it twice:
#       1) a pilot now: 5 models, to check search, PDF reading and parsing;
#       2) the night job "fastegy-datasheets": the remaining models between
#          22:00 and 07:00 Cairo time, one search every 45 s, saving after each
#          model (the reader picks the file up without a restart).
# Any failure before the night job starts puts the previous reader back.
# Stop the job any time:  docker rm -f fastegy-datasheets
# Run    : bash ds_1_install.sh
# Version: 1.0 — 2026-10-08
# =============================================================================
set -euo pipefail

SRC=https://raw.githubusercontent.com/mohamedfastegy/odoo/4bde6411ac579cc27bccfb1dceacdc5f9aa4e67d/fastegy_ai
LC=librechat
RD=fastegy-reader
JOB=fastegy-datasheets
RDDIR=/root/fastegy-reader
DATA=$RDDIR/data
KEYFILE=$RDDIR/.key
ENGINES=yahoo,startpage,yandex
TS=$(date +%Y%m%d_%H%M%S)

for c in "$LC" "$RD"; do
  [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = true ] || { echo "$c is not running; nothing changed."; exit 1; }
done
[ "$(docker inspect "$RD" -f '{{.Config.Image}}')" = fastegy-reader:4 ] || { echo "$RD is not running reader v4; nothing changed."; exit 1; }
[ -s "$KEYFILE" ] && [ -f "$DATA/carried.json" ] && [ -f "$RDDIR/Dockerfile" ] || { echo "Missing reader files; nothing changed."; exit 1; }
NET=$(docker inspect "$LC" -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' | awk '{print $1}')
KEY=$(cat "$KEYFILE")
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

curl -fsSL "$SRC/reader.py" -o "$STAGE/reader.py"
curl -fsSL "$SRC/products.py" -o "$STAGE/products.py"
curl -fsSL "$SRC/kb/ds_collect.py" -o "$STAGE/ds_collect.py"
(cd "$STAGE" && sha256sum -c --quiet) <<'SUMS' || { echo "Downloaded files do not match; nothing changed."; exit 1; }
22eea92c84df0586776bfa07a0c597f8b13832a8c296273e9eed551c0765e98c  reader.py
e735a5a01e3b9e9cefec66310a996458ff9c4eddebbe2f4c9da8abd809f7b396  products.py
16f5cd02ab59dab94203c5c8c621216e3f2038346fc8020bc789a07e809ef10e  ds_collect.py
SUMS
echo "files downloaded and verified"

run_reader() {  # same settings as kb_10, plus the datasheets file
  docker rm -f "$RD" >/dev/null 2>&1 || true
  docker run -d --name "$RD" --restart unless-stopped --network "$NET" --label fastegy.ai=web-search \
    --memory 384m -e READER_KEY="$KEY" -e SEARCH_ENGINES="$ENGINES" -e MCP_KEY="$KEY" \
    -e CATALOG_PATH=/app/catalog.json -v "$RDDIR/catalog.json:/app/catalog.json:ro" \
    -e CARRIED_PATH=/app/data/carried.json -e DATASHEETS_PATH=/app/data/ds/datasheets.json \
    -v "$DATA:/app/data:ro" fastegy-reader:4 >/dev/null
}
restore() {
  echo "!! $1 — putting the previous reader back"
  for f in reader.py products.py Dockerfile; do cp -p "$RDDIR/$f.bak.$TS" "$RDDIR/$f"; done
  rm -f "$RDDIR/ds_collect.py"
  docker tag fastegy-reader:4-prev fastegy-reader:4
  run_reader
  echo "Previous reader restored; no datasheet job was started."; exit 1
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
             && (nvr.match(/\n- DS-7/g) || []).length >= 3 && k1.includes("PoE: in this entry")
             && nvr.includes("not listed here because FastEgy");
  process.exit(ok ? 0 : 2);
})().catch(e => { console.log("test error: " + e.message); process.exit(2); });'
run_tests() {
  local code
  code=$(python3 -c 'import json,sys; m=json.load(open(sys.argv[1]))["models"]; print(m[len(m)//2]["code"])' "$DATA/carried.json")
  docker exec -e K="$KEY" -e CODE="$code" "$LC" node -e "$TEST_JS"
}
for f in reader.py products.py Dockerfile; do cp -p "$RDDIR/$f" "$RDDIR/$f.bak.$TS"; done
cp "$STAGE/reader.py" "$STAGE/products.py" "$STAGE/ds_collect.py" "$RDDIR/"
cat >"$RDDIR/Dockerfile" <<'DOCKERFILE'
FROM python:3.12-slim
RUN pip install --no-cache-dir "trafilatura>=1.12,<3" "pypdf>=4,<6" "openpyxl>=3.1,<4" tzdata
COPY reader.py products.py odoo_products.py ds_collect.py /app/
USER nobody
EXPOSE 3002
CMD ["python", "-u", "/app/reader.py"]
DOCKERFILE
mkdir -p "$DATA/ds" && chown 65534:65534 "$DATA/ds"
docker tag fastegy-reader:4 fastegy-reader:4-prev
echo "building fastegy-reader v4 (reader v5, catalog v4.5, datasheet collector)..."
docker build -q -t fastegy-reader:4 "$RDDIR" >/dev/null || restore "build failed"
run_reader || restore "reader did not start"
echo "== tests"
run_tests || restore "tests failed"

collect() { docker run --rm --network "$NET" -v "$DATA:/data" fastegy-reader:4 python /app/ds_collect.py "$@"; }
echo "== pilot: 5 models now (about 2 minutes)"
collect --now --limit 5 --sleep 20 || restore "the pilot run failed"
COUNT='import json,sys; d=json.load(open(sys.argv[1]))["datasheets"]; f=[r["code"] for r in d if r["status"]=="found"]; print(len(f), f[0] if f else "")'
read -r FOUND CODE < <(python3 -c "$COUNT" "$DATA/ds/datasheets.json")
echo "pilot: $FOUND of 5 datasheets kept"
[ "$FOUND" -ge 2 ] || restore "the pilot kept fewer than 2 datasheets"
SHOW='
(async () => {
  const r = await fetch("http://fastegy-reader:3002/mcp", { method: "POST",
    headers: { "Content-Type": "application/json", Authorization: "Bearer " + process.env.K },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "lookup_product", arguments: { code: process.env.CODE } } }) });
  const t = (await r.json()).result.content[0].text;
  console.log(t.split("\n").slice(0, 14).join("\n"));
  process.exit(t.includes("Official Hikvision datasheet") ? 0 : 2);
})().catch(e => { console.log("error: " + e.message); process.exit(2); });'
echo "== lookup_product $CODE now shows:"
docker exec -e K="$KEY" -e CODE="$CODE" "$LC" node -e "$SHOW" || restore "lookup_product does not show the datasheet"

echo "== starting the night job ($JOB): 22:00-07:00 Cairo, one search every 45 s"
docker rm -f "$JOB" >/dev/null 2>&1 || true
docker run -d --name "$JOB" --restart on-failure:3 --network "$NET" --label fastegy.ai=datasheets \
  --memory 256m -v "$DATA:/data" fastegy-reader:4 python /app/ds_collect.py --start 22 --stop 7 --sleep 45 >/dev/null
sleep 3
docker logs "$JOB" 2>&1 | tail -2
echo "DONE ds 1. Previous image: fastegy-reader:4-prev, previous files: $RDDIR/*.bak.$TS"
echo "Watch:  docker logs --tail 20 $JOB      Stop:  docker rm -f $JOB"
