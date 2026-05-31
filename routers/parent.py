import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from typing import Optional

from auth import hash_password, require_parent, validate_password
from database import get_db
from models import BudgetEntry, EventCategory, Invitation, KidProfile, Receipt, ShoppingEvent, User
from templates_config import templates

router = APIRouter(prefix="/parent")

AVATAR_COLORS = [
    "#6366f1", "#22c55e", "#f59e0b", "#ef4444",
    "#06b6d4", "#ec4899", "#8b5cf6", "#14b8a6",
]


def _auth(request: Request):
    user = require_parent(request)
    if not user:
        return None, RedirectResponse("/login", status_code=302)
    return user, {"current_user": user, "role": "parent"}


def _fid(user: dict) -> int:
    return int(user.get("family_id", 0)) if user else 0


# ── Dashboard ────────────────────────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    user, ctx = _auth(request)
    if not user:
        return ctx

    fid = _fid(user)
    kids = db.query(KidProfile).filter(KidProfile.family_id == fid).order_by(KidProfile.name).all()
    events = (
        db.query(ShoppingEvent)
        .filter(ShoppingEvent.is_active == True, ShoppingEvent.family_id == fid)
        .order_by(ShoppingEvent.created_at.desc())
        .all()
    )

    summary = {}
    for event in events:
        summary[event.id] = {}
        for kid in kids:
            entries = (
                db.query(BudgetEntry)
                .filter(BudgetEntry.event_id == event.id, BudgetEntry.kid_id == kid.id)
                .all()
            )
            summary[event.id][kid.id] = {
                "budgeted": sum(e.budgeted_amount for e in entries),
                "spent": sum(e.spent_amount for e in entries),
            }

    ctx.update({"kids": kids, "events": events, "summary": summary, "active": "dashboard"})
    return templates.TemplateResponse(request, "parent/dashboard.html", ctx)


# ── Kids ─────────────────────────────────────────────────────────────────────

@router.get("/kids", response_class=HTMLResponse)
async def kids_list(request: Request, db: Session = Depends(get_db)):
    user, ctx = _auth(request)
    if not user:
        return ctx
    kids = db.query(KidProfile).filter(KidProfile.family_id == _fid(user)).order_by(KidProfile.name).all()
    ctx.update({"kids": kids, "active": "kids"})
    return templates.TemplateResponse(request, "parent/kid_list.html", ctx)


@router.get("/kids/new", response_class=HTMLResponse)
async def kid_new_page(request: Request):
    user, ctx = _auth(request)
    if not user:
        return ctx
    ctx.update({"active": "kids", "edit_mode": False, "avatar_colors": AVATAR_COLORS})
    return templates.TemplateResponse(request, "parent/kid_form.html", ctx)


@router.post("/kids/new", response_class=HTMLResponse)
async def kid_new_post(
    request: Request,
    display_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    avatar_color: str = Form("#6366f1"),
    can_adjust_budgets: bool = Form(False),
    db: Session = Depends(get_db),
):
    user, ctx = _auth(request)
    if not user:
        return ctx

    def _err(msg):
        ctx.update({
            "active": "kids", "edit_mode": False, "avatar_colors": AVATAR_COLORS,
            "error": msg,
            "form": {"display_name": display_name, "email": email, "avatar_color": avatar_color},
        })
        return templates.TemplateResponse(request, "parent/kid_form.html", ctx)

    pw_error = validate_password(password)
    if pw_error:
        return _err(pw_error)
    if password != confirm_password:
        return _err("Passwords do not match.")
    if db.query(User).filter(User.email == email.lower().strip()).first():
        return _err("That email is already in use.")

    fid = _fid(user)
    kid_profile = KidProfile(
        name=display_name.strip(),
        avatar_color=avatar_color,
        can_adjust_budgets=can_adjust_budgets,
        family_id=fid,
    )
    db.add(kid_profile)
    db.flush()

    kid_user = User(
        display_name=display_name.strip(),
        email=email.lower().strip(),
        password_hash=hash_password(password),
        role="kid",
        kid_profile_id=kid_profile.id,
        family_id=fid,
    )
    db.add(kid_user)
    db.commit()
    return RedirectResponse("/parent/kids", status_code=302)


