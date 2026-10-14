# موديول التركيبات — نسخة 6.9.281 (مشفّرة)

الملف ده مشفّر عشان المستودع عام. الباسورد اتبعت لصاحب الشركة لوحده، ومش موجود هنا (نفس باسورد نسخ 277 لحد 280).

`fastegy_v281.tar.enc`

بصمة الملف المشفّر:

`2cb7d0cd2e3d175c2a4dc1f5d70fc5b2`

جواه 3 ملفات:

- `fastegy_installation_v6.9.281.tar.xz`
- `deploy_v281.sh`
- `test_v281.sh`

---

## التشغيل على شيل الستيجنج

```bash
cd ~/src/user/private_addons/fastegy_projectx
curl -fsSL -o fastegy_v281.tar.enc https://raw.githubusercontent.com/mohamedfastegy/odoo/claude/fastegy-installation-v281/fastegy_installation/fastegy_v281.tar.enc
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in fastegy_v281.tar.enc -out fastegy_v281.tar && tar -xf fastegy_v281.tar && rm -f fastegy_v281.tar.enc fastegy_v281.tar
bash deploy_v281.sh
```

الأمر التالت هيطلب الباسورد: الصقه ودوس Enter (مش هيظهر وانت بتكتبه).

لو الباسورد غلط هيطلع خطأ ومش هيطلّع أي ملف — جرّب تاني.

سكريبت النشر بيتأكد من سلامة النسخة قبل ما يلمس أي حاجة، وبعدها بيشغّل الاختبار (مابيحفظش حاجة).

لو التحديث فشل عشان مهمة مجدولة كانت شغالة في نفس اللحظة، السكريبت بيرجّع النسخة القديمة ويعيد لوحده (لحد ٣ محاولات).
