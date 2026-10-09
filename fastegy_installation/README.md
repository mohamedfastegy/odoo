# موديول التركيبات — نسخة 6.9.277 (مشفّرة)

الملف ده مشفّر عشان المستودع عام. الباسورد اتبعت لصاحب الشركة لوحده، ومش موجود هنا.

`fastegy_v277.tar.enc`

بصمة الملف المشفّر:

`8d52e99d9f22a8030c7d524643be8f84`

جواه 3 ملفات:

- `fastegy_installation_v6.9.277.tar.xz`
- `deploy_v277.sh`
- `test_v277.sh`

---

## التشغيل على شيل الستيجنج

```bash
cd ~/src/user/private_addons/fastegy_projectx
curl -fsSL -o fastegy_v277.tar.enc https://raw.githubusercontent.com/mohamedfastegy/odoo/claude/fastegy-installation-v277/fastegy_installation/fastegy_v277.tar.enc
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in fastegy_v277.tar.enc -out fastegy_v277.tar && tar -xf fastegy_v277.tar && rm -f fastegy_v277.tar.enc fastegy_v277.tar
bash deploy_v277.sh
```

الأمر التالت هيطلب الباسورد: الصقه ودوس Enter (مش هيظهر وانت بتكتبه).

لو الباسورد غلط هيطلع خطأ ومش هيطلّع أي ملف — جرّب تاني.

سكريبت النشر بيتأكد من سلامة النسخة قبل ما يلمس أي حاجة، وبعدها بيشغّل الاختبار (مابيحفظش حاجة).

لو التحديث فشل عشان مهمة مجدولة كانت شغالة في نفس اللحظة، السكريبت بيرجّع النسخة القديمة ويعيد لوحده (لحد ٣ محاولات).
