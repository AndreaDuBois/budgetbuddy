from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from auth import require_kid
from database import get_db
from models import BudgetEntry, KidEventBudget, KidProfile, Receipt, ShoppingEvent
from templates_config import templates

router = APIRouter(prefix="/kid")


def _auth(request: Request):
    user = require_kid(request)
    if not user:
        return None, RedirectResponse("/login", status_code=302)
    return user, {"current_user": user, "role": "kid"}


def _get_kid(user: dict, db: Session):
    kid_id = int(user.get("kid_id", 0))
    return db.query(KidProfile).filter(KidProfile.id == kid_id).first()


def _family_events(db: Session, family_id: int):
    """All active events scoped to a family."""
    return (
        db.query(ShoppingEvent)
        .filter(ShoppingEvent.is_active == True, ShoppingEvent.family_id == family_id)
        .order_by(ShoppingEvent.created_at.desc())
        .all()
    )


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, event_id: int = 0, db: Session = Depends(get_db)):
    user, ctx = _auth(request)
    if not user:
        return ctx

    kid = _get_kid(user, db)
    if not kid:
        return RedirectResponse("/login", status_code=302)

    fid = kid.family_id or 0
    all_events = _family_events(db, fid)

    if event_id:
        event = db.query(ShoppingEvent).filter(
            ShoppingEvent.id == event_id, ShoppingEvent.family_id == fid
        ).first()
    else:
        event = all_events[0] if all_events else None

    entries = []
    if event:
        entries = (
            db.query(BudgetEntry)
            .filter(BudgetEntry.event_id == event.id, BudgetEntry.kid_id == kid.id)
            .all()
        )

    total_budgeted = sum(e.budgeted_amount for e in entries)
    total_spent = sum(e.spent_amount for e in entries)

    # Overall budget set by parent (e.g. $450 total shopping money)
    kid_event_budget = None
    if event:
        kid_event_budget = db.query(KidEventBudget).filter(
            KidEventBudget.kid_id == kid.id,
            KidEventBudget.event_id == event.id,
        ).first()

    # Over-budget categories for the modal
    over_budget = [
        e for e in entries
        if e.budgeted_amount > 0 and e.spent_amount > e.budgeted_amount
    ]

    advice = request.query_params.get("advice", "")

    ctx.update({
        "kid": kid,
        "event": event,
        "all_events": all_events,
        "entries": entries,
        "total_budgeted": total_budgeted,
        "total_spent": total_spent,
        "kid_event_budget": kid_event_budget,
        "over_budget": over_budget,
        "advice": advice,
        "active": "dashboard",
    })
    return templates.TemplateResponse(request, "kid/dashboard.html", ctx)


# ── Upload picker ─────────────────────────────────────────────────────────────

@router.get("/upload", response_class=HTMLResponse)
async def upload_picker(request: Request, db: Session = Depends(get_db)):
    user, ctx = _auth(request)
    if not user:
        return ctx

    kid = _get_kid(user, db)
    if not kid:
        return RedirectResponse("/login", status_code=302)

    all_events = _family_events(db, kid.family_id or 0)
    event = all_events[0] if all_events else None

    ctx.update({"kid": kid, "event": event, "all_events": all_events, "active": "upload"})
    return templates.TemplateResponse(request, "kid/upload.html", ctx)


# ── Receipt history ───────────────────────────────────────────────────────────

@router.get("/receipts", response_class=HTMLResponse)
async def receipts_list(request: Request, db: Session = Depends(get_db)):
    user, ctx = _auth(request)
    if not user:
        return ctx

    kid = _get_kid(user, db)
    if not kid:
        return RedirectResponse("/login", status_code=302)

    receipts = (
        db.query(Receipt)
        .filter(Receipt.kid_id == kid.id)
        .order_by(Receipt.created_at.desc())
        .all()
    )
    ctx.update({"kid": kid, "receipts": receipts, "active": "receipts"})
    return templates.TemplateResponse(request, "kid/receipt_history.html", ctx)


@router.get("/receipts/{receipt_id}", response_class=HTMLResponse)
async def receipt_detail(request: Request, receipt_id: int, db: Session = Depends(get_db)):
    user, ctx = _auth(request)
    if not user:
        return ctx

    kid = _get_kid(user, db)
    receipt = db.query(Receipt).filter(
        Receipt.id == receipt_id, Receipt.kid_id == kid.id
    ).first() if kid else None

    if not receipt:
        return RedirectResponse("/kid/receipts", status_code=302)

    ctx.update({"kid": kid, "receipt": receipt, "active": "receipts"})
    return templates.TemplateResponse(request, "kid/receipt_detail.html", ctx)


# ── Budget adjust ─────────────────────────────────────────────────────────────

@router.post("/budgets/{entry_id}/adjust", response_class=HTMLResponse)
async def budget_adjust(
    request: Request,
    entry_id: int,
    amount: float = Form(...),
    db: Session = Depends(get_db),
):
    user, ctx = _auth(request)
    if not user:
        return ctx

    kid = _get_kid(user, db)
    if not kid or not kid.can_adjust_budgets:
        return RedirectResponse("/kid/dashboard", status_code=302)

    entry = db.query(BudgetEntry).filter(
        BudgetEntry.id == entry_id, BudgetEntry.kid_id == kid.id
    ).first()

    if entry:
        entry.budgeted_amount = max(0.0, amount)
        entry.last_updated_by = "kid"
        db.commit()

    return RedirectResponse("/kid/dashboard", status_code=302)
