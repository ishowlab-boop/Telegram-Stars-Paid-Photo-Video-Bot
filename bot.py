"""Telegram Stars Paid Media Bot — starter project.

Only bot-owned paid media. Never publish your BOT_TOKEN.
Uses official Bot API directly to avoid third-party SDK version mismatch.
"""
import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import requests

LOG = logging.getLogger("starsbot")
API_ROOT = "https://api.telegram.org/bot"
MAX_PRICE = 25_000


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def inline_buttons(rows):
    return {"inline_keyboard": rows}


def reply_keyboard(rows):
    return {
        "keyboard": rows,
        "resize_keyboard": True,
        "is_persistent": True,
        "input_field_placeholder": "Choose an option...",
    }


def b(text, data):
    return {"text": text, "callback_data": data}


class ApiError(Exception):
    pass


class TelegramAPI:
    def __init__(self, token):
        self.url = API_ROOT + token + "/"
        self.session = requests.Session()

    def call(self, method, payload=None, timeout=30):
        # The secret token stays in the request URL and is never logged.
        try:
            response = self.session.post(
                self.url + method, json=payload or {}, timeout=timeout
            )
            # Telegram returns useful structured JSON errors even with HTTP 4xx.
            # Parse JSON before checking the status, without logging token-bearing URLs.
            result = response.json()
        except (requests.RequestException, ValueError):
            raise ApiError("Telegram connection failed or returned a non-JSON response") from None
        if not isinstance(result, dict):
            raise ApiError(f"{method}: invalid Telegram API response")
        if not result.get("ok"):
            description = str(result.get("description", "Unknown Telegram API error"))
            raise ApiError(f"{method}: {description} (code {result.get('error_code', '?')})")
        return result.get("result")


