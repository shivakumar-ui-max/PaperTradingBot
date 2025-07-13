from dotenv import load_dotenv
load_dotenv()

import os
import yfinance as yf
import time
import threading
from datetime import datetime
from pymongo import MongoClient
from telegram.ext import Updater, CommandHandler, CallbackContext, ConversationHandler, MessageHandler, Filters
from telegram import Update
from apscheduler.schedulers.background import BackgroundScheduler

from flask import Flask
app = Flask(__name__)

PORT = int(os.environ["PORT"])
APP_URL = "https://papertradingbot.onrender.com"

# === ENV CONFIG ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
MONGO_URI = os.getenv("MONGO_URI") or "mongodb://localhost:27017"

# === MONGO SETUP ===
client = MongoClient(MONGO_URI, tls=True, tlsAllowInvalidCertificates=True)
db = client["PaperTrade"]
logs_collection = db["TradeLogs"]
stocks_collection = db["TrackedStocks"]
balance_collection = db["Balance"]

# === STATES ===
ASK_BALANCE, ADD_SYMBOL, ADD_ENTRY, ADD_SL, ADD_QTY, ADD_TARGET, PRICE_SYMBOL = range(7)

# === DATA ===
balance = {"value": 100000}
sent_messages = []
temp_stock = {}

# === TELEGRAM ===
def send_message(context, text):
    try:
        message = context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
        sent_messages.append(message.message_id)
    except:
        pass

def trade_log(symbol, action, price, qty, pnl, reason, bal):
    logs_collection.insert_one({
        "symbol": symbol,
        "action": action,
        "price": price,
        "quantity": qty,
        "pnl": pnl,
        "reason": reason,
        "balance_after": round(bal, 2),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })

# === COMMANDS ===

def view_balance(update: Update, context: CallbackContext):
    bal_text = f"💰 Current Balance: ₹{balance['value']:.2f}"
    stock_count = stocks_collection.count_documents({})
    msg = f"{bal_text}\n📊 Tracking {stock_count} stocks." if stock_count else f"{bal_text}\n📉 No stocks being tracked."
    update.message.reply_text(msg)

def ask_balance(update: Update, context: CallbackContext):
    update.message.reply_text("💸 Enter your balance:")
    return ASK_BALANCE

def receive_balance(update: Update, context: CallbackContext):
    try:
        amount = float(update.message.text.replace(",", "").strip())
        if amount <= 0: raise ValueError()
        balance['value'] = amount
        balance_collection.delete_many({})
        balance_collection.insert_one({"value": amount})
        msg = f"✅ Balance set to ₹{amount:,.2f}"
    except:
        msg = "❌ Invalid input. Enter a valid number."
    update.message.reply_text(msg)
    return ConversationHandler.END

def cancel(update: Update, context: CallbackContext):
    update.message.reply_text("❌ Operation cancelled.")
    return ConversationHandler.END

def price_start(update: Update, context: CallbackContext):
    update.message.reply_text("📈 Enter stock symbol (e.g., TCS.NS):")
    return PRICE_SYMBOL

def show_price(update: Update, context: CallbackContext):
    symbol = update.message.text.upper()
    try:
        data = yf.download(symbol, period="1d", interval="1m", progress=False)
        if data.empty:
            update.message.reply_text(f"❌ No data for {symbol}.")
        else:
            price = data["Close"].dropna().iloc[-1].item()
            update.message.reply_text(f"💹 {symbol} Price: ₹{price:.2f}")
    except Exception as e:
        update.message.reply_text(f"❌ Error: {e}")
    return ConversationHandler.END

def add_stock_start(update: Update, context: CallbackContext):
    temp_stock.clear()
    update.message.reply_text("📌 Enter stock symbol (e.g., TCS.NS):")
    return ADD_SYMBOL

def add_stock_symbol(update: Update, context: CallbackContext):
    temp_stock["symbol"] = update.message.text.upper()
    update.message.reply_text("✏️ Entry Price:")
    return ADD_ENTRY

