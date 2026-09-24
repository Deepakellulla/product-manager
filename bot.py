"""Telegram bot to add / edit / remove products in the Product Catalog Google Sheet.

Sheet layout expected on every product tab:
  Row 4 = headers, products start on row 5 (up to row 204)
  A = No. (formula, never touched)  B = Name  C = Price  D = Category  E = Status  F = Details
"""
import asyncio
import base64
import difflib
import html
import json
import logging
import os
import re
import tempfile
import time
import zlib
from functools import wraps

import gspread
import requests
from dotenv import load_dotenv
from telegram import (
    BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, Update,
)
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters,
)

load_dotenv()
logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("catalog-bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SHEET_ID = os.environ.get("SHEET_ID", "")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
CREDS_FILE = os.getenv("GOOGLE_CREDS_FILE", "service_account.json")
TABS = [t.strip() for t in os.getenv("TABS", "All Products,Bundles,PC Games,Console Games").split(",") if t.strip()]
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# Models are tried in order; if one is overloaded (503) or gone (404) the next one is used.
GEMINI_MODELS = [m.strip() for m in os.environ.get("GEMINI_MODELS", "gemini-3.6-flash,gemini-3.5-flash-lite,gemini-3.7-flash").split(",") if m.strip()]

FIRST_ROW = 5
LAST_ROW = int(os.environ.get("LAST_ROW", "204"))  # last sheet row the bot may use; raise it after extending your sheet
MAX_ITEMS = LAST_ROW - FIRST_ROW + 1
COLS = "BCDEF"
FIELD_NAMES = ["Name", "Price", "Category", "Status", "Details"]
STATUSES = ["In Stock", "Out of Stock"]

book = None
_ws_cache = {}


# ───────────────────────── Google Sheets helpers (blocking) ─────────────────────────
def get_ws(tab):
    if tab not in _ws_cache:
        _ws_cache[tab] = book.worksheet(tab)
    return _ws_cache[tab]


def _blank():
    return [""] * 5


def _is_item(row):
    return str(row[0]).strip() != ""


def _get_rows(tab):
    raw = get_ws(tab).get(f"B{FIRST_ROW}:F{LAST_ROW}", value_render_option="UNFORMATTED_VALUE")
    rows = [(list(r) + [""] * 5)[:5] for r in raw]
    rows += [_blank() for _ in range(MAX_ITEMS - len(rows))]
    return rows


def list_items(tab):
    return [(i + 1, r) for i, r in enumerate(_get_rows(tab)) if _is_item(r)]


def get_item(tab, n):
    if not 1 <= n <= MAX_ITEMS:
        return None
    r = FIRST_ROW + n - 1
    raw = get_ws(tab).get(f"B{r}:F{r}", value_render_option="UNFORMATTED_VALUE")
    row = (list(raw[0]) + [""] * 5)[:5] if raw else _blank()
    return row if _is_item(row) else None


def add_item(tab, values):
    for i, r in enumerate(_get_rows(tab)):
        if not _is_item(r):
            row = FIRST_ROW + i
            get_ws(tab).update(range_name=f"B{row}:F{row}", values=[values], value_input_option="RAW")
            return i + 1
    raise RuntimeError(f"This tab is full ({MAX_ITEMS} products).")


def update_field(tab, n, idx, value):
    get_ws(tab).update(range_name=f"{COLS[idx]}{FIRST_ROW + n - 1}", values=[[value]], value_input_option="RAW")


def remove_item(tab, n):
    rows = _get_rows(tab)
    del rows[n - 1]
    rows = [r for r in rows if _is_item(r)]           # close any gaps
    rows += [_blank() for _ in range(MAX_ITEMS - len(rows))]
    get_ws(tab).update(range_name=f"B{FIRST_ROW}:F{LAST_ROW}", values=rows, value_input_option="RAW")


def bulk_add_items(tab, rows):
    """Append many rows in a single write. Skips any whose name already exists in the tab
    (case-insensitive), including duplicates within the same batch. Returns (added, skipped_names)."""
    existing = {str(r[0]).strip().lower() for _, r in list_items(tab)}
    to_add, skipped = [], []
    for row in rows:
        key = str(row[0]).strip().lower()
        if not key or key in existing:
            if key:
                skipped.append(row[0])
            continue
        existing.add(key)
        to_add.append(row)
    if not to_add:
        return 0, skipped
    current = _get_rows(tab)
    first_empty = next((i for i, r in enumerate(current) if not _is_item(r)), None)
    room = MAX_ITEMS - first_empty if first_empty is not None else 0
    if first_empty is None or len(to_add) > room:
        raise RuntimeError(f"Not enough room in '{tab}' - only {max(room, 0)} slot(s) left, tried to add {len(to_add)}.")
    start_row = FIRST_ROW + first_empty
    end_row = start_row + len(to_add) - 1
    get_ws(tab).update(range_name=f"B{start_row}:F{end_row}", values=to_add, value_input_option="RAW")
    return len(to_add), skipped


def get_categories(tab=None):
    """Categories actually in use, taken from product data (column D) so the list always
    matches the real sheet instead of a separately maintained one. tab=None scans every
    tab; a specific tab scans just that one. Falls back to the Lists tab (if any) for
    categories not yet used on any product."""
    seen, cats = set(), []
    for t in ([tab] if tab else TABS):
        for _, row in list_items(t):
            cat = str(row[2]).strip()
            if cat and cat not in seen:
                seen.add(cat)
                cats.append(cat)
    if not tab:
        try:
            for v in book.worksheet("Lists").col_values(1)[1:31]:
                v = v.strip()
                if v and v not in seen:
                    seen.add(v)
                    cats.append(v)
        except gspread.exceptions.WorksheetNotFound:
            pass
    return cats


def get_details_options(tab, limit=8):
    """The most-used Details/Delivery strings already in `tab`, most common first,
    so adding a product can reuse one instead of retyping it."""
    counts, order = {}, []
    for _, row in list_items(tab):
        d = str(row[4]).strip()
        if not d:
            continue
        if d not in counts:
            counts[d] = 0
            order.append(d)
        counts[d] += 1
    order.sort(key=lambda d: -counts[d])
    return order[:limit]


def page_items(tab, category, page, page_size=6):
    """One page of in-stock-and-out-of-stock items in `tab` matching `category` exactly.
    Returns (page_rows, total_pages) where page_rows is [(n, row), ...]."""
    want = "" if category == "(no category)" else category
    matches = [(n, row) for n, row in list_items(tab) if str(row[2]).strip() == want]
    total_pages = max(1, (len(matches) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    return matches[start:start + page_size], total_pages, page


MAX_RESULTS = 8


def _crc(name):
    """Short fingerprint of a product name, used to detect stale buttons."""
    return format(zlib.crc32(str(name).encode("utf-8")) & 0xFFFF, "x")


def _fuzzy_hit(tokens, name):
    words = re.findall(r"\w+", name.lower())
    for t in tokens:
        if len(t) < 3 or not any(difflib.SequenceMatcher(None, t, w).ratio() >= 0.75 for w in words):
            return False
    return True


def search_all(query):
    """Search every tab. Returns [(tab_index, number, row)]; falls back to typo-tolerant matching."""
    tokens = query.lower().split()
    exact, fuzzy = [], []
    for ti, tab in enumerate(TABS):
        for n, row in list_items(tab):
            hay = f"{row[0]} {row[2]}".lower()
            if all(t in hay for t in tokens):
                exact.append((ti, n, row))
            elif _fuzzy_hit(tokens, str(row[0])):
                fuzzy.append((ti, n, row))
    return exact or fuzzy


def resolve_item(tab, n, crc):
    """Return the row only if product n still has the same name as when the button was made."""
    row = get_item(tab, n)
    return row if row and _crc(row[0]) == crc else None


# ───────────────────────── formatting / parsing ─────────────────────────
def parse_price(text):
    t = text.replace(",", "").replace("₹", "").strip()
    return float(t) if "." in t else int(t)


def fmt_price(p):
    if isinstance(p, (int, float)):
        return f"{p:,.0f}" if float(p).is_integer() else f"{p:,.2f}"
    return str(p)


def fmt_item(n, row):
    name, price, cat, status, details = row
    icon = "✅" if status == "In Stock" else "❌" if status else "•"
    out = f"{n}. {name}\n   {fmt_price(price)} | {cat} | {icon} {status}"
    return out + (f"\n   {details}" if str(details).strip() else "")


def fmt_item_html(n, row):
    """Same content as fmt_item, styled with HTML for the browse view (bold name, price)."""
    name, price, cat, status, details = row
    icon, word = ("✅", "In Stock") if status == "In Stock" else ("❌", "Out of Stock") if status else ("•", status)
    out = f"<b>{n}. {html.escape(str(name))}</b>\n💰 <b>{html.escape(fmt_price(price))}</b>  ·  {icon} {word}"
    return out + (f"\n📝 {html.escape(str(details))}" if str(details).strip() else "")


MAIN_MENU = ReplyKeyboardMarkup(
    [["📋 Browse", "🔍 Find"], ["➕ Add", "❓ Help"]], resize_keyboard=True)


def cur_tab(context):
    return context.user_data.get("tab", TABS[0])


def arg_n(context):
    try:
        return int(context.args[0])
    except (IndexError, ValueError):
        return None


def keyboard(labels, prefix, per_row=2):
    btns = [InlineKeyboardButton(l, callback_data=f"{prefix}:{i}") for i, l in enumerate(labels)]
    return InlineKeyboardMarkup([btns[i:i + per_row] for i in range(0, len(btns), per_row)])


RM_KB = InlineKeyboardMarkup([[InlineKeyboardButton("Yes, delete", callback_data="rm:yes"),
                               InlineKeyboardButton("No", callback_data="rm:no")]])


def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *a, **k):
        user = update.effective_user
        if user is None or user.id not in ADMIN_IDS:
            if update.callback_query:
                await update.callback_query.answer("Not authorised", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text(
                    f"Not authorised. Your Telegram ID is {user.id if user else '?'}.")
            return ConversationHandler.END
        return await func(update, context, *a, **k)
    return wrapper


# ───────────────────────── simple commands ─────────────────────────
HELP = (
    "Product catalog bot\n\n"
    "Use the menu buttons below, or these commands:\n\n"
    "/browse - browse products by tab and category\n"
    "/find <name> - search all tabs, with quick edit / stock / delete buttons\n"
    "/list - show products in the current tab\n"
    "/add - add a product (guided)\n"
    "/edit <no> - change a product's details\n"
    "/stock <no> - toggle In Stock / Out of Stock\n"
    "/remove <no> - delete a product\n"
    "/tab - switch between sheet tabs\n"
    "/cancel - stop the current action\n"
    "/myid - show your Telegram ID"
)


@admin_only
async def start(update, context):
    await update.message.reply_text(HELP, reply_markup=MAIN_MENU)


@admin_only
async def find_prompt(update, context):
    context.user_data["awaiting_find"] = True
    await update.message.reply_text("What are you looking for? Type a product name.")


@admin_only
async def menu_text_router(update, context):
    """Handles taps on the persistent menu buttons, and a pending /find prompt."""
    text = update.message.text
    if context.user_data.pop("awaiting_find", False):
        context.args = text.split()
        await find_cmd(update, context)
        return
    if text == "📋 Browse":
        await browse_cmd(update, context)
    elif text == "🔍 Find":
        await find_prompt(update, context)
    elif text == "➕ Add":
        await add_start(update, context)
    elif text == "❓ Help":
        await update.message.reply_text(HELP, reply_markup=MAIN_MENU)


async def myid(update, context):
    await update.message.reply_text(f"Your Telegram ID: {update.effective_user.id}")


@admin_only
async def tab_cmd(update, context):
    labels = [("• " if t == cur_tab(context) else "") + t for t in TABS]
    await update.message.reply_text("Choose a sheet tab:", reply_markup=keyboard(labels, "tab", 1))


@admin_only
async def tab_chosen(update, context):
    q = update.callback_query
    await q.answer()
    context.user_data["tab"] = TABS[int(q.data.split(":")[1])]
    await q.edit_message_text(f"Now working on: {context.user_data['tab']}")


# ───────────────────────── /browse (tab -> category -> paged results) ─────────────────────────
def page_kb(ti, ci, page, total_pages):
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("◀ Prev", callback_data=f"bp:{ti}:{ci}:{page - 1}"))
    row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        row.append(InlineKeyboardButton("Next ▶", callback_data=f"bp:{ti}:{ci}:{page + 1}"))
    back = InlineKeyboardButton("⬅ Categories", callback_data=f"bt:{ti}")
    return InlineKeyboardMarkup([row, [back]])


@admin_only
async def browse_cmd(update, context):
    await update.effective_message.reply_text("Browse - choose a tab:", reply_markup=keyboard(TABS, "bt", 1))


@admin_only
async def browse_tab(update, context):
    q = update.callback_query
    await q.answer()
    ti = int(q.data.split(":")[1])
    tab = TABS[ti]
    items = await asyncio.to_thread(list_items, tab)
    if not items:
        await q.edit_message_text(f"'{tab}' has no products yet.")
        return
    counts = {}
    cats = []
    for _, row in items:
        c = str(row[2]).strip() or "(no category)"
        if c not in counts:
            counts[c] = 0
            cats.append(c)
        counts[c] += 1
    context.user_data["browse_cats"] = cats
    labels = [f"{c} ({counts[c]})" for c in cats]
    kb = keyboard(labels, f"bc:{ti}", 1)
    await q.edit_message_text(f"[{tab}] Choose a category:", reply_markup=kb)


@admin_only
async def browse_category(update, context):
    q = update.callback_query
    await q.answer()
    _, ti_ci = q.data.split(":", 1)
    ti_s, ci_s = ti_ci.split(":")
    ti, ci = int(ti_s), int(ci_s)
    await _send_browse_page(q, context, ti, ci, 0)


@admin_only
async def browse_page(update, context):
    q = update.callback_query
    await q.answer()
    _, ti, ci, page = q.data.split(":")
    await _send_browse_page(q, context, int(ti), int(ci), int(page))


async def _send_browse_page(q, context, ti, ci, page):
    tab = TABS[ti]
    cats = context.user_data.get("browse_cats") or await asyncio.to_thread(get_categories, tab)
    if ci >= len(cats):
        await q.edit_message_text("That category list changed - run /browse again.")
        return
    category = cats[ci]
    rows, total_pages, page = await asyncio.to_thread(page_items, tab, category, page)
    if not rows:
        await q.edit_message_text(f"[{tab}] {category}\n\nNo products in this category.")
        return
    body = "\n\n".join(fmt_item_html(n, r) for n, r in rows)
    text = f"<b>[{tab}] {html.escape(category)}</b>\n\n{body}"
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=page_kb(ti, ci, page, total_pages))


@admin_only
async def noop_cb(update, context):
    await update.callback_query.answer()


@admin_only
async def list_cmd(update, context):
    tab = cur_tab(context)
    items = await asyncio.to_thread(list_items, tab)
    if not items:
        await update.message.reply_text(f"'{tab}' has no products yet.")
        return
    chunks, cur = [], f"{tab} ({len(items)} products)\n\n"
    for n, row in items:
        block = fmt_item(n, row) + "\n\n"
        if len(cur) + len(block) > 3800:
            chunks.append(cur)
            cur = ""
        cur += block
    chunks.append(cur)
    for c in chunks:
        await update.message.reply_text(c)


@admin_only
async def stock_cmd(update, context):
    n = arg_n(context)
    if n is None:
        await update.message.reply_text("Usage: /stock <number>   (see numbers with /list)")
        return
    tab = cur_tab(context)
    row = await asyncio.to_thread(get_item, tab, n)
    if row is None:
        await update.message.reply_text(f"No product number {n} in '{tab}'.")
        return
    new = "Out of Stock" if row[3] == "In Stock" else "In Stock"
    await asyncio.to_thread(update_field, tab, n, 3, new)
    await update.message.reply_text(f"{row[0]} is now {new}.")


@admin_only
async def remove_cmd(update, context):
    n = arg_n(context)
    if n is None:
        await update.message.reply_text("Usage: /remove <number>   (see numbers with /list)")
        return
    tab = cur_tab(context)
    row = await asyncio.to_thread(get_item, tab, n)
    if row is None:
        await update.message.reply_text(f"No product number {n} in '{tab}'.")
        return
    context.user_data["rm"] = (tab, n, row[0])
    await update.message.reply_text(
        f"Delete No. {n}: {row[0]}?\n(Numbers of later products shift up by one.)", reply_markup=RM_KB)


@admin_only
async def remove_choice(update, context):
    q = update.callback_query
    await q.answer()
    pending = context.user_data.pop("rm", None)
    if q.data != "rm:yes" or not pending:
        await q.edit_message_text("Cancelled.")
        return
    tab, n, name = pending
    row = await asyncio.to_thread(get_item, tab, n)
    if row is None or row[0] != name:
        await q.edit_message_text("The sheet changed in the meantime. Run /remove again.")
        return
    await asyncio.to_thread(remove_item, tab, n)
    await q.edit_message_text(f"Removed: {name}")


# ───────────────────────── /find ─────────────────────────
def result_markup(ti, n, name):
    c = _crc(name)
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✏️ Edit", callback_data=f"fe:{ti}:{n}:{c}"),
        InlineKeyboardButton("🔄 Stock", callback_data=f"fs:{ti}:{n}:{c}"),
        InlineKeyboardButton("🗑 Delete", callback_data=f"fd:{ti}:{n}:{c}"),
    ]])


