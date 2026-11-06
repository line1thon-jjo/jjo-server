from datetime import datetime, timezone
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, Integer, MetaData, select, func, event
from sqlalchemy.schema import CreateSchema, DropSchema
from sqlalchemy.orm import Session
from app.main import app
from app.core.database import get_db
from app.core.config import settings
from app.core.security import issue, verify
from app.domains.auth import router as routes
from app.domains.auth.models import User
from app.domains.auth.service import GoogleIdentity
import jwt

@pytest.fixture(params=['sqlite', 'postgresql'])
def client(monkeypatch, request):
    monkeypatch.setattr(settings, 'signup_token_secret', 's' * 40)
    monkeypatch.setattr(settings, 'access_token_secret', 'a' * 40)
    schema = None
    if request.param == 'postgresql':
        url = os.environ.get('AUTH_TEST_DATABASE_URL')
        if not url:
            pytest.skip('AUTH_TEST_DATABASE_URL is not configured')
        engine = create_engine(url)
        if engine.dialect.name != 'postgresql':
            engine.dispose()
            pytest.fail('AUTH_TEST_DATABASE_URL must point to PostgreSQL')
        schema = 'auth_api_test_' + uuid4().hex
        # Real request commits must survive into independent sessions. Isolate
        # those commits in a new schema, never production/public tables.
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        engine = engine.execution_options(schema_translate_map={None: schema})
    else:
        engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=__import__('sqlalchemy').pool.StaticPool)
        @event.listens_for(engine, 'connect')
        def connect(c, _):
            c.isolation_level = None
        @event.listens_for(engine, 'begin')
        def begin(c):
            c.exec_driver_sql('BEGIN')
    try:
        table = User.__table__.to_metadata(MetaData())
        if schema is None:
            table.c.id.type = Integer()
        table.create(engine)
        def session():
            with Session(engine, expire_on_commit=False) as db:
                yield db
        app.dependency_overrides[get_db] = session
        monkeypatch.setattr(routes, 'verify_google', lambda _: GoogleIdentity('subject', 'person@example.test', 'Name'))
        with TestClient(app) as c:
            yield c, engine
    finally:
        app.dependency_overrides.clear()
        try:
            if schema is not None:
                # Drop only this fixture's randomly named schema and its data.
                with engine.begin() as connection:
                    connection.execute(DropSchema(schema, cascade=True))
        finally:
            engine.dispose()


def pending(c):
    response = c.post('/auth/google', json={'id_token': 'test'})
    assert response.status_code == 200
    return response.json()['signup_token']


def signup(c, token, **agreements):
    return c.post('/auth/signup', json={'signup_token': token, 'agreements': dict(service_terms=True, privacy_collection_use=True, marketing=False, **agreements)})


def test_signup_login_me_and_replay(client):
    c, engine = client
    token = pending(c)
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(User)) == 0
    result = signup(c, token)
    assert result.status_code == 201, result.text
    body = result.json()
    assert body['next_step'] == 'onboarding'
    assert body['user']['onboarding_completed'] is False
    assert signup(c, token).status_code == 409
    assert c.get('/auth/me', headers={'Authorization': 'Bearer ' + token}).status_code == 401
    access = body['access_token']
    assert signup(c, access).status_code == 401
    assert c.get('/auth/me', headers={'Authorization': 'Bearer ' + access}).json()['next_step'] == 'onboarding'
    assert c.post('/auth/google', json={'id_token': 'test'}).json()['next_step'] == 'onboarding'
    with Session(engine) as db:
        user = db.scalar(select(User))
        assert user.marketing_agreed is False
        assert user.terms_agreed_at is not None
        user.onboarding_completed = True
        db.commit()
    assert c.post('/auth/google', json={'id_token': 'test'}).json()['next_step'] == 'practices'
    assert c.get('/auth/me', headers={'Authorization': 'Bearer ' + access}).json()['user']['onboarding_completed'] is True

@pytest.mark.parametrize('field', ['service_terms', 'privacy_collection_use'])
def test_required(client, field):
    c, engine = client
    token = pending(c)
    agreements = dict(service_terms=True, privacy_collection_use=True)
    agreements[field] = False
    assert c.post('/auth/signup', json={'signup_token': token, 'agreements': agreements}).status_code == 400
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(User)) == 0


