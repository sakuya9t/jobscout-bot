"""Resume upload / list / activate / delete. The most-recent upload becomes the
active resume used for scoring; the original file is also kept on disk."""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..config import settings
from ..db import get_db
from ..models import Resume, User
from ..schemas import ResumeOut
from ..services.resume_parser import extract_text

router = APIRouter(prefix="/api/resumes", tags=["resumes"])

MAX_RESUME_BYTES = 5 * 1024 * 1024  # 5 MB


def _safe_filename(name: str | None) -> str:
    """Reduce a user-supplied filename to a harmless basename so it can't escape
    the per-user resume dir (path separators, ``..``, hidden dotfiles)."""
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    base = base.lstrip(".")[:200]
    return base or "resume"


def _stored_path(user_id: int, resume: Resume):
    return settings.resume_dir / str(user_id) / f"{resume.id}_{resume.filename}"


@router.post("", response_model=ResumeOut, status_code=status.HTTP_201_CREATED)
async def upload_resume(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Resume:
    data = await file.read()
    if len(data) > MAX_RESUME_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"Resume exceeds the {MAX_RESUME_BYTES // (1024 * 1024)} MB limit",
        )
    safe_name = _safe_filename(file.filename)
    try:
        text = extract_text(safe_name, data)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    # New upload becomes the sole active resume.
    for r in db.scalars(select(Resume).where(Resume.user_id == user.id, Resume.is_active == True)):  # noqa: E712
        r.is_active = False

    resume = Resume(user_id=user.id, filename=safe_name, content_text=text)
    db.add(resume)
    db.flush()  # assign resume.id without committing yet

    # Persist the original file BEFORE committing so a write failure rolls the row
    # back instead of leaving a DB record with no file on disk.
    try:
        user_dir = settings.resume_dir / str(user.id)
        user_dir.mkdir(parents=True, exist_ok=True)
        _stored_path(user.id, resume).write_bytes(data)
    except OSError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Failed to store the uploaded resume"
        ) from exc

    db.commit()
    db.refresh(resume)
    return resume


@router.get("", response_model=list[ResumeOut])
def list_resumes(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return list(
        db.scalars(select(Resume).where(Resume.user_id == user.id).order_by(Resume.created_at.desc()))
    )


def _owned(db: Session, user: User, resume_id: int) -> Resume:
    resume = db.get(Resume, resume_id)
    if not resume or resume.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Resume not found")
    return resume


@router.post("/{resume_id}/activate", response_model=ResumeOut)
def activate(resume_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    resume = _owned(db, user, resume_id)
    for r in db.scalars(select(Resume).where(Resume.user_id == user.id, Resume.is_active == True)):  # noqa: E712
        r.is_active = False
    resume.is_active = True
    db.commit()
    db.refresh(resume)
    return resume


@router.delete("/{resume_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(resume_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    resume = _owned(db, user, resume_id)
    stored = _stored_path(user.id, resume)
    db.delete(resume)  # cascades to this resume's MatchResults via the relationship
    db.commit()
    stored.unlink(missing_ok=True)