def _parse_cb(data):
    _, ti, n, crc = data.split(":")
    return int(ti), int(n), crc


@admin_only
async def find_cmd(update, context):
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Usage: /find <part of a name>\nExample: /find netflix")
        return
    results = await asyncio.to_thread(search_all, query)
    if not results:
        await update.message.reply_text(f"No products found for '{query}'.")
        return
    for ti, n, row in results[:MAX_RESULTS]:
        await update.message.reply_text(f"[{TABS[ti]}]\n{fmt_item(n, row)}", reply_markup=result_markup(ti, n, row[0]))
    if len(results) > MAX_RESULTS:
        await update.message.reply_text(
            f"Showing {MAX_RESULTS} of {len(results)} matches. Add more words to narrow it down.")


@admin_only
async def find_stock(update, context):
    q = update.callback_query
    ti, n, crc = _parse_cb(q.data)
    tab = TABS[ti]
    row = await asyncio.to_thread(resolve_item, tab, n, crc)
    if row is None:
        await q.answer("This product changed - run /find again.", show_alert=True)
        return
    row[3] = "Out of Stock" if row[3] == "In Stock" else "In Stock"
    await asyncio.to_thread(update_field, tab, n, 3, row[3])
    await q.answer(f"Now {row[3]}")
    await q.edit_message_text(f"[{tab}]\n{fmt_item(n, row)}", reply_markup=result_markup(ti, n, row[0]))


