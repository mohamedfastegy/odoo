#!/usr/bin/env bash
# =============================================================================
# FastEgy AI — knowledge base 15: "متوفر" and DORI
#
# The test after ds_6 showed two slips the tool notes alone don't cover:
#   - the fast option wrote "أقرب كود متوفر" for a near match ("متوفر" reads
#     as in stock);
#   - the smart option, reading a datasheet from the web, turned DORI's
#     D/O/R/I into colours ("دائم/أحمر/أخضر/أزرق").
# Change : the stock rule now gives the words to use ("موجود في قائمة
#          منتجاتنا", "أقرب كود عندنا") and bans "متوفر"/"متاح" in any
#          sentence; a new rule spells out DORI (Detect, Observe, Recognize,
#          Identify: distances). The other rules stay. LibreChat restarts
#          (~30 s); rollback on failure.
# Run    : bash kb_15_rules.sh
# Version: 1.0 — 2026-10-09 (same steps as kb_12)
# =============================================================================
set -euo pipefail

LC=librechat
PYCTR=litellm
CFG=/root/librechat/librechat.yaml
TS=$(date +%Y%m%d_%H%M%S)

for c in "$LC" "$PYCTR"; do
  [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = true ] || { echo "$c is not running; nothing changed."; exit 1; }
done
BAK="$CFG.bak.$TS"
cp -p "$CFG" "$BAK"
PY_CFG=$(cat <<'PY'
import copy, sys, yaml
FINAL_RULES = [
    '- لو السؤال فيه كود موديل هيك فيجن أو إزي فيز: استخدم أداة lookup_product الأول، قبل البحث في النت.',
    '- الأداة بترد من مصدرين: قائمة منتجات FastEgy من Odoo (يعني إحنا بنبيع الموديل ده، ووصفنا ليه بالعربي)، وكتالوج هيك فيجن (مواصفات رسمية). اذكر المصدر زي ما الأداة كاتباه.',
    '- وجود الموديل في قائمة منتجات FastEgy معناه إننا بنبيعه، مش إنه في المخزن. اكتبها "موجود في قائمة منتجاتنا"، وللأكواد القريبة "أقرب كود عندنا". ولو مش في القائمة قول "مش في قائمة منتجاتنا" بس، وما تقولش إننا مش بنبيعه. ما تتكلمش عن مخزون ولا سعر: ممنوع كلمة "متوفر" أو "متاح" في أي جملة (حتى "أقرب كود متوفر")، وممنوع "سعره مناسب" أو "بسعر منافس" أو "أرخص"، وما تعرضش إنك تتحقق من السعر أو المخزون. وما تدوّرش في النت على سعر أي منتج: لو حد سأل "بكام" قول إن الأسعار عند فريق المبيعات.',
    '- لو رجعت NO EXACT MATCH أو NOT FOUND: قول إن الكود ده بالظبط مش في قائمتنا ولا في الكتالوج، واعرض الأكواد القريبة. ولو معاك أداة بحث في النت: دوّر على ملف المواصفات الرسمي (datasheet) للكود ده بالظبط، ولو لقيته هات مواصفاته منه واذكر الرابط. ما تدّيش مواصفات من كود قريب.',
    '- لو محتاج مواصفة مش موجودة: دوّر على ملف المواصفات الرسمي (datasheet) للكود نفسه بالظبط، وما تاخدش مواصفات من موديل قريب (زي اللي آخره /SL أو F أو من غيرها).',
    '- لما تجيب مواصفات من ملف أو صفحة: اكتب بس القيم المكتوبة فيها بالظبط، ولو قيمة مش موجودة قول "مش مذكورة في الملف"، وما تكمّلش من عندك (زي PoE أو القدرة بالوات).',
    '- DORI في مواصفات الكاميرات مسافات، مش ألوان: D = Detect (اكتشاف)، O = Observe (ملاحظة)، R = Recognize (تمييز)، I = Identify (تحديد الهوية). اكتبها كده، وما تترجمش الحروف لحاجة تانية.',
    '- للترشيح أو المقارنة: استخدم search_catalog، ورشّح من المنتجات اللي في قائمة FastEgy الأول.',
    '- لو المصدرين اختلفوا في رقم (زي سعة الهارد): اعتمد على كتالوج هيك فيجن، وقول إن وصف FastEgy مكتوب فيه رقم مختلف.',
    '- ما تضيفش مميزات ولا مقارنات مش مكتوبة في نتيجة الأداة، وانسب كل معلومة لمصدرها الصح.',
    '- التنسيق: ما تستخدمش أي وسوم HTML زي <br> (بتظهر للمستخدم كنص). في الجدول خلي كل خانة سطر واحد قصير وافصل بين المميزات بفاصلة، ولو المميزات كتير استخدم قائمة نقط بدل الجدول.',
    '- خلي الرد مختصر ومرتب: أول سطر الخلاصة أو الترشيح، بعدين التفاصيل، وما تكررش نفس الملاحظة في أكتر من مكان.',
]
PAST_RULES = [
    '- لو السؤال فيه كود موديل هيك فيجن أو إزي فيز: استخدم أداة lookup_product الأول، قبل البحث في النت.',
    '- الأداة بترد من مصدرين: قائمة منتجات FastEgy من Odoo (يعني إحنا بنبيع الموديل ده، ووصفنا ليه بالعربي)، وكتالوج هيك فيجن (مواصفات رسمية). اذكر المصدر زي ما الأداة كاتباه.',
    '- وجود الموديل في قائمة منتجات FastEgy معناه إننا بنبيعه، مش إنه متوفر في المخزن. ولو مش في القائمة قول "مش في قائمة منتجاتنا" بس، وما تقولش إننا مش بنبيعه. ما تتكلمش عن مخزون ولا سعر ولا توفر: ممنوع جمل زي "متوفر" أو "متاح للطلب" أو "سعره مناسب" أو "بسعر منافس" أو "أرخص"، وما تعرضش إنك تتحقق من السعر أو المخزون. وما تدوّرش في النت على سعر أي منتج: لو حد سأل "بكام" قول إن الأسعار عند فريق المبيعات.',
    '- لو رجعت NO EXACT MATCH أو NOT FOUND: قول إن الكود ده بالظبط مش في قائمتنا ولا في الكتالوج، واعرض الأكواد القريبة. ولو معاك أداة بحث في النت: دوّر على ملف المواصفات الرسمي (datasheet) للكود ده بالظبط، ولو لقيته هات مواصفاته منه واذكر الرابط. ما تدّيش مواصفات من كود قريب.',
    '- لو محتاج مواصفة مش موجودة: دوّر على ملف المواصفات الرسمي (datasheet) للكود نفسه بالظبط، وما تاخدش مواصفات من موديل قريب (زي اللي آخره /SL أو F أو من غيرها).',
    '- لما تجيب مواصفات من ملف أو صفحة: اكتب بس القيم المكتوبة فيها بالظبط، ولو قيمة مش موجودة قول "مش مذكورة في الملف"، وما تكمّلش من عندك (زي PoE أو القدرة بالوات).',
    '- للترشيح أو المقارنة: استخدم search_catalog، ورشّح من المنتجات اللي في قائمة FastEgy الأول.',
    '- لو المصدرين اختلفوا في رقم (زي سعة الهارد): اعتمد على كتالوج هيك فيجن، وقول إن وصف FastEgy مكتوب فيه رقم مختلف.',
    '- ما تضيفش مميزات ولا مقارنات مش مكتوبة في نتيجة الأداة، وانسب كل معلومة لمصدرها الصح.',
    '- التنسيق: ما تستخدمش أي وسوم HTML زي <br> (بتظهر للمستخدم كنص). في الجدول خلي كل خانة سطر واحد قصير وافصل بين المميزات بفاصلة، ولو المميزات كتير استخدم قائمة نقط بدل الجدول.',
    '- خلي الرد مختصر ومرتب: أول سطر الخلاصة أو الترشيح، بعدين التفاصيل، وما تكررش نفس الملاحظة في أكتر من مكان.',
    '- لو الأداة رجعت EXACT MATCH: اعتمد على المواصفات اللي فيها، وقول إن المصدر كتالوج هيك فيجن للمنتجات الأكتر مبيعًا.',
    '- لو رجعت NO EXACT MATCH أو NOT FOUND: اعرض الأكواد القريبة واسأل المستخدم يقصد أنهي، وما تدّيش مواصفات لكود ما اتطابقش.',
    '- لو محتاج مواصفة مش في الكتالوج: دوّر على ملف المواصفات الرسمي (datasheet) للكود نفسه بالظبط، وما تاخدش مواصفات من موديل قريب (زي اللي آخره /SL أو F أو من غيرها).',
    '- وجود الموديل في قائمة منتجات FastEgy معناه إننا بنبيعه، مش إنه متوفر في المخزن. ما تقولش سعر ولا كمية ولا إنه متاح للتسليم.',
    '- وجود الموديل في قائمة منتجات FastEgy معناه إننا بنبيعه، مش إنه متوفر في المخزن. ولو مش في القائمة قول "مش في قائمة منتجاتنا" بس، وما تقولش إننا مش بنبيعه. ما تتكلمش عن مخزون ولا سعر ولا توفر.',
    '- وجود الموديل في قائمة منتجات FastEgy معناه إننا بنبيعه، مش إنه متوفر في المخزن. ولو مش في القائمة قول "مش في قائمة منتجاتنا" بس، وما تقولش إننا مش بنبيعه. ما تتكلمش عن مخزون ولا سعر ولا توفر: ممنوع جمل زي "متوفر" أو "متاح للطلب" أو "سعره مناسب" أو "أرخص"، وما تعرضش إنك تتحقق من السعر أو المخزون، وقول إن ده عند فريق المبيعات.',
]
MARKS = ("lookup_product", "search_catalog")
old = yaml.safe_load(sys.stdin)
cfg = copy.deepcopy(old)
done = 0
for s in cfg["modelSpecs"]["list"]:
    if s.get("name") in ("fastegy-fast", "fastegy-strong"):
        p = s["preset"]["promptPrefix"]
        if "lookup_product" not in p:
            sys.exit("ERROR: the catalog rules are missing (run kb_2 first); nothing changed")
        lines = [l for l in p.split("\n")
                 if l not in FINAL_RULES and l not in PAST_RULES and not any(m in l for m in MARKS)]
        s["preset"]["promptPrefix"] = "\n".join(lines).rstrip() + "\n" + "\n".join(FINAL_RULES)
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
trap 'rm -f "$TMP_Y"' EXIT
docker exec -i "$PYCTR" python3 -c "$PY_CFG" <"$CFG" >"$TMP_Y" || { echo "Config rewrite failed; nothing changed."; exit 1; }
grep -q 'DORI في مواصفات الكاميرات' "$TMP_Y" && grep -q 'أقرب كود عندنا' "$TMP_Y" || { echo "Rewrite produced unexpected output; nothing changed."; exit 1; }
cat "$TMP_Y" >"$CFG"

rollback() {
  echo "!! $1 — rolling back librechat.yaml"
  cat "$BAK" >"$CFG"; docker restart "$LC" >/dev/null
  echo "Restored $BAK and restarted LibreChat."; exit 1
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
echo "DONE kb 15. Backup: $BAK"
echo "Check: bash /root/chat_test.sh   (or: bash /root/chat_test.sh \"your question\")"
