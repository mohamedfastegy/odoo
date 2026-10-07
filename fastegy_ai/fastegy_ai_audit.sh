#!/usr/bin/env bash
# =============================================================================
# FastEgy AI (LibreChat on the VPS) — full read-only audit
#
# Purpose : Collect a complete, REDACTED picture of the LibreChat install
#           (containers, config, models, agents, knowledge files, vector DB,
#           usage, errors) so the knowledge-base (RAG) plan is built on facts.
# Safety  : READ-ONLY. Restarts nothing, changes nothing, writes only the
#           report file. Every line of the report passes through redact()
#           (API keys, passwords, tokens, URL credentials are masked).
# Usage   : sudo bash fastegy_ai_audit.sh            -> ~/fastegy_ai_audit_<date>.txt
#           sudo bash fastegy_ai_audit.sh /path/out.txt
# Version : 1.0 — 2026-10-07
# =============================================================================

set -u
OUT="${1:-$HOME/fastegy_ai_audit_$(date +%Y%m%d_%H%M).txt}"
T=25   # timeout (seconds) for each potentially slow command

if ! command -v perl >/dev/null 2>&1; then
  echo "perl is required for redaction (apt install perl-base). Aborting." >&2; exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Cannot talk to Docker. Run as root (sudo) or as a user in the docker group." >&2; exit 1
fi