@admin_only
async def find_delete(update, context):
    q = update.callback_query
    ti, n, crc = _parse_cb(q.data)
    tab = TABS[ti]
    row = await asyncio.to_thread(resolve_item, tab, n, crc)
    if row is None:
        await q.answer("This product changed - run /find again.", show_alert=True)
        return
    context.user_data["rm"] = (tab, n, row[0])
    await q.answer()
    await q.message.reply_text(
        f"Delete No. {n} from '{tab}': {row[0]}?\n(Numbers of later products shift up by one.)", reply_markup=RM_KB)


@admin_only
async def find_edit(update, context):
    q = update.callback_query
    ti, n, crc = _parse_cb(q.data)
    tab = TABS[ti]
    row = await asyncio.to_thread(resolve_item, tab, n, crc)
    if row is None:
        await q.answer("This product changed - run /find again.", show_alert=True)
        return ConversationHandler.END
    await q.answer()
    return await _begin_edit(update, context, tab, n, row)


# ───────────────────────── /add conversation ─────────────────────────
(NAME, PRICE, CATEGORY, STATUS, DETAILS, REVIEW, REVIEW_FIELD, REVIEW_VALUE, REVIEW_PICK,
 EDIT_FIELD, EDIT_CHOICE, EDIT_VALUE) = range(12)
