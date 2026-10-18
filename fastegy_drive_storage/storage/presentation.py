"""Business folders are presentation only; blob keys and file IDs never change."""
from .policy import safe_name

# Relationships inspected on staging. Never guess a parent by a similar field name.
PARENTS = {
    "fastegy.installation.photo": ("task_id", "fastegy.installation"),
    "fastegy.installation.expense": ("installation_id", "fastegy.installation"),
    "fastegy.install.attendance": ("task_id", "fastegy.installation"),
    "fastegy.custody.expense": ("task_id", "fastegy.installation"),
    "fastegy.survey.camera.photo": ("survey_id", "fastegy.survey.response"),
    "fastegy.survey.response": ("task_id", "fastegy.installation"),
}
INSTALLATION_MODELS = set(PARENTS) | {"fastegy.installation"}


def resolve_anchor(record):
    """Follow audited single parents only. An absent parent stays independently filed."""
    current = record
    while current and current._name in PARENTS:
        field_name, target = PARENTS[current._name]
        field = current._fields.get(field_name)
        if not field or field.type != "many2one" or field.comodel_name != target:
            break
        parent = current[field_name].exists()
        if not parent:
            break
        if "company_id" in current._fields and "company_id" in parent._fields:
            if current.company_id and parent.company_id and current.company_id != parent.company_id:
                raise ValueError("Business folder parent belongs to a different company")
        current = parent
    return current


def category(model, mimetype, chatter=False):
    if model in {"fastegy.installation.expense", "fastegy.custody.expense"}:
        return "expenses", "المصروفات وإيصالاتها"
    if model == "fastegy.install.attendance":
        return "attendance", "الحضور"
    if chatter:
        return "chatter", "مرفقات الرسائل والملاحظات"
    if (mimetype or "").startswith("image/"):
        return "images", "صور التركيب" if model in INSTALLATION_MODELS else "الصور"
    if (mimetype or "").startswith("video/"):
        return "videos", "الفيديوهات"
    if (mimetype or "").startswith("audio/"):
        return "audio", "التسجيلات الصوتية"
    return "documents", "المستندات"


def business_folders(company_id, app_key, app_label, model, record_id, reference, bucket):
    # Explicit stable identities allow folder renaming without making duplicate folders.
    # Reuse the existing company partition so no second company folder is introduced.
    key, label = bucket
    return ["company_%s" % (company_id or 0),
        {"key": "business:v1:app:" + app_key, "name": safe_name(app_label)},
        {"key": "business:v1:record:%s:%s" % (model, record_id or 0),
         "name": safe_name(reference, "%s #%s" % (model, record_id or 0))},
        {"key": "business:v1:category:" + key, "name": label}]
