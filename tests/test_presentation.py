"""Audited business parent resolution and stable folder identity contracts."""
from types import SimpleNamespace
import importlib
import pytest
from test_drive_backend import CLIENT_MODULE

presentation = importlib.import_module(CLIENT_MODULE.__package__ + '.presentation')


class Record:
    def __init__(self, model, ident=1, company=2, **links):
        self._name, self.id, self.company_id = model, ident, company
        self._fields = {'company_id': SimpleNamespace(type='many2one', comodel_name='res.company')}
        self.links = links
        for name, target in links.items():
            self._fields[name] = SimpleNamespace(type='many2one', comodel_name=target._name)
    def __getitem__(self, key):
        return self.links[key]
    def exists(self):
        return self


def test_photos_expenses_and_attendance_resolve_to_same_order():
    order = Record('fastegy.installation', 12)
    for model, field in [('fastegy.installation.photo','task_id'),
                         ('fastegy.installation.expense','installation_id'),
                         ('fastegy.install.attendance','task_id'),
                         ('fastegy.custody.expense','task_id')]:
        child = Record(model, **{field: order})
        assert presentation.resolve_anchor(child) is order


def test_survey_photo_uses_verified_order_or_stays_with_independent_survey():
    order = Record('fastegy.installation', 12)
    survey = Record('fastegy.survey.response', 3, task_id=order)
    photo = Record('fastegy.survey.camera.photo', survey_id=survey)
    assert presentation.resolve_anchor(photo) is order
    independent = Record('fastegy.survey.response', 4)
    photo = Record('fastegy.survey.camera.photo', survey_id=independent)
    assert presentation.resolve_anchor(photo) is independent


def test_changed_schema_or_unrelated_parent_is_never_guessed():
    child = Record('fastegy.installation.photo', task_id=Record('res.partner'))
    assert presentation.resolve_anchor(child) is child


def test_parent_company_conflict_is_rejected():
    child = Record('fastegy.installation.photo', company=1, task_id=Record('fastegy.installation', company=2))
    with pytest.raises(ValueError, match='different company'):
        presentation.resolve_anchor(child)


def test_business_folder_keys_survive_rename_and_separate_equal_order_names():
    def folders(i, label):
        return presentation.business_folders(2, 'installation', 'التركيبات', 'fastegy.installation', i, label, ('images','صور التركيب'))
    first, renamed, other = folders(1,'FE-1'), folders(1,'FE-2'), folders(2,'FE-1')
    assert [x['key'] for x in first[1:]] == [x['key'] for x in renamed[1:]]
    assert first[2]['name'] != renamed[2]['name']
    assert first[2]['key'] != other[2]['key']
    assert first[0] == 'company_2'


def test_business_categories_preserve_source_and_file_type():
    assert presentation.category('fastegy.installation.expense', 'image/png')[0] == 'expenses'
    assert presentation.category('fastegy.installation', 'image/png', True)[0] == 'chatter'
    assert presentation.category('fastegy.installation', 'image/png')[0] == 'images'
    assert presentation.category('fastegy.installation', 'application/pdf')[0] == 'documents'
    assert presentation.category('fastegy.installation', 'video/mp4')[0] == 'videos'
