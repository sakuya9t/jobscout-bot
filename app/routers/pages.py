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
    return templates.TemplateResponse("login.html", {"request": request, "mode": "login"})


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "mode": "register"})


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: User | None = Depends(get_optional_user)):
    if user is None:
        return RedirectResponse("/login")
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "user": user, "link_code": user.telegram_link_code},
    )
