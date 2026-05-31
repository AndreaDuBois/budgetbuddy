import os
import uuid
from datetime import date
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ai_processing import parse_image_receipt, parse_pdf_receipt, parse_text_receipt, ParsedReceipt
from auth import require_authenticated, require_parent
from database import get_db
from models import BudgetEntry, EventCategory, KidProfile, Receipt, ReceiptLineItem, ShoppingEvent, User
from templates_config import templates

router = APIRouter(prefix="/receipts")

UPLOAD_DIR = os.getenv("UPLOAD_DIR", "./uploads")


def _auth(request: Request):
    user = require_authenticated(request)
    if not user:
        return None, RedirectResponse("/login", status_code=302)
    return user, {"current_user": user, "role": user.get("role")}


def _active_event(db: Session, family_id: int = 0):
    q = db.query(ShoppingEvent).filter(ShoppingEvent.is_active == True)
    if family_id:
        q = q.filter(ShoppingEvent.family_id == family_id)
    return q.order_by(ShoppingEvent.created_at.desc()).first()


def _recalc_spent(db: Session, kid_id: int, event_id: int):
    """Recalculate spent_amount for every BudgetEntry for this kid+event."""
    entries = db.query(BudgetEntry).filter(
        BudgetEntry.kid_id == kid_id,
        BudgetEntry.event_id == event_id,
    ).all()
    for entry in entries:
        total = (
            db.query(func.sum(ReceiptLineItem.total_price))
            .join(Receipt)
            .filter(
                Receipt.kid_id == kid_id,
                Receipt.event_id == event_id,
                Receipt.status == "confirmed",
                ReceiptLineItem.category_id == entry.category_id,
                ReceiptLineItem.is_in_budget == True,
                ReceiptLineItem.review_status == "confirmed",
            )
            .scalar()
        ) or 0.0
        entry.spent_amount = total


def _build_advice(db: Session, kid_id: int, event_id: int) -> str:
    """Return advice string if any category is over budget, else empty string."""
    entries = db.query(BudgetEntry).filter(
        BudgetEntry.kid_id == kid_id,
        BudgetEntry.event_id == event_id,
    ).all()
    over = [e for e in entries if e.budgeted_amount > 0 and e.spent_amount > e.budgeted_amount]
    if not over:
        return ""
    parts = [f"${e.spent_amount - e.budgeted_amount:.2f} over on {e.category.name}" for e in over]
    return "Heads up! You're " + ", ".join(parts) + ". Consider adjusting your budget."


# ── Upload picker (works for both parents and kids) ───────────────────────────

@router.get("/upload", response_class=HTMLResponse)
async def upload_picker(request: Request, event_id: int = 0, db: Session = Depends(get_db)):
    user, ctx = _auth(request)
    if not user:
        return ctx
    event, all_events, kids = _ctx_for_upload(user, db, event_id)
    ctx.update({"event": event, "all_events": all_events, "kids": kids, "active": "upload"})
    return templates.TemplateResponse(request, "receipt_upload_picker.html", ctx)


# ── Manual entry ──────────────────────────────────────────────────────────────

@router.get("/upload/manual", response_class=HTMLResponse)
async def upload_manual_page(
    request: Request,
    event_id: int = 0,
    kid_id: int = 0,
    db: Session = Depends(get_db),
):
    user, ctx = _auth(request)
    if not user:
        return ctx

    event, all_events, kids = _ctx_for_upload(user, db, event_id)

    # Determine which kid the receipt is for
    if user.get("role") == "parent":
        selected_kid = db.query(KidProfile).filter(KidProfile.id == kid_id).first() if kid_id else None
    else:
        kids = []
        selected_kid = db.query(KidProfile).filter(
            KidProfile.id == int(user.get("kid_id", 0))
        ).first()

    ctx.update({
        "event": event, "all_events": all_events,
        "kids": kids, "selected_kid": selected_kid,
        "active": "upload",
    })
    return templates.TemplateResponse(request, "receipt_upload_manual.html", ctx)


