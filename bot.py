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
