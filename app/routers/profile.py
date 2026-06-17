"""The user-level applicant profile: the structured information a job application
asks for, kept once so it can autofill forms and feed the phase-2 auto-apply.

A single record per user (no id in the path — isolation is automatic), saved as a
whole: scalar fields are upserted and the education/work-history lists are replaced
from the payload. ``import-from-resume`` returns an LLM-extracted draft for the
dashboard to review before saving; it never persists on its own."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..db import get_db
from ..models import ApplicantProfile, ProfileEducation, ProfileExperience, User
from ..schemas import (
    ApplicantProfileIn,
    ApplicantProfileOut,
    ProfileEducationIn,
    ProfileEducationOut,
    ProfileExperienceIn,
    ProfileExperienceOut,
)
from ..services import profile_extract
from ..services.ollama_client import OllamaError

router = APIRouter(prefix="/api/profile", tags=["profile"])

# Scalar profile columns (everything on the profile except the child lists).
_SCALAR_FIELDS = [f for f in ApplicantProfileIn.model_fields if f not in ("education", "experience")]
_EDU_FIELDS = list(ProfileEducationIn.model_fields)
_EXP_FIELDS = list(ProfileExperienceIn.model_fields)


def _edu_out(e: ProfileEducation) -> ProfileEducationOut:
    return ProfileEducationOut(id=e.id, **{f: getattr(e, f) for f in _EDU_FIELDS})


def _exp_out(x: ProfileExperience) -> ProfileExperienceOut:
    return ProfileExperienceOut(id=x.id, **{f: getattr(x, f) for f in _EXP_FIELDS})


def _current(db: Session, user: User) -> ApplicantProfileOut:
    """The user's saved profile, or a blank default with ``email`` pre-filled from
    the account when none exists yet."""
    profile = db.scalar(select(ApplicantProfile).where(ApplicantProfile.user_id == user.id))
    scalars = {f: getattr(profile, f) for f in _SCALAR_FIELDS} if profile else {}
    if not scalars.get("email"):
        scalars["email"] = user.email
    edu = db.scalars(
        select(ProfileEducation)
        .where(ProfileEducation.user_id == user.id)
        .order_by(ProfileEducation.sort_order, ProfileEducation.id)
    )
    exp = db.scalars(
        select(ProfileExperience)
        .where(ProfileExperience.user_id == user.id)
        .order_by(ProfileExperience.sort_order, ProfileExperience.id)
    )
    return ApplicantProfileOut(
        **scalars,
        education=[_edu_out(e) for e in edu],
        experience=[_exp_out(x) for x in exp],
    )


@router.get("", response_model=ApplicantProfileOut)
def get_profile(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _current(db, user)


def _blank_edu(e: ProfileEducationIn) -> bool:
    return not any(getattr(e, f) for f in _EDU_FIELDS)


def _blank_exp(x: ProfileExperienceIn) -> bool:
    # is_current alone (with no company/title/dates/etc.) doesn't make a real entry.
    return not any(getattr(x, f) for f in _EXP_FIELDS if f != "is_current")


@router.put("", response_model=ApplicantProfileOut)
def save_profile(
    payload: ApplicantProfileIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upsert the whole profile: set the scalar fields, then replace the user's
    education/work-history rows from the payload (blank rows are dropped)."""
    profile = db.scalar(select(ApplicantProfile).where(ApplicantProfile.user_id == user.id))
    if profile is None:
        profile = ApplicantProfile(user_id=user.id)
        db.add(profile)
    for field in _SCALAR_FIELDS:
        setattr(profile, field, getattr(payload, field))

    # Replace the child lists wholesale (delete-and-insert) — the form is saved as
    # a single document, so this keeps stored rows exactly matching the payload.
    db.execute(delete(ProfileEducation).where(ProfileEducation.user_id == user.id))
    db.execute(delete(ProfileExperience).where(ProfileExperience.user_id == user.id))
    order = 0
    for e in payload.education:
        if _blank_edu(e):
            continue
        db.add(ProfileEducation(user_id=user.id, sort_order=order, **e.model_dump()))
        order += 1
    order = 0
    for x in payload.experience:
        if _blank_exp(x):
            continue
        db.add(ProfileExperience(user_id=user.id, sort_order=order, **x.model_dump()))
        order += 1
    db.commit()
    return _current(db, user)


@router.post("/import-from-resume", response_model=ApplicantProfileOut)
def import_from_resume(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Extract a draft profile from the user's active résumé via the LLM and return
    it (NOT persisted). The dashboard loads it into the form for review; Save then
    persists it through PUT."""
    try:
        draft = profile_extract.extract_from_resume(db, user)
    except profile_extract.NoResumeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except OllamaError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not import from your résumé: {exc}"
        ) from exc
    # Normalize (blank -> None, drop unknown keys) via the request model, then shape
    # the response. Pre-fill the contact email from the account when the résumé had none.
    normalized = ApplicantProfileIn(**draft)
    out = ApplicantProfileOut(**normalized.model_dump())
    if not out.email:
        out.email = user.email
    return out
