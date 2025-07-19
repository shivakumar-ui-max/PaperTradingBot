from dotenv import load_dotenv
load_dotenv()

import os
import yfinance as yf
import time
import threading
from datetime import datetime, date
from pymongo import MongoClient
from telegram.ext import Updater, CommandHandler, CallbackContext, ConversationHandler, MessageHandler, Filters
from telegram import Update, ReplyKeyboardMarkup
from flask import Flask

app = Flask(__name__)

PORT = int(os.environ["PORT"])
APP_URL = os.getenv("APP_URL")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")

client = MongoClient(MONGO_URI, tls=True, tlsAllowInvalidCertificates=True)
db = client["PaperTrade"]

logs_collection = db["TradeLogs"]
stocks_collection = db["TrackedStocks"]
balance_collection = db["Balance"]

# STATES
ASK_BALANCE, ADD_SYMBOL, ADD_ENTRY, ADD_SL, ADD_TARGET, ADD_QTY, DELETE_TRACK = range(7)

temp_stock = {}

# Utilities
def get_user_balance(user_id):
    bal = balance_collection.find_one({"user_id": user_id})
    return bal["value"] if bal else 100000

def update_user_balance(user_id, value):
    balance_collection.update_one({"user_id": user_id}, {"$set": {"value": value}}, upsert=True)

def check_in_progress(update):
    uid = update.effective_user.id
    if uid in temp_stock and temp_stock[uid].get("in_progress"):
        update.message.reply_text("⚠️ You're in the middle of Add/Modify Stock.\nPlease type /cancel to stop current operation.")
        return True
    return False

