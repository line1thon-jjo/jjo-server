"""Read endpoints use real credentials and the same DB fixture as creation."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.security import issue
from app.domains.auth.models import User
from app.domains.practice.models import Practice
from app.domains.practice.tests.test_api import api, client


@pytest.mark.parametrize('path', ['/practices', '/practices/1'])
def test_read_requires_valid_credentials(api, path):
    c, engine, _, _ = api
    with Session(engine) as db:
        user = User(google_sub='incomplete', email='pending@example.test', name='Pending')
        db.add(user)
        db.commit()
        incomplete_id = user.id
    tokens = [None, 'invalid', issue('signup', 'new', email='new@example.test', name='New'),
              issue('access', '999'), issue('access', 'not-an-id'),
              issue('access', incomplete_id)]
    for token in tokens:
        headers = {} if token is None else {'Authorization': 'Bearer ' + token}
        response = c.get(path, headers=headers)
        assert response.status_code == 401
        assert response.json() == {'error': {
            'code': 'ACCESS_CREDENTIAL_INVALID',
            'message': 'ACCESS_CREDENTIAL_INVALID', 'fields': [],
        }}


def test_empty_list(api):
    c, _, headers, _ = api
    response = c.get('/practices', headers=headers)
    assert response.status_code == 200
    assert response.json() == []


def test_created_practice_can_be_read_without_regenerating_title(api, monkeypatch):
    from app.domains.practice import title

    c, _, headers, _ = api
    script = ' \r\n발표 시작\n다음 줄\t '
    created = c.post('/practices', headers=headers, json={'script': script})
    assert created.status_code == 201
    body = created.json()

    def fail(*args, **kwargs):
        pytest.fail('Reading must not call Gemini')

    monkeypatch.setattr(title.genai, 'Client', fail)
    detail = c.get(f"/practices/{body['id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json() == body
    assert detail.json()['script'] == script
    assert set(body) == {'id', 'title', 'script', 'created_at', 'updated_at'}
    listing = c.get('/practices', headers=headers)
    assert listing.status_code == 200
    assert listing.json() == [body]


def test_owner_filter_and_stable_creation_order(api):
    c, engine, headers, uid = api
    old = datetime(2025, 1, 1, tzinfo=timezone.utc)
    new = datetime(2025, 1, 2, tzinfo=timezone.utc)
    with Session(engine) as db:
        other = User(google_sub='other', email='other@example.test', name='Other',
                     terms_agreed_at=old)
        db.add(other)
        db.flush()
        other_id = other.id
        practices = [
            Practice(user_id=uid, title='new', script='new script', created_at=new, updated_at=old),
            Practice(user_id=uid, title='tie', script='tie script', created_at=new, updated_at=old),
            Practice(user_id=uid, title='old', script='old script', created_at=old, updated_at=new),
            Practice(user_id=other_id, title='private', script='private script', created_at=new),
        ]
        db.add_all(practices)
        db.commit()
        ids = [p.id for p in practices]
    other_headers = {'Authorization': 'Bearer ' + issue('access', other_id)}
    assert [p['id'] for p in c.get('/practices', headers=other_headers).json()] == [ids[3]]

    # Observe loaded objects rather than SQL spelling or driver parameters.
    # Filtering after loading another owner's Practice must also fail this test.
    loaded_owners = []

    def capture(session, instance):
        if isinstance(instance, Practice):
            loaded_owners.append(instance.user_id)

    event.listen(Session, 'loaded_as_persistent', capture)
    try:
        response = c.get('/practices', headers=headers)
        assert response.status_code == 200
        # The oldest practice has the highest owned ID and latest updated_at:
        # neither ID alone nor update time can substitute for creation order.
        assert [p['id'] for p in response.json()] == [ids[1], ids[0], ids[2]]
        assert loaded_owners == [uid] * 3
        loaded_owners.clear()
        forbidden = c.get(f'/practices/{ids[3]}', headers=headers)
        assert loaded_owners == []
    finally:
        event.remove(Session, 'loaded_as_persistent', capture)
    missing = c.get('/practices/999999', headers=headers)
    assert forbidden.status_code == missing.status_code == 404
    assert forbidden.json() == missing.json() == {'error': {
        'code': 'PRACTICE_NOT_FOUND', 'message': 'PRACTICE_NOT_FOUND', 'fields': [],
    }}
    assert c.get(f'/practices/{ids[3]}', headers=other_headers).json()['script'] == 'private script'


def test_invalid_practice_id_uses_existing_validation_envelope(api):
    c, _, headers, _ = api
    response = c.get('/practices/not-an-id', headers=headers)
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'VALIDATION_ERROR'
    assert response.json()['error']['fields'] == ['practice_id']