TXT = filters.TEXT & ~filters.COMMAND


@admin_only
async def add_start(update, context):
    if update.callback_query:
        await update.callback_query.answer()
    context.user_data["new"] = {}
    await update.effective_message.reply_text(
        f"Adding to '{cur_tab(context)}'. Send /cancel to stop.\n\nProduct name?")
    return NAME


async def add_name(update, context):
    context.user_data["new"]["name"] = update.message.text.strip()
    await update.message.reply_text("Price? (number only, e.g. 499)")
    return PRICE


async def add_price(update, context):
    try:
        price = parse_price(update.message.text)
    except ValueError:
        await update.message.reply_text("Please send the price as a number, e.g. 499")
        return PRICE
    context.user_data["new"]["price"] = price
    cats = await asyncio.to_thread(get_categories, cur_tab(context))
    context.user_data["cats"] = cats
    await update.message.reply_text("Category?", reply_markup=keyboard(cats, "cat"))
    return CATEGORY


async def add_category(update, context):
    q = update.callback_query
    await q.answer()
    cat = context.user_data["cats"][int(q.data.split(":")[1])]
    context.user_data["new"]["category"] = cat
    await q.edit_message_text(f"Category: {cat}\n\nStatus?", reply_markup=keyboard(STATUSES, "stat"))
    return STATUS


