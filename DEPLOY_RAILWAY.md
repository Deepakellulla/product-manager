# Deploying to Railway

Railway keeps the bot running 24/7, without you leaving a laptop on. This assumes
you've already finished steps 1-3 in README.md (sheet is a Google Sheet, bot token
from BotFather, service account created and shared as Editor on the sheet).

## 1. Push the code to GitHub
Railway deploys from a GitHub repo.
1. Create a new, **private** GitHub repo (private matters — your `.env` and key file
   are git-ignored, but keep the repo private anyway).
2. Upload this whole `telegram-bot` folder to it (or `git push` if you use git locally).
   `.gitignore` already excludes `.env` and `service_account.json`, so they won't be
   uploaded even by accident.

## 2. Create the Railway project
1. Go to https://railway.app and sign in (GitHub login is easiest).
2. **New Project → Deploy from GitHub repo** → pick the repo you just created.
3. Railway detects `Procfile` and `requirements.txt` automatically and starts building.
   The first build takes a minute or two.

## 3. Turn your service account JSON into one variable
Railway doesn't give you a place to upload a separate file, so the bot also accepts
the key as a single base64 variable instead of `service_account.json`.

Run this on your own computer, in the folder with your key file:

**Mac/Linux:**
```bash
base64 -i service_account.json | tr -d '\n'
```
**Windows (PowerShell):**
```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("service_account.json"))
```
Copy the long string it prints — you'll paste it into Railway in the next step.

## 4. Set environment variables
In Railway, open your service → **Variables** tab → add these:

| Variable | Value |
|---|---|
| `BOT_TOKEN` | from BotFather |
| `SHEET_ID` | the long ID from your Google Sheet's URL |
| `ADMIN_IDS` | your Telegram ID (get it from `/myid`, see step 6) |
| `GOOGLE_CREDS_JSON` | the base64 string from step 3 |
| `TABS` | `All Products,Bundles,PC Games,Console Games` (or your own tab names) |

Leave `GOOGLE_CREDS_FILE` unset — the bot only needs it if you're not using
`GOOGLE_CREDS_JSON`.

## 5. Deploy
Railway redeploys automatically whenever you add or change a variable. Open the
**Deployments** tab and watch the build log; once it says `Bot started.` in the logs,
it's live.

## 6. Get your Telegram ID (if you haven't already)
Message your bot `/myid` in Telegram — it works even before `ADMIN_IDS` is set. Copy
the number it replies with, put it in the `ADMIN_IDS` variable in Railway, and the
service will redeploy on its own.

## Updating the bot later
Push new code to the same GitHub branch (or re-upload the changed files) — Railway
rebuilds and restarts automatically. No need to touch the variables again unless
something like the token or sheet ID changes.

## Cost
Railway's free trial credit is enough to run a small bot like this for a while; after
that it bills per usage on the Hobby plan (a few dollars a month for a lightweight
bot idling most of the time). Check Railway's current pricing page for exact numbers,
since it can change.

## Troubleshooting
- **Build succeeds but bot doesn't respond:** check the Deployments log for
  `Set BOT_TOKEN and SHEET_ID` or `tabs were not found` errors — usually a typo in a
  variable name or a tab name that doesn't match the sheet exactly.
- **"Not authorised" for everyone, including you:** `ADMIN_IDS` is empty or wrong —
  redo step 6.
- **PERMISSION_DENIED from Google:** the sheet isn't shared with the service
  account's `client_email` as Editor.
