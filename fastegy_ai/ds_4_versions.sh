#!/usr/bin/env bash
# =============================================================================
# FastEgy AI — datasheets 4: versions marked line by line; English specs only
#
# A datasheet often covers several versions (DS-2CD1067G3-LIU(F)(/SL)(/SRB)).
# Its key features don't say which version has what, so the assistant could
# give DS-2CD1067G3-LIU/SL the 512 GB SD slot that only the F versions have.
#   - products.py v4.7: for such datasheets the key features are left out, and
#     spec lines written per version ("-LIUF、LIUF/S(L)(RB)：...") are marked
#     [DS-...] or [other versions, NOT DS-...] for the asked code. Rebuilds
#     fastegy-reader:4 (previous image kept as fastegy-reader:4-prev).
#   - collector v1.3: keeps a datasheet only with an English "Specification"
#     section and tries localized links (/fr-fr/) last. Datasheets already
#     kept without a specification are checked again. The job is stopped for
#     a few seconds and resumes where it was, with the same settings.
# Run    : bash ds_4_versions.sh
# Version: 1.0 — 2026-10-08 (reader steps as ds_2)
# =============================================================================
set -euo pipefail

SRC=https://raw.githubusercontent.com/mohamedfastegy/odoo/3f13ea4a88d2daa6d41ad45a52c26cd5fdea2cf5/fastegy_ai
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
docker inspect "$JOB" -f '{{range .Mounts}}{{.Destination}} {{end}}' 2>/dev/null | grep -q /app/ds_collect.py ||
  { echo "$JOB is not the ds_3 job (collector mounted from the server); nothing changed."; exit 1; }
NET=$(docker inspect "$LC" -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' | awk '{print $1}')
KEY=$(cat "$KEYFILE")
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

curl -fsSL "$SRC/products.py" -o "$STAGE/products.py"
curl -fsSL "$SRC/kb/ds_collect.py" -o "$STAGE/ds_collect.py"
(cd "$STAGE" && sha256sum -c --quiet) <<'SUMS' || { echo "Downloaded files do not match; nothing changed."; exit 1; }
1a0a85ab97d5bdf010095ba4326140c527afb567a7c5fb1ecda8da7a41623eeb  products.py
93f4282b98441ef82f3dce4945d20615d880de3525f508459cd96b10c9b60672  ds_collect.py
SUMS
echo "products.py v4.7 and collector v1.3 downloaded and verified"

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
    + (lv.includes("[other versions, NOT DS-2CD1067G3-LIU/SL]") ? " | other-version lines marked" : "")));
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
                           && !lv.includes("Support on-board storage")));
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
echo "building fastegy-reader v4 (catalog v4.7)..."
docker build -q -t fastegy-reader:4 "$RDDIR" >/dev/null || restore "build failed"
run_reader || restore "reader did not start"
echo "== tests"
run_tests || restore "tests failed"

echo "== collector v1.3 (the job pauses a few seconds and resumes where it was)"
restore_job() {
  echo "!! $1 — putting the previous collector and progress file back (the new reader stays)"
  cp -p "$RDDIR/ds_collect.py.bak.$TS" "$RDDIR/ds_collect.py"
  cp -p "$DS.bak.$TS" "$DS"
  docker start "$JOB" >/dev/null
  echo "Previous collector restored; the job runs again."; exit 1
}
cp -p "$RDDIR/ds_collect.py" "$RDDIR/ds_collect.py.bak.$TS"
docker stop -t 30 "$JOB" >/dev/null
cp -p "$DS" "$DS.bak.$TS"
install -m 644 "$STAGE/ds_collect.py" "$RDDIR/ds_collect.py"
python3 - "$DS" <<'PY' || restore_job "could not update the progress file"
import json, os, sys
path = sys.argv[1]
data = json.load(open(path, encoding="utf-8"))
recs = data["datasheets"]
again = [r["code"] for r in recs if r.get("status") == "found" and not (r.get("spec") or "").strip()]
for r in recs:
    if r["code"] in again:
        r["status"], r["why"] = "retry", "no Specification section"
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=1)
os.chown(tmp, 65534, 65534)
os.replace(tmp, path)
found = sum(r.get("status") == "found" for r in recs)
print(f"progress: {len(recs)} models checked, {found} datasheets kept; checked again (no specification): {', '.join(again) or 'none'}")
PY
START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
docker start "$JOB" >/dev/null
sleep 10
LOG=$(docker logs --since "$START" "$JOB" 2>&1)
tail -3 <<<"$LOG"
[ "$(docker inspect -f '{{.State.Running}}' "$JOB")" = true ] || restore_job "the job is not running"
grep -q "models to check" <<<"$LOG" || restore_job "the job did not start checking"
grep -q Traceback <<<"$LOG" && restore_job "the job crashed"
docker exec "$JOB" grep -q "Version 1.3" /app/ds_collect.py || restore_job "the job does not see collector v1.3"
echo "DONE ds 4. Previous image: fastegy-reader:4-prev; previous files: $RDDIR/products.py.bak.$TS, $RDDIR/ds_collect.py.bak.$TS, $DS.bak.$TS"
