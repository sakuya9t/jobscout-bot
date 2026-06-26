"""HTML pages. Auth uses the same httpOnly cookie as the JSON API, so the
dashboard's fetch() calls are authenticated automatically."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..auth import get_optional_user
from ..models import User

router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"mode": "login"})


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"mode": "register"})


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