# Commands
def start(update: Update, context: CallbackContext):
    keyboard = [
        ["1️⃣ Balance", "2️⃣ Add / Modify Stock"],
        ["3️⃣ Portfolio", "4️⃣ Delete Tracking Stock"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    text = (
        "📈 *Welcome to Paper Trading Bot*\n\n"
        "Select an action:\n\n"
        "1️⃣ *Balance*\n"
        "2️⃣ *Add / Modify Stock*\n"
        "3️⃣ *Portfolio*\n"
        "4️⃣ *Delete Tracking Stock*\n\n"
        "Type /help for command list."
    )
    update.message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)

def help_cmd(update: Update, context: CallbackContext):
    text = (
        "📖 *Help Menu*\n\n"
        "/start - Show main menu\n"
        "/help - Show this message\n"
        "/setbalance - Set initial balance\n"
        "/cancel - Cancel current operation\n\n"
        "*Available Options:*\n"
        "1️⃣ Balance - Show balance\n"
        "2️⃣ Add/Modify Stock - Add new or modify existing stock\n"
        "3️⃣ Portfolio - Show holdings and P&L\n"
        "4️⃣ Delete Tracking Stock - Remove stock from tracking (no holdings)"
    )
    update.message.reply_text(text, parse_mode="Markdown")

def view_balance(update: Update, context: CallbackContext):
    if check_in_progress(update):
        return
    uid = update.effective_user.id
    balance = get_user_balance(uid)
    update.message.reply_text(f"💰 Your Balance: ₹{balance:.2f}")

def set_balance(update: Update, context: CallbackContext):
    if check_in_progress(update):
        return
    update.message.reply_text("💸 Enter new balance:")
    return ASK_BALANCE

def receive_balance(update: Update, context: CallbackContext):
    uid = update.effective_user.id
    try:
        value = float(update.message.text.strip())
        update_user_balance(uid, value)
        update.message.reply_text(f"✅ Balance set to ₹{value:.2f}")
    except:
        update.message.reply_text("❌ Invalid number.")
    return ConversationHandler.END

def add_stock_start(update: Update, context: CallbackContext):
    uid = update.effective_user.id
    temp_stock[uid] = {"in_progress": True}
    update.message.reply_text("📌 Enter Stock Symbol (e.g., TCS.NS):")
    return ADD_SYMBOL

def add_stock_symbol(update: Update, context: CallbackContext):
    uid = update.effective_user.id
    temp_stock[uid]["symbol"] = update.message.text.upper()
    update.message.reply_text("✏️ Entry Price:")
    return ADD_ENTRY

def add_stock_entry(update: Update, context: CallbackContext):
    uid = update.effective_user.id
    temp_stock[uid]["entry"] = float(update.message.text)
    update.message.reply_text("🛑 Stop Loss:")
    return ADD_SL

def add_stock_sl(update: Update, context: CallbackContext):
    uid = update.effective_user.id
    temp_stock[uid]["sl"] = float(update.message.text)
    update.message.reply_text("🎯 Target (or type 'skip'):")
    return ADD_TARGET

def add_stock_target(update: Update, context: CallbackContext):
    uid = update.effective_user.id
    text = update.message.text.lower()
    temp_stock[uid]["target"] = None if text == "skip" else float(text)
    update.message.reply_text("📦 Quantity:")
    return ADD_QTY

def add_stock_qty(update: Update, context: CallbackContext):
    uid = update.effective_user.id
    stock = temp_stock[uid]
    stock["qty"] = int(update.message.text)
    stock["position"] = 0
    stock["user_id"] = uid

    existing = stocks_collection.find_one({"symbol": stock["symbol"], "user_id": uid})
    if existing:
        stocks_collection.update_one(
            {"symbol": stock["symbol"], "user_id": uid},
            {"$set": {"sl": stock["sl"], "target": stock["target"]}}
        )
        update.message.reply_text(f"🔧 Updated SL/Target for {stock['symbol']}")
    else:
        stocks_collection.insert_one(stock)
        update.message.reply_text(f"✅ {stock['symbol']} added to tracking")

    temp_stock.pop(uid)
    return ConversationHandler.END

def cancel(update: Update, context: CallbackContext):
    uid = update.effective_user.id
    temp_stock.pop(uid, None)
    update.message.reply_text("❌ Operation cancelled. You can now use other commands.")
    return ConversationHandler.END

def delete_stock(update: Update, context: CallbackContext):
    if check_in_progress(update):
        return
    update.message.reply_text("🗑️ Enter stock symbol to delete:")
    return DELETE_TRACK

def confirm_delete(update: Update, context: CallbackContext):
    uid = update.effective_user.id
    symbol = update.message.text.upper()
    stock = stocks_collection.find_one({"symbol": symbol, "user_id": uid})
    if not stock:
        update.message.reply_text("❌ Stock not found.")
    elif stock.get("position", 0) > 0:
        update.message.reply_text("❌ Cannot delete while holding position.")
    else:
        stocks_collection.delete_one({"symbol": symbol, "user_id": uid})
        update.message.reply_text(f"✅ {symbol} removed from tracking.")
    return ConversationHandler.END
def portfolio(update: Update, context: CallbackContext):
    stocks = list(stocks_collection.find())
    logs = list(logs_collection.find({"sell_price": {"$ne": None}}))

    today = date.today().strftime("%Y-%m-%d")
    today_display = date.today().strftime("%d-%B-%Y")

    lines = [f"📊 Portfolio:  📅 {today_display}\n"]

    # HOLDINGS
    holding_lines = []
    for s in stocks:
        if s.get("position", 0) > 0:
            symbol = s['symbol']
            qty = s['qty']
            sl = s['sl']
            target = s.get('target', 'None')
            invested = s.get("invested", 0)

            data = yf.download(symbol, period="1d", interval="1m", progress=False)
            if not data.empty and "Close" in data.columns and not data["Close"].dropna().empty:
                current_price = float(data["Close"].dropna().iloc[-1])
            else:
                current_price = s.get("entry_price", s["entry"])

            entry_price = s.get("entry_price", s["entry"])
            percent = ((current_price - entry_price) / entry_price) * 100
            status = "🟢" if float(percent) >= 0 else "❌"

            holding_lines.append(
                f"{status} Holding {symbol} | Entry: ₹{entry_price:.2f} | Now: ₹{current_price:.2f} | {percent:+.2f}% | Qty: {qty} | SL: {sl} | Target: {target} | Invested: ₹{invested:.2f}"
            )

    if holding_lines:
        lines.append("HOLDING\n")
        lines.extend(holding_lines)

    # SOLD STOCKS
    sold_lines = []
    today_pnl = 0
    total_pnl = 0

    for log in logs:
        pnl = log['pnl']
        total_pnl += pnl

        if log["sell_time"].startswith(today):
            today_pnl += pnl

        symbol = log['symbol']
        qty = log['quantity']
        buy = log['buy_price']
        sell = log['sell_price']
        reason = log['reason']
        time_sold = log['sell_time'].split()[1]

        sold_lines.append(
            f"🔴 {symbol} | {reason} | ₹{buy:.2f} → ₹{sell:.2f} | Qty: {qty} | P&L: ₹{pnl:+.2f} | {time_sold}"
        )

    if sold_lines:
        lines.append("\nSOLD:\n")
        lines.extend(sold_lines)

    # TODAY P&L
    lines.append(f"\nTODAY {today_display} P&L: ₹{today_pnl:+.2f}")

    # OVERALL P&L
    lines.append("\n--------------------------------------------------------\n")
    lines.append(f"📈 Overall Realized P&L (History): ₹{total_pnl:+.2f}")

    update.message.reply_text("\n".join(lines))


# Tracking Thread
def track(bot):
    while True:
        for stock in stocks_collection.find():
            try:
                symbol = stock["symbol"]
                user_id = stock["user_id"]
                data = yf.download(symbol, period="1d", interval="1m", progress=False)
                if data.empty: continue

                price = data["Close"].dropna().iloc[-1]
                bal = get_user_balance(user_id)

                if stock["position"] == 0 and price >= stock["entry"]:
                    cost = price * stock["qty"]
                    if bal < cost: continue
                    update_user_balance(user_id, bal - cost)
                    stocks_collection.update_one({"_id": stock["_id"]}, {"$set": {
                        "position": stock["qty"],
                        "entry_price": price,
                        "invested": cost
                    }})
                    logs_collection.insert_one({
                        "user_id": user_id,
                        "symbol": symbol,
                        "quantity": stock["qty"],
                        "buy_price": price,
                        "buy_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "sell_price": None
                    })
                    bot.send_message(chat_id=user_id, text=f"🟢 BUY {symbol} @ ₹{price:.2f}")

                elif stock["position"] > 0:
                    low = data["Low"].dropna().iloc[-1]
                    high = data["High"].dropna().iloc[-1]

                    reason = None
                    if low <= stock["sl"]:
                        reason = "STOP LOSS"
                    elif stock.get("target") and high >= stock["target"]:
                        reason = "TARGET"

                    if reason:
                        qty = stock["qty"]
                        pnl = (price - stock["entry_price"]) * qty
                        new_bal = get_user_balance(user_id) + (price * qty)
                        update_user_balance(user_id, new_bal)

                        stocks_collection.delete_one({"_id": stock["_id"]})
                        logs_collection.update_one({"symbol": symbol, "user_id": user_id, "sell_price": None}, {"$set": {
                            "sell_price": price,
                            "sell_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "pnl": pnl,
                            "reason": reason
                        }})
                        bot.send_message(chat_id=user_id, text=f"🔴 SELL {symbol} ({reason}) @ ₹{price:.2f} | P&L: ₹{pnl:.2f}")

            except Exception as e:
                print(f"[Tracking Error] {e}")
        time.sleep(60)

# Main Function
def main():
    updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(Filters.regex("(?i)^2|add|modify"), add_stock_start)],
        states={
            ADD_SYMBOL: [MessageHandler(Filters.text & ~Filters.command, add_stock_symbol)],
            ADD_ENTRY: [MessageHandler(Filters.text & ~Filters.command, add_stock_entry)],
            ADD_SL: [MessageHandler(Filters.text & ~Filters.command, add_stock_sl)],
            ADD_TARGET: [MessageHandler(Filters.text & ~Filters.command, add_stock_target)],
            ADD_QTY: [MessageHandler(Filters.text & ~Filters.command, add_stock_qty)],
            ASK_BALANCE: [MessageHandler(Filters.text & ~Filters.command, receive_balance)],
            DELETE_TRACK: [MessageHandler(Filters.text & ~Filters.command, confirm_delete)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    dp.add_handler(conv_handler)
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("help", help_cmd))
    dp.add_handler(CommandHandler("setbalance", set_balance))
    dp.add_handler(MessageHandler(Filters.regex("(?i)^1|balance"), view_balance))
    dp.add_handler(MessageHandler(Filters.regex("(?i)^3|portfolio"), portfolio))
    dp.add_handler(MessageHandler(Filters.regex("(?i)^4|delete"), delete_stock))

    threading.Thread(target=track, args=(updater.bot,), daemon=True).start()

    @app.route("/", methods=["GET"])
    def home():
        return "Bot is Running", 200

    updater.start_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TELEGRAM_BOT_TOKEN,
        webhook_url=f"{APP_URL}/{TELEGRAM_BOT_TOKEN}"
    )
    updater.idle()

if __name__ == "__main__":
    main()
