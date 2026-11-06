from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.domains.auth.models import User
from app.domains.auth.router import AuthError, AuthRoute, get_current_user
from app.domains.practice.schemas import PracticeCreate, PracticeResponse
from app.domains.practice.script_upload_router import router as script_upload_router
from app.domains.practice.service import create_practice, get_practice, list_practices

router = APIRouter(prefix="/practices", tags=["practice"], route_class=AuthRoute)
router.include_router(script_upload_router)


@router.get("", response_model=list[PracticeResponse])
def list_owned(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[PracticeResponse]:
    return [PracticeResponse.model_validate(practice)
            for practice in list_practices(db, user_id=user.id)]


@router.get("/{practice_id}", response_model=PracticeResponse)
def detail(
    practice_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PracticeResponse:
    practice = get_practice(db, user_id=user.id, practice_id=practice_id)
    if practice is None:
        raise AuthError(404, "PRACTICE_NOT_FOUND")
    return PracticeResponse.model_validate(practice)


@router.post("", status_code=201, response_model=PracticeResponse)
def create(
    body: PracticeCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PracticeResponse:
    try:
        practice = create_practice(db, user_id=user.id, data=body)
        # Validate the response model before commit so validation failures also roll back.
        result = PracticeResponse.model_validate(practice)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return result