@router.post("/upload/manual", response_class=HTMLResponse)
async def upload_manual_post(request: Request, db: Session = Depends(get_db)):
    user, ctx = _auth(request)
    if not user:
        return ctx

    form = await request.form()

    event_id = int(form.get("event_id", 0))
    store_name = str(form.get("store_name", "")).strip() or None
    receipt_date_raw = str(form.get("receipt_date", "")).strip()
    row_count = int(form.get("row_count", 1))

    # Determine which kid
    if user.get("role") == "parent":
        kid_id = int(form.get("kid_id", 0))
    else:
        kid_id = int(user.get("kid_id", 0))

    if not event_id or not kid_id:
        return RedirectResponse("/kid/upload" if user.get("role") == "kid" else "/parent/dashboard",
                                status_code=302)

    # Parse receipt date
    receipt_date = None
    if receipt_date_raw:
        try:
            receipt_date = date.fromisoformat(receipt_date_raw)
        except ValueError:
            pass

    # Find uploader user id
    uploader_id = int(user.get("sub", 0))

    receipt = Receipt(
        kid_id=kid_id,
        event_id=event_id,
        uploaded_by_user_id=uploader_id,
        upload_method="manual",
        store_name=store_name,
        receipt_date=receipt_date,
        status="pending_review",
    )
    db.add(receipt)
    db.flush()

    total = 0.0
    for i in range(1, row_count + 1):
        desc = str(form.get(f"desc_{i}", "")).strip()
        if not desc:
            continue
        try:
            qty = int(form.get(f"qty_{i}", 1))
        except (ValueError, TypeError):
            qty = 1
        try:
            price = float(form.get(f"price_{i}", 0))
        except (ValueError, TypeError):
            price = 0.0

        item = ReceiptLineItem(
            receipt_id=receipt.id,
            description=desc,
            quantity=qty,
            unit_price=price / qty if qty else price,
            total_price=price,
            confidence=None,
            review_status="pending",
            is_in_budget=True,
        )
        db.add(item)
        total += price

    receipt.total_amount = round(total, 2)
    db.commit()

    return RedirectResponse(f"/receipts/{receipt.id}/review", status_code=302)


# ── Shared helper ─────────────────────────────────────────────────────────────

def _ctx_for_upload(user, db, event_id):
    """Return (event, all_events, kids) for upload pages."""
    fid = int(user.get("family_id", 0))
    all_events = (
        db.query(ShoppingEvent)
        .filter(ShoppingEvent.is_active == True, ShoppingEvent.family_id == fid)
        .order_by(ShoppingEvent.created_at.desc())
        .all()
    )
    if event_id:
        event = db.query(ShoppingEvent).filter(
            ShoppingEvent.id == event_id, ShoppingEvent.family_id == fid
        ).first()
    else:
        event = all_events[0] if all_events else None
    kids = (
        db.query(KidProfile).filter(KidProfile.family_id == fid).order_by(KidProfile.name).all()
        if user.get("role") == "parent" else []
    )
    return event, all_events, kids


def _save_parsed_receipt(
    db: Session,
    parsed: ParsedReceipt,
    upload_method: str,
    kid_id: int,
    event_id: int,
    uploader_id: int,
    file_path: str | None = None,
    store_name_override: str | None = None,
) -> Receipt:
    """Persist a ParsedReceipt to the DB and return the Receipt object."""
    categories = (db.query(EventCategory)
                  .filter(EventCategory.event_id == event_id)
                  .all())
    cat_by_name = {c.name: c.id for c in categories}

    receipt_date = None
    if parsed.receipt_date:
        try:
            receipt_date = date.fromisoformat(parsed.receipt_date)
        except ValueError:
            pass

    receipt = Receipt(
        kid_id=kid_id,
        event_id=event_id,
        uploaded_by_user_id=uploader_id,
        upload_method=upload_method,
        store_name=store_name_override or parsed.store_name,
        receipt_date=receipt_date,
        total_amount=parsed.receipt_total,
        file_path=file_path,
        groq_raw_response=parsed.raw_groq_response,
        status="pending_review",
    )
    db.add(receipt)
    db.flush()

    for item in parsed.items:
        cat_id = cat_by_name.get(item.suggested_category_name) if item.suggested_category_name else None
        db.add(ReceiptLineItem(
            receipt_id=receipt.id,
            description=item.description,
            quantity=item.quantity,
            unit_price=item.unit_price,
            total_price=item.total_price,
            category_id=cat_id,
            confidence=item.confidence,
            review_status="pending",
            is_in_budget=True,
        ))

    db.commit()
    return receipt


# ── Paste upload ──────────────────────────────────────────────────────────────

@router.get("/upload/paste", response_class=HTMLResponse)
async def upload_paste_page(request: Request, event_id: int = 0, db: Session = Depends(get_db)):
    user, ctx = _auth(request)
    if not user:
        return ctx
    event, all_events, kids = _ctx_for_upload(user, db, event_id)
    ctx.update({"event": event, "all_events": all_events, "kids": kids, "active": "upload"})
    return templates.TemplateResponse(request, "receipt_upload_paste.html", ctx)