async def add_status(update, context):
    q = update.callback_query
    await q.answer()
    status = STATUSES[int(q.data.split(":")[1])]
    context.user_data["new"]["status"] = status
    opts = await asyncio.to_thread(get_details_options, cur_tab(context))
    context.user_data["detail_opts"] = opts
    if opts:
        kb = keyboard(opts + ["✏️ Type my own"], "dd", 1)
        await q.edit_message_text(f"Status: {status}\n\nDetails / delivery info - pick one, or add a new one:",
                                   reply_markup=kb)
    else:
        await q.edit_message_text(f"Status: {status}\n\nDetails / delivery info? (or send /skip)")
    return DETAILS


async def add_details_choice(update, context):
    q = update.callback_query
    await q.answer()
    idx = int(q.data.split(":")[1])
    opts = context.user_data.get("detail_opts", [])
    if idx < len(opts):
        context.user_data["new"]["details"] = opts[idx]
        return await _show_review(update, context)
    await q.edit_message_text("Type the details / delivery info:")
    return DETAILS


async def add_details(update, context):
    context.user_data["new"]["details"] = update.message.text.strip()
    return await _show_review(update, context)


async def add_skip(update, context):
    context.user_data["new"]["details"] = ""
    return await _show_review(update, context)


# ── review screen: confirm, fix a field, or cancel, before the sheet is touched ──
REVIEW_KB = InlineKeyboardMarkup([[
    InlineKeyboardButton("✅ Confirm & Save", callback_data="rv:confirm"),
    InlineKeyboardButton("✏️ Edit", callback_data="rv:edit"),
    InlineKeyboardButton("❌ Cancel", callback_data="rv:cancel"),
]])


def _review_row(new):
    return [new["name"], new["price"], new["category"], new["status"], new.get("details", "")]


async def _show_review(update, context):
    tab = cur_tab(context)
    text = f"Add to '{tab}' - review before saving:\n\n{fmt_item('New', _review_row(context.user_data['new']))}"
    await update.effective_message.reply_text(text, reply_markup=REVIEW_KB)
    return REVIEW


async def review_action(update, context):
    q = update.callback_query
    await q.answer()
    action = q.data.split(":")[1]
    if action == "cancel":
        context.user_data.pop("new", None)
        await q.edit_message_text("Cancelled - nothing was saved.")
        return ConversationHandler.END
    if action == "edit":
        await q.edit_message_text("Which field do you want to change?", reply_markup=keyboard(FIELD_NAMES, "rf", 3))
        return REVIEW_FIELD
    # confirm
    new = context.user_data.pop("new")
    tab = cur_tab(context)
    row = _review_row(new)
    try:
        n = await asyncio.to_thread(add_item, tab, row)
    except RuntimeError as e:
        await q.edit_message_text(str(e))
        return ConversationHandler.END
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add another", callback_data="aa")]])
    await q.edit_message_text(f"Added to '{tab}':\n\n{fmt_item(n, row)}", reply_markup=kb)
    return ConversationHandler.END


async def review_field(update, context):
    q = update.callback_query
    await q.answer()
    idx = int(q.data.split(":")[1])
    context.user_data["review_idx"] = idx
    if idx in (0, 1):
        await q.edit_message_text(f"Send the new {FIELD_NAMES[idx].lower()}:")
        return REVIEW_VALUE
    if idx == 2:
        cats = await asyncio.to_thread(get_categories, cur_tab(context))
        context.user_data["cats"] = cats
        await q.edit_message_text("New category?", reply_markup=keyboard(cats, "rc"))
        return REVIEW_PICK
    if idx == 3:
        await q.edit_message_text("New status?", reply_markup=keyboard(STATUSES, "rs"))
        return REVIEW_PICK
    opts = await asyncio.to_thread(get_details_options, cur_tab(context))
    context.user_data["detail_opts"] = opts
    if opts:
        await q.edit_message_text("New details?", reply_markup=keyboard(opts + ["✏️ Type my own"], "rd"))
        return REVIEW_PICK
    await q.edit_message_text("Send the new details:")
    return REVIEW_VALUE


