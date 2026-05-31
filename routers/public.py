from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from auth import (
    create_access_token, hash_password, require_authenticated,
    require_parent, verify_password
)
from database import get_db
from models import KidProfile, User
from templates_config import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def root(request: Request):
    user = require_authenticated(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.get("role") == "parent":
        return RedirectResponse("/parent/dashboard", status_code=302)
    return RedirectResponse("/kid/dashboard", status_code=302)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    user = require_authenticated(request)
    if user:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"error": error})


@router.post("/login", response_class=HTMLResponse)
async def login_post(
    request: Request,
    response: Response,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == email.lower().strip()).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request, "login.html", {"error": "Invalid email or password"}, status_code=401
        )

    token_data = {
        "sub": str(user.id),
        "role": user.role,
        "email": user.email,
        "display_name": user.display_name,
    }
    if user.role == "kid" and user.kid_profile_id:
        token_data["kid_id"] = str(user.kid_profile_id)

    token = create_access_token(token_data)
    redirect_url = "/parent/dashboard" if user.role == "parent" else "/kid/dashboard"
    resp = RedirectResponse(redirect_url, status_code=302)
    resp.set_cookie("access_token", token, httponly=True, samesite="lax", max_age=28800)
    return resp


@router.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("access_token")
    return resp


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request, db: Session = Depends(get_db)):
    if db.query(User).filter(User.role == "parent").first():
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "setup.html", {})


@router.post("/setup", response_class=HTMLResponse)
async def setup_post(
    request: Request,
    display_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    if db.query(User).filter(User.role == "parent").first():
        return RedirectResponse("/login", status_code=302)

    if db.query(User).filter(User.email == email.lower().strip()).first():
        return templates.TemplateResponse(
            request, "setup.html", {"error": "That email is already registered"}
        )

    user = User(
        display_name=display_name.strip(),
        email=email.lower().strip(),
        password_hash=hash_password(password),
        role="parent",
    )
    db.add(user)
    db.commit()
    return RedirectResponse("/login?setup=1", status_code=302)


@router.get("/offline", response_class=HTMLResponse)
async def offline(request: Request):
    return templates.TemplateResponse(request, "offline.html", {})
