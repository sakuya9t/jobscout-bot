"""Resume upload / list / delete. One resume per account: a new upload replaces
the previous one (and its scored matches) and is the resume used for scoring; the
original file is also kept on disk."""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..config import settings
from ..db import get_db
from ..models import Resume, User
from ..schemas import ResumeContentOut, ResumeOut
from ..services.resume_parser import extract_text

router = APIRouter(prefix="/api/resumes", tags=["resumes"])

MAX_RESUME_BYTES = 5 * 1024 * 1024  # 5 MB

# Content types for inline preview of the stored original file. Anything else is
# served as a generic binary (the dashboard falls back to the extracted text).
_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/plain",
    ".markdown": "text/plain",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


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
    # Bounded read: ask for one byte past the cap instead of buffering an
    # arbitrarily large upload into memory just to reject it afterwards.
    data = await file.read(MAX_RESUME_BYTES + 1)
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

    # One resume per account: this upload replaces any existing resume. Capture the
    # old rows/files now; the new resume gets a fresh id (so a distinct on-disk path)
    # and the old matches cascade away on delete, forcing a re-score on the new resume.
    old_resumes = list(db.scalars(select(Resume).where(Resume.user_id == user.id)))

    # Cache by resume *version* (content): re-uploading identical text is a no-op so
    # the matches already scored against it survive instead of being recomputed.
    for r in old_resumes:
        if r.content_text == text:
            return r

    old_paths = [_stored_path(user.id, r) for r in old_resumes]

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

    for r in old_resumes:
        db.delete(r)  # cascades to that resume's MatchResults
    db.commit()
    db.refresh(resume)
    # Delete old files only now that the replacement is durably committed.
    for path in old_paths:
        path.unlink(missing_ok=True)
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


@router.get("/{resume_id}/file")
def get_resume_file(
    resume_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Stream the original uploaded file for in-page preview (inline, not a
    download). PDFs render natively in the dashboard's preview iframe; the browser
    handles txt/md too. Scoped to the owner via ``_owned``."""
    resume = _owned(db, user, resume_id)
    path = _stored_path(user.id, resume)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Stored resume file not found")
    ext = os.path.splitext(resume.filename)[1].lower()
    return FileResponse(
        path,
        media_type=_MEDIA_TYPES.get(ext, "application/octet-stream"),
        filename=resume.filename,
        content_disposition_type="inline",
    )


@router.get("/{resume_id}/content", response_model=ResumeContentOut)
def get_resume_content(
    resume_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """The résumé's extracted plain text — the dashboard preview's fallback for
    formats the browser can't render inline (e.g. .docx)."""
    return _owned(db, user, resume_id)


@router.delete("/{resume_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(resume_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    resume = _owned(db, user, resume_id)
    stored = _stored_path(user.id, resume)
    db.delete(resume)  # cascades to this resume's MatchResults via the relationship
    db.commit()
    stored.unlink(missing_ok=True)