def test_atomic_rollback(client, monkeypatch):
    c, engine = client
    token = pending(c)
    original = routes.complete_signup
    def fail(*args):
        original(*args)
        raise RuntimeError('failure after consent flush')
    monkeypatch.setattr(routes, 'complete_signup', fail)
    assert signup(c, token).status_code == 503
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(User)) == 0

@pytest.mark.parametrize('error,status', [(ValueError(),401),(ConnectionError(),503)])
def test_google_errors(client, monkeypatch, error, status):
    c, _ = client
    def fail(_):
        raise error
    monkeypatch.setattr(routes, 'verify_google', fail)
    assert c.post('/auth/google', json={'id_token': 'test'}).status_code == status


def test_google_verifier(monkeypatch):
    from app.domains.auth import google
    from google.auth.exceptions import TransportError
    monkeypatch.setattr(settings, 'google_client_ids', ['test-client'])
    def verified(token, request, audience):
        assert audience == ['test-client']
        return dict(sub='s', email='a@example.test', name='A')
    monkeypatch.setattr(google.id_token, 'verify_oauth2_token', verified)
    assert google.verify_google('test').sub == 's'
    for error, expected in [(ValueError(), ValueError), (TransportError(), ConnectionError)]:
        def fail(*a, **kw):
            raise error
        monkeypatch.setattr(google.id_token, 'verify_oauth2_token', fail)
        with pytest.raises(expected):
            google.verify_google('test')


def test_token_validation(client):
    for kind in ['signup','access']:
        token = issue(kind, '123', email='test@example.test', name='Test')
        assert verify(kind, token)['sub'] == '123'
        other = 'signup' if kind == 'access' else 'access'
        with pytest.raises(jwt.InvalidTokenError):
            verify(other, token)
        with pytest.raises(jwt.InvalidTokenError):
            verify(kind, token + 'bad')
        data = verify(kind, token)
        data['exp'] = 1
        secret = getattr(settings, kind + '_token_secret')
        with pytest.raises(jwt.ExpiredSignatureError):
            verify(kind, jwt.encode(data, secret, algorithm='HS256'))


def test_validation_and_missing_auth(client):
    c, _ = client
    assert c.get('/auth/me').status_code == 401
    assert c.post('/auth/google', json={'id_token':'x', 'sub':'forged'}).status_code == 422
    assert c.post('/auth/signup', json={'signup_token':pending(c), 'agreements':{'service_terms':'true','privacy_collection_use':True}}).status_code == 422


def test_missing_user_and_incomplete_account(client):
    c, engine = client
    access = issue('access', '999')
    assert c.get('/auth/me', headers={'Authorization':'Bearer '+access}).status_code == 401
    with Session(engine) as db:
        db.add(User(google_sub='subject', email='person@example.test', name='Name'))
        db.commit()
    assert c.post('/auth/google', json={'id_token':'test'}).status_code == 409


def test_replay_preserves_consent(client):
    c, engine = client
    token = pending(c)
    assert signup(c, token).status_code == 201
    with Session(engine) as db:
        before = db.scalar(select(User)).terms_agreed_at
    response = c.post('/auth/signup', json={'signup_token':token, 'agreements':dict(service_terms=True, privacy_collection_use=True, marketing=True)})
    assert response.status_code == 409
    with Session(engine) as db:
        user = db.scalar(select(User))
        assert user.marketing_agreed is False
        assert user.terms_agreed_at == before


def test_google_real_crypto(monkeypatch):
    """Use Google's real verifier; replace only its certificate HTTP transport."""
    import time
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    from app.domains.auth import google
    from google.auth.exceptions import GoogleAuthError
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    monkeypatch.setattr(settings, 'google_client_ids', ['allowed-client'])
    monkeypatch.setattr(google.id_token, '_fetch_certs', lambda *args: {'test-key': public})
    claims = dict(sub='subject', email='person@example.test', name='Name', iss='https://accounts.google.com', aud='allowed-client', iat=int(time.time()), exp=int(time.time())+600)
    def token(data, signing_key=private):
        return jwt.encode(data, signing_key, algorithm='RS256', headers={'kid':'test-key'})
    assert google.verify_google(token(claims)).sub == 'subject'
    for changes in [dict(iss='invalid'), dict(aud='other-client'), dict(exp=1)]:
        with pytest.raises(ValueError):
            google.verify_google(token(dict(claims, **changes)))
    with pytest.raises(ValueError):
        google.verify_google(token(claims, rsa.generate_private_key(public_exponent=65537, key_size=2048)))


