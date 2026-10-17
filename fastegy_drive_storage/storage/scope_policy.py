"""Deterministic application discovery; no database, network or source execution."""
import hashlib
import json


PROTECTED_PREFIXES = ("ir.", "mail.", "bus.", "fs.", "fastegy.drive.")


def scope_key(model, field=""):
    return model + "/" + (field or "")


def discover_scope(module_names, descriptors):
    """Use definition provenance, never name prefixes or unrestricted relations."""
    selected = set(module_names)
    result = []
    for model in sorted(descriptors, key=lambda item: item["name"]):
        owned = model.get("owner") in selected
        relevant = [f for f in model.get("fields", [])
                    if owned or selected.intersection(f.get("modules", []))]
        if not owned and not relevant:
            continue
        blocked = (model["name"].startswith(PROTECTED_PREFIXES)
                   or model.get("abstract") or model.get("transient") or not model.get("auto", True))
        source = sorted(({model.get("owner")} if owned else set()).union(
            *(selected.intersection(f.get("modules", [])) for f in relevant)))
        result.append({"scope_key": scope_key(model["name"]), "model_name": model["name"],
            "field_name": "", "label": model.get("label") or model["name"],
            "source_modules": ", ".join(source), "kind": "model",
            "status": "excluded" if blocked else ("ready" if owned else "shared"),
            "reason": "system_or_temporary" if blocked else ("owned_model" if owned else "shared_model")})
        for field in sorted(relevant, key=lambda item: item["name"]):
            if blocked:
                status, reason = "excluded", "system_or_temporary"
            elif field.get("inherited"):
                status, reason = "review", "delegated_field"
            elif not field.get("store"):
                status, reason = "computed", "nonstored"
            elif not field.get("attachment"):
                status, reason = "column", "direct_column"
            elif not owned and field.get("origin") not in selected:
                status, reason = "review", "overridden_shared_field"
            else:
                status, reason = "ready", "owned_model" if owned else "introduced_field"
            result.append({"scope_key": scope_key(model["name"], field["name"]),
                "model_name": model["name"], "field_name": field["name"],
                "label": field.get("label") or field["name"], "kind": "field",
                "source_modules": ", ".join(sorted(selected.intersection(field.get("modules", []))))
                    or (model.get("owner") if owned else ""), "status": status, "reason": reason})
    return result


def scope_fingerprint(modules, rows):
    # لا تؤثر اللغة أو عناوين الحقول في بصمة توجيه الملفات.
    payload = {"modules": sorted(modules), "rows": [
        {k: row[k] for k in ("scope_key", "source_modules", "status", "reason")}
        for row in sorted(rows, key=lambda item: item["scope_key"])]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