@router.post("/upload/paste", response_class=HTMLResponse)
async def upload_paste_post(
    request: Request,
    event_id: int = Form(...),
    kid_id: int = Form(0),
    receipt_text: str = Form(...),
    db: Session = Depends(get_db),
):
    user, ctx = _auth(request)
    if not user:
        return ctx

    if user.get("role") == "kid":
        kid_id = int(user.get("kid_id", 0))

    event = db.query(ShoppingEvent).filter(ShoppingEvent.id == event_id).first()
    if not event or not kid_id:
        return RedirectResponse("/kid/upload", status_code=302)

    categories = db.query(EventCategory).filter(EventCategory.event_id == event_id).all()
    cat_names = [c.name for c in categories]

    try:
        parsed = parse_text_receipt(receipt_text, cat_names)
    except Exception:
        parsed = ParsedReceipt(None, None, [], None, "")

    receipt = _save_parsed_receipt(
        db, parsed, "paste", kid_id, event_id, int(user.get("sub", 0))
    )
    return RedirectResponse(f"/receipts/{receipt.id}/review", status_code=302)


# ── Image upload ──────────────────────────────────────────────────────────────

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


@router.get("/upload/image", response_class=HTMLResponse)
async def upload_image_page(request: Request, event_id: int = 0, db: Session = Depends(get_db)):
    user, ctx = _auth(request)
    if not user:
        return ctx
    event, all_events, kids = _ctx_for_upload(user, db, event_id)
    ctx.update({"event": event, "all_events": all_events, "kids": kids, "active": "upload"})
    return templates.TemplateResponse(request, "receipt_upload_image.html", ctx)


