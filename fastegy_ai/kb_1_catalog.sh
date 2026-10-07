#!/usr/bin/env bash
# =============================================================================
# FastEgy AI — knowledge base 1: product catalog tool + memory fix
#
# 1) fastegy-reader v3: same page reader + search relay, plus
#      - a product-catalog tool for the assistant (MCP: lookup_product,
#        search_catalog) built from the Hikvision Hot-Selling 2025 H2 brochure
#        (351 entries / 415 codes; file: /root/fastegy-reader/catalog.json,
#        replaceable without a rebuild)
#      - reads official datasheet PDFs
#      - search relay queries only the engines that work (faster)
#    Tested from inside LibreChat; any failure puts reader v2 back.
# 2) librechat.yaml: register the tool for both options, allow only
#    fastegy-reader:3002 as an internal tool address, add catalog rules,
#    and stop the memory agent from saving product specs / prices / customers.
#    LibreChat restarts (~30 s); rollback on failure.
# 3) Delete the saved memories under "products_discussed" (they held invented
#    camera specs).
# Run    : sudo bash kb_1_catalog.sh
# Version: 1.0 — 2026-10-08
# =============================================================================
set -euo pipefail

SRC=https://raw.githubusercontent.com/mohamedfastegy/odoo/a7027c570be3ec726d3547a6dfb59a22708bfd48/fastegy_ai
LC=librechat
RD=fastegy-reader
SX=searxng
PYCTR=litellm
MONGO=librechat-mongo
RDDIR=/root/fastegy-reader
KEYFILE=$RDDIR/.key
CFG=/root/librechat/librechat.yaml
ENGINES=yahoo,startpage,yandex
TS=$(date +%Y%m%d_%H%M%S)

for c in "$LC" "$RD" "$SX" "$PYCTR" "$MONGO"; do
  [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = true ] || { echo "$c is not running; nothing changed."; exit 1; }
done
[ -s "$KEYFILE" ] || { echo "Missing $KEYFILE; nothing changed."; exit 1; }
[ "$(docker inspect "$RD" -f '{{index .Config.Labels "fastegy.ai"}}')" = web-search ] ||
  { echo "$RD was not created by the web_* scripts; nothing changed."; exit 1; }
NET=$(docker inspect "$LC" -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' | awk '{print $1}')
KEY=$(cat "$KEYFILE")

# ------------------------------------------------ 0) download + verify files
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
curl -fsSL "$SRC/reader.py" -o "$STAGE/reader.py"
curl -fsSL "$SRC/products.py" -o "$STAGE/products.py"
curl -fsSL "$SRC/kb/hikvision_hot_selling_2025H2.json" -o "$STAGE/catalog.json"
(cd "$STAGE" && sha256sum -c --quiet) <<'SUMS' || { echo "Downloaded files do not match; nothing changed."; exit 1; }
ed1ff358432c97dfd6c6fcaea7ad789ebcd63b7f74525bfdb87dfa489e6722a9  reader.py
6af111009dee389375f7055f4109332fb8258ebd85f275e4710e7ae0c632c2f5  products.py
65abecc00f7e71bd44f356607d16f52c2e6a5aa457d04eda5669767110e0df69  catalog.json
SUMS
echo "files downloaded and verified"

# ------------------------------------------------ 1) reader v3
for f in reader.py products.py Dockerfile catalog.json; do
  [ -f "$RDDIR/$f" ] && cp -p "$RDDIR/$f" "$RDDIR/$f.bak.$TS"
done
cp "$STAGE/reader.py" "$STAGE/products.py" "$STAGE/catalog.json" "$RDDIR/"
chmod 644 "$RDDIR/catalog.json"
cat >"$RDDIR/Dockerfile" <<'EOF'
FROM python:3.12-slim
RUN pip install --no-cache-dir "trafilatura>=1.12,<3" "pypdf>=4,<6"
COPY reader.py products.py /app/
USER nobody
EXPOSE 3002
CMD ["python", "-u", "/app/reader.py"]
EOF

run_reader() {  # run_reader IMAGE [extra docker args...]
  local img=$1; shift
  docker rm -f "$RD" >/dev/null 2>&1 || true
  docker run -d --name "$RD" --restart unless-stopped --network "$NET" --label fastegy.ai=web-search \
    --memory 384m -e READER_KEY="$KEY" -e SEARCH_ENGINES="$ENGINES" "$@" "$img" >/dev/null
}
restore_reader() {
  echo "!! $1 — putting reader v2 back"
  for f in reader.py products.py Dockerfile catalog.json; do
    if [ -f "$RDDIR/$f.bak.$TS" ]; then cp -p "$RDDIR/$f.bak.$TS" "$RDDIR/$f"; else rm -f "$RDDIR/$f"; fi
  done
  run_reader fastegy-reader:2
  echo "Reader v2 restored; LibreChat was not changed."; exit 1
}

echo "building fastegy-reader v3..."
docker build -q -t fastegy-reader:3 "$RDDIR" >/dev/null || restore_reader "build failed"
run_reader fastegy-reader:3 -e MCP_KEY="$KEY" -e CATALOG_PATH=/app/catalog.json \
  -v "$RDDIR/catalog.json:/app/catalog.json:ro" || restore_reader "reader v3 did not start"

# ------------------------------------------------ 2) tests from LibreChat's side
TEST_JS='
const base = "http://fastegy-reader:3002";
const rpc = (id, method, params) => fetch(base + "/mcp", { method: "POST",
  headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream",
             Authorization: "Bearer " + process.env.K },
  body: JSON.stringify({ jsonrpc: "2.0", id, method, params }) }).then(r => r.json());
