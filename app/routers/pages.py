"""HTML entry points. The app UI is the Vue SPA under ``/app/*`` (served by the static
mount + catch-all in ``main.py``). These routes keep the anonymous landing page
server-rendered (SEO) and redirect every legacy page URL to its SPA equivalent so old
links and bookmarks still work."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..auth import get_optional_user
from ..models import User

router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _to_app(request: Request, path: str) -> RedirectResponse:
    """Redirect to an ``/app`` path, preserving any query string (e.g. ``?next=``)."""
    query = request.url.query
    return RedirectResponse(f"{path}?{query}" if query else path)


@router.get("/", response_class=HTMLResponse)
def home(request: Request, user: User | None = Depends(get_optional_user)):
    # Anonymous visitors get the marketing landing page (kept server-rendered for SEO);
    # a logged-in user goes straight to the SPA.
    if user is None:
        return templates.TemplateResponse(request, "landing.html", {})
    return RedirectResponse("/app")


# Legacy page URLs → their SPA routes (the server-rendered pages were retired at cutover).
@router.get("/login")
def login_page(request: Request):
    return _to_app(request, "/app/login")


@router.get("/register")
def register_page(request: Request):
    return _to_app(request, "/app/register")


@router.get("/forgot-password")
def forgot_password_page(request: Request):
    return _to_app(request, "/app/forgot-password")


@router.get("/set-new-password")
def set_new_password_page(request: Request):
    return _to_app(request, "/app/set-new-password")


@router.get("/positions/{position_id}")
def position_detail_page(position_id: int, request: Request):
    return _to_app(request, f"/app/positions/{position_id}")


@router.get("/companies/{company_id}")
def company_detail_page(company_id: int, request: Request):
    return _to_app(request, f"/app/companies/{company_id}")
