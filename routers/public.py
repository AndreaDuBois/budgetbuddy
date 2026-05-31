import os
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from auth import (
    create_access_token, hash_password, require_authenticated,
    validate_password, verify_password
)
from database import get_db
from models import Family, Invitation, KidProfile, User
import secrets as _secrets
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
        "family_id": str(user.family_id or 0),
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
async def setup_page(request: Request):
    user = require_authenticated(request)
    if user:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "setup.html", {})


@router.post("/setup", response_class=HTMLResponse)
async def setup_post(
    request: Request,
    display_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    form_vals = {"display_name": display_name, "email": email}

    pw_error = validate_password(password)
    if pw_error:
        return templates.TemplateResponse(request, "setup.html", {"error": pw_error, "form": form_vals})
    if password != confirm_password:
        return templates.TemplateResponse(request, "setup.html", {"error": "Passwords do not match.", "form": form_vals})
    if db.query(User).filter(User.email == email.lower().strip()).first():
        return templates.TemplateResponse(request, "setup.html", {"error": "That email is already registered.", "form": form_vals})

    family = Family()
    db.add(family)
    db.flush()

    user = User(
        display_name=display_name.strip(),
        email=email.lower().strip(),
        password_hash=hash_password(password),
        role="parent",
        family_id=family.id,
    )
    db.add(user)
    db.commit()
    return RedirectResponse("/login?setup=1", status_code=302)


# ── Invite join flow ──────────────────────────────────────────────────────────

@router.get("/join/{token}", response_class=HTMLResponse)
async def join_page(request: Request, token: str, db: Session = Depends(get_db)):
    invite = db.query(Invitation).filter(Invitation.token == token).first()
    if not invite or invite.used_at or invite.expires_at < datetime.utcnow():
        return templates.TemplateResponse(request, "join.html", {"invalid": True})

    logged_in = require_authenticated(request)
    kid_name = invite.kid_profile.name if invite.kid_profile else None
    return templates.TemplateResponse(request, "join.html", {
        "token": token,
        "role": invite.role,
        "email": invite.email or "",
        "kid_name": kid_name,
        "logged_in_user": logged_in,
    })


@router.post("/join/{token}/link", response_class=HTMLResponse)
async def join_link_post(request: Request, token: str, db: Session = Depends(get_db)):
    """Link an already-logged-in user's account to the family in the invite."""
    user_jwt = require_authenticated(request)
    if not user_jwt:
        return RedirectResponse(f"/join/{token}", status_code=302)

    invite = db.query(Invitation).filter(Invitation.token == token).first()
    if not invite or invite.used_at or invite.expires_at < datetime.utcnow():
        return templates.TemplateResponse(request, "join.html", {"invalid": True})

    user = db.query(User).filter(User.id == int(user_jwt["sub"])).first()
    if not user:
        return RedirectResponse("/login", status_code=302)

    user.family_id = invite.family_id

    # If this is a kid invite, also link the user to the correct KidProfile
    if invite.role == "kid" and invite.kid_profile_id:
        user.kid_profile_id = invite.kid_profile_id
        user.role = "kid"

    invite.used_at = datetime.utcnow()
    db.commit()

    token_data = {
        "sub": str(user.id),
        "role": user.role,
        "email": user.email,
        "display_name": user.display_name,
        "family_id": str(user.family_id),
    }
    if user.role == "kid" and user.kid_profile_id:
        token_data["kid_id"] = str(user.kid_profile_id)

    new_token = create_access_token(token_data)
    dest = "/parent/dashboard" if user.role == "parent" else "/kid/dashboard"
    resp = RedirectResponse(dest, status_code=302)
    resp.set_cookie("access_token", new_token, httponly=True, samesite="lax", max_age=28800)
    return resp


@router.post("/join/{token}", response_class=HTMLResponse)
async def join_post(
    request: Request,
    token: str,
    display_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    invite = db.query(Invitation).filter(Invitation.token == token).first()
    if not invite or invite.used_at or invite.expires_at < datetime.utcnow():
        return templates.TemplateResponse(request, "join.html", {"invalid": True})

    kid_name = invite.kid_profile.name if invite.kid_profile else None
    form_vals = {"display_name": display_name, "email": email}
    ctx_base = {"token": token, "role": invite.role, "email": invite.email or "", "kid_name": kid_name, "form": form_vals}

    pw_error = validate_password(password)
    if pw_error:
        return templates.TemplateResponse(request, "join.html", {**ctx_base, "error": pw_error})
    if password != confirm_password:
        return templates.TemplateResponse(request, "join.html", {**ctx_base, "error": "Passwords do not match."})

    email_clean = email.lower().strip()

    if invite.role == "kid":
        kid_user = db.query(User).filter(User.kid_profile_id == invite.kid_profile_id).first()
        if kid_user:
            # Kid already has an account — update email/password and ensure family is correct
            conflict = db.query(User).filter(User.email == email_clean, User.id != kid_user.id).first()
            if conflict:
                return templates.TemplateResponse(request, "join.html", {**ctx_base, "error": "That email is already in use."})
            kid_user.email = email_clean
            kid_user.display_name = display_name.strip()
            kid_user.password_hash = hash_password(password)
            kid_user.family_id = invite.family_id  # Always stamp correct family
        else:
            # Kid was added without an email — create their login account now
            if db.query(User).filter(User.email == email_clean).first():
                return templates.TemplateResponse(request, "join.html", {**ctx_base, "error": "That email is already in use by another account."})
            kid_profile_name = invite.kid_profile.name if invite.kid_profile else display_name.strip()
            kid_user = User(
                display_name=kid_profile_name,
                email=email_clean,
                password_hash=hash_password(password),
                role="kid",
                kid_profile_id=invite.kid_profile_id,
                family_id=invite.family_id,
            )
            db.add(kid_user)
    else:
        if db.query(User).filter(User.email == email_clean).first():
            return templates.TemplateResponse(request, "join.html", {**ctx_base, "error": "An account with that email already exists."})
        new_user = User(
            display_name=display_name.strip(),
            email=email_clean,
            password_hash=hash_password(password),
            role="parent",
            family_id=invite.family_id,
        )
        db.add(new_user)

    invite.used_at = datetime.utcnow()
    db.commit()
    return RedirectResponse("/login?joined=1", status_code=302)


@router.get("/accounts/profile", response_class=HTMLResponse)
async def profile_page(request: Request, db: Session = Depends(get_db)):
    user_jwt = require_authenticated(request)
    if not user_jwt:
        return RedirectResponse("/login", status_code=302)
    user = db.query(User).filter(User.id == int(user_jwt["sub"])).first()
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "accounts/profile.html", {
        "current_user": user_jwt,
        "role": user_jwt.get("role"),
        "user": user,
        "saved": request.query_params.get("saved"),
    })


@router.post("/accounts/profile", response_class=HTMLResponse)
async def profile_post(
    request: Request,
    display_name: str = Form(...),
    email: str = Form(...),
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    db: Session = Depends(get_db),
):
    user_jwt = require_authenticated(request)
    if not user_jwt:
        return RedirectResponse("/login", status_code=302)

    user = db.query(User).filter(User.id == int(user_jwt["sub"])).first()
    if not user:
        return RedirectResponse("/login", status_code=302)

    def _err(msg):
        return templates.TemplateResponse(request, "accounts/profile.html", {
            "current_user": user_jwt, "role": user_jwt.get("role"),
            "user": user, "error": msg,
        })

    email_clean = email.lower().strip()
    if email_clean != user.email:
        if db.query(User).filter(User.email == email_clean, User.id != user.id).first():
            return _err("That email is already in use by another account.")

    if new_password:
        if not verify_password(current_password, user.password_hash):
            return _err("Current password is incorrect.")
        pw_error = validate_password(new_password)
        if pw_error:
            return _err(pw_error)
        if new_password != confirm_password:
            return _err("New passwords do not match.")
        user.password_hash = hash_password(new_password)

    user.display_name = display_name.strip()
    user.email = email_clean
    db.commit()

    token_data = {
        "sub": str(user.id),
        "role": user.role,
        "email": user.email,
        "display_name": user.display_name,
        "family_id": str(user.family_id or 0),
    }
    if user.role == "kid" and user.kid_profile_id:
        token_data["kid_id"] = str(user.kid_profile_id)
    new_token = create_access_token(token_data)
    resp = RedirectResponse("/accounts/profile?saved=1", status_code=302)
    resp.set_cookie("access_token", new_token, httponly=True, samesite="lax", max_age=28800)
    return resp


@router.get("/data", response_class=HTMLResponse)
async def admin_data(request: Request, key: str = "", db: Session = Depends(get_db)):
    admin_key = os.getenv("ADMIN_KEY", "")
    if not admin_key or key != admin_key:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "Forbidden"}, status_code=403)

    from models import BudgetEntry, Family, KidProfile, Receipt, ShoppingEvent

    families = db.query(Family).order_by(Family.id).all()
    rows = []
    for fam in families:
        parents = db.query(User).filter(User.family_id == fam.id, User.role == "parent").all()
        kids = db.query(KidProfile).filter(KidProfile.family_id == fam.id).all()
        kid_users = db.query(User).filter(User.family_id == fam.id, User.role == "kid").all()
        events = db.query(ShoppingEvent).filter(ShoppingEvent.family_id == fam.id).all()
        receipts = (
            db.query(Receipt)
            .join(KidProfile, Receipt.kid_id == KidProfile.id)
            .filter(KidProfile.family_id == fam.id)
            .count()
        )
        rows.append({
            "family": fam,
            "parents": parents,
            "kids": kids,
            "kid_users": kid_users,
            "events": events,
            "receipt_count": receipts,
        })

    total_users = db.query(User).count()
    total_families = db.query(Family).count()
    total_receipts = db.query(Receipt).count()

    return templates.TemplateResponse(request, "data.html", {
        "rows": rows,
        "total_users": total_users,
        "total_families": total_families,
        "total_receipts": total_receipts,
    })


@router.get("/about", response_class=HTMLResponse)
async def about_page(request: Request):
    return templates.TemplateResponse(request, "about.html", {})


@router.get("/faq", response_class=HTMLResponse)
async def faq_page(request: Request):
    return templates.TemplateResponse(request, "faq.html", {})


@router.get("/disclaimer", response_class=HTMLResponse)
async def disclaimer_page(request: Request):
    return templates.TemplateResponse(request, "disclaimer.html", {})


@router.get("/offline", response_class=HTMLResponse)
async def offline(request: Request):
    return templates.TemplateResponse(request, "offline.html", {})