# --- Mask secrets in everything that reaches the report ----------------------
redact() {
  perl -pe '
    # key: value / KEY=value  (keeps ${VAR} references, user_provided, numbers, <set ...> markers)
    s/((?:api[_-]?key|apikey|secret|password|passwd|token|credential|private[_-]?key|salt|creds_(?:key|iv)|master_key)\w*["\x27]?\s*[:=]\s*["\x27]?)(?!\$\{|<|user_provided|[0-9.]+["\x27]?\s*(?:#.*)?$)([^"\x27\s#,}]+)/$1***REDACTED***/ig;
    # credentials inside URLs  scheme://user:pass@host
    s{(\b[a-z][a-z0-9+.-]*://[^:/\s@]+:)[^@\s/]+@}{$1***@}ig;
    # well-known key formats
    s/\bsk-[A-Za-z0-9_-]{10,}/sk-***REDACTED***/g;
    s/\bAIza[0-9A-Za-z_-]{20,}/AIza***REDACTED***/g;
    s/\b(gsk_|xai-|pplx-|tvly-|hf_|jina_|fc-|ghp_|gho_|github_pat_)[A-Za-z0-9_-]{16,}/$1***REDACTED***/g;
    s/\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9._-]+/eyJ***REDACTED***/g;
    s/(Bearer\s+)[A-Za-z0-9._~+\/=-]{8,}/$1***REDACTED***/ig;
  '
}

section() { printf '\n\n==================== %s ====================\n' "$1"; }
run()     { timeout "$T" "$@" 2>&1 || echo "[command failed or timed out: exit $?]"; }

# .env style lines on stdin -> NAME=value only for known-safe names, otherwise <set, N chars>/<empty>
env_summary() {
  local sensitive='(KEY|SECRET|PASSWORD|PASSWD|TOKEN|SALT|CREDS|PRIVATE|_IV$|URI$)'
  local safe='^(HOST|PORT|DOMAIN_CLIENT|DOMAIN_SERVER|NO_INDEX|TRUST_PROXY|ENDPOINTS|CONFIG_PATH|DEBUG_LOGGING|DEBUG_CONSOLE|CONSOLE_JSON|SEARCH|MEILI_HOST|MEILI_NO_ANALYTICS|RAG_PORT|RAG_API_URL|RAG_OPENAI_BASEURL|RAG_USE_FULL_CONTEXT|EMBEDDINGS_PROVIDER|EMBEDDINGS_MODEL|CHUNK_SIZE|CHUNK_OVERLAP|PDF_EXTRACT_IMAGES|COLLECTION_NAME|VECTOR_DB_TYPE|DB_HOST|DB_PORT|POSTGRES_DB|OLLAMA_BASE_URL|ALLOW_[A-Z_]+|SESSION_EXPIRY|REFRESH_TOKEN_EXPIRY|APP_TITLE|CUSTOM_FOOTER|HELP_AND_FAQ_URL|TITLE_CONVO|CHECK_BALANCE|LIMIT_[A-Z_]+|[A-Z_]*_MODELS?|[A-Z_]*_BASEURL|[A-Z_]*_BASE_URL|UID|GID|NODE_ENV|TZ)$'
  local line name val
  grep -E '^[[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*=' | while IFS= read -r line; do
    line="${line#"${line%%[![:space:]]*}"}"; line="${line#export }"
    name="${line%%=*}"; val="${line#*=}"
    if [[ "$name" =~ $sensitive && ! "$name" =~ EXPIRY$ ]]; then
      if [ "$val" = user_provided ]; then echo "$name=user_provided"
      elif [ -z "$val" ] || [ "$val" = '""' ] || [ "$val" = "''" ]; then echo "$name=<empty>"; else echo "$name=<set, ${#val} chars>"; fi
    elif [[ "$name" =~ $safe ]]; then
      echo "$name=$val"
    elif [ -z "$val" ] || [ "$val" = '""' ] || [ "$val" = "''" ]; then
      echo "$name=<empty>"
    else
      echo "$name=<set, ${#val} chars>"
    fi
  done
}

# first running container whose image matches $1 (and not $2)
find_ctr() {
  docker ps --format '{{.Names}}\t{{.Image}}' |
    awk -F'\t' -v re="$1" -v ex="${2:-^$}" 'tolower($2) ~ re && tolower($2) !~ ex {print $1; exit}'
}
ctr_env() { docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$1" 2>/dev/null; }
ctr_env_get() { ctr_env "$1" | sed -n "s/^$2=//p" | head -n1; }

API=$(find_ctr 'librechat' 'rag|admin|meili|mongo|pgvector')
[ -z "$API" ] && API=$(docker ps --format '{{.Names}}' | grep -ixE 'librechat|librechat-api' | head -n1)
RAG=$(find_ctr 'rag-api|rag_api')
MONGO=$(find_ctr '(^|/)mongo(:|@|$)')
[ -z "$MONGO" ] && MONGO=$(docker ps --format '{{.Names}}' | grep -iE 'mongo' | head -n1)
VDB=$(find_ctr 'pgvector')
[ -z "$VDB" ] && VDB=$(docker ps --format '{{.Names}}' | grep -iE 'vectordb' | head -n1)
MEILI=$(find_ctr 'meilisearch')
WD=""
[ -n "$API" ] && WD=$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$API" 2>/dev/null)

main() {
  echo "FastEgy AI audit — $(date -Is) — host $(hostname)"
  echo "Detected: api=${API:-NONE} rag_api=${RAG:-NONE} mongo=${MONGO:-NONE} vectordb=${VDB:-NONE} meili=${MEILI:-NONE}"
  echo "Compose working dir: ${WD:-unknown}"

  # ---------------------------------------------------------------- host
  section "1. HOST RESOURCES"
  grep -E '^(PRETTY_NAME|VERSION_ID)=' /etc/os-release 2>/dev/null
  uname -r; uptime
  echo "CPU cores: $(nproc)"
  free -h
  df -h / 2>/dev/null
  command -v nvidia-smi >/dev/null && nvidia-smi -L
  docker --version; docker compose version 2>/dev/null

  section "2. ALL CONTAINERS ON THE VPS"
  docker ps -a --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
  echo
  run docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}'
  echo
  run docker system df

  section "3. LISTENING PORTS / REVERSE PROXY"
  run ss -tlnp
  if [ -d /etc/nginx ]; then echo "-- nginx server_name / proxy_pass:"; grep -RhoE '^\s*(server_name|proxy_pass)\s+[^;]+' /etc/nginx/sites-enabled /etc/nginx/conf.d 2>/dev/null | sort -u; fi
  [ -f /etc/caddy/Caddyfile ] && { echo "-- Caddyfile:"; cat /etc/caddy/Caddyfile; }
  docker ps --format '{{.Names}} {{.Image}}' | grep -iE 'traefik|caddy|nginx|npm|proxy' || true

  if [ -z "$API" ]; then
    section "LibreChat container NOT FOUND"
    echo "-- non-docker installs?"; command -v pm2 >/dev/null && run pm2 list
    systemctl list-units --type=service 2>/dev/null | grep -iE 'libre|chat' || true
    find / -xdev -maxdepth 4 -name 'librechat.y*ml' 2>/dev/null | head
    return
  fi

  # ---------------------------------------------------------------- versions
  section "4. LIBRECHAT VERSION & IMAGES"
  docker exec "$API" sh -c 'grep -m1 "\"version\"" /app/package.json' 2>/dev/null
  for c in "$API" "$RAG" "$MONGO" "$VDB" "$MEILI"; do
    [ -n "$c" ] && docker inspect -f "$c: {{.Config.Image}} | started {{.State.StartedAt}} | restarts {{.RestartCount}}" "$c"
  done

  # ---------------------------------------------------------------- compose
  section "5. COMPOSE FILES"
  files=$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' "$API" 2>/dev/null)
  echo "config_files label: ${files:-none}"
  ls -la "$WD" 2>/dev/null
  # label files + any override next to them, each printed once
  { tr ',' '\n' <<<"$files"; ls "$WD"/docker-compose.override.y*ml 2>/dev/null; } | awk 'NF && !seen[$0]++' |
  while IFS= read -r f; do
    [ -f "$f" ] || continue
    echo; echo "------ $f"; cat "$f"
  done

  # ---------------------------------------------------------------- librechat.yaml
  section "6. librechat.yaml (in effect inside the container)"
  cfg=$(ctr_env_get "$API" CONFIG_PATH); cfg=${cfg:-/app/librechat.yaml}
  echo "CONFIG_PATH=$cfg"
  case "$cfg" in
    http*) echo "Config is loaded from a URL — fetch it separately." ;;
    *) docker exec "$API" cat "$cfg" 2>/dev/null || { echo "(not readable in container, trying host)"; cat "$WD/librechat.yaml" 2>/dev/null; } ;;
  esac

  # ---------------------------------------------------------------- env
  section "7. .env (names; values only for non-secret settings)"
  if [ -f "$WD/.env" ]; then env_summary <"$WD/.env"; else echo "no $WD/.env"; fi
  echo; echo "-- api container env:"; ctr_env "$API" | env_summary
  if [ -n "$RAG" ]; then echo; echo "-- rag_api container env:"; ctr_env "$RAG" | env_summary; fi

  # ---------------------------------------------------------------- RAG API
  section "8. RAG API (file_search / knowledge)"
  if [ -n "$RAG" ]; then
    port=$(ctr_env_get "$RAG" RAG_PORT); port=${port:-8000}
    echo "-- /health:"
    run docker exec "$RAG" python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:$port/health',timeout=10).read().decode())"
    echo "-- recent errors (72h):"
    docker logs --since 72h "$RAG" 2>&1 | grep -iE 'error|exception|traceback|fail' | tail -n 40 | cut -c1-400
  else
    echo "rag_api container NOT running -> agents cannot use file_search (knowledge files)."
  fi

  # ---------------------------------------------------------------- vector DB
  section "9. VECTOR DB (pgvector)"
  if [ -n "$VDB" ]; then
    timeout "$T" docker exec -i "$VDB" sh -c 'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f -' 2>&1 <<'SQL'
\echo '-- extensions'
\dx
\echo '-- tables'
\dt+
\echo '-- collections'
SELECT c.name AS collection, count(e.*) AS chunks
FROM langchain_pg_collection c LEFT JOIN langchain_pg_embedding e ON e.collection_id = c.uuid
GROUP BY c.name;
\echo '-- totals'
SELECT count(DISTINCT cmetadata->>'file_id') AS files, count(*) AS chunks,
       pg_size_pretty(pg_total_relation_size('langchain_pg_embedding')) AS size
FROM langchain_pg_embedding;
SELECT vector_dims(embedding) AS embedding_dims FROM langchain_pg_embedding LIMIT 1;
\echo '-- biggest documents (by chunks)'
SELECT regexp_replace(cmetadata->>'source', '^.*/', '') AS document, count(*) AS chunks
FROM langchain_pg_embedding GROUP BY 1 ORDER BY 2 DESC LIMIT 30;
SQL
  else
    echo "vectordb container NOT running."
  fi

  # ---------------------------------------------------------------- MongoDB
  section "10. MONGODB — agents, knowledge files, usage"
  if [ -n "$MONGO" ]; then
    muri=$(ctr_env_get "$API" MONGO_URI); muri=${muri:-mongodb://mongodb:27017/LibreChat}
    case "$muri" in
      mongodb+srv://*) echo "MONGO_URI points to an external cluster (Atlas) — skipped." ;;
      *)
        local_uri=$(printf '%s' "$muri" | sed -E 's#^(mongodb://([^@/]*@)?)[^/?]+#\1127.0.0.1:27017#')
        shell=$(docker exec "$MONGO" sh -c 'command -v mongosh || command -v mongo' 2>/dev/null)
        if [ -z "$shell" ]; then echo "no mongosh/mongo shell in $MONGO"; else
          timeout 90 docker exec "$MONGO" "$shell" --quiet "$local_uri" --eval "$MONGO_JS" 2>&1
        fi ;;
    esac
  fi

  # ---------------------------------------------------------------- Ollama
  section "11. LOCAL MODELS (Ollama)"
  docker ps --format '{{.Names}} {{.Image}}' | grep -i ollama || echo "no ollama container"
  if command -v curl >/dev/null; then curl -sS -m 5 http://127.0.0.1:11434/api/tags 2>&1 | head -c 2000; echo; fi

  # ---------------------------------------------------------------- storage
  section "12. STORAGE USED BY LIBRECHAT"
  for d in uploads images logs data-node meili_data*; do
    for p in "$WD"/$d; do [ -e "$p" ] && du -sh "$p" 2>/dev/null; done
  done

  # ---------------------------------------------------------------- logs
  section "13. LIBRECHAT ERRORS / WARNINGS (last 72h, max 80 lines)"
  docker logs --since 72h "$API" 2>&1 | grep -iE 'error|warn' | tail -n 80 | cut -c1-400

  section "END"
}

# Mongo report script (works with mongosh and the legacy mongo shell)
read -r -d '' MONGO_JS <<'JS'
function j(o){ print(JSON.stringify(o)); }
function cnt(c,q){ try { return db.getCollection(c).countDocuments(q||{}); } catch(e){ return 'n/a'; } }
print('-- collections (estimated docs)');
db.getCollectionNames().sort().forEach(function(c){ print('  ' + c + ': ' + db.getCollection(c).estimatedDocumentCount()); });

print('\n-- users by role');
db.users.aggregate([{$group:{_id:'$role', n:{$sum:1}}}]).forEach(j);

print('\n-- agents (newest first)');
db.agents.find({}, {id:1,name:1,description:1,provider:1,model:1,tools:1,tool_resources:1,category:1,is_promoted:1,updatedAt:1,instructions:1})
  .sort({updatedAt:-1}).forEach(function(a){
    var tr = a.tool_resources || {}, files = {};
    Object.keys(tr).forEach(function(k){ files[k] = ((tr[k] && tr[k].file_ids) || []).length; });
    j({id:a.id, name:a.name, provider:a.provider, model:a.model, tools:a.tools, files:files,
       category:a.category, promoted:a.is_promoted, updatedAt:a.updatedAt,
       instructions_chars:(a.instructions||'').length});
    print('   instructions: ' + (a.instructions||'').slice(0,1200).replace(/\n/g,' | '));
  });

print('\n-- files grouped by context / source / embedded');
db.files.aggregate([
  {$group:{_id:{context:'$context', source:'$source', embedded:'$embedded'}, n:{$sum:1}, bytes:{$sum:'$bytes'}}},
  {$sort:{n:-1}}, {$limit:40}
]).forEach(function(r){ var o=r._id; o.n=r.n; o.mb=Math.round((r.bytes||0)/10485.76)/100; j(o); });

print('\n-- knowledge files uploaded to agents (latest 150)');
db.files.find({context:'agents'}, {_id:0,filename:1,bytes:1,embedded:1,type:1,createdAt:1})
  .sort({createdAt:-1}).limit(150).forEach(j);

var since = new Date(Date.now() - 30*24*3600*1000);
print('\n-- last 30 days');
var active = 'n/a'; try { active = db.messages.distinct('user', {createdAt:{$gte:since}}).length; } catch(e) {}
j({conversations:cnt('conversations',{updatedAt:{$gte:since}}), messages:cnt('messages',{createdAt:{$gte:since}}),
   active_users:active, thumbs_up:cnt('messages',{'feedback.rating':'thumbsUp'}), thumbs_down:cnt('messages',{'feedback.rating':'thumbsDown'})});

print('\n-- AI replies by endpoint / model (30d)');
db.messages.aggregate([
  {$match:{createdAt:{$gte:since}, isCreatedByUser:false}},
  {$group:{_id:{endpoint:'$endpoint', model:'$model'}, n:{$sum:1}}},
  {$sort:{n:-1}}, {$limit:25}], {allowDiskUse:true}).forEach(j);

print('\n-- conversations by model spec / agent (30d)');
db.conversations.aggregate([
  {$match:{updatedAt:{$gte:since}}},
  {$group:{_id:{spec:'$spec', endpoint:'$endpoint', agent:'$agent_id'}, n:{$sum:1}}},
  {$sort:{n:-1}}, {$limit:25}]).forEach(j);
JS

echo "Running FastEgy AI audit (1-2 minutes)..." >&2
{ main; } 2>&1 | redact >"$OUT"
chmod 600 "$OUT" 2>/dev/null
echo "Done. Report: $OUT ($(wc -l <"$OUT") lines)"
echo "Skim it before sharing; secrets are masked automatically."