async def review_value(update, context):
    idx, text = context.user_data["review_idx"], update.message.text.strip()
    if idx == 1:
        try:
            text = parse_price(text)
        except ValueError:
            await update.message.reply_text("Please send the price as a number, e.g. 499")
            return REVIEW_VALUE
    key = ["name", "price", "category", "status", "details"][idx]
    context.user_data["new"][key] = text
    return await _show_review(update, context)


async def review_pick_category(update, context):
    q = update.callback_query
    await q.answer()
    context.user_data["new"]["category"] = context.user_data["cats"][int(q.data.split(":")[1])]
    return await _show_review(update, context)


async def review_pick_status(update, context):
    q = update.callback_query
    await q.answer()
    context.user_data["new"]["status"] = STATUSES[int(q.data.split(":")[1])]
    return await _show_review(update, context)


async def review_pick_details(update, context):
    q = update.callback_query
    await q.answer()
    idx = int(q.data.split(":")[1])
    opts = context.user_data.get("detail_opts", [])
    if idx < len(opts):
        context.user_data["new"]["details"] = opts[idx]
        return await _show_review(update, context)
    await q.edit_message_text("Send the new details:")
    return REVIEW_VALUE


# ───────────────────────── screenshot import (Gemini vision, free tier) ─────────────────────────
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_PROMPT = (
    "You are reading a screenshot of a spreadsheet that lists products for resale "
    "(subscriptions, software, games, etc). Extract every product row you can clearly read. "
    "Respond with ONLY a JSON array (no prose, no markdown fences), where each element is:\n"
    '{"name": string, "price": number or null, "category": string, '
    '"status": "In Stock" or "Out of Stock" or "", "details": string}\n'
    "Use null/empty string for anything not visible or not legible. Do not invent data, "
    "and do not include rows that are just column headers."
)


