# موديول التركيبات — نسخة 6.9.282 (مشفّرة)

الملف ده مشفّر عشان المستودع عام. الباسورد اتبعت لصاحب الشركة لوحده، ومش موجود هنا (نفس باسورد نسخ 277 لحد 281).

`fastegy_v282.tar.enc`

بصمة الملف المشفّر:

`87aafa245cf21ce4f0af9d272d0a1020`

جواه 3 ملفات:

- `fastegy_installation_v6.9.282.tar.xz`
- `deploy_v282.sh`
- `test_v282.sh`

---

## التشغيل على شيل الستيجنج

```bash
cd ~/src/user/private_addons/fastegy_projectx
curl -fsSL -o fastegy_v282.tar.enc https://raw.githubusercontent.com/mohamedfastegy/odoo/claude/fastegy-installation-v282/fastegy_installation/fastegy_v282.tar.enc
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in fastegy_v282.tar.enc -out fastegy_v282.tar && tar -xf fastegy_v282.tar && rm -f fastegy_v282.tar.enc fastegy_v282.tar
bash deploy_v282.sh
```

الأمر التالت هيطلب الباسورد: الصقه ودوس Enter (مش هيظهر وانت بتكتبه).

لو الباسورد غلط هيطلع خطأ ومش هيطلّع أي ملف — جرّب تاني.

سكريبت النشر بيتأكد من سلامة النسخة قبل ما يلمس أي حاجة، وبعدها بيشغّل الاختبار (مابيحفظش حاجة).

لو التحديث فشل عشان مهمة مجدولة كانت شغالة في نفس اللحظة، السكريبت بيرجّع النسخة القديمة ويعيد لوحده (لحد ٣ محاولات).