@router.post("/upload/image", response_class=HTMLResponse)
async def upload_image_post(
    request: Request,
    event_id: int = Form(...),
    kid_id: int = Form(0),
    image_file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user, ctx = _auth(request)
    if not user:
        return ctx

    if user.get("role") == "kid":
        kid_id = int(user.get("kid_id", 0))

    event = db.query(ShoppingEvent).filter(ShoppingEvent.id == event_id).first()
    if not event or not kid_id:
        return RedirectResponse("/kid/upload", status_code=302)

    # Read and size-check the file
    data = await image_file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        event, all_events, kids = _ctx_for_upload(user, db, event_id)
        ctx.update({"event": event, "all_events": all_events, "kids": kids,
                    "error": "File too large (max 10 MB).", "active": "upload"})
        return templates.TemplateResponse(request, "receipt_upload_image.html", ctx)

    # Save to disk
    dest_dir = Path(UPLOAD_DIR) / str(uuid.uuid4())
    dest_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(image_file.filename or "receipt.jpg").suffix or ".jpg"
    dest_path = dest_dir / f"original{suffix}"
    dest_path.write_bytes(data)

    categories = db.query(EventCategory).filter(EventCategory.event_id == event_id).all()
    cat_names = [c.name for c in categories]

    try:
        parsed = parse_image_receipt(str(dest_path), cat_names)
    except Exception:
        parsed = ParsedReceipt(None, None, [], None, "")

    receipt = _save_parsed_receipt(
        db, parsed, "image", kid_id, event_id, int(user.get("sub", 0)),
        file_path=str(dest_path),
    )
    return RedirectResponse(f"/receipts/{receipt.id}/review", status_code=302)


# ── PDF upload ────────────────────────────────────────────────────────────────

@router.get("/upload/pdf", response_class=HTMLResponse)
async def upload_pdf_page(request: Request, event_id: int = 0, db: Session = Depends(get_db)):
    user, ctx = _auth(request)
    if not user:
        return ctx
    event, all_events, kids = _ctx_for_upload(user, db, event_id)
    ctx.update({"event": event, "all_events": all_events, "kids": kids, "active": "upload"})
    return templates.TemplateResponse(request, "receipt_upload_pdf.html", ctx)


@router.post("/upload/pdf", response_class=HTMLResponse)
async def upload_pdf_post(
    request: Request,
    event_id: int = Form(...),
    kid_id: int = Form(0),
    pdf_file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user, ctx = _auth(request)
    if not user:
        return ctx

    if user.get("role") == "kid":
        kid_id = int(user.get("kid_id", 0))

    event = db.query(ShoppingEvent).filter(ShoppingEvent.id == event_id).first()
    if not event or not kid_id:
        return RedirectResponse("/kid/upload", status_code=302)

    data = await pdf_file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        event, all_events, kids = _ctx_for_upload(user, db, event_id)
        ctx.update({"event": event, "all_events": all_events, "kids": kids,
                    "error": "File too large (max 10 MB).", "active": "upload"})
        return templates.TemplateResponse(request, "receipt_upload_pdf.html", ctx)

    dest_dir = Path(UPLOAD_DIR) / str(uuid.uuid4())
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / "original.pdf"
    dest_path.write_bytes(data)

    categories = db.query(EventCategory).filter(EventCategory.event_id == event_id).all()
    cat_names = [c.name for c in categories]

    try:
        parsed = parse_pdf_receipt(str(dest_path), cat_names)
    except Exception:
        parsed = ParsedReceipt(None, None, [], None, "")

    receipt = _save_parsed_receipt(
        db, parsed, "pdf", kid_id, event_id, int(user.get("sub", 0)),
        file_path=str(dest_path),
    )
    return RedirectResponse(f"/receipts/{receipt.id}/review", status_code=302)


# ── Review ─────────────────────────────────────────────────────────────────────

@router.get("/{receipt_id}/review", response_class=HTMLResponse)
async def review_page(request: Request, receipt_id: int, db: Session = Depends(get_db)):
    user, ctx = _auth(request)
    if not user:
        return ctx

    receipt = db.query(Receipt).filter(Receipt.id == receipt_id).first()
    if not receipt:
        return RedirectResponse("/", status_code=302)

    # Kids can only review their own receipts
    if user.get("role") == "kid" and receipt.kid_id != int(user.get("kid_id", 0)):
        return RedirectResponse("/kid/receipts", status_code=302)

    categories = (
        db.query(EventCategory)
        .filter(EventCategory.event_id == receipt.event_id)
        .order_by(EventCategory.sort_order)
        .all()
    )

    needs_attention = [i for i in receipt.line_items
                       if i.confidence is None or i.confidence < 0.75 or i.category_id is None]
    ready = [i for i in receipt.line_items
             if i not in needs_attention]

    ctx.update({
        "receipt": receipt,
        "categories": categories,
        "needs_attention": needs_attention,
        "ready": ready,
        "active": "upload",
    })
    return templates.TemplateResponse(request, "receipt_review.html", ctx)


@router.post("/{receipt_id}/review", response_class=HTMLResponse)
async def review_post(request: Request, receipt_id: int, db: Session = Depends(get_db)):
    user, ctx = _auth(request)
    if not user:
        return ctx

    receipt = db.query(Receipt).filter(Receipt.id == receipt_id).first()
    if not receipt:
        return RedirectResponse("/", status_code=302)

    if user.get("role") == "kid" and receipt.kid_id != int(user.get("kid_id", 0)):
        return RedirectResponse("/kid/receipts", status_code=302)

    form = await request.form()

    for item in receipt.line_items:
        val = str(form.get(f"category_{item.id}", "none")).strip()
        if val == "none":
            item.category_id = None
            item.is_in_budget = False
        else:
            try:
                item.category_id = int(val)
                item.is_in_budget = True
            except ValueError:
                item.category_id = None
                item.is_in_budget = False
        item.review_status = "confirmed"

    receipt.status = "confirmed"
    db.flush()

    _recalc_spent(db, receipt.kid_id, receipt.event_id)
    db.commit()

    advice = _build_advice(db, receipt.kid_id, receipt.event_id)
    advice_param = f"?advice={quote(advice)}" if advice else ""

    if user.get("role") == "parent":
        return RedirectResponse(f"/parent/kids/{receipt.kid_id}{advice_param}", status_code=302)
    return RedirectResponse(f"/kid/dashboard{advice_param}", status_code=302)


# ── Delete ─────────────────────────────────────────────────────────────────────

@router.post("/{receipt_id}/delete", response_class=HTMLResponse)
async def receipt_delete(request: Request, receipt_id: int, db: Session = Depends(get_db)):
    user = require_parent(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    receipt = db.query(Receipt).filter(Receipt.id == receipt_id).first()
    if receipt:
        kid_id = receipt.kid_id
        event_id = receipt.event_id
        db.delete(receipt)
        db.flush()
        _recalc_spent(db, kid_id, event_id)
        db.commit()

    return RedirectResponse("/parent/receipts", status_code=302)