@router.get("/kids/{kid_id}", response_class=HTMLResponse)
async def kid_detail(request: Request, kid_id: int, db: Session = Depends(get_db)):
    user, ctx = _auth(request)
    if not user:
        return ctx

    fid = _fid(user)
    kid = db.query(KidProfile).filter(KidProfile.id == kid_id, KidProfile.family_id == fid).first()
    if not kid:
        return RedirectResponse("/parent/kids", status_code=302)

    kid_user = db.query(User).filter(User.kid_profile_id == kid_id).first()
    events = (
        db.query(ShoppingEvent)
        .filter(ShoppingEvent.family_id == fid)
        .order_by(ShoppingEvent.created_at.desc())
        .all()
    )

    budget_map = {}
    for event in events:
        entries = (
            db.query(BudgetEntry)
            .filter(BudgetEntry.event_id == event.id, BudgetEntry.kid_id == kid_id)
            .all()
        )
        budget_map[event.id] = {
            "entries": entries,
            "total_budgeted": sum(e.budgeted_amount for e in entries),
            "total_spent": sum(e.spent_amount for e in entries),
        }

    ctx.update({
        "kid": kid, "kid_user": kid_user, "events": events,
        "budget_map": budget_map, "active": "kids",
    })
    return templates.TemplateResponse(request, "parent/kid_detail.html", ctx)


@router.get("/kids/{kid_id}/edit", response_class=HTMLResponse)
async def kid_edit_page(request: Request, kid_id: int, db: Session = Depends(get_db)):
    user, ctx = _auth(request)
    if not user:
        return ctx
    fid = _fid(user)
    kid = db.query(KidProfile).filter(KidProfile.id == kid_id, KidProfile.family_id == fid).first()
    if not kid:
        return RedirectResponse("/parent/kids", status_code=302)
    kid_user = db.query(User).filter(User.kid_profile_id == kid_id).first()
    ctx.update({
        "active": "kids", "edit_mode": True, "avatar_colors": AVATAR_COLORS,
        "kid": kid, "kid_user": kid_user,
    })
    return templates.TemplateResponse(request, "parent/kid_form.html", ctx)


@router.post("/kids/{kid_id}/edit", response_class=HTMLResponse)
async def kid_edit_post(
    request: Request,
    kid_id: int,
    display_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(""),
    confirm_password: str = Form(""),
    avatar_color: str = Form("#6366f1"),
    can_adjust_budgets: bool = Form(False),
    db: Session = Depends(get_db),
):
    user, ctx = _auth(request)
    if not user:
        return ctx

    fid = _fid(user)
    kid = db.query(KidProfile).filter(KidProfile.id == kid_id, KidProfile.family_id == fid).first()
    kid_user = db.query(User).filter(User.kid_profile_id == kid_id).first()
    if not kid:
        return RedirectResponse("/parent/kids", status_code=302)

    def _err(msg):
        ctx.update({
            "active": "kids", "edit_mode": True, "avatar_colors": AVATAR_COLORS,
            "error": msg, "kid": kid, "kid_user": kid_user,
        })
        return templates.TemplateResponse(request, "parent/kid_form.html", ctx)

    if password:
        pw_error = validate_password(password)
        if pw_error:
            return _err(pw_error)
        if password != confirm_password:
            return _err("Passwords do not match.")

    conflicting = (
        db.query(User)
        .filter(User.email == email.lower().strip(), User.id != (kid_user.id if kid_user else -1))
        .first()
    )
    if conflicting:
        return _err("That email is already in use.")

    kid.name = display_name.strip()
    kid.avatar_color = avatar_color
    kid.can_adjust_budgets = can_adjust_budgets

    if kid_user:
        kid_user.display_name = display_name.strip()
        kid_user.email = email.lower().strip()
        if password:
            kid_user.password_hash = hash_password(password)

    db.commit()
    return RedirectResponse(f"/parent/kids/{kid_id}", status_code=302)