class Storage:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('photo','video')),
                    file_id TEXT NOT NULL,
                    price INTEGER NOT NULL CHECK(price BETWEEN 1 AND 25000),
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS offers (
                    payload TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    item_id INTEGER NOT NULL,
                    price INTEGER NOT NULL,
                    sent INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    created_epoch INTEGER NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE INDEX IF NOT EXISTS offers_index
                    ON offers(user_id, item_id, created_epoch);
                CREATE TABLE IF NOT EXISTS sales (
                    payload TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    item_id INTEGER NOT NULL,
                    price INTEGER NOT NULL,
                    purchased_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE INDEX IF NOT EXISTS sales_index ON sales(user_id, item_id);
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS business_connections (
                    id TEXT PRIMARY KEY,
                    owner_id INTEGER NOT NULL,
                    is_enabled INTEGER NOT NULL,
                    can_reply INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS business_buyers (
                    chat_id INTEGER PRIMARY KEY,
                    connection_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    last_incoming_epoch INTEGER NOT NULL,
                    FOREIGN KEY(connection_id) REFERENCES business_connections(id)
                );
            """)
            # Safe, one-time migration for existing Railway databases.
            columns = [r[1] for r in conn.execute("PRAGMA table_info(offers)")]
            if "business_connection_id" not in columns:
                conn.execute("ALTER TABLE offers ADD COLUMN business_connection_id TEXT")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=20)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=20000")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def add_item(self, title, kind, file_id, price):
        with self.connect() as c:
            cur = c.execute("INSERT INTO items(title,kind,file_id,price,created_at) VALUES(?,?,?,?,?)",
                            (title, kind, file_id, price, utc_now()))
            return cur.lastrowid

    def item(self, item_id):
        with self.connect() as c:
            return c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()

    def catalog(self):
        with self.connect() as c:
            return c.execute("SELECT * FROM items WHERE active=1 ORDER BY id DESC LIMIT 50").fetchall()

    def remove_item(self, item_id):
        # Soft-delete: hide from future sends/catalog while preserving sales history.
        with self.connect() as c:
            return c.execute("UPDATE items SET active=0 WHERE id=? AND active=1", (item_id,)).rowcount

    def update_item_price(self, item_id, price):
        if not isinstance(price, int) or not 1 <= price <= MAX_PRICE:
            return 0
        with self.connect() as c:
            return c.execute(
                "UPDATE items SET price=? WHERE id=? AND active=1",
                (price, item_id),
            ).rowcount

    def purchased(self, user_id, item_id):
        with self.connect() as c:
            return c.execute("SELECT 1 FROM sales WHERE user_id=? AND item_id=? LIMIT 1",
                             (user_id, item_id)).fetchone() is not None

    def library(self, user_id):
        with self.connect() as c:
            return c.execute("""SELECT DISTINCT i.* FROM items i
                 JOIN sales s ON s.item_id=i.id WHERE s.user_id=?
                 ORDER BY i.id DESC LIMIT 50""", (user_id,)).fetchall()

    def recently_offered(self, user_id, item_id):
        with self.connect() as c:
            row = c.execute("""SELECT created_epoch FROM offers
                WHERE user_id=? AND item_id=? AND sent=1
                ORDER BY created_epoch DESC LIMIT 1""", (user_id, item_id)).fetchone()
        return bool(row and (time.time() - row["created_epoch"] < 120))

    def save_offer(self, payload, user_id, item_id, price, business_connection_id=None):
        with self.connect() as c:
            c.execute("""INSERT INTO offers(payload,user_id,item_id,price,created_at,created_epoch,business_connection_id)
                        VALUES(?,?,?,?,?,?,?)""",
                      (payload, user_id, item_id, price, utc_now(), int(time.time()), business_connection_id))

    def mark_sent(self, payload):
        with self.connect() as c:
            c.execute("UPDATE offers SET sent=1 WHERE payload=?", (payload,))

    def drop_offer(self, payload):
        with self.connect() as c:
            c.execute("DELETE FROM offers WHERE payload=? AND sent=0", (payload,))

    def record_sale(self, payload, user_id):
        """Return (new, row). A Telegram purchase update is the only source of truth."""
        with self.connect() as c:
            offer = c.execute("SELECT * FROM offers WHERE payload=? AND user_id=?",
                              (payload, user_id)).fetchone()
            if not offer:
                return False, None
            c.execute("INSERT OR IGNORE INTO sales(payload,user_id,item_id,price,purchased_at) VALUES(?,?,?,?,?)",
                      (payload, user_id, offer["item_id"], offer["price"], utc_now()))
            is_new = c.execute("SELECT changes()").fetchone()[0] == 1
            return is_new, dict(offer)

    def stats(self):
        with self.connect() as c:
            item_count = c.execute("SELECT COUNT(*) FROM items WHERE active=1").fetchone()[0]
            sales = c.execute("SELECT COUNT(*), COALESCE(SUM(price),0), COUNT(DISTINCT user_id) FROM sales").fetchone()
        return item_count, sales[0], sales[1], sales[2]


    def save_business_connection(self, connection, owner_id):
        """Persist only the admin's own connection. Never accept third-party owners."""
        user = connection.get("user") or {}
        if user.get("id") != owner_id:
            return False
        rights = connection.get("rights") or {}
        can_reply = bool(rights.get("can_reply", connection.get("can_reply", False)))
        with self.connect() as c:
            c.execute("""INSERT INTO business_connections(id,owner_id,is_enabled,can_reply,updated_at)
                         VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                         owner_id=excluded.owner_id,is_enabled=excluded.is_enabled,
                         can_reply=excluded.can_reply,updated_at=excluded.updated_at""",
                      (connection["id"], owner_id, int(bool(connection.get("is_enabled"))),
                       int(can_reply), utc_now()))
        return True

    def business_connection(self, connection_id):
        with self.connect() as c:
            row = c.execute("SELECT * FROM business_connections WHERE id=?", (connection_id,)).fetchone()
            return dict(row) if row else None

    def save_business_buyer(self, chat_id, connection_id, name, incoming_epoch):
        """Store sender metadata only; return True on first observation of this buyer."""
        with self.connect() as c:
            new_buyer = c.execute("SELECT 1 FROM business_buyers WHERE chat_id=?", (chat_id,)).fetchone() is None
            c.execute("""INSERT INTO business_buyers(chat_id,connection_id,display_name,last_incoming_epoch)
                         VALUES(?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET
                         connection_id=excluded.connection_id,
                         display_name=excluded.display_name,
                         last_incoming_epoch=MAX(business_buyers.last_incoming_epoch, excluded.last_incoming_epoch)""",
                      (chat_id, connection_id, name[:80], int(incoming_epoch)))
        return new_buyer

    def all_business_buyers(self, owner_id, limit=60):
        """Show both recent and older known senders, never Telegram-wide contacts."""
        with self.connect() as c:
            rows = c.execute("""SELECT b.*, c.can_reply, c.is_enabled FROM business_buyers b
                            JOIN business_connections c ON b.connection_id=c.id
                            WHERE c.owner_id=? ORDER BY b.last_incoming_epoch DESC LIMIT ?""",
                             (owner_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def eligible_business_buyer(self, chat_id, owner_id):
        with self.connect() as c:
            row = c.execute("""SELECT b.*, c.can_reply, c.is_enabled, c.owner_id
                             FROM business_buyers b JOIN business_connections c ON b.connection_id=c.id
                             WHERE b.chat_id=? AND c.owner_id=?""", (chat_id, owner_id)).fetchone()
        if not row or not row["is_enabled"] or not row["can_reply"]:
            return None
        # Business replies are restricted to chats with recent incoming messages.
        if int(time.time()) - row["last_incoming_epoch"] >= 24 * 3600:
            return None
        return dict(row)

    def eligible_business_buyers(self, owner_id):
        cutoff = int(time.time()) - 24 * 3600
        with self.connect() as c:
            rows = c.execute("""SELECT b.* FROM business_buyers b
                            JOIN business_connections c ON b.connection_id=c.id
                            WHERE c.owner_id=? AND c.is_enabled=1 AND c.can_reply=1
                              AND b.last_incoming_epoch>? ORDER BY b.last_incoming_epoch DESC LIMIT 40""",
                             (owner_id, cutoff)).fetchall()
        return [dict(row) for row in rows]

    def get_offset(self):
        with self.connect() as c:
            row = c.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
            return int(row[0]) if row else 0

    def set_offset(self, offset):
        with self.connect() as c:
            c.execute("INSERT INTO meta(key,value) VALUES('offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (str(offset),))


class StarsBot:
    def __init__(self, api, db, admin_id):
        self.api = api
        self.db = db
        self.admin = int(admin_id)
        self.drafts = {}  # transient admin-only upload wizard, not purchase data
        self.price_drafts = {}  # temporary per-buyer Stars price input
        self.item_price_drafts = {}  # temporary saved-item price edit
        self.batch_price_drafts = {}  # temporary one-price-for-many-buyers input
        self.send_drafts = {}  # admin-only multi-buyer send selection

    def menu_keyboard(self, uid):
        if uid == self.admin:
            return reply_keyboard([
                ["📤 Send to Buyer", "🗂 Saved Media"],
                ["➕ Add Media", "👥 My Buyers"],
                ["💰 Stars Balance", "💸 Withdraw"],
                ["📊 Sales & Stats", "🛍 Browse"],
            ])
        return reply_keyboard([
            ["🛍 Browse", "📚 My Library"],
            ["🆔 My ID"],
        ])

    def send(self, chat_id, text, keyboard=None):
        data = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if keyboard:
            data["reply_markup"] = keyboard
        return self.api.call("sendMessage", data)

    def admin_only(self, uid, chat):
        if uid != self.admin:
            self.send(chat, "⛔ Admin only.")
            return False
        return True

    def help(self, uid, chat):
        if uid == self.admin:
            message = (
                "⭐ My Stars Media — Admin Panel\n\n"
                "📤 Send to Buyer — send saved media to one or many buyers\n"
                "🗂 Saved Media — edit price, send again, or hide media\n"
                "➕ Add Media — upload a new photo/video once\n"
                "👥 My Buyers — recent personal-chat buyers\n"
                "💰 Stars Balance — current bot Stars balance\n"
                "💸 Withdraw — withdrawal instructions\n"
                "📊 Sales & Stats — local sales summary\n\n"
                "Commands: /add /items /buyers /balance /stats /delete ID /cancel"
            )
        else:
            message = (
                "⭐ Welcome to My Stars Media!\n\n"
                "/browse — locked photo/video catalog\n"
                "/library — your purchases\n"
                "/id — your Telegram user ID\n"
                "/help — commands"
            )
        return self.send(chat, message, self.menu_keyboard(uid))

    def withdraw_info(self, chat):
        try:
            balance = self.api.call("getMyStarBalance") or {}
            amount = balance.get("amount", "Unknown")
        except ApiError as exc:
            return self.send(
                chat,
                f"Could not read the Telegram Stars balance: {exc}\n"
                "You can still check it from the bot profile."
            )
        return self.send(
            chat,
            f"💸 Telegram Bot Stars Balance: ⭐{amount}\n\n"
            "Withdrawal is completed by the BOT OWNER in Telegram, not by this bot menu.\n"
            "1. Open this bot's Telegram profile.\n"
            "2. Tap Edit → Balance.\n"
            "3. Choose Withdraw via Fragment if Telegram shows it as available.\n"
            "4. Complete the Fragment steps and connect your TON wallet.\n\n"
            "Telegram currently shows a 1,000-Star minimum in its withdrawal UI. "
            "Eligibility and available amount are controlled by Telegram. "
            "Never send your 2-step password or wallet recovery phrase to anyone."
        )

    def show_saved_media(self, chat):
        catalog = self.db.catalog()
        if not catalog:
            return self.send(chat, "No saved media yet. Use ➕ Add Media or /add.")
        rows = [
            [b(f"{'🖼' if item['kind']=='photo' else '🎬'} #{item['id']} {item['title'][:22]} · ⭐{item['price']}",
               f"manage:{item['id']}")]
            for item in catalog
        ]
        return self.send(chat, "🗂 Saved Media — choose an item to manage:", inline_buttons(rows))

    def show_item_actions(self, chat, item_id):
        item = self.db.item(item_id)
        if not item or not item['active']:
            return self.send(chat, "This saved item is not active.")
        return self.send(
            chat,
            f"🗂 {item['title']}\nItem ID: {item_id}\n"
            f"Type: {item['kind']}\nSaved price: ⭐{item['price']}\n\n"
            "Choose an action:",
            inline_buttons([
                [b("📤 Send to Buyer(s)", f"senditem:{item_id}")],
                [b("✏️ Edit Saved Price", f"editprice:{item_id}")],
                [b("🗑 Hide/Delete from Saved List", f"askdelete:{item_id}")],
                [b("⬅️ Back to Saved Media", "saved")],
            ])
        )

    def show_send_media(self, chat):
        catalog = self.db.catalog()
        if not catalog:
            return self.send(chat, "No saved media yet. Use ➕ Add Media first.")
        rows = [
            [b(f"{'🖼' if item['kind']=='photo' else '🎬'} {item['title'][:24]} · saved ⭐{item['price']}",
               f"senditem:{item['id']}")]
            for item in catalog
        ]
        return self.send(chat, "📤 Choose saved media to send. Its saved Stars price will be used by default:", inline_buttons(rows))

    def ask_item_price(self, chat, item_id):
        item = self.db.item(item_id)
        if not item or not item['active']:
            return self.send(chat, "Saved item not found.")
        self.item_price_drafts[self.admin] = item_id
        return self.send(
            chat,
            f"✏️ Edit price for {item['title']}\nCurrent saved price: ⭐{item['price']}\n"
            "Send the new default Stars price (1–25000). This changes the saved price for future sends."
        )

    def render_multi_select(self, chat):
        draft = self.send_drafts.get(self.admin)
        if not draft:
            return self.send(chat, "Send selection expired. Tap 📤 Send to Buyer again.")
        item = self.db.item(draft['item_id'])
        if not item or not item['active']:
            self.send_drafts.pop(self.admin, None)
            return self.send(chat, "Saved media is no longer active.")
        buyers = self.db.eligible_business_buyers(self.admin)
        eligible_ids = {x['chat_id'] for x in buyers}
        draft['buyer_ids'].intersection_update(eligible_ids)
        if not buyers:
            self.send_drafts.pop(self.admin, None)
            return self.send(chat, "No eligible buyers right now. Ask buyers to message your personal Telegram account again.")
        rows = []
        for buyer in buyers:
            chosen = buyer['chat_id'] in draft['buyer_ids']
            label = ("☑️ " if chosen else "⬜ ") + buyer['display_name'][:18] + f" · {buyer['chat_id']}"
            rows.append([b(label[:60], f"mt:{buyer['chat_id']}")])
        rows.extend([
            [b("☑️ Select All", "ma"), b("Clear", "mc")],
            [b(f"➡️ Next ({len(draft['buyer_ids'])} selected)", "mn")],
            [b("❌ Cancel", "mx")],
        ])
        return self.send(
            chat,
            f"📤 Send: {item['title']}\nSaved price: ⭐{item['price']}\n\n"
            "Select one or more eligible buyers. The same saved media and price will be reused; no re-upload needed.",
            inline_buttons(rows)
        )

    def render_multi_review(self, chat):
        draft = self.send_drafts.get(self.admin)
        if not draft:
            return self.send(chat, "Send selection expired. Start again from 📤 Send to Buyer.")
        item = self.db.item(draft['item_id'])
        if not item or not item['active']:
            self.send_drafts.pop(self.admin, None)
            return self.send(chat, "Saved media is no longer active.")
        buyer_ids = sorted(draft['buyer_ids'])
        if not buyer_ids:
            return self.render_multi_select(chat)
        price = int(draft.get('price', item['price']))
        buyer_rows = []
        for uid in buyer_ids[:20]:
            buyer = self.db.eligible_business_buyer(uid, self.admin)
            if buyer:
                buyer_rows.append(f"• {buyer['display_name']} ({uid})")
        text = (
            f"📤 Batch send confirmation\nMedia: {item['title']}\n"
            f"Recipients selected: {len(buyer_ids)}\nStars price per buyer: ⭐{price}\n\n"
            + ("\n".join(buyer_rows) if buyer_rows else "Recipients will be rechecked before sending.")
            + "\n\nSame saved media will be sent separately to each selected buyer."
        )
        return self.send(chat, text, inline_buttons([
            [b(f"✅ Send to {len(buyer_ids)} buyers · ⭐{price} each", "ms")],
            [b("✏️ Change price for this batch", "mp")],
            [b("⬅️ Back to buyer selection", "mb")],
            [b("❌ Cancel", "mx")],
        ]))

    def send_batch(self, chat):
        draft = self.send_drafts.get(self.admin)
        if not draft:
            return self.send(chat, "Send selection expired. Start again.")
        item = self.db.item(draft['item_id'])
        if not item or not item['active']:
            self.send_drafts.pop(self.admin, None)
            return self.send(chat, "Saved media is no longer active.")
        price = int(draft.get('price', item['price']))
        if not 1 <= price <= MAX_PRICE:
            return self.send(chat, "Invalid Stars price.")
        sent, skipped, failed = [], [], []
        for uid in list(draft['buyer_ids']):
            buyer = self.db.eligible_business_buyer(uid, self.admin)
            if not buyer:
                skipped.append(f"{uid}: reply window/permission expired")
                continue
            ok, reason = self.send_one_business_paid_media(uid, item['id'], price)
            if ok:
                sent.append(f"{buyer['display_name']} ({uid})")
            elif reason == "recently_sent":
                skipped.append(f"{buyer['display_name']} ({uid}): already sent recently")
            else:
                failed.append(f"{buyer['display_name']} ({uid}): {reason}")
            time.sleep(0.12)
        self.send_drafts.pop(self.admin, None)
        self.batch_price_drafts.pop(self.admin, None)
        summary = (
            f"📊 Batch send finished\nMedia: {item['title']}\nPrice per buyer: ⭐{price}\n"
            f"✅ Sent: {len(sent)}\n⏭️ Skipped: {len(skipped)}\n❌ Failed: {len(failed)}"
        )
        if sent:
            summary += "\n\nSent to:\n" + "\n".join("• " + x for x in sent[:20])
        if skipped:
            summary += "\n\nSkipped:\n" + "\n".join("• " + x for x in skipped[:10])
        if failed:
            summary += "\n\nFailed:\n" + "\n".join("• " + x for x in failed[:10])
        return self.send(chat, summary, self.menu_keyboard(self.admin))

    def browse(self, chat):
        items = self.db.catalog()
        if not items:
            return self.send(chat, "No paid media published yet.")
        rows = [[b(f"{'🖼️' if x['kind']=='photo' else '🎬'} {x['title'][:28]} — ⭐{x['price']}", f"buy:{x['id']}")]
                for x in items]
        self.send(chat, "🛍️ Select an item. A locked media message will appear; unlock it using Telegram Stars.",
                  inline_buttons(rows))

    def library(self, uid, chat):
        items = self.db.library(uid)
        if not items:
            return self.send(chat, "No purchases yet. Open /browse to see paid media.")
        self.send(chat, "✅ Your unlocked media (tap to receive it again):",
                  inline_buttons([[b(f"📁 {x['title'][:35]}", f"again:{x['id']}")]
                                  for x in items]))

    def buy(self, uid, chat, item_id):
        item = self.db.item(item_id)
        if not item or not item["active"]:
            return self.send(chat, "This item is unavailable.")
        if self.db.purchased(uid, item_id):
            return self.send(chat, "You already purchased this item. Use /library to view it again.")
        if self.db.recently_offered(uid, item_id):
            return self.send(chat, "🔒 Check the paid media message just sent above. Tap Unlock there.")
        payload = "offer:" + uuid.uuid4().hex
        self.db.save_offer(payload, uid, item_id, item["price"])
        media = [{"type": item["kind"], "media": item["file_id"]}]
        try:
            self.api.call("sendPaidMedia", {
                "chat_id": chat,
                "star_count": item["price"],
                "media": media,
                "payload": payload,
                "caption": item["title"][:1024],
                "protect_content": True,
            })
        except ApiError:
            self.db.drop_offer(payload)
            raise
        self.db.mark_sent(payload)

    def again(self, uid, chat, item_id):
        if not self.db.purchased(uid, item_id):
            return self.send(chat, "⛔ Purchase required. Use /browse.")
        item = self.db.item(item_id)
        if not item:
            return self.send(chat, "This item is unavailable.")
        method = "sendPhoto" if item["kind"] == "photo" else "sendVideo"
        key = "photo" if item["kind"] == "photo" else "video"
        self.api.call(method, {"chat_id": chat, key: item["file_id"],
                               "caption": item["title"], "protect_content": True})

    def purchased(self, data):
        buyer = data.get("from", {})
        uid = buyer.get("id")
        payload = data.get("paid_media_payload", "")
        if not isinstance(uid, int) or not payload:
            return
        new, offer = self.db.record_sale(payload, uid)
        if not offer:
            LOG.warning("Received unknown paid-media purchase payload; verify retained DB and data volume")
            return
        if new and not offer.get("business_connection_id"):
            # Business buyers might not have a private chat with the bot.
            # Sending this notice is non-essential: the original paid media is
            # already unlocked by Telegram after successful payment.
            try:
                self.send(uid, "✅ Payment confirmed. Your paid media above is unlocked!\n"
                               "You can also use /library to reopen it.")
            except ApiError:
                LOG.warning("Payment saved, but buyer confirmation message failed")


    def handle_business_connection(self, connection):
        if self.db.save_business_connection(connection, self.admin):
            LOG.info("Admin business connection updated (enabled=%s)", bool(connection.get("is_enabled")))

    def handle_business_message(self, msg):
        """Store only eligible buyer IDs; no business-chat content is copied or stored."""
        connection_id = msg.get("business_connection_id")
        user = msg.get("from") or {}
        chat = msg.get("chat") or {}
        if not connection_id or chat.get("type") != "private":
            return
        uid, chat_id = user.get("id"), chat.get("id")
        if not isinstance(uid, int) or uid == self.admin or user.get("is_bot") or uid != chat_id:
            return
        connection = self.db.business_connection(connection_id)
        if connection is None:
            # Handles connecting the bot before deploying this update.
            try:
                fresh = self.api.call("getBusinessConnection", {"business_connection_id": connection_id})
            except ApiError:
                LOG.warning("Cannot validate business connection; ignoring message")
                return
            self.db.save_business_connection(fresh, self.admin)
            connection = self.db.business_connection(connection_id)
        if not connection or connection["owner_id"] != self.admin or not connection["is_enabled"]:
            return
        when = int(msg.get("date") or 0)
        if when <= 0 or when > int(time.time()) + 300:
            return
        name = (user.get("first_name") or "Buyer") + " " + (user.get("last_name") or "")
        is_new = self.db.save_business_buyer(uid, connection_id, name.strip(), when)
        if is_new:
            # Notify admin only once per new sender; don't copy their private message.
            try:
                self.send(self.admin,
                    f"👤 New personal-chat buyer: {name.strip()}\nID: {uid}\n"
                    "Open /buyers to choose media and set a Stars price.")
            except ApiError:
                LOG.warning("New buyer saved but admin notification could not be sent")

    def list_business_buyers(self, chat):
        buyers = self.db.all_business_buyers(self.admin)
        if not buyers:
            return self.send(chat,
                "No personal-chat buyers recorded yet. Enable All Private Chats Except... "
                "in Telegram Business → Chatbots, grant Reply permission, "
                "then ask a buyer to send a NEW message to your PERSONAL Telegram account.")
        cutoff = int(time.time()) - 24 * 3600
        recent = [x for x in buyers if x['is_enabled'] and x['can_reply'] and x['last_incoming_epoch'] > cutoff]
        expired = [x for x in buyers if x not in recent]
        rows = [[b(f"👤 {x['display_name'][:20]} · {x['chat_id']}", f"bizbuyer:{x['chat_id']}")]
                for x in recent]
        note = "Recent buyers (tap a name to send locked media):"
        if expired:
            note += "\n\nOlder/unavailable buyer IDs (ask them to message you again):\n"
            note += "\n".join(f"• {x['display_name']} · {x['chat_id']}" for x in expired[:30])
        if not recent:
            note = "No currently eligible buyers.\n\n" + note
        return self.send(chat, note, inline_buttons(rows) if rows else None)

    def list_business_items(self, chat, buyer_id):
        buyer = self.db.eligible_business_buyer(buyer_id, self.admin)
        if not buyer:
            return self.send(chat, "This buyer is not eligible now. Ask them to message your personal account again.")
        items = self.db.catalog()
        if not items:
            return self.send(chat, "No paid items. Use /add first.")
        rows = [[b(f"{'🖼' if i['kind']=='photo' else '🎬'} {i['title'][:24]} · ⭐{i['price']}",
                   f"bizitem:{buyer_id}:{i['id']}")] for i in items]
        return self.send(chat, f"Select paid media for {buyer['display_name']}:", inline_buttons(rows))

    def ask_business_price(self, chat, buyer_id, item_id):
        """Set a per-buyer price without changing the catalog default price."""
        buyer = self.db.eligible_business_buyer(buyer_id, self.admin)
        item = self.db.item(item_id)
        if not buyer or not item or not item['active']:
            return self.send(chat, "Buyer/item unavailable or reply window expired. Ask for a new message.")
        self.price_drafts[self.admin] = {"buyer_id": buyer_id, "item_id": item_id}
        return self.send(chat,
            f"👤 Buyer: {buyer['display_name']} (ID {buyer_id})\n"
            f"📷 Item: {item['title']}\nDefault price: ⭐{item['price']}\n\n"
            "Send a NEW Stars price (1–25000), or use the default button below.",
            inline_buttons([[b(f"⭐ Use default {item['price']}", f"bizdefault:{buyer_id}:{item_id}")],
                            [b("❌ Cancel", "bizcancel")]]))

    def prepare_business_send(self, chat, buyer_id, item_id, price=None):
        buyer = self.db.eligible_business_buyer(buyer_id, self.admin)
        item = self.db.item(item_id)
        if not buyer or not item or not item["active"]:
            return self.send(chat, "Buyer/item unavailable. Ask buyer to message your personal chat again if needed.")
        if price is None:
            price = item['price']
        if not isinstance(price, int) or not 1 <= price <= MAX_PRICE:
            return self.send(chat, "Invalid Stars price. Enter 1–25000.")
        self.price_drafts.pop(self.admin, None)
        return self.send(chat, f"Send item #{item_id} ({item['title']}) for ⭐{price} "
                         f"to buyer {buyer['display_name']}?",
                         inline_buttons([[b("✅ Send locked media", f"bizconfirm:{buyer_id}:{item_id}:{price}")],
                                         [b("❌ Cancel", "bizcancel")]]))

    def send_one_business_paid_media(self, buyer_id, item_id, price):
        buyer = self.db.eligible_business_buyer(buyer_id, self.admin)
        item = self.db.item(item_id)
        if not buyer or not item or not item["active"]:
            return False, "buyer_or_item_unavailable"
        if not isinstance(price, int) or not 1 <= price <= MAX_PRICE:
            return False, "invalid_price"
        if self.db.recently_offered(buyer_id, item_id):
            return False, "recently_sent"
        payload = "offer:" + uuid.uuid4().hex
        self.db.save_offer(payload, buyer_id, item_id, price, buyer["connection_id"])
        try:
            self.api.call("sendPaidMedia", {
                "business_connection_id": buyer["connection_id"],
                "chat_id": buyer_id,
                "star_count": price,
                "media": [{"type": item["kind"], "media": item["file_id"]}],
                "payload": payload,
                "caption": item["title"][:1024],
                "protect_content": True,
            })
        except ApiError as exc:
            self.db.drop_offer(payload)
            return False, str(exc)
        self.db.mark_sent(payload)
        return True, "sent"

    def send_business_paid_media(self, chat, buyer_id, item_id, price=None):
        item = self.db.item(item_id)
        if price is None and item:
            price = item["price"]
        if not isinstance(price, int):
            return self.send(chat, "Invalid Stars price.")
        ok, reason = self.send_one_business_paid_media(buyer_id, item_id, price)
        if not ok:
            if reason == "recently_sent":
                return self.send(chat, "Already sent recently. Check your personal chat with the buyer.")
            return self.send(chat, f"❌ Not sent: {reason}. Check buyer permission and recent message window.")
        buyer = self.db.eligible_business_buyer(buyer_id, self.admin)
        name = buyer['display_name'] if buyer else str(buyer_id)
        return self.send(chat, f"✅ Locked media sent to {name}. Buyer sees ⭐{price} Unlock.")

    def handle_message(self, msg):
        uid = (msg.get("from") or {}).get("id")
        chat_obj = msg.get("chat") or {}
        chat = chat_obj.get("id")
        if uid is None or chat is None or chat_obj.get("type") != "private":
            return
        text = (msg.get("text") or "").strip()
        cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""

        # Persistent reply-keyboard menu actions.
        if uid == self.admin:
            if text == "📤 Send to Buyer":
                return self.show_send_media(chat)
            if text == "🗂 Saved Media":
                return self.show_saved_media(chat)
            if text == "➕ Add Media":
                text = "/add"
                cmd = "/add"
            elif text == "👥 My Buyers":
                return self.list_business_buyers(chat)
            elif text == "💰 Stars Balance":
                text = "/balance"
                cmd = "/balance"
            elif text == "💸 Withdraw":
                return self.withdraw_info(chat)
            elif text == "📊 Sales & Stats":
                text = "/stats"
                cmd = "/stats"
            elif text == "🛍 Browse":
                return self.browse(chat)
        else:
            if text == "🛍 Browse":
                return self.browse(chat)
            if text == "📚 My Library":
                return self.library(uid, chat)
            if text == "🆔 My ID":
                return self.send(chat, f"Your Telegram user ID: {uid}", self.menu_keyboard(uid))

        if cmd == "/id":
            return self.send(chat, f"Your Telegram user ID: {uid}")
        if cmd in ("/start", "/help"):
            return self.help(uid, chat)
        if cmd in ("/browse", "/catalog"):
            return self.browse(chat)
        if cmd == "/library":
            return self.library(uid, chat)

        if uid == self.admin:
            if cmd == "/buyers":
                return self.list_business_buyers(chat)
            if cmd in ("/send", "/sendbuyer"):
                if cmd == "/send" and len(text.split()) == 1:
                    return self.show_send_media(chat)
                args = text.split()
                if len(args) not in (3, 4) or not all(x.isdigit() for x in args[1:]):
                    return self.send(chat, "Usage: /sendbuyer BUYER_ID ITEM_ID [PRICE] (or use /buyers)")
                if len(args) == 4:
                    return self.prepare_business_send(chat, int(args[1]), int(args[2]), int(args[3]))
                return self.ask_business_price(chat, int(args[1]), int(args[2]))
            if cmd in ("/saved", "/media"):
                return self.show_saved_media(chat)
            if cmd == "/withdraw":
                return self.withdraw_info(chat)
            if cmd == "/cancel":
                self.drafts.pop(uid, None)
                self.price_drafts.pop(uid, None)
                self.item_price_drafts.pop(uid, None)
                self.batch_price_drafts.pop(uid, None)
                self.send_drafts.pop(uid, None)
                return self.send(chat, "Cancelled.", self.menu_keyboard(self.admin))
            if cmd == "/add":
                self.price_drafts.pop(uid, None)
                self.drafts[uid] = {"step": "media"}
                return self.send(chat, "Step 1/3: Send a PHOTO or VIDEO directly to this bot. Do not send as a document.\n/cancel to stop.")
            if cmd == "/items":
                items = self.db.catalog()
                return self.send(chat, "\n".join(f"#{i['id']} · ⭐{i['price']} · {i['title']}" for i in items)
                                 if items else "No active items")
            if cmd == "/stats":
                n, sales, total, buyers = self.db.stats()
                return self.send(chat, f"📊 Local stats\nActive items: {n}\nPaid unlocks: {sales}\n"
                                       f"Unique buyers: {buyers}\nRecorded gross sales: ⭐{total}\n"
                                       "Official available Stars may differ due to refunds, restrictions, and withdrawal timing. /balance for Telegram balance.")
            if cmd == "/balance":
                balance = self.api.call("getMyStarBalance")
                # getMyStarBalance returns StarAmount: {amount, nanostar_amount?}
                return self.send(chat, f"⭐ Telegram bot Stars balance: {balance.get('amount', 'N/A')}\n"
                                       "Withdrawal eligibility is controlled by Telegram/Fragment, not this bot.")
            if cmd == "/delete":
                parts = text.split()
                if len(parts) != 2 or not parts[1].isdigit():
                    return self.send(chat, "Usage: /delete 3")
                changed = self.db.remove_item(int(parts[1]))
                return self.send(chat, "Item hidden from catalog." if changed else "Item not found/already hidden.")

            if uid in self.item_price_drafts and not cmd:
                if not text.isdigit() or not 1 <= int(text) <= MAX_PRICE:
                    return self.send(chat, "Enter a new saved price from 1 to 25000, or /cancel.")
                item_id = self.item_price_drafts.pop(uid)
                changed = self.db.update_item_price(item_id, int(text))
                return self.send(
                    chat,
                    f"✅ Saved price updated to ⭐{int(text)}." if changed else "Item not found or inactive.",
                    self.menu_keyboard(self.admin)
                )

            if uid in self.batch_price_drafts and not cmd:
                if not text.isdigit() or not 1 <= int(text) <= MAX_PRICE:
                    return self.send(chat, "Enter one batch price from 1 to 25000, or /cancel.")
                draft = self.send_drafts.get(uid)
                self.batch_price_drafts.pop(uid, None)
                if not draft:
                    return self.send(chat, "Batch selection expired. Start again.")
                draft['price'] = int(text)
                return self.render_multi_review(chat)

            price_draft = self.price_drafts.get(uid)
            if price_draft and not cmd:
                if not text.isdigit() or not 1 <= int(text) <= MAX_PRICE:
                    return self.send(chat, "Enter a Stars price from 1 to 25000, or /cancel.")
                return self.prepare_business_send(chat, price_draft['buyer_id'],
                                                  price_draft['item_id'], int(text))

            draft = self.drafts.get(uid)
            if draft:
                if cmd:
                    return self.send(chat, "Upload in progress. Finish it or /cancel.")
                if draft["step"] == "media":
                    photos = msg.get("photo") or []
                    video = msg.get("video")
                    if photos:
                        draft.update(step="title", kind="photo", file_id=photos[-1]["file_id"])
                    elif video:
                        draft.update(step="title", kind="video", file_id=video["file_id"])
                    else:
                        return self.send(chat, "Please send a photo or video (not a file/document).")
                    return self.send(chat, "Step 2/3: Send a short title (max 100 characters).")
                if draft["step"] == "title":
                    if not text or len(text) > 100:
                        return self.send(chat, "Title must be 1–100 characters.")
                    draft.update(step="price", title=text)
                    return self.send(chat, "Step 3/3: Enter Stars price as a whole number from 1 to 25000, e.g. 50.")
                if draft["step"] == "price":
                    if not text.isdigit() or not (1 <= int(text) <= MAX_PRICE):
                        return self.send(chat, "Enter a whole Stars price between 1 and 25000.")
                    item_id = self.db.add_item(draft["title"], draft["kind"], draft["file_id"], int(text))
                    self.drafts.pop(uid, None)
                    return self.send(chat, f"✅ Item #{item_id} published! Price: ⭐{text}\n"
                                           "Use /browse to test paid media with a different user account.")
        if cmd:
            return self.help(uid, chat)
        self.send(chat, "Use /browse to view locked media.")

    def handle_callback(self, cb):
        sender = cb.get("from") or {}
        uid = sender.get("id")
        mid = cb.get("message") or {}
        chat_obj = mid.get("chat") or {}
        chat = chat_obj.get("id")
        callback_id = cb.get("id")
        if callback_id:
            try:
                self.api.call("answerCallbackQuery", {"callback_query_id": callback_id})
            except ApiError:
                LOG.warning("Unable to acknowledge callback query")
        if uid is None or chat is None or chat_obj.get("type") != "private" or chat != uid:
            return
        data = cb.get("data", "")
        if uid == self.admin:
            parts = data.split(":")
            if data == "saved":
                return self.show_saved_media(chat)
            if data == "mx":
                self.send_drafts.pop(uid, None)
                self.batch_price_drafts.pop(uid, None)
                return self.send(chat, "Batch send cancelled.", self.menu_keyboard(self.admin))
            if data == "ma":
                draft = self.send_drafts.get(uid)
                if draft:
                    draft['buyer_ids'] = {x['chat_id'] for x in self.db.eligible_business_buyers(self.admin)}
                return self.render_multi_select(chat)
            if data == "mc":
                draft = self.send_drafts.get(uid)
                if draft:
                    draft['buyer_ids'].clear()
                return self.render_multi_select(chat)
            if data == "mn":
                return self.render_multi_review(chat)
            if data == "mb":
                return self.render_multi_select(chat)
            if data == "ms":
                return self.send_batch(chat)
            if data == "mp":
                draft = self.send_drafts.get(uid)
                if not draft or not draft.get('buyer_ids'):
                    return self.render_multi_select(chat)
                self.batch_price_drafts[uid] = True
                return self.send(chat, "Send one Stars price for all selected buyers (1–25000).\nThis does not change the saved media price.")
            if data.startswith("mt:") and data[3:].isdigit():
                draft = self.send_drafts.get(uid)
                buyer_id = int(data[3:])
                if draft is not None:
                    if buyer_id in draft['buyer_ids']:
                        draft['buyer_ids'].remove(buyer_id)
                    else:
                        draft['buyer_ids'].add(buyer_id)
                return self.render_multi_select(chat)
            if data.startswith("manage:") and data[7:].isdigit():
                return self.show_item_actions(chat, int(data[7:]))
            if data.startswith("senditem:") and data[9:].isdigit():
                item_id = int(data[9:])
                item = self.db.item(item_id)
                if not item or not item['active']:
                    return self.send(chat, "Saved item not found.")
                self.send_drafts[uid] = {"item_id": item_id, "buyer_ids": set(), "price": int(item['price'])}
                return self.render_multi_select(chat)
            if data.startswith("editprice:") and data[10:].isdigit():
                return self.ask_item_price(chat, int(data[10:]))
            if data.startswith("askdelete:") and data[10:].isdigit():
                item_id = int(data[10:])
                item = self.db.item(item_id)
                if not item or not item['active']:
                    return self.send(chat, "Saved item not found.")
                return self.send(chat, f"Hide {item['title']} from Saved Media and future sends? Purchase history will be kept.",
                                 inline_buttons([[b("🗑 Yes, hide item", f"hideitem:{item_id}")],
                                                 [b("Cancel", f"manage:{item_id}")]]))
            if data.startswith("hideitem:") and data[9:].isdigit():
                item_id = int(data[9:])
                changed = self.db.remove_item(item_id)
                return self.send(chat, "🗑 Item hidden from future sends. Sales history remains محفوظ." if changed else "Item was already hidden or not found.", self.menu_keyboard(self.admin))
            if data == "bizcancel":
                self.price_drafts.pop(uid, None)
                return self.send(chat, "Cancelled.")
            if len(parts) == 2 and parts[0] == "bizbuyer" and parts[1].isdigit():
                return self.list_business_items(chat, int(parts[1]))
            if len(parts) == 3 and parts[0] == "bizitem" and parts[1].isdigit() and parts[2].isdigit():
                return self.ask_business_price(chat, int(parts[1]), int(parts[2]))
            if len(parts) == 3 and parts[0] == "bizdefault" and parts[1].isdigit() and parts[2].isdigit():
                return self.prepare_business_send(chat, int(parts[1]), int(parts[2]))
            if len(parts) == 4 and parts[0] == "bizconfirm" and all(p.isdigit() for p in parts[1:]):
                return self.send_business_paid_media(chat, int(parts[1]), int(parts[2]), int(parts[3]))
            # Compatibility with callbacks generated before the update.
            if len(parts) == 3 and parts[0] == "bizconfirm" and parts[1].isdigit() and parts[2].isdigit():
                return self.send_business_paid_media(chat, int(parts[1]), int(parts[2]))
        if data == "catalog":
            return self.browse(chat)
        if data.startswith("buy:") and data[4:].isdigit():
            return self.buy(uid, chat, int(data[4:]))
        if data.startswith("again:") and data[6:].isdigit():
            return self.again(uid, chat, int(data[6:]))

    def handle_update(self, update):
        if "business_connection" in update:
            return self.handle_business_connection(update["business_connection"])
        if "business_message" in update:
            return self.handle_business_message(update["business_message"])
        if "purchased_paid_media" in update:
            return self.purchased(update["purchased_paid_media"])
        if "callback_query" in update:
            return self.handle_callback(update["callback_query"])
        if "message" in update:
            return self.handle_message(update["message"])


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("BOT_TOKEN", "").strip()
    admin = os.environ.get("ADMIN_ID", "").strip()
    if not token or not admin.isdecimal():
        raise SystemExit("Missing BOT_TOKEN or numeric ADMIN_ID. Configure Railway Variables.")

    # Railway volume (mount at /data) preserves buyers and media catalog.
    default_path = "/data/starsbot.sqlite" if os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") == "/data" else "./data/starsbot.sqlite"
    db_path = os.environ.get("DATABASE_PATH", default_path)
    db = Storage(db_path)
    api = TelegramAPI(token)
    bot = StarsBot(api, db, int(admin))
    LOG.info("Starting Telegram Stars media bot (database: %s)", db_path)
    api.call("getMe")
    api.call("deleteWebhook", {"drop_pending_updates": False})
    offset = db.get_offset()
    while True:
        try:
            updates = api.call("getUpdates", {"offset": offset, "timeout": 30,
                                               "allowed_updates": ["message", "callback_query", "purchased_paid_media", "business_connection", "business_message"]},
                               timeout=45)
            for update in updates:
                try:
                    bot.handle_update(update)
                except ApiError as exc:
                    LOG.error("Telegram operation failed: %s", exc)
                    # User-request failures (e.g. invalid file_id) are recorded;
                    # later updates still proceed. The user can retry the action.
                except Exception:
                    LOG.exception("Unexpected update processing failure; retry after restart")
                    raise
                offset = int(update["update_id"]) + 1
                db.set_offset(offset)
        except KeyboardInterrupt:
            LOG.info("Stopping bot")
            break
        except Exception as exc:
            if isinstance(exc, ApiError):
                LOG.error("Polling failure: %s", exc)
            else:
                LOG.exception("Polling failure")
            time.sleep(5)


if __name__ == "__main__":
    main()
