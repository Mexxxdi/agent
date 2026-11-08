# WhatsApp COD Order Confirmation Agent

Lightweight production agent that confirms Shopify Cash-on-Delivery orders over **WhatsApp** (Meta Cloud API), driven by **Google Sheets** and a **FastAPI** backend.

## How it works

1. Shopify (or any source) writes a new COD order into **Sheet 1 (Raw Orders)**.
2. Google Apps Script finds rows with name + phone but empty `Status`, then `POST`s to FastAPI.
3. FastAPI sends an interactive WhatsApp message (Confirm / Edit / Cancel).
4. Customer replies → FastAPI updates Sheet 1 status/colors and, on confirm/edit, copies the order to **Sheet 2 (Confirmed Orders)**.

```
Shopify → Sheet 1 → Apps Script → FastAPI → WhatsApp
                         ↑              │
                         └──────────────┘  (button / text replies)
                                    ↓
                              Sheet 2 (confirmed)
```

## Sheet layout

### Sheet 1 — Raw Orders (`RAW_ORDERS_SHEET_ID`)

| A Nom | B Num | C Adress | D Prix | E Ville | F Model | G Status |
|-------|-------|----------|--------|---------|---------|----------|

`Status` values used by the agent: `Sending...`, `Message Sent (Pending)`, `Confirmed ✅`, `Canceled ❌`, `Editing Details ✏️`, `Confirmed (Edited) ✅`.

### Sheet 2 — Confirmed Orders (`CONFIRMED_ORDERS_SHEET_ID`)

Separate Google Spreadsheet. First worksheet headers:

| A Nom | B Num | C Adress | D Prix | E Ville | F Model |
|-------|-------|----------|--------|---------|---------|

---

## 1. Google Cloud service account

1. Create (or reuse) a Google Cloud project.
2. Enable **Google Sheets API** and **Google Drive API**.
3. Create a **Service Account** → download the JSON key.
4. Share **both** spreadsheets with the service account email (`…@….iam.gserviceaccount.com`) as **Editor**.
5. Copy each spreadsheet ID from the URL:  
   `https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit`

Locally you can save the key as `service_account.json` in this folder, or paste the JSON into `GOOGLE_CREDENTIALS_JSON`.

---

## 2. Prepare the two spreadsheets

1. **Sheet 1**: add header row exactly as above, including column G `Status`.
2. **Sheet 2**: create a **new** spreadsheet (separate file), add the 6 headers on row 1.
3. Note both spreadsheet IDs for `.env`.

---

## 3. Meta WhatsApp Cloud API

1. In [Meta for Developers](https://developers.facebook.com/), create an app with **WhatsApp**.
2. From WhatsApp → API Setup, copy:
   - **Temporary** (or permanent) access token → `WHATSAPP_TOKEN`
   - **Phone number ID** → `PHONE_NUMBER_ID`
3. Choose a private string for `VERIFY_TOKEN` (you will enter the same value in Meta’s webhook UI).
4. After the API is deployed (step 5), configure the webhook:
   - Callback URL: `https://<your-host>/webhook/whatsapp`
   - Verify token: same as `VERIFY_TOKEN`
   - Subscribe to the **`messages`** field on your WhatsApp Business Account.

> Interactive reply buttons work inside the 24-hour customer care window. For production outreach to customers who have not messaged you first, you typically need an approved **message template**; this agent uses session interactive messages after the order trigger.

---

## 4. Environment variables

Copy `.env.example` to `.env` and fill in:

| Variable | Purpose |
|----------|---------|
| `STORE_NAME` | Shown in the Darija confirmation body |
| `WHATSAPP_TOKEN` | Meta Graph API bearer token |
| `PHONE_NUMBER_ID` | WhatsApp sender phone number ID |
| `VERIFY_TOKEN` | Meta webhook handshake |
| `SHEET_WEBHOOK_SECRET` | Shared secret for Apps Script → FastAPI |
| `RAW_ORDERS_SHEET_ID` | Sheet 1 ID |
| `CONFIRMED_ORDERS_SHEET_ID` | Sheet 2 ID |
| `GOOGLE_CREDENTIALS_JSON` | Full service-account JSON **or** path like `service_account.json` |

---

## 5. Deploy

### Option A — Local

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
# source .venv/bin/activate

pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Health check: `GET http://localhost:8000/health` → `{"status":"healthy"}`.

Expose with a tunnel (ngrok, Cloudflare Tunnel, etc.) so Meta and Apps Script can reach you:

`https://<tunnel-host>/webhook/whatsapp`

### Option B — Render (Docker)

1. Push this repo to GitHub.
2. In Render, use the included [`render.yaml`](render.yaml) blueprint **or** create a **Web Service** with **Docker**.
3. Set all environment variables (`sync: false` secrets) in the Render dashboard. Paste the service-account JSON as a single-line value for `GOOGLE_CREDENTIALS_JSON`.
4. After deploy, note the public URL, e.g. `https://whatsapp-cod-agent.onrender.com`.
5. Point Meta’s webhook to `https://<render-url>/webhook/whatsapp` and verify.

---

## 6. Wire Google Apps Script (Sheet 1)

1. Open Sheet 1 → **Extensions → Apps Script**.
2. Paste the contents of [`apps_script.js`](apps_script.js) and save.
3. **Project Settings → Script properties** add:
   - `API_BASE_URL` = `https://<your-host>` (no trailing slash)
   - `WEBHOOK_SECRET` = same as `SHEET_WEBHOOK_SECRET`
4. **Triggers → Add Trigger**:
   - Function: `checkNewOrders`
   - Time-driven → every 1 minute  
   (Optional installable `onEdit` calling `onEditInstallable` for faster pickup.)
5. Authorize the script when prompted (needs permission to call external URLs).

The script sets `Status` to `Sending...`, POSTs to `/webhook/new-order` with header `x-webhook-secret`, and clears Status on HTTP failure so the row can retry.

---

## 7. Smoke test

1. Add a test row on Sheet 1 with a real WhatsApp-capable Moroccan number (`06…` / `07…` / `+212…`).
2. Leave `Status` empty and wait for the trigger (or run `checkNewOrders` manually).
3. You should receive the Darija interactive message with three buttons.
4. Try each path:
   - **Confirm order** → Sheet 1 green `Confirmed ✅`, row copied to Sheet 2.
   - **Cancel order** → Sheet 1 red `Canceled ❌`, not copied.
   - **Edit order** → reply with city, then address → Sheet 1 green `Confirmed (Edited) ✅`, updated row on Sheet 2.

---

## API reference

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Cloud health check |
| `POST` | `/webhook/new-order` | Apps Script new-order trigger (`x-webhook-secret`) |
| `GET` | `/webhook/whatsapp` | Meta verify handshake |
| `POST` | `/webhook/whatsapp` | Inbound messages / button clicks |

### New-order body example

```json
{
  "row_index": 2,
  "nom": "Kamal",
  "num": "0612345678",
  "adress": "Rue Example 12",
  "prix": "299",
  "ville": "Casablanca",
  "model": "Sneaker Noir 42"
}
```

Phone numbers are normalized to `2126…` / `2127…` before calling WhatsApp.

---

## Notes

- **Edit sessions** are stored in memory on the FastAPI process. A redeploy/restart clears them; customers mid-edit would need to tap **Edit order** again. Fine for a single Render instance.
- Row indexes in button IDs (`confirm_2`, etc.) are **1-based Google Sheet row numbers**.
- Do not commit real `.env`, tokens, or `service_account.json`.
