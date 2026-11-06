"""BE1 settings orchestration, placed in practice to respect import direction."""

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.domains.auth.models import User
from app.domains.auth.router import AuthError, AuthRoute, get_current_user
from app.domains.practice.account_deletion import (
    AccountNotFound, CleanupPending, DeletionJournal, DeletionUnconfirmed,
    delete_account, get_deletion_journal,
)

router = APIRouter(prefix="/auth", tags=["auth"], route_class=AuthRoute)


@router.delete(
    "/me", status_code=204, response_class=Response,
    responses={
        401: {"description": "미인증·만료·잘못된 토큰·이미 탈퇴한 계정"},
        503: {"description": "ACCOUNT_DELETION_UNCONFIRMED: 삭제 미확정 또는 "
              "ACCOUNT_DELETED_CLEANUP_PENDING: DB 삭제 완료, 파일 정리 재시도 필요. "
              "후자는 계정이 이미 삭제되었으므로 프론트 토큰·캐시를 비우고 정리 지연을 안내한다. "
              "일반 탈퇴 API 재요청은 401이며 파일 정리는 운영 복구 명령으로 처리한다. "
              "기존 error {code, message, fields} 형식."},
    },
)
def withdraw(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    journal: DeletionJournal = Depends(get_deletion_journal),
):
    """Delete only the authenticated account. No user ID or request body needed."""
    try:
        delete_account(db, user_id=user.id, journal=journal)
    except AccountNotFound:
        raise AuthError(401, "ACCESS_CREDENTIAL_INVALID") from None
    except DeletionUnconfirmed:
        raise AuthError(503, "ACCOUNT_DELETION_UNCONFIRMED") from None
    except CleanupPending:
        raise AuthError(503, "ACCOUNT_DELETED_CLEANUP_PENDING") from None
    return Response(status_code=204)
