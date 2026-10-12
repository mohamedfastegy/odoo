# موديول التركيبات — نسخة 6.9.279 (مشفّرة)

الملف ده مشفّر عشان المستودع عام. الباسورد اتبعت لصاحب الشركة لوحده، ومش موجود هنا (نفس باسورد نسخة 6.9.277 و6.9.278).

`fastegy_v279.tar.enc`

بصمة الملف المشفّر:

`be902a74c6d5a8313bb571677d1f692b`

جواه 3 ملفات:

- `fastegy_installation_v6.9.279.tar.xz`
- `deploy_v279.sh`
- `test_v279.sh`

---

## التشغيل على شيل الستيجنج

```bash
cd ~/src/user/private_addons/fastegy_projectx
curl -fsSL -o fastegy_v279.tar.enc https://raw.githubusercontent.com/mohamedfastegy/odoo/claude/fastegy-installation-v279/fastegy_installation/fastegy_v279.tar.enc
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in fastegy_v279.tar.enc -out fastegy_v279.tar && tar -xf fastegy_v279.tar && rm -f fastegy_v279.tar.enc fastegy_v279.tar
bash deploy_v279.sh
```

الأمر التالت هيطلب الباسورد: الصقه ودوس Enter (مش هيظهر وانت بتكتبه).

لو الباسورد غلط هيطلع خطأ ومش هيطلّع أي ملف — جرّب تاني.

سكريبت النشر بيتأكد من سلامة النسخة قبل ما يلمس أي حاجة، وبعدها بيشغّل الاختبار (مابيحفظش حاجة).

لو التحديث فشل عشان مهمة مجدولة كانت شغالة في نفس اللحظة، السكريبت بيرجّع النسخة القديمة ويعيد لوحده (لحد ٣ محاولات).
