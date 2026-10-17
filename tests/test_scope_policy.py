import copy
import importlib

from test_drive_backend import CLIENT_MODULE

policy = importlib.import_module(CLIENT_MODULE.__package__ + ".scope_policy")


def field(name="photo", **kw):
    return dict(name=name, store=True, attachment=True, modules=["install"], origin="install", **kw)


def model(name="visit.visit", **kw):
    return dict(name=name, owner="install", fields=[field()], **kw)


def test_application_ownership_does_not_follow_names_or_dependencies():
    rows = policy.discover_scope(["install"], [model(),
        dict(name="visit.shared", owner="contacts", fields=[]),
        dict(name="res.partner", owner="base", fields=[]),
        dict(name="completely.different.name", owner="install", fields=[])])
    assert {r["model_name"] for r in rows} == {"visit.visit", "completely.different.name"}


def test_shared_model_routes_only_fields_introduced_by_selected_application():
    fields = [field("new_photo"), dict(field("existing_photo"), origin="base", modules=["base", "install"]),
              dict(field("other_photo"), origin="other", modules=["other"])]
    rows = policy.discover_scope(["install"], [dict(name="res.partner", owner="base", fields=fields)])
    states = {r["scope_key"]: r["status"] for r in rows}
    assert states == {"res.partner/": "shared", "res.partner/new_photo": "ready",
                      "res.partner/existing_photo": "review"}


def test_direct_computed_and_delegated_fields_are_not_claimed_as_routed():
    fields = [dict(field("column"), attachment=False), dict(field("calculated"), store=False),
              dict(field("delegated"), inherited=True), field("photo")]
    rows = policy.discover_scope(["install"], [dict(name="job.task", owner="install", fields=fields)])
    assert {r["field_name"]: r["status"] for r in rows if r["kind"] == "field"} == {
        "column": "column", "calculated": "computed", "delegated": "review", "photo": "ready"}


def test_owned_model_includes_binary_fields_added_by_other_extensions():
    m = dict(model(), fields=[dict(field(), modules=["other"], origin="other")])
    assert all(r["status"] == "ready" for r in policy.discover_scope(["install"], [m]))


def test_system_transient_and_report_models_are_excluded():
    descriptors = [model(name="ir.attachment"), dict(model(name="wizard.upload"), transient=True),
                   dict(model(name="report.summary"), auto=False), dict(model(name="mixin.example"), abstract=True)]
    assert {r["status"] for r in policy.discover_scope(["install"], descriptors)} == {"excluded"}


def test_stable_discovery_and_fingerprint_ignore_language_and_order():
    models = [model(), model(name="job.task")]
    rows = policy.discover_scope(["install"], models)
    assert rows == policy.discover_scope(["install"], reversed(models))
    translated = copy.deepcopy(rows)
    for r in translated:
        r["label"] = "ترجمة مختلفة"
    assert policy.scope_fingerprint(["install"], rows) == policy.scope_fingerprint(["install"], translated)


def test_new_field_changes_fingerprint_and_empty_selection_covers_nothing():
    m = model()
    first = policy.scope_fingerprint(["install"], policy.discover_scope(["install"], [m]))
    m["fields"].append(field("second"))
    assert first != policy.scope_fingerprint(["install"], policy.discover_scope(["install"], [m]))
    assert policy.discover_scope([], [m]) == []
