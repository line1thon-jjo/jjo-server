import jwt
from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.security import HTTPBearer
from app.core.database import get_db
from app.core.config import settings
from app.core.security import issue, verify
from app.domains.auth.google import verify_google
from app.domains.auth.schemas import GoogleRequest, SignupRequest
from app.domains.auth.service import GoogleIdentity, get_user_by_google_sub, get_user_by_id, complete_signup, complete_onboarding

class AuthError(Exception):
    def __init__(self, status, code, fields=None):
        self.status, self.code, self.fields = status, code, fields or []

class AuthRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError as exc:
                return JSONResponse(status_code=422, content={"error": {"code": "VALIDATION_ERROR", "message": "요청 형식을 확인해 주세요.", "fields": [".".join(map(str, e['loc'][1:])) for e in exc.errors()]}})
            except AuthError as exc:
                return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": "필수 약관에 동의해야 합니다." if exc.code == "REQUIRED_AGREEMENT_MISSING" else exc.code, "fields": exc.fields}})
        return handler

router = APIRouter(prefix="/auth", tags=["auth"], route_class=AuthRoute)
bearer = HTTPBearer(auto_error=False)

def user_response(user):
    return dict(id=user.id, name=user.name, email=user.email, profile_image_url=user.profile_image_url,
                onboarding_completed=user.onboarding_completed)

def authenticated(user):
    return dict(status="authenticated", next_step="practices" if user.onboarding_completed else "onboarding",
                user=user_response(user), access_token=issue("access", user.id), token_type="Bearer",
                expires_in=settings.access_token_ttl_seconds)

def credential(kind, token):
    try:
        return verify(kind, token)
    except jwt.InvalidTokenError:
        raise AuthError(401, f"{kind.upper()}_CREDENTIAL_INVALID")
    except RuntimeError:
        raise AuthError(503, "AUTH_CONFIGURATION_ERROR")

@router.post('/google')
def google_login(body: GoogleRequest, db=Depends(get_db)):
    try:
        identity = verify_google(body.id_token)
    except ConnectionError:
        raise AuthError(503, "GOOGLE_VERIFICATION_UNAVAILABLE")
    except ValueError:
        raise AuthError(401, "GOOGLE_CREDENTIAL_INVALID")
    except RuntimeError:
        raise AuthError(503, "AUTH_CONFIGURATION_ERROR")
    user = get_user_by_google_sub(db, identity.sub)
    try:
        if user is None:
            return dict(status="signup_required", next_step="terms", signup_token=issue("signup", identity.sub, email=identity.email, name=identity.name, profile_image_url=identity.profile_image_url), signup_token_expires_in=settings.signup_token_ttl_seconds)
        if user.terms_agreed_at is None:
            raise AuthError(409, "ACCOUNT_STATE_INVALID")
        return authenticated(user)
    except RuntimeError:
        raise AuthError(503, "AUTH_CONFIGURATION_ERROR")

@router.post('/signup', status_code=201)
def signup(body: SignupRequest, db=Depends(get_db)):
    claims = credential("signup", body.signup_token)
    if not body.agreements.service_terms or not body.agreements.privacy_collection_use:
        raise AuthError(400, "REQUIRED_AGREEMENT_MISSING", ["agreements." + name for name in ("service_terms", "privacy_collection_use") if not getattr(body.agreements, name)])
    identity = GoogleIdentity(claims['sub'], claims['email'], claims['name'], claims.get('profile_image_url'))
    try:
        with db.begin():
            user = complete_signup(db, identity, body.agreements.marketing)
            result = authenticated(user)
        return result
    except ValueError:
        raise AuthError(409, "SIGNUP_ALREADY_COMPLETED")
    except RuntimeError:
        raise AuthError(503, "AUTH_CONFIGURATION_ERROR")

def get_current_user(auth=Depends(bearer), db=Depends(get_db)):
    if auth is None:
        raise AuthError(401, "ACCESS_CREDENTIAL_INVALID")
    claims = credential("access", auth.credentials)
    try:
        uid = int(claims['sub'])
    except ValueError:
        raise AuthError(401, "ACCESS_CREDENTIAL_INVALID")
    user = get_user_by_id(db, uid)
    if user is None or user.terms_agreed_at is None:
        raise AuthError(401, "ACCESS_CREDENTIAL_INVALID")
    return user

@router.get('/me')
def me(user=Depends(get_current_user)):
    return dict(user=user_response(user), next_step="practices" if user.onboarding_completed else "onboarding")

@router.post('/onboarding/complete')
def onboarding_complete(user=Depends(get_current_user), db=Depends(get_db)):
    """Complete onboarding for the access credential's owner; no body is required."""
    try:
        complete_onboarding(db, user)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return dict(user=user_response(user), next_step="practices")
