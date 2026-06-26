"""HTML pages. Auth uses the same httpOnly cookie as the JSON API, so the
dashboard's fetch() calls are authenticated automatically."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..auth import get_optional_user
from ..config import settings
from ..models import User

router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"mode": "login"})


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"mode": "register"})


@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request):
    # Public, like /login. The page shows the TTL so the copy matches the configured
    # expiry; the POST it submits never reveals whether the email exists.
    return templates.TemplateResponse(
        request, "forgot_password.html", {"ttl": settings.password_reset_ttl_minutes}
    )


@router.get("/set-new-password", response_class=HTMLResponse)
def set_new_password_page(request: Request):
    # Public page; the POST it submits requires a valid session (401 -> /login) and only
    # succeeds while must_change_password is set.
    return templates.TemplateResponse(request, "set_new_password.html", {})


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: User | None = Depends(get_optional_user)):
    if user is None:
        return templates.TemplateResponse(request, "landing.html", {})
    # Telegram state (token/link status) is loaded client-side from
    # /api/telegram-config, so the template only needs the user for the header.
    return templates.TemplateResponse(request, "dashboard.html", {"user": user})


@router.get("/positions/{position_id}", response_class=HTMLResponse)
def position_detail_page(
    position_id: int, request: Request, user: User | None = Depends(get_optional_user)
):
    """Per-position detail page. Auth like the dashboard (anonymous -> /login); the
    page's fetch() calls hit /api/positions/{id}/detail and handle a 404 (a position
    not in this user's job list) client-side."""
    if user is None:
        return RedirectResponse("/login")
    return templates.TemplateResponse(
        request, "position_detail.html", {"user": user, "position_id": position_id}
    )


@router.get("/companies/{company_id}", response_class=HTMLResponse)
def company_detail_page(
    company_id: int, request: Request, user: User | None = Depends(get_optional_user)
):
    """Per-company watch-list detail page. Auth like the dashboard; its fetch()
    calls hit /api/companies/{id}/detail and handle a 404 (a company not on this
    user's list) client-side."""
    if user is None:
        return RedirectResponse("/login")
    return templates.TemplateResponse(
        request, "company_detail.html", {"user": user, "company_id": company_id}
    )