def add_stock_entry(update: Update, context: CallbackContext):
    try:
        temp_stock["entry"] = float(update.message.text)
        update.message.reply_text("🛑 Stop Loss:")
        return ADD_SL
    except:
        update.message.reply_text("❌ Invalid entry price.")
        return ADD_ENTRY

def add_stock_sl(update: Update, context: CallbackContext):
    try:
        temp_stock["sl"] = float(update.message.text)
        update.message.reply_text("🎯 Target Price (or type 'skip'):")
        return ADD_TARGET
    except:
        update.message.reply_text("❌ Invalid stop loss.")
        return ADD_SL

def add_stock_target(update: Update, context: CallbackContext):
    text = update.message.text.strip().lower()
    temp_stock["target"] = None if text == "skip" else float(text)
    update.message.reply_text("📦 Quantity:")
    return ADD_QTY

def add_stock_qty(update: Update, context: CallbackContext):
    try:
        temp_stock["qty"] = int(update.message.text)
        temp_stock["entry_price"] = None
        temp_stock["position"] = 0
        stocks_collection.insert_one(temp_stock.copy())
        msg = (
            f"✅ Added {temp_stock['symbol']} | Entry: ₹{temp_stock['entry']} | SL: ₹{temp_stock['sl']} | Qty: {temp_stock['qty']}"
        )
        update.message.reply_text(msg)
        return ConversationHandler.END
    except:
        update.message.reply_text("❌ Invalid quantity.")
        return ADD_QTY

def reset_stocks(update: Update, context: CallbackContext):
    stocks_collection.delete_many({})
    update.message.reply_text("♻️ All tracked stocks reset.")

def portfolio(update: Update, context: CallbackContext):
    all_stocks = list(stocks_collection.find())
    if not all_stocks:
        update.message.reply_text("📉 Portfolio is empty.")
        return

    lines = ["📊 Portfolio:"]
    for stock in all_stocks:
        status = "🟢 Holding" if stock.get("position", 0) > 0 else "🕒 Tracking"
        lines.append(f"{status} {stock['symbol']} | Entry: ₹{stock['entry']} | SL: ₹{stock['sl']} | Qty: {stock['qty']}")
    update.message.reply_text("\n".join(lines))

def pnl_summary(update: Update, context: CallbackContext):
    total_pnl = 0
    for log in logs_collection.find({"action": "SELL"}):
        total_pnl += log.get("pnl", 0)
    emoji = "✅" if total_pnl >= 0 else "❌"
    update.message.reply_text(f"{emoji} Total P&L: ₹{total_pnl:.2f}")

def daily_summary(update: Update, context: CallbackContext):
    all_stocks = list(stocks_collection.find())
    if not all_stocks:
        update.message.reply_text("📭 No holdings to calculate daily P&L.")
        return

    lines, total_pnl = ["📅 **Daily P&L Summary:**\n"], 0

    for stock in all_stocks:
        if stock.get("position", 0) == 0:
            continue
        symbol = stock["symbol"]
        entry = stock.get("entry_price") or stock["entry"]
        qty = stock["qty"]

        try:
            data = yf.download(symbol, period="1d", interval="1m", progress=False)
            if data.empty: continue

            price = data["Close"].dropna().iloc[-1].item()
            pnl = (price - entry) * qty
            pct = ((price - entry) / entry) * 100
            total_pnl += pnl

            emoji = "✅" if pnl >= 0 else "❌"
            lines.append(f"{emoji} {symbol}: ₹{entry} → ₹{price:.2f} | {pct:.2f}% | Qty: {qty} | P&L: ₹{pnl:.2f}")
        except:
            lines.append(f"❌ {symbol}: Error fetching price")

    lines.append(f"\n📊 **Total P&L: ₹{total_pnl:.2f}**")
    update.message.reply_text("\n".join(lines), parse_mode="Markdown")

def delete_old_messages(context):
    for msg_id in sent_messages:
        try:
            context.bot.delete_message(chat_id=TELEGRAM_CHAT_ID, message_id=msg_id)
        except:
            pass
    sent_messages.clear()

