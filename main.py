"""
WhatsApp COD Order Confirmation Agent
FastAPI + Meta WhatsApp Cloud API + Google Sheets (gspread)
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import httpx
import gspread
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from google.oauth2.service_account import Credentials
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cod-agent")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

STORE_NAME = os.getenv("STORE_NAME", "Our Store")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")
SHEET_WEBHOOK_SECRET = os.getenv("SHEET_WEBHOOK_SECRET", "")
RAW_ORDERS_SHEET_ID = os.getenv("RAW_ORDERS_SHEET_ID", "")
CONFIRMED_ORDERS_SHEET_ID = os.getenv("CONFIRMED_ORDERS_SHEET_ID", "")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")

GRAPH_API_URL = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Soft row colors (Google Sheets API RGB 0–1)
COLOR_YELLOW = {"red": 1.0, "green": 0.95, "blue": 0.8}
COLOR_GREEN = {"red": 0.85, "green": 0.95, "blue": 0.83}
COLOR_RED = {"red": 0.96, "green": 0.8, "blue": 0.8}
COLOR_ORANGE = {"red": 0.99, "green": 0.85, "blue": 0.7}

# In-memory edit sessions: phone -> {step, row_index, city?}
edit_sessions: dict[str, dict[str, Any]] = {}

app = FastAPI(title="WhatsApp COD Order Confirmation Agent", version="1.0.0")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class NewOrderPayload(BaseModel):
    row_index: int = Field(..., description="1-based sheet row number")
    nom: str = ""
    num: str = ""
    adress: str = ""
    prix: str | int | float = ""
    ville: str = ""
    model: str = ""


# ---------------------------------------------------------------------------
# Google Sheets helpers
# ---------------------------------------------------------------------------


def get_gspread_client() -> gspread.Client:
    """Load credentials from raw JSON env string or local file path."""
    creds_value = GOOGLE_CREDENTIALS_JSON.strip()
    if not creds_value:
        # Fallback to local service_account.json
        path = "service_account.json"
        if not os.path.isfile(path):
            raise RuntimeError(
                "GOOGLE_CREDENTIALS_JSON is empty and service_account.json not found"
            )
        credentials = Credentials.from_service_account_file(path, scopes=SCOPES)
    elif creds_value.startswith("{"):
        info = json.loads(creds_value)
        credentials = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        # Treat as file path
        if not os.path.isfile(creds_value):
            raise RuntimeError(f"Credentials file not found: {creds_value}")
        credentials = Credentials.from_service_account_file(creds_value, scopes=SCOPES)

    return gspread.authorize(credentials)


def get_raw_worksheet():
    client = get_gspread_client()
    spreadsheet = client.open_by_key(RAW_ORDERS_SHEET_ID)
    return spreadsheet.sheet1


def get_confirmed_worksheet():
    client = get_gspread_client()
    spreadsheet = client.open_by_key(CONFIRMED_ORDERS_SHEET_ID)
    return spreadsheet.sheet1


def update_row_status(
    row_index: int,
    status: str,
    color: dict[str, float],
    bold: bool = False,
) -> None:
    """Update Column G status and color A{row}:G{row}."""
    ws = get_raw_worksheet()
    ws.update_cell(row_index, 7, status)
    cell_range = f"A{row_index}:G{row_index}"
    ws.format(
        cell_range,
        {
            "backgroundColor": color,
            "textFormat": {"bold": bold},
        },
    )


def read_order_row(row_index: int) -> dict[str, str]:
    """Read the 6 data columns from Sheet 1 for a given 1-based row."""
    ws = get_raw_worksheet()
    values = ws.row_values(row_index)
    # Pad to at least 6 columns
    while len(values) < 6:
        values.append("")
    return {
        "nom": values[0] if len(values) > 0 else "",
        "num": values[1] if len(values) > 1 else "",
        "adress": values[2] if len(values) > 2 else "",
        "prix": values[3] if len(values) > 3 else "",
        "ville": values[4] if len(values) > 4 else "",
        "model": values[5] if len(values) > 5 else "",
    }


def append_to_confirmed(order: dict[str, str]) -> None:
    """Append Nom, Num, Adress, Prix, Ville, Model to Sheet 2."""
    ws = get_confirmed_worksheet()
    ws.append_row(
        [
            order.get("nom", ""),
            order.get("num", ""),
            order.get("adress", ""),
            order.get("prix", ""),
            order.get("ville", ""),
            order.get("model", ""),
        ],
        value_input_option="USER_ENTERED",
    )


def update_city_and_address(row_index: int, city: str, address: str) -> None:
    ws = get_raw_worksheet()
    # Column C = Adress (3), Column E = Ville (5)
    ws.update_cell(row_index, 3, address)
    ws.update_cell(row_index, 5, city)


# ---------------------------------------------------------------------------
# Phone normalization (Morocco)
# ---------------------------------------------------------------------------


def normalize_moroccan_phone(raw: str) -> str:
    """
    Normalize Moroccan phone numbers to WhatsApp format: 2126... / 2127...
    Accepts: 06..., 07..., +212..., 00212..., 212...
    """
    if not raw:
        return ""
    digits = re.sub(r"\D", "", str(raw).strip())

    if digits.startswith("00212"):
        digits = digits[2:]  # -> 212...
    elif digits.startswith("212"):
        pass
    elif digits.startswith("0") and len(digits) >= 10:
        digits = "212" + digits[1:]
    elif len(digits) == 9 and digits[0] in ("6", "7"):
        digits = "212" + digits

    return digits


# ---------------------------------------------------------------------------
# WhatsApp Cloud API helpers
# ---------------------------------------------------------------------------


async def send_whatsapp_text(to_phone: str, body: str) -> dict[str, Any]:
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_phone,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }
    return await _post_whatsapp(payload)


async def send_confirmation_buttons(
    to_phone: str,
    nom: str,
    num: str,
    adress: str,
    prix: str,
    ville: str,
    model: str,
    row_index: int,
) -> dict[str, Any]:
    body_text = (
        f"سلام {nom} 👋 توصلنا بالطلب ديالك من {STORE_NAME}\n"
        f"\n"
        f"👤 السمية: {nom}\n"
        f"📞 النمرة: {num}\n"
        f"📍 العنوان: {adress}، {ville}\n"
        f"👟 البرودوي: {model}\n"
        f"🔢 الكمية: 1\n"
        f"💰 الثمن: {prix} درهم · الدفع عند الاستلام\n"
        f"\n"
        f"واش نأكدو الطلب ديالك؟"
    )
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {
                            "id": f"confirm_{row_index}",
                            "title": "Confirm order",
                        },
                    },
                    {
                        "type": "reply",
                        "reply": {
                            "id": f"edit_{row_index}",
                            "title": "Edit order",
                        },
                    },
                    {
                        "type": "reply",
                        "reply": {
                            "id": f"cancel_{row_index}",
                            "title": "Cancel order",
                        },
                    },
                ]
            },
        },
    }
    return await _post_whatsapp(payload)


async def _post_whatsapp(payload: dict[str, Any]) -> dict[str, Any]:
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        raise HTTPException(status_code=500, detail="WhatsApp credentials not configured")
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(GRAPH_API_URL, headers=headers, json=payload)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        if resp.status_code >= 400:
            logger.error("WhatsApp API error %s: %s", resp.status_code, data)
            raise HTTPException(status_code=502, detail={"whatsapp_error": data})
        return data


# ---------------------------------------------------------------------------
# Button / edit handlers
# ---------------------------------------------------------------------------


async def handle_confirm(row_index: int, from_phone: str) -> None:
    edit_sessions.pop(from_phone, None)
    order = read_order_row(row_index)
    update_row_status(row_index, "Confirmed ✅", COLOR_GREEN, bold=True)
    append_to_confirmed(order)
    await send_whatsapp_text(
        from_phone,
        "تم تأكيد الطلب ديالك بنجاح ✅ غادي نتواصلو معاك قريباً للتوصيل. شكراً لثقتك!",
    )


async def handle_cancel(row_index: int, from_phone: str) -> None:
    edit_sessions.pop(from_phone, None)
    update_row_status(row_index, "Canceled ❌", COLOR_RED, bold=True)
    await send_whatsapp_text(
        from_phone,
        "تم إلغاء الطلب ديالك ❌ إلى بغيتي تطلب مرة أخرى مرحبا بك في أي وقت.",
    )


async def handle_edit_start(row_index: int, from_phone: str) -> None:
    update_row_status(row_index, "Editing Details ✏️", COLOR_ORANGE, bold=False)
    edit_sessions[from_phone] = {"step": "WAIT_CITY", "row_index": row_index}
    await send_whatsapp_text(
        from_phone,
        "مرحبا! صيفط لينا دابا غير المدينة الصحيحة ديالك 🏙️:",
    )


async def handle_edit_text(from_phone: str, text: str) -> None:
    session = edit_sessions.get(from_phone)
    if not session:
        return

    step = session.get("step")
    row_index = int(session["row_index"])
    text = (text or "").strip()

    if step == "WAIT_CITY":
        session["city"] = text
        session["step"] = "WAIT_ADDRESS"
        edit_sessions[from_phone] = session
        await send_whatsapp_text(
            from_phone,
            "شكراً! دابا صيفط لينا العنوان الكامل ديالك للتوصيل 📍:",
        )
        return

    if step == "WAIT_ADDRESS":
        new_city = session.get("city", "")
        new_address = text
        update_city_and_address(row_index, new_city, new_address)
        update_row_status(row_index, "Confirmed (Edited) ✅", COLOR_GREEN, bold=True)
        order = read_order_row(row_index)
        append_to_confirmed(order)
        edit_sessions.pop(from_phone, None)
        await send_whatsapp_text(
            from_phone,
            (
                "تم تعديل وتأكيد الطلب ديالك بنجاح ✅\n"
                f"📍 المدينة: {new_city}\n"
                f"🏠 العنوان: {new_address}\n"
                "غادي نتواصلو معاك قريباً للتوصيل!"
            ),
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.post("/webhook/new-order")
async def new_order(
    payload: NewOrderPayload,
    x_webhook_secret: str | None = Header(default=None, alias="x-webhook-secret"),
):
    if not SHEET_WEBHOOK_SECRET or x_webhook_secret != SHEET_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    to_phone = normalize_moroccan_phone(payload.num)
    if not to_phone:
        raise HTTPException(status_code=400, detail="Invalid phone number")

    nom = payload.nom.strip()
    num = str(payload.num).strip()
    adress = str(payload.adress).strip()
    prix = str(payload.prix).strip()
    ville = str(payload.ville).strip()
    model = str(payload.model).strip()
    row_index = payload.row_index

    logger.info("New order row=%s phone=%s nom=%s", row_index, to_phone, nom)

    await send_confirmation_buttons(
        to_phone=to_phone,
        nom=nom,
        num=num,
        adress=adress,
        prix=prix,
        ville=ville,
        model=model,
        row_index=row_index,
    )

    update_row_status(row_index, "Message Sent (Pending)", COLOR_YELLOW, bold=False)
    return {"ok": True, "row_index": row_index, "to": to_phone}


@app.get("/webhook")
@app.get("/webhook/whatsapp")
async def whatsapp_verify(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        logger.info("WhatsApp webhook verified")
        return Response(content=hub_challenge or "", media_type="text/plain")
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook")
@app.post("/webhook/whatsapp")
async def whatsapp_incoming(request: Request):
    body = await request.json()
    logger.info("Incoming WhatsApp webhook: %s", json.dumps(body)[:2000])

    try:
        entries = body.get("entry", [])
        for entry in entries:
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages") or []
                for message in messages:
                    from_phone = normalize_moroccan_phone(message.get("from", ""))
                    msg_type = message.get("type")

                    # Interactive button reply
                    if msg_type == "interactive":
                        interactive = message.get("interactive", {})
                        button_reply = interactive.get("button_reply") or {}
                        button_id = button_reply.get("id", "")
                        await _dispatch_button(button_id, from_phone)
                        continue

                    # Text reply (edit flow)
                    if msg_type == "text":
                        text = (message.get("text") or {}).get("body", "")
                        if from_phone in edit_sessions:
                            await handle_edit_text(from_phone, text)
                        continue
    except HTTPException:
        raise
    except Exception:
        logger.exception("Error processing WhatsApp webhook")

    # Always 200 so Meta does not retry endlessly on app logic errors
    return {"ok": True}


async def _dispatch_button(button_id: str, from_phone: str) -> None:
    if not button_id:
        return

    match = re.match(r"^(confirm|edit|cancel)_(\d+)$", button_id)
    if not match:
        logger.warning("Unknown button id: %s", button_id)
        return

    action, row_str = match.groups()
    row_index = int(row_str)

    if action == "confirm":
        await handle_confirm(row_index, from_phone)
    elif action == "cancel":
        await handle_cancel(row_index, from_phone)
    elif action == "edit":
        await handle_edit_start(row_index, from_phone)


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
