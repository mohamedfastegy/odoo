# موديول التركيبات — نسخة 6.9.278 (مشفّرة)

الملف ده مشفّر عشان المستودع عام. الباسورد اتبعت لصاحب الشركة لوحده، ومش موجود هنا (نفس باسورد نسخة 6.9.277).

`fastegy_v278.tar.enc`

بصمة الملف المشفّر:

`740beea73ce4d32cd2f4b0f659277afb`

جواه 3 ملفات:

- `fastegy_installation_v6.9.278.tar.xz`
- `deploy_v278.sh`
- `test_v278.sh`

---

## التشغيل على شيل الستيجنج

```bash
cd ~/src/user/private_addons/fastegy_projectx
curl -fsSL -o fastegy_v278.tar.enc https://raw.githubusercontent.com/mohamedfastegy/odoo/claude/fastegy-installation-v278/fastegy_installation/fastegy_v278.tar.enc
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in fastegy_v278.tar.enc -out fastegy_v278.tar && tar -xf fastegy_v278.tar && rm -f fastegy_v278.tar.enc fastegy_v278.tar
bash deploy_v278.sh
```

الأمر التالت هيطلب الباسورد: الصقه ودوس Enter (مش هيظهر وانت بتكتبه).

لو الباسورد غلط هيطلع خطأ ومش هيطلّع أي ملف — جرّب تاني.

سكريبت النشر بيتأكد من سلامة النسخة قبل ما يلمس أي حاجة، وبعدها بيشغّل الاختبار (مابيحفظش حاجة).

لو التحديث فشل عشان مهمة مجدولة كانت شغالة في نفس اللحظة، السكريبت بيرجّع النسخة القديمة ويعيد لوحده (لحد ٣ محاولات).
