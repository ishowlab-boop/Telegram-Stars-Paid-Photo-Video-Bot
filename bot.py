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
            response.raise_for_status()
            result = response.json()
        except (requests.RequestException, ValueError) as exc:
            # Avoid logging sensitive URL containing bot token.
            raise ApiError("Telegram network/HTTP error: check Railway logs and connectivity") from None
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
            """)

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
        with self.connect() as c:
            return c.execute("UPDATE items SET active=0 WHERE id=? AND active=1", (item_id,)).rowcount

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

    def save_offer(self, payload, user_id, item_id, price):
        with self.connect() as c:
            c.execute("INSERT INTO offers(payload,user_id,item_id,price,created_at,created_epoch) VALUES(?,?,?,?,?,?)",
                      (payload, user_id, item_id, price, utc_now(), int(time.time())))

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
        self.send(chat,
            "⭐ Welcome to Stars Media Bot!\n\n"
            "/browse — locked photo/video catalog\n"
            "/library — items you already unlocked\n"
            "/id — your Telegram user ID\n"
            "/help — commands" +
            ("\n\nADMIN:\n/add — create a paid item\n/items — list active items\n"
             "/delete ID — hide an item\n/stats — local purchase stats\n"
             "/balance — Telegram bot Stars balance\n/cancel — cancel upload"
             if uid == self.admin else ""),
            inline_buttons([[b("🛍️ Browse locked media", "catalog")]]))

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
        if new:
            # Sending this notice is non-essential: the original paid media is
            # already unlocked by Telegram after successful payment.
            try:
                self.send(uid, "✅ Payment confirmed. Your paid media above is unlocked!\n"
                               "You can also use /library to reopen it.")
            except ApiError:
                LOG.warning("Payment saved, but buyer confirmation message failed")

    def handle_message(self, msg):
        uid = (msg.get("from") or {}).get("id")
        chat_obj = msg.get("chat") or {}
        chat = chat_obj.get("id")
        if uid is None or chat is None or chat_obj.get("type") != "private":
            return
        text = (msg.get("text") or "").strip()
        cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if cmd == "/id":
            return self.send(chat, f"Your Telegram user ID: {uid}")
        if cmd in ("/start", "/help"):
            return self.help(uid, chat)
        if cmd in ("/browse", "/catalog"):
            return self.browse(chat)
        if cmd == "/library":
            return self.library(uid, chat)

        if uid == self.admin:
            if cmd == "/cancel":
                self.drafts.pop(uid, None)
                return self.send(chat, "Upload cancelled.")
            if cmd == "/add":
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
        if data == "catalog":
            return self.browse(chat)
        if data.startswith("buy:") and data[4:].isdigit():
            return self.buy(uid, chat, int(data[4:]))
        if data.startswith("again:") and data[6:].isdigit():
            return self.again(uid, chat, int(data[6:]))

    def handle_update(self, update):
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
                                               "allowed_updates": ["message", "callback_query", "purchased_paid_media"]},
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