const scrape = (url) => fetch(base + "/v2/scrape", { method: "POST",
  headers: { "Content-Type": "application/json", Authorization: "Bearer " + process.env.K },
  body: JSON.stringify({ url, formats: ["markdown"], timeout: 20000 }) }).then(r => r.json());
(async () => {
  for (let i = 0; i < 15; i++) { try { if ((await fetch(base + "/health")).ok) break; } catch {} await new Promise(r => setTimeout(r, 2000)); }
  const init = await rpc(1, "initialize", { protocolVersion: "2025-03-26", capabilities: {}, clientInfo: { name: "kb1-test", version: "1" } });
  const tools = await rpc(2, "tools/list", {});
  const hit = await rpc(3, "tools/call", { name: "lookup_product", arguments: { code: "DS-2CD2643G2-LIZS2U/SL" } });
  const miss = await rpc(4, "tools/call", { name: "lookup_product", arguments: { code: "DS-2CD9999G9-XYZ" } });
  const text = hit.result.content[0].text;
  console.log("mcp: server=" + init.result.serverInfo.name + " tools=" + tools.result.tools.map(t => t.name).join(","));
  console.log("mcp lookup: " + text.split("\n")[0] + " / unknown code -> " + miss.result.content[0].text.split("\n")[0].slice(0, 40));
  const s = await (await fetch(base + "/search?format=json&q=" + encodeURIComponent("Hikvision ColorVu camera") + "&categories=general&engines=google,bing,duckduckgo")).json();
  console.log("search relay: " + s.results.length + " results");
  const page = await scrape("https://en.wikipedia.org/wiki/Egyptian_pound");
  console.log("page reader: " + (page.success ? "ok" : "fail " + page.error));
  const pdf = await scrape("https://assets.hikvision.com/prd/public/all/doc/m000064174/DS-2CD1043G2-LIUF_Datasheet_20230914.pdf");
  const pdfOk = pdf.success && /1043/.test(pdf.data.markdown);
  console.log("datasheet PDF: " + (pdfOk ? "ok (" + pdf.data.markdown.length + " chars)" : "not read: " + (pdf.error || "no model code in text")));
  const missText = miss.result.content[0].text;
  const ok = text.startsWith("EXACT MATCH") && (missText.startsWith("NOT FOUND") || missText.startsWith("NO EXACT"))
             && s.results.length > 0 && page.success;
  process.exit(ok ? 0 : 2);
})().catch(e => { console.log("test error: " + e.message); process.exit(2); });'
echo "== tests"
docker exec -e K="$KEY" "$LC" node -e "$TEST_JS" || restore_reader "reader v3 tests failed"