def track_stocks(bot):
    while True:
        for stock in stocks_collection.find():
            try:
                data = yf.download(stock["symbol"], period="1d", interval="1m", progress=False)
                if data.empty: continue
                price = data["Close"].dropna().iloc[-1].item()

                if stock.get("position", 0) == 0 and price >= stock["entry"]:
                    cost = stock["qty"] * price
                    if balance["value"] < cost: continue

                    stocks_collection.update_one({"_id": stock["_id"]}, {"$set": {"entry_price": price, "position": stock["qty"]}})
                    balance["value"] -= cost
                    balance_collection.update_one({}, {"$set": {"value": balance["value"]}}, upsert=True)
                    send_message(bot, f"🟢 BUY {stock['symbol']} Qty: {stock['qty']} @ ₹{price:.2f}")
                    trade_log(stock["symbol"], "BUY", price, stock["qty"], "", "ENTRY", balance["value"])

                elif stock.get("position", 0) > 0:
                    reason = None
                    if price <= stock["sl"]:
                        reason = "STOP LOSS"
                    elif stock.get("target") and price >= stock["target"]:
                        reason = "TARGET"

                    if reason:
                        pnl = (price - stock["entry_price"]) * stock["qty"]
                        balance["value"] += price * stock["qty"]
                        balance_collection.update_one({}, {"$set": {"value": balance["value"]}}, upsert=True)
                        stocks_collection.update_one({"_id": stock["_id"]}, {"$set": {"position": 0}})
                        send_message(bot, f"🔴 SELL {stock['symbol']} ({reason}) @ ₹{price:.2f} | P&L: ₹{pnl:.2f}")
                        trade_log(stock["symbol"], "SELL", price, stock["qty"], pnl, reason, balance["value"])
            except:
                continue
        time.sleep(60)

def main():
    updater = Updater(token=TELEGRAM_BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("balance", view_balance))
    dp.add_handler(CommandHandler("reset", reset_stocks))
    dp.add_handler(CommandHandler("portfolio", portfolio))
    dp.add_handler(CommandHandler("pnl", pnl_summary))
    dp.add_handler(CommandHandler("daily", daily_summary))

    dp.add_handler(ConversationHandler(
        entry_points=[CommandHandler("price", price_start)],
        states={PRICE_SYMBOL: [MessageHandler(Filters.text & ~Filters.command, show_price)]},
        fallbacks=[CommandHandler("cancel", cancel)]
    ))

    dp.add_handler(ConversationHandler(
        entry_points=[CommandHandler("addstock", add_stock_start)],
        states={
            ADD_SYMBOL: [MessageHandler(Filters.text & ~Filters.command, add_stock_symbol)],
            ADD_ENTRY: [MessageHandler(Filters.text & ~Filters.command, add_stock_entry)],
            ADD_SL: [MessageHandler(Filters.text & ~Filters.command, add_stock_sl)],
            ADD_TARGET: [MessageHandler(Filters.text & ~Filters.command, add_stock_target)],
            ADD_QTY: [MessageHandler(Filters.text & ~Filters.command, add_stock_qty)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    ))

    dp.add_handler(ConversationHandler(
        entry_points=[CommandHandler("setbalance", ask_balance)],
        states={ASK_BALANCE: [MessageHandler(Filters.text & ~Filters.command, receive_balance)]},
        fallbacks=[CommandHandler("cancel", cancel)]
    ))

    scheduler = BackgroundScheduler(timezone="Asia/Kolkata")
    scheduler.add_job(delete_old_messages, "cron", hour=23, minute=59, args=[updater.bot])
    scheduler.start()

    threading.Thread(target=track_stocks, args=(updater.bot,), daemon=True).start()

    last_balance = balance_collection.find_one()
    if last_balance:
        balance["value"] = float(last_balance["value"])

    @app.route('/', methods=['GET'])
    def health_check():
     return "Bot is running", 200

    updater.start_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TELEGRAM_BOT_TOKEN,
        webhook_url=f"{APP_URL}/{TELEGRAM_BOT_TOKEN}"
    )
    updater.idle()


if __name__ == '__main__':
    main()
