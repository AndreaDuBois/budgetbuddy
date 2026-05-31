import base64
import io
import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image
from groq import Groq

TEXT_MODEL = "llama-3.3-70b-versatile"
VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
MAX_IMAGE_PX = 2048  # longest side


@dataclass
class ParsedLineItem:
    description: str
    quantity: int
    unit_price: Optional[float]
    total_price: float
    suggested_category_name: Optional[str]
    confidence: float


@dataclass
class ParsedReceipt:
    store_name: Optional[str]
    receipt_date: Optional[str]  # YYYY-MM-DD or None
    items: list[ParsedLineItem]
    receipt_total: Optional[float]
    raw_groq_response: str


def _client() -> Groq:
    return Groq(api_key=os.getenv("GROQ_API_KEY", ""))


def _system_prompt(category_names: list[str]) -> str:
    cats = ", ".join(category_names) if category_names else "none"
    return f"""You are a receipt parser for a school clothes shopping budget app.
Extract every purchased line item from this receipt.

Available budget categories: {cats}

Return ONLY valid JSON — no prose, no markdown fences — in exactly this format:
{{
  "store": "store name or null",
  "date": "YYYY-MM-DD or null",
  "items": [
    {{
      "description": "item description as on receipt",
      "quantity": 1,
      "unit_price": 12.99,
      "total_price": 12.99,
      "category": "Shirts",
      "confidence": 0.92
    }}
  ],
  "total": 45.97
}}

Rules:
- "category" must exactly match one of the available categories, or null if none fit
- "confidence" is 0.0–1.0 — your certainty about the category match
- Include only purchased items; exclude taxes, fees, gift cards, totals
- All prices are positive floats
- If no date is visible, use null"""


def _parse_json_response(raw: str) -> dict:
    """Extract JSON from Groq response, tolerating minor formatting issues."""
    text = raw.strip()
    # Strip markdown code fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def _build_receipt(data: dict, raw: str, category_names: list[str]) -> ParsedReceipt:
    items = []
    for item in data.get("items", []):
        desc = str(item.get("description", "")).strip()
        if not desc:
            continue
        qty = int(item.get("quantity", 1) or 1)
        unit_price = float(item.get("unit_price") or 0) or None
        total_price = float(item.get("total_price") or 0)
        cat_name = item.get("category")
        # Validate category name is actually in our list
        if cat_name and cat_name not in category_names:
            cat_name = None
        confidence = float(item.get("confidence") or 0.0)
        if cat_name is None:
            confidence = 0.0

        items.append(ParsedLineItem(
            description=desc,
            quantity=qty,
            unit_price=unit_price,
            total_price=total_price,
            suggested_category_name=cat_name,
            confidence=confidence,
        ))

    total_raw = data.get("total")
    return ParsedReceipt(
        store_name=data.get("store") or None,
        receipt_date=data.get("date") or None,
        items=items,
        receipt_total=float(total_raw) if total_raw else None,
        raw_groq_response=raw,
    )


def parse_pdf_receipt(pdf_path: str, category_names: list[str]) -> ParsedReceipt:
    """Extract text from a PDF then parse with the text model."""
    import pdfplumber
    pages_text = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                pages_text.append(text)
    combined = "\n".join(pages_text).strip()
    if not combined:
        return ParsedReceipt(None, None, [], None, "")
    return parse_text_receipt(combined, category_names)


def parse_text_receipt(text: str, category_names: list[str]) -> ParsedReceipt:
    """Parse receipt from plain text (paste or PDF-extracted)."""
    client = _client()
    response = client.chat.completions.create(
        model=TEXT_MODEL,
        messages=[
            {"role": "system", "content": _system_prompt(category_names)},
            {"role": "user", "content": text},
        ],
        temperature=0.1,
        max_tokens=2048,
    )
    raw = response.choices[0].message.content or ""
    try:
        data = _parse_json_response(raw)
    except (json.JSONDecodeError, ValueError):
        return ParsedReceipt(None, None, [], None, raw)
    return _build_receipt(data, raw, category_names)


def parse_image_receipt(image_path: str, category_names: list[str]) -> ParsedReceipt:
    """Parse receipt from an image file using Groq vision."""
    # Resize and convert to JPEG to reduce token cost
    with Image.open(image_path) as img:
        img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > MAX_IMAGE_PX:
            ratio = MAX_IMAGE_PX / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode()

    prompt = _system_prompt(category_names)
    client = _client()
    response = client.chat.completions.create(
        model=VISION_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        temperature=0.1,
        max_tokens=2048,
    )
    raw = response.choices[0].message.content or ""
    try:
        data = _parse_json_response(raw)
    except (json.JSONDecodeError, ValueError):
        return ParsedReceipt(None, None, [], None, raw)
    return _build_receipt(data, raw, category_names)