# ── Invitations ───────────────────────────────────────────────────────────────

@router.post("/invite/parent", response_class=HTMLResponse)
async def invite_parent_post(
    request: Request,
    email: str = Form(""),
    db: Session = Depends(get_db),
):
    user, ctx = _auth(request)
    if not user:
        return ctx

    token = secrets.token_urlsafe(32)
    invite = Invitation(
        token=token,
        email=email.lower().strip() or None,
        role="parent",
        created_by_user_id=int(user["sub"]),
        family_id=_fid(user),
        expires_at=datetime.utcnow() + timedelta(days=7),
    )
    db.add(invite)
    db.commit()
    return RedirectResponse(f"/parent/invite/{token}", status_code=302)


@router.post("/invite/kid/{kid_id}", response_class=HTMLResponse)
async def invite_kid_post(
    request: Request,
    kid_id: int,
    db: Session = Depends(get_db),
):
    user, ctx = _auth(request)
    if not user:
        return ctx

    fid = _fid(user)
    kid = db.query(KidProfile).filter(KidProfile.id == kid_id, KidProfile.family_id == fid).first()
    if not kid:
        return RedirectResponse("/parent/kids", status_code=302)

    kid_user = db.query(User).filter(User.kid_profile_id == kid_id).first()

    token = secrets.token_urlsafe(32)
    invite = Invitation(
        token=token,
        email=kid_user.email if kid_user else None,
        role="kid",
        kid_profile_id=kid_id,
        created_by_user_id=int(user["sub"]),
        family_id=fid,
        expires_at=datetime.utcnow() + timedelta(days=7),
    )
    db.add(invite)
    db.commit()
    return RedirectResponse(f"/parent/invite/{token}", status_code=302)


@router.get("/invite/{token}", response_class=HTMLResponse)
async def invite_link_page(request: Request, token: str, db: Session = Depends(get_db)):
    user, ctx = _auth(request)
    if not user:
        return ctx

    invite = db.query(Invitation).filter(Invitation.token == token).first()
    if not invite:
        return RedirectResponse("/parent/dashboard", status_code=302)

    join_url = str(request.base_url) + f"join/{token}"
    kid_name = invite.kid_profile.name if invite.kid_profile else None
    ctx.update({
        "invite": invite,
        "join_url": join_url,
        "kid_name": kid_name,
        "active": "dashboard",
    })
    return templates.TemplateResponse(request, "parent/invite_link.html", ctx)


# ── Events ────────────────────────────────────────────────────────────────────

@router.get("/events", response_class=HTMLResponse)
async def events_list(request: Request, db: Session = Depends(get_db)):
    user, ctx = _auth(request)
    if not user:
        return ctx
    events = (
        db.query(ShoppingEvent)
        .filter(ShoppingEvent.family_id == _fid(user))
        .order_by(ShoppingEvent.created_at.desc())
        .all()
    )
    ctx.update({"events": events, "active": "events"})
    return templates.TemplateResponse(request, "parent/event_list.html", ctx)


@router.get("/events/new", response_class=HTMLResponse)
async def event_new_page(request: Request):
    user, ctx = _auth(request)
    if not user:
        return ctx
    ctx.update({"active": "events", "edit_mode": False})
    return templates.TemplateResponse(request, "parent/event_form.html", ctx)


@router.post("/events/new", response_class=HTMLResponse)
async def event_new_post(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    db: Session = Depends(get_db),
):
    user, ctx = _auth(request)
    if not user:
        return ctx
    event = ShoppingEvent(
        name=name.strip(),
        description=description.strip() or None,
        family_id=_fid(user),
    )
    db.add(event)
    db.commit()
    return RedirectResponse(f"/parent/events/{event.id}", status_code=302)


