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
from telegram import ReplyKeyboardMarkup
from datetime import date

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
ASK_BALANCE, ADD_SYMBOL, ADD_ENTRY, ADD_SL, ADD_QTY, ADD_TARGET, DELETE_TRACK, SET_BALANCE = range(8)



# === DATA ===
balance = {"value": 100000}
sent_messages = []
temp_stock = {}

# === TELEGRAM ===
def send_message(context, text):
    try:
        context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
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
def start(update: Update, context: CallbackContext):
    keyboard = [
        ["1️⃣ Balance", "2️⃣ Add / Modify Stock"],
        ["3️⃣ View Portfolio", "4️⃣ Delete Tracking Stock"],
        ["5️⃣ P&L"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    text = (
        "📈 *Welcome to Paper Trade Bot*\n\n"
        "Choose an option:\n\n"
        "1️⃣ Balance\n_View current balance_\n\n"
        "2️⃣ Add / Modify Stock\n_Add new stock or modify SL/Target_\n\n"
        "3️⃣ View Portfolio\n_Show tracked stocks & invested amount_\n\n"
        "4️⃣ Delete Tracking Stock\n_Remove stock (only if not holding)_\n\n"
        "5️⃣ P&L\n_Show today's and overall P&L_\n\n"
        "Type `/help` to view all commands."
    )
    update.message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)

def help_cmd(update: Update, context: CallbackContext):
    text = (
        "📖 *Help - Available Commands:*\n\n"
        "/start - Show main menu and actions\n"
        "/help - Show this help message\n"
        "/setbalance - Set your balance (manual input)\n"
        "/reset - Reset all tracked stocks\n"
        "/cancel - Cancel current operation\n"
    )
    update.message.reply_text(text, parse_mode="Markdown")


def view_balance(update: Update, context: CallbackContext):
    bal_text = f"💰 Current Balance: ₹{balance['value']:.2f}"
    update.message.reply_text(bal_text)

def set_balance(update: Update, context: CallbackContext):
    update.message.reply_text("💸 Enter your new balance:")
    return SET_BALANCE

def receive_balance(update: Update, context: CallbackContext):
    try:
        amount = float(update.message.text.strip())
        balance['value'] = amount
        balance_collection.delete_many({})
        balance_collection.insert_one({"value": amount})
        update.message.reply_text(f"✅ Balance set to ₹{amount:.2f}")
    except:
        update.message.reply_text("❌ Invalid input.")
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
    temp_stock["entry"] = float(update.message.text)
    update.message.reply_text("🛑 Stop Loss:")
    return ADD_SL

def add_stock_sl(update: Update, context: CallbackContext):
    temp_stock["sl"] = float(update.message.text)
    update.message.reply_text("🎯 Target (or type 'skip'):")
    return ADD_TARGET

def add_stock_target(update: Update, context: CallbackContext):
    text = update.message.text.strip().lower()
    temp_stock["target"] = None if text == "skip" else float(text)
    update.message.reply_text("📦 Quantity:")
    return ADD_QTY

def add_stock_qty(update: Update, context: CallbackContext):
    qty = int(update.message.text)
    symbol = temp_stock["symbol"]
    existing = stocks_collection.find_one({"symbol": symbol})

    if existing:
        stocks_collection.update_one({"symbol": symbol}, {"$set": {
            "sl": temp_stock["sl"],
            "target": temp_stock["target"]
        }})
        update.message.reply_text(f"🔧 Modified SL/Target for {symbol}")
    else:
        temp_stock["qty"] = qty
        temp_stock["position"] = 0
        stocks_collection.insert_one(temp_stock.copy())
        update.message.reply_text(f"✅ Added {symbol} for tracking")

    return ConversationHandler.END

# DELETE STOCKS
def portfolio(update: Update, context: CallbackContext):
    stocks = list(stocks_collection.find())
    if not stocks:
        update.message.reply_text("📉 Portfolio is empty.")
        return

    lines = ["📊 Portfolio:"]
    for s in stocks:
        status = "🟢 Holding" if s.get("position", 0) > 0 else "🕒 Tracking"
        invest = s.get("invested", 0)
        lines.append(f"{status} {s['symbol']} | SL: {s['sl']} | Target: {s.get('target', 'Not Set')} | Qty: {s['qty']} | Invested: ₹{invest:.2f}")

    update.message.reply_text("\n".join(lines))

def delete_stock(update: Update, context: CallbackContext):
    update.message.reply_text("🗑️ Enter stock symbol to delete:")
    return DELETE_TRACK

def confirm_delete(update: Update, context: CallbackContext):
    symbol = update.message.text.upper()
    stock = stocks_collection.find_one({"symbol": symbol})
    if not stock:
        update.message.reply_text("❌ Stock not found.")
    elif stock.get("position", 0) > 0:
        update.message.reply_text("❌ Cannot delete while holding position.")
    else:
        stocks_collection.delete_one({"symbol": symbol})
        update.message.reply_text(f"✅ {symbol} removed from tracking.")
    return ConversationHandler.END

def pnl(update: Update, context: CallbackContext):
    today = date.today().strftime("%Y-%m-%d")
    daily_pnl = 0
    total_pnl = 0
    for log in logs_collection.find({"action": "SELL"}):
        pnl_val = log.get("pnl", 0)
        total_pnl += pnl_val
        if log["timestamp"].startswith(today):
            daily_pnl += pnl_val

    text = (
        f"📅 *Today's P&L ({today}):* ₹{daily_pnl:.2f}\n"
        f"📊 *Overall P&L:* ₹{total_pnl:.2f}"
    )
    update.message.reply_text(text, parse_mode="Markdown")

def reset(update: Update, context: CallbackContext):
    stocks_collection.delete_many({})
    logs_collection.delete_many({})
    balance['value'] = 100000
    balance_collection.delete_many({})
    balance_collection.insert_one({"value": 100000})
    update.message.reply_text("🔄 All data reset. Balance set to ₹100000.")


# track stocks
def track(bot):
    while True:
        for stock in stocks_collection.find():
            try:
                data = yf.download(stock["symbol"], period="1d", interval="1m", progress=False)
                if data.empty: continue
                price = data["Close"].dropna().iloc[-1].item()

                if stock.get("position", 0) == 0 and price >= stock["entry"]:
                    cost = price * stock["qty"]
                    if balance["value"] < cost: continue
                    balance["value"] -= cost
                    stocks_collection.update_one({"_id": stock["_id"]}, {"$set": {
                        "position": stock["qty"],
                        "entry_price": price,
                        "invested": cost
                    }})
                    balance_collection.update_one({}, {"$set": {"value": balance["value"]}}, upsert=True)
                    send_message(bot, f"🟢 BUY {stock['symbol']} @ ₹{price:.2f}")
                    trade_log(stock["symbol"], "BUY", price, stock["qty"], "", "AUTO BUY", balance["value"])

                elif stock.get("position", 0) > 0:
                    low = data["Low"].dropna().iloc[-1]
                    high = data["High"].dropna().iloc[-1]
                    reason = None

                    if low <= stock["sl"]:
                        reason = "STOP LOSS"
                    elif stock.get("target") and high >= stock["target"]:
                        reason = "TARGET"

                    if reason:
                        pnl = (price - stock["entry_price"]) * stock["qty"]
                        balance["value"] += price * stock["qty"]
                        stocks_collection.update_one({"_id": stock["_id"]}, {"$set": {"position": 0}})
                        balance_collection.update_one({}, {"$set": {"value": balance["value"]}})
                        send_message(bot, f"🔴 SELL {stock['symbol']} ({reason}) @ ₹{price:.2f} | P&L: ₹{pnl:.2f}")
                        trade_log(stock["symbol"], "SELL", price, stock["qty"], pnl, reason, balance["value"])
            except Exception as e:
                print(f"[Error in track] {e}")
                continue

        time.sleep(60)
# main
def main():
    updater = Updater(token=TELEGRAM_BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("help", help_cmd))
    dp.add_handler(CommandHandler("pnl", pnl))
    dp.add_handler(CommandHandler("reset", reset))

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(Filters.regex("^2️⃣ Add / Modify Stock$"), add_stock_start)],
        states={
            ADD_SYMBOL: [MessageHandler(Filters.text & ~Filters.command, add_stock_symbol)],
            ADD_ENTRY: [MessageHandler(Filters.text & ~Filters.command, add_stock_entry)],
            ADD_SL: [MessageHandler(Filters.text & ~Filters.command, add_stock_sl)],
            ADD_TARGET: [MessageHandler(Filters.text & ~Filters.command, add_stock_target)],
            ADD_QTY: [MessageHandler(Filters.text & ~Filters.command, add_stock_qty)],
            DELETE_TRACK: [MessageHandler(Filters.text & ~Filters.command, confirm_delete)],
            SET_BALANCE: [MessageHandler(Filters.text & ~Filters.command, receive_balance)]

        },
        fallbacks=[CommandHandler("cancel", lambda u, c: u.message.reply_text("Cancelled."))]
    )
    dp.add_handler(conv_handler)

    dp.add_handler(MessageHandler(Filters.regex("^1️⃣ Balance$"), view_balance))
    dp.add_handler(MessageHandler(Filters.regex("^3️⃣ View Portfolio$"), portfolio))
    dp.add_handler(MessageHandler(Filters.regex("^4️⃣ Delete Tracking Stock$"), delete_stock))
    dp.add_handler(MessageHandler(Filters.regex("^5️⃣ P&L$"), pnl))

    threading.Thread(target=track, args=(updater.bot,), daemon=True).start()

    last_balance = balance_collection.find_one()
    if last_balance:
        balance["value"] = float(last_balance["value"])

    @app.route('/', methods=['GET'])
    def health():
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
