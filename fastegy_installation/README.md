# موديول التركيبات — نسخة 6.9.280 (مشفّرة)

الملف ده مشفّر عشان المستودع عام. الباسورد اتبعت لصاحب الشركة لوحده، ومش موجود هنا (نفس باسورد نسخ 277 و278 و279).

`fastegy_v280.tar.enc`

بصمة الملف المشفّر:

`0d37391e7db2766b102442eb36011658`

جواه 3 ملفات:

- `fastegy_installation_v6.9.280.tar.xz`
- `deploy_v280.sh`
- `test_v280.sh`

---

## التشغيل على شيل الستيجنج

```bash
cd ~/src/user/private_addons/fastegy_projectx
curl -fsSL -o fastegy_v280.tar.enc https://raw.githubusercontent.com/mohamedfastegy/odoo/claude/fastegy-installation-v280/fastegy_installation/fastegy_v280.tar.enc
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in fastegy_v280.tar.enc -out fastegy_v280.tar && tar -xf fastegy_v280.tar && rm -f fastegy_v280.tar.enc fastegy_v280.tar
bash deploy_v280.sh
```

الأمر التالت هيطلب الباسورد: الصقه ودوس Enter (مش هيظهر وانت بتكتبه).

لو الباسورد غلط هيطلع خطأ ومش هيطلّع أي ملف — جرّب تاني.

سكريبت النشر بيتأكد من سلامة النسخة قبل ما يلمس أي حاجة، وبعدها بيشغّل الاختبار (مابيحفظش حاجة).

لو التحديث فشل عشان مهمة مجدولة كانت شغالة في نفس اللحظة، السكريبت بيرجّع النسخة القديمة ويعيد لوحده (لحد ٣ محاولات).
