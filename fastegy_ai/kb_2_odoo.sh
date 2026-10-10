#!/usr/bin/env bash
# =============================================================================
# FastEgy AI — knowledge base 2: FastEgy's own product list (Odoo export)
#
# Usage  : bash kb_2_odoo.sh /root/Product_product.template_4.xlsx
#          (copy the Excel export from Odoo to the server first; it never goes
#          to GitHub)
# First run (reader v3 running):
#   1) fastegy-reader v4: lookup_product / search_catalog answer from two sources:
#      FastEgy's product list (does FastEgy sell it, lens options, FastEgy's
#      Arabic description, tags) and the Hikvision brochure (official key specs);
#      Arabic keyword search. Any failure puts reader v3 back.
#   2) librechat.yaml: catalog rules updated for the two sources (never claim
#      stock or price). LibreChat restarts (~30 s); rollback on failure.
#   3) deletes the old "customers_mentioned" memories (customer data).
# Later runs (reader v4 already running): only replace the product list with
#   the new export; no restart needed.
# Prices are not imported. Keys are never printed.
# Version: 1.0 — 2026-10-08
# =============================================================================
set -euo pipefail

XLSX=${1:-}
SRC=https://raw.githubusercontent.com/mohamedfastegy/odoo/299ed3fef07cb7e5706ccea8b3c5f8717f289407/fastegy_ai
LC=librechat
RD=fastegy-reader
PYCTR=litellm
MONGO=librechat-mongo
RDDIR=/root/fastegy-reader
DATA=$RDDIR/data
KEYFILE=$RDDIR/.key
CFG=/root/librechat/librechat.yaml
ENGINES=yahoo,startpage,yandex
TS=$(date +%Y%m%d_%H%M%S)

[ -n "$XLSX" ] && [ -f "$XLSX" ] || { echo "Usage: bash kb_2_odoo.sh /path/to/odoo_export.xlsx"; exit 1; }
XLSX=$(readlink -f "$XLSX")
for c in "$LC" "$RD" "$PYCTR" "$MONGO"; do
  [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = true ] || { echo "$c is not running; nothing changed."; exit 1; }
done
[ -s "$KEYFILE" ] || { echo "Missing $KEYFILE; nothing changed."; exit 1; }
[ "$(docker inspect "$RD" -f '{{index .Config.Labels "fastegy.ai"}}')" = web-search ] ||
  { echo "$RD was not created by the web_* scripts; nothing changed."; exit 1; }
RUNNING=$(docker inspect "$RD" -f '{{.Config.Image}}')
case "$RUNNING" in
  fastegy-reader:3) MODE=install ;;
  fastegy-reader:4) MODE=update ;;
  *) echo "$RD runs $RUNNING (expected fastegy-reader:3 or :4); nothing changed."; exit 1 ;;
esac
NET=$(docker inspect "$LC" -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' | awk '{print $1}')
KEY=$(cat "$KEYFILE")
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

convert() {  # Excel -> $STAGE/out/carried.json, with the converter inside the v4 image (runs as nobody)
  mkdir -p "$STAGE/out" && chmod 777 "$STAGE/out"
  install -m 644 "$XLSX" "$STAGE/in.xlsx"
  docker run --rm --network none -v "$STAGE/in.xlsx:/in.xlsx:ro" -v "$STAGE/out:/out" fastegy-reader:4 \
    python /app/odoo_products.py /in.xlsx /out/carried.json || return 1
  python3 -c 'import json,sys; n=len(json.load(open(sys.argv[1]))["models"]); print("models in the list:", n); sys.exit(n < 100)' \
    "$STAGE/out/carried.json"
}
install_list() {  # atomic replace inside the mounted directory: the reader reloads it on its own
  mkdir -p "$DATA"
  [ -f "$DATA/carried.json" ] && cp -p "$DATA/carried.json" "$DATA/carried.json.bak.$TS"
  install -m 644 "$STAGE/out/carried.json" "$DATA/.carried.json.new"
  mv -f "$DATA/.carried.json.new" "$DATA/carried.json"
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
  console.log("server version: " + init.result.serverInfo.version);
  console.log("lookup " + process.env.CODE + ": " + listed.split("\n")[0] + (listed.includes("FastEgy carries") ? " + FastEgy list" : " (FastEgy list missing)"));
  console.log("lookup unknown code: " + miss.split("\n")[0].slice(0, 60));
  console.log("arabic search: " + ar.split("\n")[0]);
  for (const [i, code] of [[5, "DS-7608NXI-K1"], [6, "DS-2CD1043G2-LIUF"]]) {
    const t = await call(i, "lookup_product", { code });
    console.log("  example " + code + ": " + t.split("\n")[0] + (t.includes("FastEgy carries") ? " | FastEgy list" : "") + (t.includes("Key specs") ? " | brochure specs" : "") + (t.includes("/SL") ? " | offers the /SL variant" : ""));
  }
  const ok = init.result.serverInfo.version === "4" && listed.startsWith("EXACT MATCH") && listed.includes("FastEgy carries")
             && miss.startsWith("NOT FOUND") && ar.startsWith("FastEgy product list");
  process.exit(ok ? 0 : 2);
})().catch(e => { console.log("test error: " + e.message); process.exit(2); });'
run_tests() {
  local code
  code=$(python3 -c 'import json,sys; m=json.load(open(sys.argv[1]))["models"]; print(m[len(m)//2]["code"])' "$DATA/carried.json")
  docker exec -e K="$KEY" -e CODE="$code" "$LC" node -e "$TEST_JS"
}

