# Product Catalog Telegram Bot

Add, edit, remove and list products in your Google Sheet straight from Telegram.
Works with the "Product Catalog Template" layout (products start at row 5, columns B–F).

## Menu
`/start` shows a persistent menu with **📋 Browse**, **🔍 Find**, **➕ Add** and **❓ Help** buttons,
so day-to-day use rarely needs typing a command. When adding or editing a product, Details/Delivery
also offers your 8 most-used values as buttons, plus a "✏️ Type my own" option for anything new.

## Commands
| Command | What it does |
|---|---|
| `/browse` | Tap through tabs → categories → paginated product lists, no typing needed |
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

### 4. Configure and run
```bash
pip install -r requirements.txt
cp .env.example .env        # then edit .env with your token, sheet ID, tab names
python bot.py
```
First run: leave `ADMIN_IDS` empty, start the bot, send `/myid` to it, put that number in `.env`, restart.

### 5. Keep it running 24/7
The bot only works while `bot.py` is running. Test on your own PC first; for always-on use, see
`DEPLOY_RAILWAY.md` for step-by-step deployment on Railway.

## Notes
- Only Telegram IDs listed in `ADMIN_IDS` can use the bot.
- Product numbers come from the sheet's No. column. After `/remove`, later products move up one number, so run `/list` before editing.
- The bot never touches column A (the No. formula), formatting or dropdowns, only cell values in B–F.
- Tab names in `.env` must match your Google Sheet tabs exactly. The `Lists` tab is used for categories.
- Keep `.env` and `service_account.json` private, and never share your bot token.