# ------------------------------------------------ 3) librechat.yaml
BAK="$CFG.bak.$TS"
cp -p "$CFG" "$BAK"
PY_CFG=$(cat <<'PY'
import copy, sys, yaml
CATALOG_RULES = [
    "- لو السؤال فيه كود موديل هيك فيجن أو إزي فيز: استخدم أداة lookup_product الأول، قبل البحث في النت.",
    "- لو الأداة رجعت EXACT MATCH: اعتمد على المواصفات اللي فيها، وقول إن المصدر كتالوج هيك فيجن للمنتجات الأكتر مبيعًا.",
    "- لو رجعت NO EXACT MATCH أو NOT FOUND: اعرض الأكواد القريبة واسأل المستخدم يقصد أنهي، وما تدّيش مواصفات لكود ما اتطابقش.",
    "- لو محتاج مواصفة مش في الكتالوج: دوّر على ملف المواصفات الرسمي (datasheet) للكود نفسه بالظبط، وما تاخدش مواصفات من موديل قريب (زي اللي آخره /SL أو F أو من غيرها).",
    "- للترشيح أو المقارنة بين المنتجات اللي بنبيعها: استخدم search_catalog.",
]
MARK = "lookup_product"
MEMORY_INSTRUCTIONS = (
    "You manage memory for FastEgy team members. Save ONLY: the user's own preferences "
    "(language, tone, answer format) and stable work context (their role, team, usual tasks). "
    "NEVER save product specifications, model codes with specs, prices, stock, exchange rates, "
    "customer names or customer details: those come from the catalog, Odoo or the web and they change. "
    "Save only facts the user stated explicitly about themselves; never infer or summarize from the "
    "assistant's answers. Each memory under 200 chars. Arabic or English.")
old = yaml.safe_load(sys.stdin)
cfg = copy.deepcopy(old)
cfg.setdefault("mcpServers", {})["fastegy-products"] = {
    "type": "streamable-http",
    "url": "http://fastegy-reader:3002/mcp",
    "headers": {"Authorization": "Bearer ${FIRECRAWL_API_KEY}"},
    "timeout": 30000,
}
ms = cfg.setdefault("mcpSettings", {})
allowed = ms.setdefault("allowedAddresses", [])
if "fastegy-reader:3002" not in allowed:
    allowed.append("fastegy-reader:3002")
done = 0
for s in cfg["modelSpecs"]["list"]:
    if s.get("name") in ("fastegy-fast", "fastegy-strong"):
        servers = s.setdefault("mcpServers", [])
        if "fastegy-products" not in servers:
            servers.append("fastegy-products")
        lines = [l for l in s["preset"]["promptPrefix"].split("\n") if l not in CATALOG_RULES and MARK not in l]
        s["preset"]["promptPrefix"] = "\n".join(lines).rstrip() + "\n" + "\n".join(CATALOG_RULES)
        done += 1
if done != 2:
    sys.exit("ERROR: expected specs fastegy-fast and fastegy-strong; nothing changed")
mem = cfg.get("memory") or {}
if mem:
    mem["validKeys"] = ["user_preferences", "work_context", "personal_facts"]
    if mem.get("agent"):
        mem["agent"]["instructions"] = MEMORY_INSTRUCTIONS
# nothing else may change
a, b = copy.deepcopy(old), copy.deepcopy(cfg)
for x in (a, b):
    x.pop("mcpServers", None); x.pop("mcpSettings", None)
    x.get("memory", {}).pop("validKeys", None)
    if x.get("memory", {}).get("agent"):
        x["memory"]["agent"].pop("instructions", None)
    for s in x["modelSpecs"]["list"]:
        if s.get("name") in ("fastegy-fast", "fastegy-strong"):
            s.pop("mcpServers", None); s["preset"].pop("promptPrefix", None)
if a != b:
    sys.exit("ERROR: unexpected config changes; nothing changed")
yaml.safe_dump(cfg, sys.stdout, sort_keys=False, allow_unicode=True, width=1000)
PY
)
TMP_Y=$(mktemp)
trap 'rm -rf "$STAGE" "$TMP_Y"' EXIT
docker exec -i "$PYCTR" python3 -c "$PY_CFG" <"$CFG" >"$TMP_Y" || { echo "Config rewrite failed; LibreChat unchanged (reader v3 stays, it is backward compatible)."; exit 1; }
grep -q 'fastegy-products' "$TMP_Y" || { echo "Rewrite produced unexpected output; LibreChat unchanged."; exit 1; }
cat "$TMP_Y" >"$CFG"

rollback() {
  echo "!! $1 — rolling back librechat.yaml"
  cat "$BAK" >"$CFG"; docker restart "$LC" >/dev/null
  echo "Restored $BAK and restarted LibreChat (reader v3 stays; it is backward compatible)."; exit 1
}
docker exec "$LC" grep -q 'fastegy-products' /app/librechat.yaml || rollback "container does not see the new file"
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
echo "== LibreChat log lines about the tool:"
docker logs --since 3m "$LC" 2>&1 | grep -i 'fastegy-products' | tail -8 || true
if docker logs --since 3m "$LC" 2>&1 | grep -i 'fastegy-products' | grep -qiE 'error|fail|ssrf|blocked'; then
  rollback "LibreChat could not connect to the catalog tool (lines above)"
fi

# ------------------------------------------------ 4) wrong memories
DB=$(docker inspect "$LC" -f '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's#^MONGO_URI=.*/\([^/?]*\).*#\1#p'); DB=${DB:-LibreChat}
echo "== memories saved under products_discussed (to be deleted):"
docker exec "$MONGO" mongosh "$DB" --quiet --eval '
  const q = { key: "products_discussed" };
  db.memoryentries.find(q, { _id: 0, value: 1 }).forEach(m => print("  - " + String(m.value).slice(0, 120)));
  print("deleted: " + db.memoryentries.deleteMany(q).deletedCount);' || echo "   (could not clean memories; do it from Settings > Personalization)"

echo "DONE kb 1. Backups: $BAK and $RDDIR/*.bak.$TS"
echo "Test: refresh (Cmd+Shift+R), NEW chat: 'ايه مواصفات DS-2CD2643G2-LIZS2U/SL؟' and 'رشّحلي NVR 16 قناة PoE'."