# ------------------------------------------------ later runs: replace the list only
if [ "$MODE" = update ]; then
  echo "reader v4 is running: updating the product list only"
  convert || { echo "Conversion failed; nothing changed."; exit 1; }
  install_list
  sleep 1
  if run_tests; then
    echo "DONE: product list updated. Previous list: $DATA/carried.json.bak.$TS"
  else
    cp -p "$DATA/carried.json.bak.$TS" "$DATA/carried.json"; echo "!! tests failed — previous list restored."; exit 1
  fi
  exit 0
fi

# ------------------------------------------------ 0) download + verify files
curl -fsSL "$SRC/reader.py" -o "$STAGE/reader.py"
curl -fsSL "$SRC/products.py" -o "$STAGE/products.py"
curl -fsSL "$SRC/kb/odoo_products.py" -o "$STAGE/odoo_products.py"
(cd "$STAGE" && sha256sum -c --quiet) <<'SUMS' || { echo "Downloaded files do not match; nothing changed."; exit 1; }
5176b9e5f54edde86e2cf16a4b7c426dfe654fb6deddd79a45d565297cd57811  reader.py
261157691db9107fbe7b939cee6df13b1355910c6d049f57fab456e40d9a44d6  products.py
19a91f7822adb4c5a04bf20f5c9882d231af916f1fa216b54bff3550f88c7433  odoo_products.py
SUMS
echo "files downloaded and verified"

# ------------------------------------------------ 1) reader v4
for f in reader.py products.py odoo_products.py Dockerfile; do
  [ -f "$RDDIR/$f" ] && cp -p "$RDDIR/$f" "$RDDIR/$f.bak.$TS"
done
cp "$STAGE/reader.py" "$STAGE/products.py" "$STAGE/odoo_products.py" "$RDDIR/"
cat >"$RDDIR/Dockerfile" <<'EOF'
FROM python:3.12-slim
RUN pip install --no-cache-dir "trafilatura>=1.12,<3" "pypdf>=4,<6" "openpyxl>=3.1,<4"
COPY reader.py products.py odoo_products.py /app/
USER nobody
EXPOSE 3002
CMD ["python", "-u", "/app/reader.py"]
EOF

run_reader() {  # run_reader IMAGE [extra docker args...]
  local img=$1; shift
  docker rm -f "$RD" >/dev/null 2>&1 || true
  docker run -d --name "$RD" --restart unless-stopped --network "$NET" --label fastegy.ai=web-search \
    --memory 384m -e READER_KEY="$KEY" -e SEARCH_ENGINES="$ENGINES" -e MCP_KEY="$KEY" \
    -e CATALOG_PATH=/app/catalog.json -v "$RDDIR/catalog.json:/app/catalog.json:ro" "$@" "$img" >/dev/null
}
restore_reader() {
  echo "!! $1 — putting reader v3 back"
  for f in reader.py products.py odoo_products.py Dockerfile; do
    if [ -f "$RDDIR/$f.bak.$TS" ]; then cp -p "$RDDIR/$f.bak.$TS" "$RDDIR/$f"; else rm -f "$RDDIR/$f"; fi
  done
  run_reader fastegy-reader:3
  echo "Reader v3 restored; LibreChat was not changed."; exit 1
}

echo "building fastegy-reader v4..."
docker build -q -t fastegy-reader:4 "$RDDIR" >/dev/null || restore_reader "build failed"
echo "converting the Odoo export..."
convert || restore_reader "could not convert $XLSX"
install_list
run_reader fastegy-reader:4 -e CARRIED_PATH=/app/data/carried.json -v "$DATA:/app/data:ro" ||
  restore_reader "reader v4 did not start"
echo "== tests"
run_tests || restore_reader "reader v4 tests failed"

