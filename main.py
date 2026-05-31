from dotenv import load_dotenv
load_dotenv()

import os
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from database import Base, engine
import models  # noqa: F401 — registers all models with Base

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