@router.get("/events/{event_id}", response_class=HTMLResponse)
async def event_detail(request: Request, event_id: int, db: Session = Depends(get_db)):
    user, ctx = _auth(request)
    if not user:
        return ctx

    fid = _fid(user)
    event = db.query(ShoppingEvent).filter(ShoppingEvent.id == event_id, ShoppingEvent.family_id == fid).first()
    if not event:
        return RedirectResponse("/parent/events", status_code=302)

    kids = db.query(KidProfile).filter(KidProfile.family_id == fid).order_by(KidProfile.name).all()
    ctx.update({"event": event, "kids": kids, "active": "events",
                "msg": request.query_params.get("msg", "")})
    return templates.TemplateResponse(request, "parent/event_detail.html", ctx)


@router.post("/events/{event_id}/edit", response_class=HTMLResponse)
async def event_edit_post(
    request: Request,
    event_id: int,
    name: str = Form(...),
    description: str = Form(""),
    is_active: bool = Form(False),
    db: Session = Depends(get_db),
):
    user, ctx = _auth(request)
    if not user:
        return ctx

    fid = _fid(user)
    event = db.query(ShoppingEvent).filter(ShoppingEvent.id == event_id, ShoppingEvent.family_id == fid).first()
    if event:
        event.name = name.strip()
        event.description = description.strip() or None
        event.is_active = is_active
        db.commit()
    return RedirectResponse(f"/parent/events/{event_id}", status_code=302)


