# Telegram Stars Paid Photo & Video Bot

Official Telegram Bot API, Python, SQLite, GitHub + Railway. This is an **MVP starter**, not a finished commercial platform. No separate website or public URL needed. Admin uploads items; users buy by unlocking the Telegram-native paid media message. Paid media purchases are confirmed by Telegram's `purchased_paid_media` update, not by a button click.

## 1. Create bot & retrieve admin ID

- Create the Telegram bot using [@BotFather](https://t.me/BotFather) (`/newbot`). Keep the token private.
- Open your new bot and send `/id` to discover your own Telegram numeric user ID. The command works before the bot is deployed only if the bot code is running, so initially you can use a trusted Telegram user-ID helper bot or start with a previously known personal numeric Telegram ID. Never share your bot token with helper bots.

## 2. GitHub and Railway

1. Upload the **contents of this ZIP** (bot.py, requirements.txt, railway.json, Procfile, README) to a new **private** GitHub repository. Do not include any `.env` file or secret values.
2. In Railway, **New Project → Deploy from GitHub Repo** → choose the repository.
3. In the new service's **Variables** tab, set:
   - `BOT_TOKEN` — the token from BotFather (secret).
   - `ADMIN_ID` — your own numeric Telegram ID (e.g. `123456789`); this must not be the bot ID.
   - `DATABASE_PATH` — `/data/starsbot.sqlite`.
4. Attach a Railway **Volume** to that service, mount path **`/data`**. This is required for the SQLite catalog, sales, and offset to survive redeploys. Set up volume before first real sales.
5. Confirm the service **Start Command** is `python bot.py`; only **one instance/replica** of this polling worker should run. No website domain or PORT variable required.
6. Open **Deploy Logs**. You should see `Starting Telegram Stars media bot...`. If authentication fails, check your Railway Variables; do not paste tokens into screenshots.

## 3. Use

For **ADMIN** in private chat with the bot:
- `/start` — welcome and commands
- `/add` — begin upload wizard: send photo/video, then title, then number of Stars (1–25,000)
- `/items` — active items IDs
- `/delete 3` — hide item #3 from new buyers
- `/stats` — locally recorded gross Stars, buyers and sales (not official withdrawable amount)
- `/balance` — official `getMyStarBalance` from Telegram
- `/cancel` — cancel an unfinished upload

For **USER**:
- `/browse` — shows item titles and prices; clicking sends real Telegram Paid Media with a blur/unlock prompt
- Pay Stars using Telegram's built-in Unlock button
- `/library` — media items previously purchased with the bot, including repeat access

**Important:** A test purchase should be made from a separate Telegram account with Stars. The admin can also use the browse interface but shouldn't assume purchases can be simulated free.

## 4. Important limitations / safety

- Only **admin** can submit content. No user submission, user revenue split, or seller wallets in this MVP.
- Telegram Stars belong to the **bot's earned balance**. They are **not** automatically moved to the admin's personal Stars wallet. Withdrawal depends on Telegram/Fragment eligibility, waiting time, minimums and applicable law.
- Never promise no-copy protection; `protect_content` reduces forwarding/saving but cannot prevent screenshots or external recording.
- Telegram's `purchased_paid_media` updates are used to record a completed sale. This local database is not a full accounting ledger and can miss events if the bot is down longer than Telegram retains updates. For reconciliation, consult official Star transactions and Telegram balances.
- If a customer bought the item and then the item is disabled, existing buyers still can access it through `/library`.
- Keep copies of important media yourself, keep the Railway volume backed up, avoid prohibited content and obtain consent / rights for all media.
- If the bot is configured with a webhook elsewhere, this polling worker uses `deleteWebhook` without dropping pending updates. Do not run another worker with the same token.
- A media upload must be sent as **photo** or **video**, not as a document.

## 5. Troubleshooting

- `401 Unauthorized`: your `BOT_TOKEN` is invalid or revoked.
- `409 Conflict`: another process is polling with the same token. Run only one instance, stop any other bot process.
- `Bad Request` on `sendPaidMedia`: check media type, invalid/expired file ID, Stars price, and full message in Railway Deploy Logs.
- No purchase update: verify `purchased_paid_media` is in allowed updates, the payload was sent, and your worker stayed online. A valid real Stars unlock is needed.
- No stats after redeployment: check the volume is mounted at `/data` and `DATABASE_PATH=/data/starsbot.sqlite`.

API reference: https://core.telegram.org/bots/api#sendpaidmedia
Telegram purchase update: https://core.telegram.org/bots/api#paidmediapurchased
Railway Volumes: https://docs.railway.com/volumes