def test_required_token_claims(client):
    for kind in ['signup', 'access']:
        original = verify(kind, issue(kind, '123', email='test@example.test', name='Test'))
        for field in ['sub', 'exp', 'iat', 'aud', 'iss', 'purpose'] + (['email','name'] if kind == 'signup' else []):
            data = dict(original)
            del data[field]
            token = jwt.encode(data, getattr(settings, kind+'_token_secret'), algorithm='HS256')
            with pytest.raises(jwt.InvalidTokenError):
                verify(kind, token)
        for field in ['aud', 'iss', 'purpose']:
            data = dict(original, **{field:'wrong'})
            with pytest.raises(jwt.InvalidTokenError):
                verify(kind, jwt.encode(data, getattr(settings, kind+'_token_secret'), algorithm='HS256'))


def test_health_without_auth_configuration(monkeypatch):
    monkeypatch.setattr(settings, 'google_client_ids', [])
    monkeypatch.setattr(settings, 'signup_token_secret', '')
    monkeypatch.setattr(settings, 'access_token_secret', '')
    with TestClient(app) as c:
        assert c.get('/health').json() == {'status':'ok'}
        assert c.post('/auth/google', json={'id_token':'test'}).status_code == 503


def test_onboarding_completion_persists_and_is_idempotent(client):
    c, engine = client
    account = signup(c, pending(c)).json()
    headers = {'Authorization': 'Bearer ' + account['access_token']}
    with Session(engine) as db:
        other = User(google_sub='other', email='other@example.test', name='Other',
                     terms_agreed_at=datetime.now(timezone.utc))
        db.add(other)
        db.commit()
        other_id = other.id

    for _ in range(2):
        response = c.post('/auth/onboarding/complete', headers=headers)
        assert response.status_code == 200, response.text
        assert response.json() == {
            'user': dict(account['user'], onboarding_completed=True),
            'next_step': 'practices',
        }
        # A fresh session proves the endpoint committed, rather than only flushed.
        with Session(engine) as db:
            assert db.get(User, account['user']['id']).onboarding_completed is True
            assert db.get(User, other_id).onboarding_completed is False
        assert c.get('/auth/me', headers=headers).json() == response.json()

    # Even unsolicited identity data cannot select another user's account.
    assert c.post('/auth/onboarding/complete', headers=headers,
                  json={'user_id': other_id}).status_code == 200
    with Session(engine) as db:
        assert db.get(User, other_id).onboarding_completed is False
    login = c.post('/auth/google', json={'id_token': 'test'}).json()
    assert login['user']['onboarding_completed'] is True
    assert login['next_step'] == 'practices'
    assert c.get('/auth/me', headers={'Authorization': 'Bearer ' + login['access_token']}).json()['user']['onboarding_completed'] is True


def test_onboarding_rejects_invalid_credentials_without_writes(client):
    c, engine = client
    signup_token = pending(c)
    account = signup(c, signup_token).json()
    with Session(engine) as db:
        incomplete = User(google_sub='incomplete', email='incomplete@example.test', name='Incomplete')
        db.add(incomplete)
        db.commit()
        incomplete_id = incomplete.id
    for token in [None, 'invalid', signup_token, issue('access', '999'),
                  issue('access', str(incomplete_id)), issue('access', 'not-an-id')]:
        headers = {} if token is None else {'Authorization': 'Bearer ' + token}
        response = c.post('/auth/onboarding/complete', headers=headers)
        assert response.status_code == 401
        assert response.json()['error']['code'] == 'ACCESS_CREDENTIAL_INVALID'
        me = c.get('/auth/me', headers=headers)
        assert me.status_code == response.status_code
        assert me.json() == response.json()
    with Session(engine) as db:
        assert all(user.onboarding_completed is False for user in db.scalars(select(User)))


def test_onboarding_rollback_on_failure(client, monkeypatch):
    c, engine = client
    account = signup(c, pending(c)).json()
    original = routes.complete_onboarding
    def fail(db, user):
        original(db, user)
        raise RuntimeError('failure after completion flush')
    monkeypatch.setattr(routes, 'complete_onboarding', fail)
    with pytest.raises(RuntimeError, match='failure after completion flush'):
        c.post('/auth/onboarding/complete', headers={'Authorization': 'Bearer ' + account['access_token']})
    with Session(engine) as db:
        assert db.get(User, account['user']['id']).onboarding_completed is False
