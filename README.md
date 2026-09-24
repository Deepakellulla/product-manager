# Product Catalog Telegram Bot

Add, edit, remove and list products in your Google Sheet straight from Telegram.
Works with the "Product Catalog Template" layout (products start at row 5, columns B–F).

## Menu
`/start` shows a persistent menu with **📋 Browse**, **🔍 Find**, **➕ Add** and **❓ Help** buttons,
so day-to-day use rarely needs typing a command. When adding or editing a product, Details/Delivery
also offers your 8 most-used values as buttons, plus a "✏️ Type my own" option for anything new.

Adding a product now ends on a **review screen** — the full product with its price, category,
status and details — before anything is written to the sheet. From there:
- **✅ Confirm & Save** writes it to the sheet, and offers an **➕ Add another** button so you can
  keep adding products back-to-back without retyping `/add`.
- **✏️ Edit** lets you fix any one field and returns to the review screen.
- **❌ Cancel** discards it — nothing is saved.

## Commands
| Command | What it does |
|---|---|
| `/browse` | Tap through tabs → categories → paginated product lists, no typing needed |
| `/import` | Bulk-add products by sending screenshots (needs `GEMINI_API_KEY`, see below) |
| `/find netflix` | Search all tabs by name (typo-tolerant) with Edit / Stock / Delete buttons on each result |
| `/list` | Show products in the current tab |
| `/add` | Guided flow: name → price → category → status → details |
| `/edit 3` | Change any field of product No. 3 |
| `/stock 3` | Toggle In Stock / Out of Stock |
| `/remove 3` | Delete product No. 3 (asks to confirm) |
| `/tab` | Switch between your sheet tabs |
| `/myid` | Show your Telegram ID |

## Setup

### 1. Make the sheet a real Google Sheet
Upload the .xlsx to Drive, open it, then **File → Save as Google Sheets**.
Copy the Sheet ID from its URL: `docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit`

### 2. Create the Telegram bot
Open **@BotFather** in Telegram → `/newbot` → copy the token.

### 3. Let the bot access your sheet (Google service account)
1. Go to https://console.cloud.google.com and create a project.
2. Enable the **Google Sheets API** and **Google Drive API** (APIs & Services → Library).
3. IAM & Admin → **Service Accounts** → Create → then open it → **Keys → Add key → JSON**.
4. Save the downloaded file next to `bot.py` as `service_account.json`.
5. Open the JSON, copy the `client_email` value, and **share your Google Sheet with that email as Editor**.

### 4. (Optional) Enable screenshot bulk-import
1. Go to https://aistudio.google.com/apikey with any Google account. No credit card needed.
2. Click **Create API key** and copy it.
3. Put it in `.env` as `GEMINI_API_KEY`. Leave it blank to skip this feature entirely.
This uses Google's free tier, which has rate limits (fine for occasional bulk imports, not for
nonstop use). If you skip this step, every command still works except `/import`.

### 5. Configure and run
```bash
pip install -r requirements.txt
cp .env.example .env        # then edit .env with your token, sheet ID, tab names
python bot.py
```
First run: leave `ADMIN_IDS` empty, start the bot, send `/myid` to it, put that number in `.env`, restart.

### 6. Keep it running 24/7
The bot only works while `bot.py` is running. Test on your own PC first; for always-on use, see
`DEPLOY_RAILWAY.md` for step-by-step deployment on Railway.

## Screenshot bulk-import
Send `/import`, then send screenshots of another seller's product list (or your own notes) one
at a time. The bot reads each one with Google's free Gemini vision API and keeps a running total.
Send `/doneimport` when finished — it shows a preview and asks you to confirm before writing
anything. Products whose name matches one already in the tab are skipped automatically, so
you can send overlapping screenshots without creating duplicates. `/cancelimport` discards
everything collected so far.

Since it's OCR-based, always check prices in the preview before confirming — misreads happen
occasionally, especially with numbers. For 300+ products you'll need several screenshots
(each one only fits ~20-30 rows), and Google's free tier has rate limits, so if a screenshot
fails with a "rate-limited" message, just wait a minute and resend it.

## Notes
- Only Telegram IDs listed in `ADMIN_IDS` can use the bot.
- Product numbers come from the sheet's No. column. After `/remove`, later products move up one number, so run `/list` before editing.
- The bot never touches column A (the No. formula), formatting or dropdowns, only cell values in B–F.
- Tab names in `.env` must match your Google Sheet tabs exactly. The `Lists` tab is used for categories.
- Keep `.env` and `service_account.json` private, and never share your bot token.
