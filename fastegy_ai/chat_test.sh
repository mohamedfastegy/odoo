#!/usr/bin/env bash
# =============================================================================
# FastEgy AI — chat test (read-only)
#
# Asks questions the way LibreChat does: the same model, the same instructions
# (promptPrefix from librechat.yaml, with today's date) and the same tools
# (the catalog tools from fastegy-reader, plus web search on the smart option),
# runs the tool calls, and prints which tools were called and the final answer.
# Changes nothing and prints no keys.
# Run    : bash chat_test.sh                      (the three standard questions)
#          bash chat_test.sh "سؤال 1" "سؤال 2"    (your own questions)
#          ONLY=fastegy-strong bash chat_test.sh   (one option only)
# Version: 1.1 — 2026-10-08 (web search now reads the top pages, as LibreChat does)
# =============================================================================
set -euo pipefail

CFG=/root/librechat/librechat.yaml
LC=librechat
PYCTR=litellm

QUESTIONS=("$@")
[ ${#QUESTIONS[@]} -gt 0 ] || QUESTIONS=(
  "عندنا DS-7608NXI-K1؟ ومواصفاته ايه؟"
  "رشحلي كاميرا خارجية 4 ميجا بمايك"
  "ايه مواصفات DS-2CD1043G2-LIUF؟"
)

# the parts of librechat.yaml the test needs, as JSON (PyYAML lives in the litellm container)
CONF=$(docker exec -i "$PYCTR" python3 -c '
import sys, yaml, json
c = yaml.safe_load(sys.stdin)
ep = [e for e in c["endpoints"]["custom"] if e.get("name") == "FastEgy AI"][0]
specs = {s["name"]: {"label": s.get("label", s["name"]), "model": s["preset"]["model"],
                     "promptPrefix": s["preset"].get("promptPrefix") or "",
                     "temperature": s["preset"].get("temperature"), "webSearch": bool(s.get("webSearch"))}
         for s in c["modelSpecs"]["list"] if s.get("name") in ("fastegy-strong", "fastegy-fast")}
print(json.dumps({"apiKey": ep["apiKey"], "baseURL": ep["baseURL"], "specs": specs}))' <"$CFG")

JS=$(cat <<'JS'
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
const cfg = input.conf;
const READER = process.env.READER_URL || "http://fastegy-reader:3002";
const MKEY = process.env.FIRECRAWL_API_KEY;
const key = /^\$\{(.+)\}$/.test(cfg.apiKey) ? process.env[cfg.apiKey.slice(2, -1)] : cfg.apiKey;
const SUFFIX = "_mcp_fastegy-products";                  // LibreChat's name for MCP tools
const today = new Date().toISOString().slice(0, 10);
let rid = 0;
const rpc = (method, params) => fetch(READER + "/mcp", { method: "POST",
  headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream", Authorization: "Bearer " + MKEY },
  body: JSON.stringify({ jsonrpc: "2.0", id: ++rid, method, params }) }).then(r => r.json());

async function runTool(name, args) {
  if (name.endsWith(SUFFIX)) {
    const r = await rpc("tools/call", { name: name.slice(0, -SUFFIX.length), arguments: args });
    return r.result ? r.result.content[0].text : "tool error: " + JSON.stringify(r.error);
  }
  if (name === "web_search") {       // like LibreChat: search, then read the top pages through the reader
    const d = await (await fetch(READER + "/search?format=json&q=" + encodeURIComponent(args.query || ""))).json();
    const top = (d.results || []).slice(0, 3);
    const pages = await Promise.all(top.map(async x => {
      try {
        const r = await (await fetch(READER + "/v2/scrape", { method: "POST",
          headers: { "Content-Type": "application/json", Authorization: "Bearer " + MKEY },
          body: JSON.stringify({ url: x.url, timeout: 15000 }) })).json();
        return { title: x.title, url: x.url, content: r.success ? r.data.markdown.slice(0, 3000) : (x.content || "") };
      } catch (e) { return { title: x.title, url: x.url, content: x.content || "" }; }
    }));
    return JSON.stringify(pages) + "\nsources read: " + top.map(x => x.url).join(" ");
  }
  return "unknown tool " + name;
}

async function chat(spec, tools, question) {
  const messages = [{ role: "system", content: spec.promptPrefix.split("{{current_date}}").join(today) },
                    { role: "user", content: question }];
  const trace = [];
  for (let round = 0; round < 6; round++) {
    const body = { model: spec.model, messages, tools, max_tokens: 2000 };
    if (spec.temperature != null) body.temperature = spec.temperature;
    const r = await fetch(cfg.baseURL.replace(/\/$/, "") + "/chat/completions", { method: "POST",
      headers: { Authorization: "Bearer " + key, "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const fb = r.headers.get("x-litellm-attempted-fallbacks");
    const d = await r.json();
    if (!d.choices) return { trace, answer: "ERROR " + JSON.stringify(d.error || d).slice(0, 300) };
    if (fb && fb !== "0") trace.push("(answered by the fallback model)");
    const m = d.choices[0].message;
    if (m.tool_calls && m.tool_calls.length) {
      messages.push({ role: "assistant", content: m.content || null, tool_calls: m.tool_calls });
      for (const tc of m.tool_calls) {
        let args = {};
        try { args = JSON.parse(tc.function.arguments || "{}"); } catch (e) {}
        const out = await runTool(tc.function.name, args);
        const first = tc.function.name === "web_search" ? out.split("\n").pop() : out.split("\n")[0];
        trace.push(tc.function.name.replace(SUFFIX, "") + " " + JSON.stringify(args) + "  ->  " + first.slice(0, 160));
        messages.push({ role: "tool", tool_call_id: tc.id, content: out.slice(0, 12000) });
      }
      continue;
    }
    return { trace, answer: m.content || "(empty answer)" };
  }
  return { trace, answer: "(no final answer after 6 rounds)" };
}

(async () => {
  await rpc("initialize", { protocolVersion: "2025-03-26", capabilities: {}, clientInfo: { name: "chat-test", version: "1" } });
  const mcp = (await rpc("tools/list", {})).result.tools.map(t => ({ type: "function",
    function: { name: t.name + SUFFIX, description: t.description, parameters: t.inputSchema } }));
  const web = { type: "function", function: { name: "web_search", description: "Search the web for current information.",
    parameters: { type: "object", properties: { query: { type: "string" } }, required: ["query"] } } };
  const only = process.env.ONLY ? process.env.ONLY.split(",") : null;   // e.g. ONLY=fastegy-strong
  for (const name of ["fastegy-strong", "fastegy-fast"]) {
    if (only && !only.includes(name)) continue;
    const spec = cfg.specs[name];
    if (!spec) continue;
    const tools = spec.webSearch ? [...mcp, web] : mcp;
    for (const q of input.questions) {
      const t0 = Date.now();
      let res;
      try { res = await chat(spec, tools, q); } catch (e) { res = { trace: [], answer: "ERROR " + e.message }; }
      console.log("\n==================================================================");
      console.log(spec.label + "  |  " + q + "  |  " + ((Date.now() - t0) / 1000).toFixed(1) + " s");
      console.log("------------------------------------------------------------------");
      res.trace.forEach(l => console.log("  tool: " + l));
      console.log(res.answer);
    }
  }
})().catch(e => { console.log("test error: " + e.message); process.exit(2); });
JS
)

CONF="$CONF" python3 -c 'import json,os,sys; print(json.dumps({"conf": json.loads(os.environ["CONF"]), "questions": sys.argv[1:]}))' "${QUESTIONS[@]}" |
  docker exec -i -e ONLY="${ONLY:-}" "$LC" node -e "$JS"