# ------------------------------------------------ 2) librechat.yaml rules
BAK="$CFG.bak.$TS"
cp -p "$CFG" "$BAK"
PY_CFG=$(cat <<'PY'
import copy, sys, yaml
OLD_RULES = [
    "- لو الأداة رجعت EXACT MATCH: اعتمد على المواصفات اللي فيها، وقول إن المصدر كتالوج هيك فيجن للمنتجات الأكتر مبيعًا.",
    "- لو رجعت NO EXACT MATCH أو NOT FOUND: اعرض الأكواد القريبة واسأل المستخدم يقصد أنهي، وما تدّيش مواصفات لكود ما اتطابقش.",
    "- لو محتاج مواصفة مش في الكتالوج: دوّر على ملف المواصفات الرسمي (datasheet) للكود نفسه بالظبط، وما تاخدش مواصفات من موديل قريب (زي اللي آخره /SL أو F أو من غيرها).",
]
CATALOG_RULES = [
    "- لو السؤال فيه كود موديل هيك فيجن أو إزي فيز: استخدم أداة lookup_product الأول، قبل البحث في النت.",
    "- الأداة بترد من مصدرين: قائمة منتجات FastEgy من Odoo (يعني إحنا بنبيع الموديل ده، ووصفنا ليه بالعربي)، وكتالوج هيك فيجن (مواصفات رسمية). اذكر المصدر زي ما الأداة كاتباه.",
    "- وجود الموديل في قائمة منتجات FastEgy معناه إننا بنبيعه، مش إنه متوفر في المخزن. ما تقولش سعر ولا كمية ولا إنه متاح للتسليم.",
    "- لو رجعت NO EXACT MATCH أو NOT FOUND: اعرض الأكواد القريبة واسأل المستخدم يقصد أنهي، وما تدّيش مواصفات لكود ما اتطابقش.",
    "- لو محتاج مواصفة مش موجودة: دوّر على ملف المواصفات الرسمي (datasheet) للكود نفسه بالظبط، وما تاخدش مواصفات من موديل قريب (زي اللي آخره /SL أو F أو من غيرها).",
    "- للترشيح أو المقارنة: استخدم search_catalog، ورشّح من المنتجات اللي في قائمة FastEgy الأول.",
]
MARKS = ("lookup_product", "search_catalog")
old = yaml.safe_load(sys.stdin)
cfg = copy.deepcopy(old)
done = 0
for s in cfg["modelSpecs"]["list"]:
    if s.get("name") in ("fastegy-fast", "fastegy-strong"):
        lines = [l for l in s["preset"]["promptPrefix"].split("\n")
                 if l not in CATALOG_RULES and l not in OLD_RULES and not any(m in l for m in MARKS)]
        s["preset"]["promptPrefix"] = "\n".join(lines).rstrip() + "\n" + "\n".join(CATALOG_RULES)
        done += 1
if done != 2:
    sys.exit("ERROR: expected specs fastegy-fast and fastegy-strong; nothing changed")
a, b = copy.deepcopy(old), copy.deepcopy(cfg)          # nothing else may change
for x in (a, b):
    for s in x["modelSpecs"]["list"]:
        if s.get("name") in ("fastegy-fast", "fastegy-strong"):
            s["preset"].pop("promptPrefix", None)
if a != b:
    sys.exit("ERROR: unexpected config changes; nothing changed")
yaml.safe_dump(cfg, sys.stdout, sort_keys=False, allow_unicode=True, width=1000)
PY
)
TMP_Y=$(mktemp)
trap 'rm -rf "$STAGE" "$TMP_Y"' EXIT
docker exec -i "$PYCTR" python3 -c "$PY_CFG" <"$CFG" >"$TMP_Y" || { echo "Config rewrite failed; LibreChat unchanged (reader v4 stays, it is backward compatible)."; exit 1; }
grep -q 'search_catalog' "$TMP_Y" || { echo "Rewrite produced unexpected output; LibreChat unchanged."; exit 1; }
cat "$TMP_Y" >"$CFG"

rollback() {
  echo "!! $1 — rolling back librechat.yaml"
  cat "$BAK" >"$CFG"; docker restart "$LC" >/dev/null
  echo "Restored $BAK and restarted LibreChat (reader v4 stays; it is backward compatible)."; exit 1
}
echo "Restarting LibreChat (site down ~30 s)..."
docker restart "$LC" >/dev/null || rollback "restart failed"
code=000
for _ in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:3080/api/config || true)
  [ "$code" = 200 ] && break
  sleep 2
done
[ "$code" = 200 ] || rollback "LibreChat did not come back (HTTP $code)"
sleep 5
if docker logs --since 3m "$LC" 2>&1 | grep -i 'invalid custom config'; then rollback "LibreChat rejected the config"; fi
docker logs --since 3m "$LC" 2>&1 | grep -i 'fastegy-products' | grep -i 'tools:' | tail -1 || true
if docker logs --since 3m "$LC" 2>&1 | grep -i 'fastegy-products' | grep -qiE 'error|fail|ssrf|blocked'; then
  rollback "LibreChat could not connect to the catalog tool"
fi

# ------------------------------------------------ 3) old memories with customer data
DB=$(docker inspect "$LC" -f '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's#^MONGO_URI=.*/\([^/?]*\).*#\1#p'); DB=${DB:-LibreChat}
docker exec "$MONGO" mongosh "$DB" --quiet --eval '
  print("customers_mentioned memories deleted: " + db.memoryentries.deleteMany({ key: "customers_mentioned" }).deletedCount);' ||
  echo "   (could not clean memories; do it from Settings > Personalization)"

echo "DONE kb 2. Backups: $BAK and $RDDIR/*.bak.$TS"
echo "Test: refresh (Cmd+Shift+R), NEW chat: 'عندنا DS-7608NXI-K1؟ ومواصفاته ايه' and 'رشحلي كاميرا خارجية 4 ميجا بمايك'."
echo "Next Odoo export: copy it to the server and run this script again with the new file."