@router.post("/events/{event_id}/categories/add", response_class=HTMLResponse)
async def category_add(
    request: Request,
    event_id: int,
    name: str = Form(...),
    qty_needed: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user, ctx = _auth(request)
    if not user:
        return ctx

    fid = _fid(user)
    event = db.query(ShoppingEvent).filter(ShoppingEvent.id == event_id, ShoppingEvent.family_id == fid).first()
    if event:
        max_order = max((c.sort_order for c in event.categories), default=-1)
        cat = EventCategory(
            event_id=event_id,
            name=name.strip(),
            qty_needed=qty_needed.strip() or None,
            notes=notes.strip() or None,
            sort_order=max_order + 1,
        )
        db.add(cat)
        db.commit()
    return RedirectResponse(f"/parent/events/{event_id}?msg=category_added", status_code=302)


@router.post("/events/{event_id}/categories/{cat_id}/edit", response_class=HTMLResponse)
async def category_edit(
    request: Request,
    event_id: int,
    cat_id: int,
    name: str = Form(...),
    qty_needed: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user, ctx = _auth(request)
    if not user:
        return ctx

    fid = _fid(user)
    event = db.query(ShoppingEvent).filter(ShoppingEvent.id == event_id, ShoppingEvent.family_id == fid).first()
    if not event:
        return RedirectResponse("/parent/events", status_code=302)

    cat = db.query(EventCategory).filter(
        EventCategory.id == cat_id, EventCategory.event_id == event_id
    ).first()
    if cat:
        cat.name = name.strip()
        cat.qty_needed = qty_needed.strip() or None
        cat.notes = notes.strip() or None
        db.commit()
    return RedirectResponse(f"/parent/events/{event_id}", status_code=302)


@router.post("/events/{event_id}/categories/{cat_id}/delete", response_class=HTMLResponse)
async def category_delete(
    request: Request,
    event_id: int,
    cat_id: int,
    db: Session = Depends(get_db),
):
    user, ctx = _auth(request)
    if not user:
        return ctx

    fid = _fid(user)
    event = db.query(ShoppingEvent).filter(ShoppingEvent.id == event_id, ShoppingEvent.family_id == fid).first()
    if not event:
        return RedirectResponse("/parent/events", status_code=302)

    cat = db.query(EventCategory).filter(
        EventCategory.id == cat_id, EventCategory.event_id == event_id
    ).first()
    if cat and not cat.receipt_items:
        db.delete(cat)
        db.commit()
    return RedirectResponse(f"/parent/events/{event_id}", status_code=302)


# ── Budgets ───────────────────────────────────────────────────────────────────

@router.get("/events/{event_id}/budgets", response_class=HTMLResponse)
async def budgets_page(request: Request, event_id: int, db: Session = Depends(get_db)):
    user, ctx = _auth(request)
    if not user:
        return ctx

    fid = _fid(user)
    event = db.query(ShoppingEvent).filter(ShoppingEvent.id == event_id, ShoppingEvent.family_id == fid).first()
    if not event:
        return RedirectResponse("/parent/events", status_code=302)

    kids = db.query(KidProfile).filter(KidProfile.family_id == fid).order_by(KidProfile.name).all()

    existing = {}
    for entry in db.query(BudgetEntry).filter(BudgetEntry.event_id == event_id).all():
        existing.setdefault(entry.kid_id, {})[entry.category_id] = entry.budgeted_amount

    ctx.update({"event": event, "kids": kids, "existing": existing, "active": "events"})
    return templates.TemplateResponse(request, "parent/event_budgets.html", ctx)


@router.post("/events/{event_id}/budgets", response_class=HTMLResponse)
async def budgets_post(
    request: Request,
    event_id: int,
    db: Session = Depends(get_db),
):
    user, ctx = _auth(request)
    if not user:
        return ctx

    fid = _fid(user)
    event = db.query(ShoppingEvent).filter(ShoppingEvent.id == event_id, ShoppingEvent.family_id == fid).first()
    if not event:
        return RedirectResponse("/parent/events", status_code=302)

    form = await request.form()
    kids = db.query(KidProfile).filter(KidProfile.family_id == fid).all()

    for cat in event.categories:
        for kid in kids:
            key = f"amount_{kid.id}_{cat.id}"
            raw = form.get(key, "").strip()
            try:
                amount = float(raw) if raw else 0.0
            except ValueError:
                amount = 0.0

            entry = db.query(BudgetEntry).filter(
                BudgetEntry.event_id == event_id,
                BudgetEntry.kid_id == kid.id,
                BudgetEntry.category_id == cat.id,
            ).first()

            if entry:
                entry.budgeted_amount = amount
                entry.last_updated_by = "parent"
            else:
                db.add(BudgetEntry(
                    kid_id=kid.id,
                    event_id=event_id,
                    category_id=cat.id,
                    budgeted_amount=amount,
                    spent_amount=0.0,
                    last_updated_by="parent",
                ))

    db.commit()
    return RedirectResponse(f"/parent/events/{event_id}?msg=budgets_saved", status_code=302)


# ── Receipts ──────────────────────────────────────────────────────────────────

@router.get("/receipts", response_class=HTMLResponse)
async def receipts_list(request: Request, db: Session = Depends(get_db)):
    user, ctx = _auth(request)
    if not user:
        return ctx
    fid = _fid(user)
    receipts = (
        db.query(Receipt)
        .join(KidProfile, Receipt.kid_id == KidProfile.id)
        .filter(KidProfile.family_id == fid)
        .order_by(Receipt.created_at.desc())
        .limit(100)
        .all()
    )
    ctx.update({"receipts": receipts, "active": "receipts"})
    return templates.TemplateResponse(request, "parent/receipt_list.html", ctx)


@router.get("/receipts/{receipt_id}", response_class=HTMLResponse)
async def receipt_detail(request: Request, receipt_id: int, db: Session = Depends(get_db)):
    user, ctx = _auth(request)
    if not user:
        return ctx
    fid = _fid(user)
    receipt = (
        db.query(Receipt)
        .join(KidProfile, Receipt.kid_id == KidProfile.id)
        .filter(Receipt.id == receipt_id, KidProfile.family_id == fid)
        .first()
    )
    if not receipt:
        return RedirectResponse("/parent/receipts", status_code=302)
    ctx.update({"receipt": receipt, "active": "receipts"})
    return templates.TemplateResponse(request, "parent/receipt_detail.html", ctx)
