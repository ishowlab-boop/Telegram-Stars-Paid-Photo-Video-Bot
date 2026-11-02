
import logging
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import requests

# =====================================
# CONFIGURATION
# =====================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

LOG = logging.getLogger("stars")

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN = int(os.getenv("ADMIN_ID", "0") or 0)
DB_PATH = os.getenv(
    "DATABASE_PATH",
    "./data/starsbot.sqlite"
)

if not TOKEN or not ADMIN:
    raise SystemExit(
        "Set BOT_TOKEN and numeric ADMIN_ID in Railway Variables"
    )

Path(DB_PATH).parent.mkdir(
    parents=True,
    exist_ok=True
)

SESSION = requests.Session()
API = f"https://api.telegram.org/bot{TOKEN}/"

DRAFTS = {}
PRICE_DRAFTS = {}


# =====================================
# TELEGRAM API
# =====================================

def call(method, **params):
    try:
        response = SESSION.post(
            API + method,
            json=params,
            timeout=45
        )

        data = response.json()

    except (requests.RequestException, ValueError):
        raise RuntimeError(
            "Telegram connection error"
        ) from None

    if not data.get("ok"):
        error = data.get(
            "description",
            "Unknown Telegram error"
        )
        raise RuntimeError(
            f"{method}: {error}"
        )

    return data["result"]


# =====================================
# DATABASE
# =====================================

