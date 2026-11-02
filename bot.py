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
