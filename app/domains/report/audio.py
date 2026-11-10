"""녹음 재생용 서명 토큰 (BE2 담당).

브라우저의 <audio> 는 Authorization 헤더를 보낼 수 없으므로, 로그인한 사용자에게 30분짜리
토큰이 붙은 주소를 내려주고 그 주소로 파일을 제공한다. 로그인 토큰과 용도·대상(aud)이 달라서
서로 바꿔 쓸 수 없다. 서명 비밀키는 인증 토큰의 비밀키를 함께 쓴다.
"""
from datetime import datetime, timezone

import jwt

from app.core.config import settings
from app.core.security import key

AUDIO_URL_TTL_SECONDS = 1800
_PURPOSE = "audio"
_AUDIENCE = "jjo-audio"


def issue_audio_token(user_id: int, recording_id: int) -> str:
    now = int(datetime.now(timezone.utc).timestamp())
    claims = {
        "sub": str(user_id), "rid": recording_id, "purpose": _PURPOSE, "aud": _AUDIENCE,
        "iss": settings.auth_issuer, "iat": now, "exp": now + AUDIO_URL_TTL_SECONDS,
    }
    return jwt.encode(claims, key("access"), algorithm="HS256")


def verify_audio_token(token: str, recording_id: int) -> int:
    """유효하면 토큰의 사용자 id 를 돌려준다. 잘못됐거나 다른 녹음의 토큰이면 jwt.InvalidTokenError."""
    data = jwt.decode(
        token, key("access"), algorithms=["HS256"], audience=_AUDIENCE, issuer=settings.auth_issuer,
        options={"require": ["sub", "rid", "exp", "iat", "aud", "iss", "purpose"]},
    )
    if data["purpose"] != _PURPOSE or data["rid"] != recording_id:
        raise jwt.InvalidTokenError()
    try:
        return int(data["sub"])
    except ValueError:
        raise jwt.InvalidTokenError() from None