def extract_products_from_image(image_bytes, mime_type="image/jpeg"):
    """Calls Gemini's free-tier vision API and returns a list of [name, price, category, status, details]
    rows ready for bulk_add_items. Raises RuntimeError with a human-readable message on failure."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set - screenshot import isn't configured yet.")
    body = {
        "contents": [{"parts": [
            {"text": GEMINI_PROMPT},
            {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(image_bytes).decode()}},
        ]}],
        "generationConfig": {"response_mime_type": "application/json"},
    }
    # Try each model in turn; retry once on temporary errors before moving to the next model.
    resp = None
    for model in GEMINI_MODELS:
        for attempt in range(2):
            if attempt:
                time.sleep(3)
            try:
                resp = requests.post(f"{GEMINI_BASE}/{model}:generateContent", params={"key": GEMINI_API_KEY},
                                     json=body, timeout=60)
            except requests.RequestException as e:
                log.warning("Gemini %s network error: %s", model, e)
                resp = None
                continue
            if resp.status_code in (500, 503, 504):
                log.warning("Gemini %s returned %s, retrying/falling back", model, resp.status_code)
                continue
            break
        if resp is not None and resp.status_code not in (404, 500, 503, 504):
            break
        if resp is not None and resp.status_code == 404:
            log.warning("Gemini model %s not available (404), trying next", model)
    if resp is None:
        raise RuntimeError("Couldn't reach Gemini - check the server's internet connection and try again.")
    if resp.status_code == 429:
        raise RuntimeError("Gemini's free tier is rate-limited right now - wait a minute and send it again.")
    if resp.status_code in (500, 503, 504):
        raise RuntimeError("Gemini is overloaded right now (its side, not yours) - please try again in a few minutes.")
    if not resp.ok:
        raise RuntimeError(f"Gemini returned an error ({resp.status_code}): {resp.text[:200]}")
    try:
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        items = json.loads(text)
    except (KeyError, IndexError, ValueError) as e:
        raise RuntimeError(f"Couldn't understand Gemini's response: {e}") from e

    rows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        if not name:
            continue
        price = it.get("price")
        try:
            price = parse_price(str(price)) if price not in (None, "") else ""
        except ValueError:
            price = ""
        status = str(it.get("status") or "").strip()
        if status not in STATUSES:
            status = ""
        rows.append([name, price, str(it.get("category") or "").strip(), status, str(it.get("details") or "").strip()])
    return rows


@admin_only
async def import_start(update, context):
    context.user_data["importing"] = True
    context.user_data["import_items"] = []
    context.user_data["import_tab"] = cur_tab(context)
    await update.message.reply_text(
        f"Bulk import into '{cur_tab(context)}' - send screenshots one at a time (or a few together).\n"
        "When you're done, send /doneimport. To stop without adding anything, send /cancelimport.")


@admin_only
async def import_photo(update, context):
    if not context.user_data.get("importing"):
        await update.message.reply_text("Send /import first to start a bulk import from screenshots.")
        return
    photo = update.message.photo[-1]
    status_msg = await update.message.reply_text("Reading this screenshot...")
    tg_file = await context.bot.get_file(photo.file_id)
    image_bytes = bytes(await tg_file.download_as_bytearray())
    try:
        rows = await asyncio.to_thread(extract_products_from_image, image_bytes)
    except RuntimeError as e:
        await status_msg.edit_text(f"Couldn't read that screenshot: {e}")
        return
    context.user_data["import_items"].extend(rows)
    total = len(context.user_data["import_items"])
    await status_msg.edit_text(
        f"Found {len(rows)} product(s) in this screenshot (running total: {total}).\n"
        "Send more screenshots, or /doneimport when finished.")


@admin_only
async def import_done(update, context):
    items = context.user_data.get("import_items") or []
    if not items:
        await update.message.reply_text("No screenshots processed yet - send some photos first, or /cancelimport.")
        return
    tab = context.user_data.get("import_tab", cur_tab(context))
    preview = "\n".join(f"{i + 1}. {r[0]} - {fmt_price(r[1]) if r[1] != '' else '?'}" for i, r in enumerate(items[:10]))
    more = f"\n...and {len(items) - 10} more." if len(items) > 10 else ""
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Add all to sheet", callback_data="im:confirm"),
                                InlineKeyboardButton("❌ Discard", callback_data="im:cancel")]])
    await update.message.reply_text(
        f"Ready to add {len(items)} product(s) to '{tab}':\n\n{preview}{more}\n\n"
        "Products with a name matching one already in the sheet will be skipped automatically.",
        reply_markup=kb)


@admin_only
async def import_cancel(update, context):
    for k in ("importing", "import_items", "import_tab"):
        context.user_data.pop(k, None)
    await update.message.reply_text("Import cancelled - nothing was added.")


@admin_only
async def import_confirm_cb(update, context):
    q = update.callback_query
    await q.answer()
    items = context.user_data.pop("import_items", [])
    tab = context.user_data.pop("import_tab", cur_tab(context))
    context.user_data.pop("importing", None)
    if not items:
        await q.edit_message_text("Nothing to add.")
        return
    try:
        added, skipped = await asyncio.to_thread(bulk_add_items, tab, items)
    except RuntimeError as e:
        await q.edit_message_text(str(e))
        return
    msg = f"Added {added} product(s) to '{tab}'."
    if skipped:
        shown = ", ".join(skipped[:10]) + (f", +{len(skipped) - 10} more" if len(skipped) > 10 else "")
        msg += f"\nSkipped {len(skipped)} already in the sheet: {shown}"
    await q.edit_message_text(msg)


@admin_only
async def import_cancel_cb(update, context):
    q = update.callback_query
    await q.answer()
    for k in ("importing", "import_items", "import_tab"):
        context.user_data.pop(k, None)
    await q.edit_message_text("Discarded - nothing was added.")


# ───────────────────────── /edit conversation ─────────────────────────
@admin_only
async def edit_start(update, context):
    n = arg_n(context)
    if n is None:
        await update.message.reply_text("Usage: /edit <number>   (see numbers with /list)")
        return ConversationHandler.END
    tab = cur_tab(context)
    row = await asyncio.to_thread(get_item, tab, n)
    if row is None:
        await update.message.reply_text(f"No product number {n} in '{tab}'.")
        return ConversationHandler.END
    return await _begin_edit(update, context, tab, n, row)


async def _begin_edit(update, context, tab, n, row):
    context.user_data["edit_n"] = n
    context.user_data["edit_tab"] = tab
    await update.effective_message.reply_text(
        f"Editing [{tab}]:\n{fmt_item(n, row)}\n\nWhich field?", reply_markup=keyboard(FIELD_NAMES, "ef", 3))
    return EDIT_FIELD


async def edit_field(update, context):
    q = update.callback_query
    await q.answer()
    idx = int(q.data.split(":")[1])
    context.user_data["edit_idx"] = idx
    if idx == 4:
        opts = await asyncio.to_thread(get_details_options, context.user_data["edit_tab"])
        context.user_data["choices"] = opts
        if opts:
            kb = keyboard(opts + ["✏️ Type my own"], "ev")
            await q.edit_message_text("Choose the new details, or add a new one:", reply_markup=kb)
            return EDIT_CHOICE
        await q.edit_message_text("Send the new details:")
        return EDIT_VALUE
    if idx in (2, 3):
        choices = await asyncio.to_thread(get_categories) if idx == 2 else STATUSES
        context.user_data["choices"] = choices
        await q.edit_message_text(f"Choose the new {FIELD_NAMES[idx].lower()}:", reply_markup=keyboard(choices, "ev"))
        return EDIT_CHOICE
    await q.edit_message_text(f"Send the new {FIELD_NAMES[idx].lower()}:")
    return EDIT_VALUE


async def _save_edit(update, context, value):
    n, idx, tab = context.user_data["edit_n"], context.user_data["edit_idx"], context.user_data["edit_tab"]
    await asyncio.to_thread(update_field, tab, n, idx, value)
    row = await asyncio.to_thread(get_item, tab, n)
    await update.effective_message.reply_text(f"Updated:\n{fmt_item(n, row)}")
    return ConversationHandler.END


async def edit_choice(update, context):
    q = update.callback_query
    await q.answer()
    choices = context.user_data["choices"]
    idx = int(q.data.split(":")[1])
    if idx >= len(choices):  # the appended "Type my own" option
        await q.edit_message_text("Send the new value:")
        return EDIT_VALUE
    return await _save_edit(update, context, choices[idx])


async def edit_value(update, context):
    idx, text = context.user_data["edit_idx"], update.message.text.strip()
    if idx == 1:
        try:
            text = parse_price(text)
        except ValueError:
            await update.message.reply_text("Please send the price as a number, e.g. 499")
            return EDIT_VALUE
    return await _save_edit(update, context, text)


async def cancel(update, context):
    for k in ("new", "edit_n", "edit_idx", "edit_tab", "choices", "cats", "detail_opts", "review_idx"):
        context.user_data.pop(k, None)
    await update.effective_message.reply_text("Cancelled.")
    return ConversationHandler.END


async def on_error(update, context):
    log.exception("Unhandled error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("Something went wrong (see the bot logs). Please try again.")


async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("find", "Search products by name"), BotCommand("list", "Show products"), BotCommand("add", "Add a product"),
        BotCommand("edit", "Edit a product"), BotCommand("stock", "Toggle stock status"),
        BotCommand("remove", "Delete a product"), BotCommand("tab", "Switch sheet tab"),
        BotCommand("cancel", "Cancel current action"), BotCommand("myid", "Show my Telegram ID"),
        BotCommand("browse", "Browse products by tab and category"),
        BotCommand("import", "Bulk-add products from screenshots"),
    ])


def main():
    global book
    if not BOT_TOKEN or not SHEET_ID:
        raise SystemExit("Set BOT_TOKEN and SHEET_ID in your .env file.")
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS is empty - nobody can use the bot yet. Send /myid to it, then add your ID to .env.")
    creds_b64 = os.environ.get("GOOGLE_CREDS_JSON", "")
    if creds_b64:
        # Railway/hosting friendly path: one base64 env var instead of a JSON file on disk.
        data = base64.b64decode(creds_b64)
        with tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False) as f:
            f.write(data)
            creds_path = f.name
    else:
        creds_path = CREDS_FILE
    book = gspread.service_account(filename=creds_path).open_by_key(SHEET_ID)
    have = {w.title for w in book.worksheets()}
    missing = [t for t in TABS + ["Lists"] if t not in have]
    if missing:
        raise SystemExit(f"These tabs were not found in the sheet: {missing}")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("add", add_start),
            CommandHandler("edit", edit_start),
            MessageHandler(filters.Regex(r"^➕ Add$"), add_start),
            CallbackQueryHandler(add_start, pattern=r"^aa$"),
            CallbackQueryHandler(find_edit, pattern=r"^fe:\d+:\d+:[0-9a-f]+$"),
        ],
        states={
            NAME: [MessageHandler(TXT, add_name)],
            PRICE: [MessageHandler(TXT, add_price)],
            CATEGORY: [CallbackQueryHandler(add_category, pattern=r"^cat:\d+$")],
            STATUS: [CallbackQueryHandler(add_status, pattern=r"^stat:\d+$")],
            DETAILS: [CommandHandler("skip", add_skip), CallbackQueryHandler(add_details_choice, pattern=r"^dd:\d+$"),
                      MessageHandler(TXT, add_details)],
            REVIEW: [CallbackQueryHandler(review_action, pattern=r"^rv:(confirm|edit|cancel)$")],
            REVIEW_FIELD: [CallbackQueryHandler(review_field, pattern=r"^rf:\d$")],
            REVIEW_VALUE: [MessageHandler(TXT, review_value)],
            REVIEW_PICK: [
                CallbackQueryHandler(review_pick_category, pattern=r"^rc:\d+$"),
                CallbackQueryHandler(review_pick_status, pattern=r"^rs:\d+$"),
                CallbackQueryHandler(review_pick_details, pattern=r"^rd:\d+$"),
            ],
            EDIT_FIELD: [CallbackQueryHandler(edit_field, pattern=r"^ef:\d$")],
            EDIT_CHOICE: [CallbackQueryHandler(edit_choice, pattern=r"^ev:\d+$")],
            EDIT_VALUE: [MessageHandler(TXT, edit_value)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("find", find_cmd))
    app.add_handler(CommandHandler("browse", browse_cmd))
    app.add_handler(CommandHandler("import", import_start))
    app.add_handler(CommandHandler("doneimport", import_done))
    app.add_handler(CommandHandler("cancelimport", import_cancel))
    app.add_handler(MessageHandler(filters.PHOTO, import_photo))
    app.add_handler(CallbackQueryHandler(import_confirm_cb, pattern=r"^im:confirm$"))
    app.add_handler(CallbackQueryHandler(import_cancel_cb, pattern=r"^im:cancel$"))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("stock", stock_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("tab", tab_cmd))
    app.add_handler(CallbackQueryHandler(tab_chosen, pattern=r"^tab:\d+$"))
    app.add_handler(CallbackQueryHandler(find_stock, pattern=r"^fs:\d+:\d+:[0-9a-f]+$"))
    app.add_handler(CallbackQueryHandler(find_delete, pattern=r"^fd:\d+:\d+:[0-9a-f]+$"))
    app.add_handler(CallbackQueryHandler(remove_choice, pattern=r"^rm:(yes|no)$"))
    app.add_handler(CallbackQueryHandler(browse_tab, pattern=r"^bt:\d+$"))
    app.add_handler(CallbackQueryHandler(browse_category, pattern=r"^bc:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(browse_page, pattern=r"^bp:\d+:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(noop_cb, pattern=r"^noop$"))
    app.add_handler(MessageHandler(TXT, menu_text_router))
    app.add_error_handler(on_error)
    log.info("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
