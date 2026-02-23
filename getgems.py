import os
import logging
import aiohttp
from collections import defaultdict
from typing import Any, Optional, Dict, List

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ========= LOAD ENV =========

TG_TOKEN = (os.getenv("TG_TOKEN") or "").strip()
GETGEMS_API_KEY = (os.getenv("GETGEMS_API_KEY") or "").strip()

# ТВОЙ Render URL, например: https://getgems-bot.onrender.com
PUBLIC_URL = (os.getenv("PUBLIC_URL") or "").strip()

# Render сам даёт PORT
PORT = int(os.getenv("PORT", "10000"))

if not TG_TOKEN or not GETGEMS_API_KEY:
    raise RuntimeError("Set TG_TOKEN, GETGEMS_API_KEY")

BASE = "https://api.getgems.io/public-api"
CHECK_INTERVAL = 2  # ⚡ 2 секунды

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("fast-bot")

previous_addresses: set[str] = set()
SESSION: aiohttp.ClientSession | None = None


# ========= HELPERS =========
def headers():
    return {"Authorization": GETGEMS_API_KEY, "accept": "application/json"}


def ton_from_any(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        x = float(str(v))
        if x > 1_000_000:
            return x / 1e9
        return x
    except Exception:
        return None


def extract_model(name: str) -> str:
    if " #" in name:
        return name.split(" #", 1)[0].strip()
    return name.strip()


def nft_link(addr: str) -> str:
    return f"https://getgems.io/nft/{addr}"


# ========= API =========
async def fetch_gifts() -> List[dict]:
    global SESSION
    if SESSION is None or SESSION.closed:
        SESSION = aiohttp.ClientSession()

    async with SESSION.get(
        f"{BASE}/v1/nfts/offchain/on-sale/gifts",
        headers=headers(),
        timeout=10,
    ) as r:
        if r.status != 200:
            txt = await r.text()
            raise RuntimeError(f"GetGems {r.status}: {txt[:200]}")
        data = await r.json()
        return data.get("response", {}).get("items", []) or []


# ========= MARKET =========
def build_market_avg(items: List[dict]) -> Dict[str, float]:
    market = defaultdict(list)
    for it in items:
        name = it.get("name") or ""
        sale = it.get("sale") or {}
        raw = sale.get("fixPrice") or sale.get("price") or sale.get("fullPrice")
        price = ton_from_any(raw)
        if not name or price is None:
            continue
        market[extract_model(name)].append(price)

    # средняя по модели: даже если 1 item, тоже считаем
    return {m: sum(p) / len(p) for m, p in market.items()}


def format_listing(it: dict, avg_map: Dict[str, float]) -> Optional[str]:
    addr = it.get("address") or it.get("nftAddress")
    name = it.get("name") or "(no name)"
    sale = it.get("sale") or {}

    raw = sale.get("fixPrice") or sale.get("price") or sale.get("fullPrice")
    price = ton_from_any(raw)

    if not addr:
        return None

    model = extract_model(name)
    avg = avg_map.get(model)

    price_str = f"{price:.2f} TON" if price is not None else "—"
    avg_str = f"{avg:.2f} TON" if avg is not None else "—"

    diff = ""
    if price is not None and avg is not None and avg != 0:
        pct = (price / avg - 1) * 100
        if pct < 0:
            diff = f"\n🔥 Дешевле рынка на {abs(pct):.1f}%"
        else:
            diff = f"\n⚠️ Дороже рынка на {pct:.1f}%"

    return (
        "⚡ НОВЫЙ GIFTS ЛИСТИНГ\n"
        f"{name}\n"
        f"Цена: {price_str}\n"
        f"Средняя по модели: {avg_str}"
        f"{diff}\n"
        f"🔗 {nft_link(str(addr))}"
    )


# ========= TELEGRAM =========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start:
    - запоминаем chat_id (куда слать)
    - ставим baseline (чтобы не спамить старым)
    """
    global previous_addresses

    chat_id = update.effective_chat.id
    context.application.bot_data["chat_id"] = chat_id

    await update.message.reply_text("🚀 Монитор включён. Буду присылать только НОВОЕ.")

    items = await fetch_gifts()
    previous_addresses = {
        str(it.get("address") or it.get("nftAddress"))
        for it in items
        if (it.get("address") or it.get("nftAddress"))
    }

    log.info("Baseline set with %d items", len(previous_addresses))


# ========= MONITOR =========
async def monitor(context: ContextTypes.DEFAULT_TYPE):
    global previous_addresses

    chat_id = context.application.bot_data.get("chat_id")
    if not chat_id:
        return  # пока не написали /start — некуда слать

    try:
        items = await fetch_gifts()
    except Exception as e:
        log.warning("fetch error: %s", e)
        return

    avg_map = build_market_avg(items)

    current_addresses = {
        str(it.get("address") or it.get("nftAddress"))
        for it in items
        if (it.get("address") or it.get("nftAddress"))
    }

    new_addresses = current_addresses - previous_addresses
    log.info("items=%d new=%d", len(items), len(new_addresses))

    if not new_addresses:
        previous_addresses = current_addresses
        return

    for it in items:
        addr = it.get("address") or it.get("nftAddress")
        if not addr:
            continue
        if str(addr) not in new_addresses:
            continue

        msg = format_listing(it, avg_map)
        if msg:
            await context.application.bot.send_message(chat_id, msg)

    previous_addresses = current_addresses


# ========= MAIN (WEBHOOK) =========
def main():
    app = Application.builder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))

    # Монитор каждые 2 секунды
    app.job_queue.run_repeating(monitor, interval=CHECK_INTERVAL, first=3)

    # Webhook endpoint
    webhook_path = "/webhook"
    webhook_url = PUBLIC_URL.rstrip("/") + webhook_path

    log.info("Webhook starting on port=%s url=%s", PORT, webhook_url)

    # Важно: это открывает порт => Render НЕ будет таймаутить
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=webhook_path.lstrip("/"),
        webhook_url=webhook_url,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()