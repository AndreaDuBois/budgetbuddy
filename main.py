from dotenv import load_dotenv
load_dotenv()

import os
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from database import Base, engine, SessionLocal
import models  # noqa: F401

from routers import public, parent, kid, receipts

Base.metadata.create_all(bind=engine)

UPLOAD_DIR = os.getenv("UPLOAD_DIR", "./uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = FastAPI(title="Pocket Money", docs_url=None, redoc_url=None)

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

app.include_router(public.router)
app.include_router(parent.router)
app.include_router(kid.router)
app.include_router(receipts.router)


@app.on_event("startup")
async def _migrate():
    """Add family_id columns and migrate existing data to a default family."""
    with engine.connect() as conn:
        # Add columns to existing tables (silently skip if already present)
        for table, col in [
            ("users",           "family_id INTEGER"),
            ("kid_profiles",    "family_id INTEGER"),
            ("shopping_events", "family_id INTEGER"),
            ("invitations",     "family_id INTEGER"),
        ]:
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col}"))
                conn.commit()
            except Exception:
                pass  # column already exists

        # Assign all unowned rows to a single default family
        orphan = conn.execute(
            text("SELECT id FROM users WHERE family_id IS NULL LIMIT 1")
        ).fetchone()
        if orphan:
            conn.execute(text("INSERT INTO families (created_at) VALUES (datetime('now'))"))
            conn.commit()
            fam_id = conn.execute(text("SELECT last_insert_rowid()")).fetchone()[0]
            for tbl in ("users", "kid_profiles", "shopping_events", "kid_event_budgets"):
                conn.execute(
                    text(f"UPDATE {tbl} SET family_id = :fid WHERE family_id IS NULL"),
                    {"fid": fam_id},
                )
            try:
                conn.execute(
                    text("UPDATE invitations SET family_id = :fid WHERE family_id IS NULL"),
                    {"fid": fam_id},
                )
            except Exception:
                pass
            conn.commit()