@contextmanager
def connection():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=20
    )

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=20000")

    try:
        yield conn
        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def initialize():
    with connection() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            kind TEXT NOT NULL,
            file_id TEXT NOT NULL,
            price INTEGER NOT NULL,
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
            business_connection_id TEXT
        );

        CREATE TABLE IF NOT EXISTS sales (
            payload TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            price INTEGER NOT NULL,
            purchased_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
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
            last_incoming_epoch INTEGER NOT NULL
        );
        """)

        columns = [
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(offers)"
            )
        ]

        if "business_connection_id" not in columns:
            conn.execute(
                "ALTER TABLE offers "
                "ADD COLUMN business_connection_id TEXT"
            )


def one(query, args=()):
    with connection() as conn:
        return conn.execute(
            query,
            args
        ).fetchone()


def many(query, args=()):
    with connection() as conn:
        return conn.execute(
            query,
            args
        ).fetchall()


def execute(query, args=()):
    with connection() as conn:
        cursor = conn.execute(
            query,
            args
        )
        return cursor.lastrowid


# =====================================
# BUTTONS AND MESSAGES
# =====================================

def buttons(rows):
    return {
        "inline_keyboard": [
            [
                {
                    "text": label,
                    "callback_data": data
                }
                for label, data in row
            ]
            for row in rows
        ]
    }


def say(chat, text, rows=None):
    args = {
        "chat_id": chat,
        "text": text
    }

    if rows:
        args["reply_markup"] = buttons(rows)

    return call("sendMessage", **args)


# =====================================
# MEDIA CATALOG
# =====================================

def items():
    return many(
        "SELECT * FROM items "
        "WHERE active=1 "
        "ORDER BY id DESC LIMIT 50"
    )


def item(item_id):
    return one(
        "SELECT * FROM items "
        "WHERE id=? AND active=1",
        (item_id,)
    )


def show_help(chat, is_admin):
    message = (
        "⭐ My Stars Media\n\n"
        "/start - Menu\n"
        "/browse - Paid photos/videos\n"
        "/library - Your purchases\n"
        "/id - Your Telegram ID"
    )

    if is_admin:
        message += (
            "\n\nADMIN:\n"
            "/add - Upload photo/video\n"
            "/items - Published media\n"
            "/buyers - Personal chat buyers\n"
            "/sendbuyer BUYER_ID ITEM_ID PRICE\n"
            "/balance - Bot Stars balance\n"
            "/stats - Sales information\n"
            "/delete ITEM_ID - Hide item\n"
            "/cancel - Cancel operation"
        )

    say(
        chat,
        message,
        [[("🛍️ Browse", "browse")]]
    )


def show_browse(chat):
    catalog = items()

    if not catalog:
        return say(
            chat,
            "No paid media yet. Admin can use /add."
        )

    rows = [
        [
            (
                f"{'🖼' if x['kind'] == 'photo' else '🎬'} "
                f"{x['title'][:22]} · ⭐{x['price']}",
                f"buy:{x['id']}"
            )
        ]
        for x in catalog
    ]

    return say(
        chat,
        "Select paid media:",
        rows
    )


# =====================================
# SEND PAID MEDIA
# =====================================

def paid_send(
    chat,
    media_item,
    price,
    business_id=None
):
    if not 1 <= price <= 25000:
        raise ValueError("Invalid Stars price")

    payload = "sale_" + uuid.uuid4().hex
    now = int(time.time())

    execute(
        """
        INSERT INTO offers(
            payload,
            user_id,
            item_id,
            price,
            sent,
            created_at,
            created_epoch,
            business_connection_id
        )
        VALUES(?,?,?,?,0,?,?,?)
        """,
        (
            payload,
            chat,
            media_item["id"],
            price,
            str(now),
            now,
            business_id
        )
    )

    args = {
        "chat_id": chat,
        "star_count": price,
        "media": [
            {
                "type": media_item["kind"],
                "media": media_item["file_id"]
            }
        ],
        "caption": media_item["title"],
        "payload": payload,
        "protect_content": True
    }

    if business_id:
        args["business_connection_id"] = business_id

    try:
        call("sendPaidMedia", **args)

    except Exception:
        execute(
            "DELETE FROM offers "
            "WHERE payload=? AND sent=0",
            (payload,)
        )
        raise

    execute(
        "UPDATE offers SET sent=1 "
        "WHERE payload=?",
        (payload,)
    )


# =====================================
# TELEGRAM BUSINESS CONNECTION
# =====================================

def update_business_connection(data):
    owner = (data.get("user") or {}).get("id")

    if owner != ADMIN:
        return False

    rights = data.get("rights") or {}

    can_reply = rights.get(
        "can_reply",
        data.get("can_reply", False)
    )

    execute(
        """
        INSERT INTO business_connections(
            id,
            owner_id,
            is_enabled,
            can_reply,
            updated_at
        )
        VALUES(?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            owner_id=excluded.owner_id,
            is_enabled=excluded.is_enabled,
            can_reply=excluded.can_reply,
            updated_at=excluded.updated_at
        """,
        (
            data["id"],
            ADMIN,
            int(bool(data.get("is_enabled"))),
            int(bool(can_reply)),
            str(int(time.time()))
        )
    )

    return True


# =====================================
# AUTOMATIC BUYER DETECTION
# =====================================

def receive_business(msg):
    business_id = msg.get(
        "business_connection_id"
    )

    sender = msg.get("from") or {}
    chat = msg.get("chat") or {}

    uid = sender.get("id")

    if not business_id:
        return

    if chat.get("type") != "private":
        return

    if not isinstance(uid, int):
        return

    if uid == ADMIN or sender.get("is_bot"):
        return

    if uid != chat.get("id"):
        return

    bc = one(
        "SELECT * FROM business_connections "
        "WHERE id=?",
        (business_id,)
    )

    if not bc:
        data = call(
            "getBusinessConnection",
            business_connection_id=business_id
        )

        if not update_business_connection(data):
            return

        bc = one(
            "SELECT * FROM business_connections "
            "WHERE id=?",
            (business_id,)
        )

    if not bc:
        return

    if bc["owner_id"] != ADMIN:
        return

    if not bc["is_enabled"]:
        return

    stamp = int(msg.get("date") or 0)

    if stamp <= 0:
        return

    if stamp > time.time() + 300:
        return

    name = (
        (sender.get("first_name") or "Buyer")
        + " "
        + (sender.get("last_name") or "")
    )

    is_new = not one(
        "SELECT 1 FROM business_buyers "
        "WHERE chat_id=?",
        (uid,)
    )

    execute(
        """
        INSERT INTO business_buyers(
            chat_id,
            connection_id,
            display_name,
            last_incoming_epoch
        )
        VALUES(?,?,?,?)
        ON CONFLICT(chat_id) DO UPDATE SET
            connection_id=excluded.connection_id,
            display_name=excluded.display_name,
            last_incoming_epoch=MAX(
                last_incoming_epoch,
                excluded.last_incoming_epoch
            )
        """,
        (
            uid,
            business_id,
            name.strip()[:80],
            stamp
        )
    )

    if is_new:
        try:
            say(
                ADMIN,
                f"👤 New buyer: {name.strip()}\n"
                f"ID: {uid}\n"
                "Use /buyers"
            )

        except Exception:
            LOG.warning(
                "Buyer saved; admin notification unavailable"
            )


# =====================================
# ELIGIBLE BUYERS
# =====================================

def eligible_buyer(uid):
    row = one(
        """
        SELECT
            b.*,
            c.can_reply,
            c.is_enabled
        FROM business_buyers b
        JOIN business_connections c
            ON c.id=b.connection_id
        WHERE
            b.chat_id=?
            AND c.owner_id=?
        """,
        (uid, ADMIN)
    )

    if not row:
        return None

    if not row["is_enabled"]:
        return None

    if not row["can_reply"]:
        return None

    if time.time() - row["last_incoming_epoch"] >= 86400:
        return None

    return row


def show_buyers(chat):
    buyers = many(
        """
        SELECT
            b.*,
            c.can_reply,
            c.is_enabled
        FROM business_buyers b
        JOIN business_connections c
            ON c.id=b.connection_id
        WHERE c.owner_id=?
        ORDER BY b.last_incoming_epoch DESC
        LIMIT 50
        """,
        (ADMIN,)
    )

    if not buyers:
        return say(
            chat,
            "No buyers found yet.\n\n"
            "Enable All Private Chats Except... "
            "in Telegram Business.\n"
            "Allow replies and ask someone to send "
            "a NEW message to your PERSONAL account."
        )

    recent = [
        x for x in buyers
        if x["is_enabled"]
        and x["can_reply"]
        and 0 <= time.time() - x["last_incoming_epoch"] < 86400
    ]

    old = [
        x for x in buyers
        if x not in recent
    ]

    note = (
        "👥 Known buyers.\n"
        "Tap an eligible buyer to send paid media."
    )

    if old:
        note += (
            "\n\nOlder/unavailable buyers "
            "(ask for a new message):\n"
        )

        note += "\n".join(
            f"• {x['display_name']} · {x['chat_id']}"
            for x in old[:30]
        )

    keyboard = [
        [
            (
                f"👤 {x['display_name'][:18]} · {x['chat_id']}",
                f"buyer:{x['chat_id']}"
            )
        ]
        for x in recent
    ]

    return say(
        chat,
        note,
        keyboard if keyboard else None
    )


# =====================================
# BUYER MEDIA SELECTION
# =====================================

def select_buyer(chat, uid):
    buyer = eligible_buyer(uid)

    if not buyer:
        return say(
            chat,
            "Buyer cannot receive paid media now. "
            "Ask for a new personal message."
        )

    catalog = items()

    if not catalog:
        return say(
            chat,
            "No media. Upload with /add first."
        )

    rows = [
        [
            (
                f"{x['title'][:24]} ⭐{x['price']}",
                f"choose:{uid}:{x['id']}"
            )
        ]
        for x in catalog
    ]

    return say(
        chat,
        f"Select media for {buyer['display_name']}:",
        rows
    )


# =====================================
# CUSTOM STARS PRICE
# =====================================

def ask_price(chat, uid, item_id):
    buyer = eligible_buyer(uid)
    media_item = item(item_id)

    if not buyer or not media_item:
        return say(
            chat,
            "Buyer or media unavailable."
        )

    PRICE_DRAFTS[ADMIN] = (uid, item_id)

    return say(
        chat,
        f"Buyer: {buyer['display_name']}\n"
        f"Media: {media_item['title']}\n"
        f"Default: ⭐{media_item['price']}\n\n"
        "Send a NEW Stars price (1-25000), "
        "or tap the default price:",
        [
            [
                (
                    f"⭐ Default {media_item['price']}",
                    f"price:{uid}:{item_id}:{media_item['price']}"
                )
            ],
            [
                ("Cancel", "cancel")
            ]
        ]
    )


def confirm(chat, uid, item_id, price):
    PRICE_DRAFTS.pop(ADMIN, None)

    if not eligible_buyer(uid):
        return say(
            chat,
            "Buyer is not currently eligible."
        )

    if not item(item_id):
        return say(
            chat,
            "Media item unavailable."
        )

    if not 1 <= price <= 25000:
        return say(
            chat,
            "Invalid Stars price."
        )

    return say(
        chat,
        f"Send locked media #{item_id} "
        f"to {uid} for ⭐{price}?",
        [
            [
                (
                    "✅ Send paid media",
                    f"send:{uid}:{item_id}:{price}"
                )
            ],
            [
                ("Cancel", "cancel")
            ]
        ]
    )


# =====================================
# MESSAGE HANDLER
# =====================================

def message_handler(msg):
    sender = msg.get("from") or {}
    chat = msg.get("chat") or {}

    uid = sender.get("id")
    chat_id = chat.get("id")

    if not isinstance(uid, int):
        return

    if chat.get("type") != "private":
        return

    if chat_id != uid:
        return

    text = (msg.get("text") or "").strip()

    command = (
        text.split()[0].split("@")[0].lower()
        if text.startswith("/")
        else ""
    )

    if command in ("/start", "/help"):
        return show_help(
            chat_id,
            uid == ADMIN
        )

    if command == "/id":
        return say(
            chat_id,
            f"Your ID: {uid}"
        )

    if command == "/browse":
        return show_browse(chat_id)

    if command == "/library":
        bought = many(
            """
            SELECT DISTINCT i.*
            FROM items i
            JOIN sales s ON s.item_id=i.id
            WHERE s.user_id=?
            ORDER BY i.id DESC
            LIMIT 50
            """,
            (uid,)
        )

        if not bought:
            return say(
                chat_id,
                "No purchases yet."
            )

        rows = [
            [
                (
                    x["title"],
                    f"again:{x['id']}"
                )
            ]
            for x in bought
        ]

        return say(
            chat_id,
            "Your purchased items:",
            rows
        )

    # ADMIN ONLY

    if uid != ADMIN:
        return say(
            chat_id,
            "Use /browse to view paid media."
        )

    if command == "/buyers":
        return show_buyers(chat_id)

    if command == "/cancel":
        DRAFTS.pop(uid, None)
        PRICE_DRAFTS.pop(uid, None)

        return say(
            chat_id,
            "Cancelled."
        )

    if command == "/add":
        DRAFTS[uid] = {
            "stage": "media"
        }

        return say(
            chat_id,
            "Step 1/3: Send one PHOTO or VIDEO "
            "(not a document)."
        )

    if command == "/items":
        catalog = items()

        output = "\n".join(
            f"#{x['id']} ⭐{x['price']} {x['title']}"
            for x in catalog
        )

        return say(
            chat_id,
            output or "No items"
        )

    if command == "/balance":
        result = call("getMyStarBalance")

        return say(
            chat_id,
            f"Telegram bot Stars balance: "
            f"⭐{result.get('amount', 0)}"
        )

    if command == "/stats":
        record = one(
            "SELECT COUNT(*) AS n, "
            "COALESCE(SUM(price),0) AS stars "
            "FROM sales"
        )

        return say(
            chat_id,
            f"Sales: {record['n']}\n"
            f"Gross recorded Stars: ⭐{record['stars']}"
        )

    if command == "/delete":
        parts = text.split()

        if len(parts) != 2 or not parts[1].isdigit():
            return say(
                chat_id,
                "Usage: /delete ITEM_ID"
            )

        execute(
            "UPDATE items SET active=0 WHERE id=?",
            (int(parts[1]),)
        )

        return say(
            chat_id,
            "Item hidden."
        )

    if command == "/sendbuyer":
        args = text.split()

        if len(args) not in (3, 4):
            return say(
                chat_id,
                "Usage: /sendbuyer BUYER_ID ITEM_ID [PRICE]"
            )

        if not all(x.isdigit() for x in args[1:]):
            return say(
                chat_id,
                "Use numeric Buyer ID and Item ID."
            )

        buyer_id = int(args[1])
        item_id = int(args[2])

        if len(args) == 4:
            return confirm(
                chat_id,
                buyer_id,
                item_id,
                int(args[3])
            )

        return ask_price(
            chat_id,
            buyer_id,
            item_id
        )

    # CUSTOM PRICE INPUT

    if uid in PRICE_DRAFTS and not command:
        if not text.isdigit():
            return say(
                chat_id,
                "Enter price 1-25000 or /cancel"
            )

        price = int(text)

        if not 1 <= price <= 25000:
            return say(
                chat_id,
                "Enter price 1-25000"
            )

        buyer_id, item_id = PRICE_DRAFTS[uid]

        return confirm(
            chat_id,
            buyer_id,
            item_id,
            price
        )

    # ADMIN MEDIA UPLOAD

    draft = DRAFTS.get(uid)

    if draft:
        if command:
            return say(
                chat_id,
                "Finish upload or use /cancel"
            )

        if draft["stage"] == "media":
            photos = msg.get("photo") or []
            video = msg.get("video")

            if photos:
                draft.update(
                    stage="title",
                    kind="photo",
                    file_id=photos[-1]["file_id"]
                )

            elif video:
                draft.update(
                    stage="title",
                    kind="video",
                    file_id=video["file_id"]
                )

            else:
                return say(
                    chat_id,
                    "Send a photo or video, not a document."
                )

            return say(
                chat_id,
                "Step 2/3: Send a title (1-100 characters)."
            )

        if draft["stage"] == "title":
            if not 1 <= len(text) <= 100:
                return say(
                    chat_id,
                    "Title must be 1-100 characters."
                )

            draft.update(
                stage="price",
                title=text
            )

            return say(
                chat_id,
                "Step 3/3: Send a Stars price (1-25000)."
            )

        if draft["stage"] == "price":
            if not text.isdigit():
                return say(
                    chat_id,
                    "Price must be 1-25000."
                )

            price = int(text)

            if not 1 <= price <= 25000:
                return say(
                    chat_id,
                    "Price must be 1-25000."
                )

            item_id = execute(
                """
                INSERT INTO items(
                    title,
                    kind,
                    file_id,
                    price,
                    created_at
                )
                VALUES(?,?,?,?,?)
                """,
                (
                    draft["title"],
                    draft["kind"],
                    draft["file_id"],
                    price,
                    str(int(time.time()))
                )
            )

            DRAFTS.pop(uid, None)

            return say(
                chat_id,
                f"✅ Item #{item_id} added.\n"
                f"Price: ⭐{price}\n"
                "Open /buyers to send."
            )

    return show_help(
        chat_id,
        True
    )


# =====================================
# CALLBACK BUTTON HANDLER
# =====================================

def callback_handler(cb):
    uid = (cb.get("from") or {}).get("id")

    chat = (
        (cb.get("message") or {})
        .get("chat", {})
        .get("id")
    )

    try:
        call(
            "answerCallbackQuery",
            callback_query_id=cb["id"]
        )
    except Exception:
        LOG.warning(
            "Callback acknowledgement failed"
        )

    if uid != chat or not isinstance(uid, int):
        return

    action = (
        cb.get("data") or ""
    ).split(":")

    if action[0] == "browse":
        return show_browse(chat)

    # PUBLIC PAID MEDIA

    if (
        action[0] == "buy"
        and len(action) == 2
        and action[1].isdigit()
    ):
        media_item = item(
            int(action[1])
        )

        if not media_item:
            return say(
                chat,
                "Item not found."
            )

        old = one(
            "SELECT 1 FROM sales "
            "WHERE user_id=? AND item_id=?",
            (uid, media_item["id"])
        )

        if old:
            return say(
                chat,
                "Already bought. Use /library"
            )

        return paid_send(
            chat,
            media_item,
            media_item["price"]
        )

    # BUYER PURCHASED LIBRARY

    if (
        action[0] == "again"
        and len(action) == 2
        and action[1].isdigit()
    ):
        media_item = item(
            int(action[1])
        )

        old = one(
            "SELECT 1 FROM sales "
            "WHERE user_id=? AND item_id=?",
            (uid, int(action[1]))
        )

        if not media_item or not old:
            return say(
                chat,
                "Purchase not found."
            )

        method = (
            "sendPhoto"
            if media_item["kind"] == "photo"
            else "sendVideo"
        )

        return call(
            method,
            **{
                "chat_id": uid,
                media_item["kind"]: media_item["file_id"],
                "protect_content": True
            }
        )

    if uid != ADMIN:
        return

    # ADMIN CALLBACKS

    if action[0] == "cancel":
        PRICE_DRAFTS.pop(uid, None)
        return say(chat, "Cancelled.")

    if (
        action[0] == "buyer"
        and len(action) == 2
        and action[1].isdigit()
    ):
        return select_buyer(
            chat,
            int(action[1])
        )

    if (
        action[0] == "choose"
        and len(action) == 3
        and all(x.isdigit() for x in action[1:])
    ):
        return ask_price(
            chat,
            int(action[1]),
            int(action[2])
        )

    if (
        action[0] == "price"
        and len(action) == 4
        and all(x.isdigit() for x in action[1:])
    ):
        return confirm(
            chat,
            int(action[1]),
            int(action[2]),
            int(action[3])
        )

    if (
        action[0] == "send"
        and len(action) == 4
        and all(x.isdigit() for x in action[1:])
    ):
        buyer_id, item_id, price = map(
            int,
            action[1:]
        )

        buyer = eligible_buyer(buyer_id)
        media_item = item(item_id)

        if not buyer or not media_item:
            return say(
                chat,
                "Buyer/item no longer eligible."
            )

        if not 1 <= price <= 25000:
            return say(
                chat,
                "Invalid price."
            )

        recent = one(
            """
            SELECT 1
            FROM offers
            WHERE
                user_id=?
                AND item_id=?
                AND sent=1
                AND created_epoch>?
            LIMIT 1
            """,
            (
                buyer_id,
                item_id,
                int(time.time()) - 120
            )
        )

        if recent:
            return say(
                chat,
                "Already sent recently. Try later."
            )

        paid_send(
            buyer_id,
            media_item,
            price,
            buyer["connection_id"]
        )

        return say(
            chat,
            f"✅ Sent to {buyer['display_name']} "
            f"for ⭐{price}"
        )


# =====================================
# PAYMENT TRACKING
# =====================================

def record_purchase(data):
    uid = (
        data.get("from") or {}
    ).get("id")

    payload = data.get(
        "paid_media_payload"
    )

    if not isinstance(uid, int) or not payload:
        return

    offer = one(
        "SELECT * FROM offers "
        "WHERE payload=? AND user_id=?",
        (payload, uid)
    )

    if not offer:
        LOG.warning(
            "Unrecognized purchase payload"
        )
        return

    execute(
        """
        INSERT OR IGNORE INTO sales(
            payload,
            user_id,
            item_id,
            price,
            purchased_at
        )
        VALUES(?,?,?,?,?)
        """,
        (
            payload,
            uid,
            offer["item_id"],
            offer["price"],
            str(int(time.time()))
        )
    )


# =====================================
# UPDATE ROUTER
# =====================================

def process_update(update):
    if "business_connection" in update:
        update_business_connection(
            update["business_connection"]
        )

    elif "business_message" in update:
        receive_business(
            update["business_message"]
        )

    elif "purchased_paid_media" in update:
        record_purchase(
            update["purchased_paid_media"]
        )

    elif "callback_query" in update:
        callback_handler(
            update["callback_query"]
        )

    elif "message" in update:
        message_handler(
            update["message"]
        )


# =====================================
# MAIN BOT LOOP
# =====================================

def main():
    initialize()

    me = call("getMe")

    LOG.info(
        "Running as @%s",
        me.get("username", "bot")
    )

    call(
        "deleteWebhook",
        drop_pending_updates=False
    )

    offset_row = one(
        "SELECT value FROM meta "
        "WHERE key='offset'"
    )

    offset = (
        int(offset_row["value"])
        if offset_row
        else 0
    )

    allowed = [
        "message",
        "callback_query",
        "business_connection",
        "business_message",
        "purchased_paid_media"
    ]

    while True:
        try:
            updates = call(
                "getUpdates",
                offset=offset,
                timeout=30,
                allowed_updates=allowed
            )

            for update in updates:
                try:
                    process_update(update)

                except Exception:
                    LOG.exception(
                        "Update processing failed"
                    )

                offset = int(
                    update["update_id"]
                ) + 1

                execute(
                    """
                    INSERT INTO meta(key,value)
                    VALUES('offset',?)
                    ON CONFLICT(key)
                    DO UPDATE SET
                        value=excluded.value
                    """,
                    (str(offset),)
                )

        except Exception:
            LOG.exception(
                "Polling error"
            )
            time.sleep(5)


if __name__ == "__main__":
    main()
