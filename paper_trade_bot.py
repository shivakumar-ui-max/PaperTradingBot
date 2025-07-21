from dotenv import load_dotenv
load_dotenv()

import os
import threading
import time
from datetime import datetime, date
from pymongo import MongoClient
from telegram.ext import Updater, CommandHandler, CallbackContext, ConversationHandler, MessageHandler, Filters
from telegram import Update, ReplyKeyboardMarkup
from flask import Flask
from nsepython import nse_quote_ltp

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

ASK_BALANCE, ADD_SYMBOL, ADD_ENTRY, ADD_SL, ADD_TARGET, ADD_QTY, DELETE_TRACK = range(7)

temp_stock = {}

# Utilities
def get_user_balance(user_id):
    bal = balance_collection.find_one({"user_id": user_id})
    return bal["value"] if bal else 100000

def update_user_balance(user_id, value):
    balance_collection.update_one({"user_id": user_id}, {"$set": {"value": value}}, upsert=True)

def get_ltp(symbol):
    try:
        return float(nse_quote_ltp(symbol))
    except:
        return None

# Commands
def start(update: Update, context: CallbackContext):
    keyboard = [["1️⃣ Balance", "2️⃣ Add / Modify Stock"], ["3️⃣ Portfolio", "4️⃣ Delete Tracking Stock"]]
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
        "/cancel - Cancel current operation"
    )
    update.message.reply_text(text, parse_mode="Markdown")

# Balance
def view_balance(update: Update, context: CallbackContext):
    uid = update.effective_user.id
    balance = get_user_balance(uid)
    update.message.reply_text(f"💰 Your Balance: ₹{balance:.2f}")

def set_balance(update: Update, context: CallbackContext):
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

# Add Stock
def add_stock_start(update: Update, context: CallbackContext):
    uid = update.effective_user.id
    temp_stock[uid] = {"in_progress": True}
    update.message.reply_text("📌 Enter Stock Symbol (e.g., TCS):")
    return ADD_SYMBOL

def add_stock_symbol(update: Update, context: CallbackContext):
    uid = update.effective_user.id
    symbol = update.message.text.upper()
    ltp = get_ltp(symbol)
    if ltp is None:
        update.message.reply_text("❌ Invalid Symbol. Try again:")
        return ADD_SYMBOL
    temp_stock[uid]["symbol"] = symbol
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
    stocks_collection.insert_one(stock)
    temp_stock.pop(uid)
    update.message.reply_text(f"✅ {stock['symbol']} added to tracking")
    return ConversationHandler.END

def cancel(update: Update, context: CallbackContext):
    update.message.reply_text("❌ Your current operation has been canceled.")
    return ConversationHandler.END


# Delete Stock
def delete_stock(update: Update, context: CallbackContext):
    update.message.reply_text("🗑️ Enter stock symbol to delete:")
    return DELETE_TRACK

def confirm_delete(update: Update, context: CallbackContext):
    uid = update.effective_user.id
    symbol = update.message.text.upper()
    stocks_collection.delete_one({"symbol": symbol, "user_id": uid})
    update.message.reply_text(f"✅ {symbol} removed from tracking.")
    return ConversationHandler.END

# Portfolio
from datetime import datetime
def portfolio(update, context):
    uid = str(update.effective_chat.id)
    stocks = list(stocks_collection.find({"user_id": uid}))
    logs = list(logs_collection.find({"user_id": uid}))

    lines = []
    lines.append(f"📊 Portfolio: 📅 {datetime.now().strftime('%d-%B-%Y')}\n")

    # TRACKING STOCKS
    tracking = [s for s in stocks if not s.get("in_position")]
    if tracking:
        lines.append("TRACKING:")
        for s in tracking:
            ltp = s.get('ltp', s['entry'])  # Use last updated LTP or fallback
            lines.append(f"📍 {s['symbol']} | Entry: ₹{s['entry']} | LTP: ₹{ltp} | Qty: {s['qty']}")
        lines.append("\n")

    # HOLDING STOCKS
    holding = [s for s in stocks if s.get("in_position")]
    if holding:
        lines.append("HOLDING:")
        for s in holding:
            ltp = s.get('ltp', s['entry'])
            pnl = (ltp - s['entry']) * s['qty']
            status = "🟢" if pnl >= 0 else "🔴"
            lines.append(f"{status} {s['symbol']} | Entry: ₹{s['entry']} | Now: ₹{ltp} | Qty: {s['qty']} | P&L: ₹{pnl:+.2f}")
        lines.append("\n")

    # TODAY P&L
    today = datetime.now().strftime("%Y-%m-%d")
    today_logs = [l for l in logs if l.get("sell_time", "").startswith(today)]
    today_pnl = sum(l["pnl"] for l in today_logs if l.get("sell_price"))
    lines.append(f"TODAY P&L: ₹{today_pnl:+.2f}")
    lines.append("\n" + "-"*56)

    # OVERALL HISTORY P&L
    total_pnl = sum(l["pnl"] for l in logs if l.get("sell_price"))
    lines.append(f"📈 Overall Realized P&L (History): ₹{total_pnl:+.2f}")

    update.message.reply_text("\n".join(lines))


# Tracking Thread
def track(bot):
    while True:
        for stock in stocks_collection.find():
            try:
                symbol = stock["symbol"]
                user_id = stock["user_id"]
                ltp = get_ltp(symbol)
                if ltp is None:
                    continue

                if stock["position"] == 0 and ltp <= stock["entry"]:
                    cost = ltp * stock["qty"]
                    bal = get_user_balance(user_id)
                    if bal < cost:
                        continue
                    update_user_balance(user_id, bal - cost)
                    stocks_collection.update_one({"_id": stock["_id"]}, {"$set": {"position": stock["qty"], "entry_price": ltp}})
                    logs_collection.insert_one({"user_id": user_id, "symbol": symbol, "qty": stock["qty"], "buy_price": ltp, "buy_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "sell_price": None})
                    bot.send_message(chat_id=user_id, text=f"🟢 BUY {symbol} @ ₹{ltp}")
                elif stock["position"] > 0:
                    if ltp <= stock["sl"] or (stock.get("target") and ltp >= stock["target"]):
                        pnl = (ltp - stock["entry_price"]) * stock["qty"]
                        new_bal = get_user_balance(user_id) + (ltp * stock["qty"])
                        update_user_balance(user_id, new_bal)
                        stocks_collection.delete_one({"_id": stock["_id"]})
                        logs_collection.update_one({"user_id": user_id, "symbol": symbol, "sell_price": None}, {"$set": {"sell_price": ltp, "sell_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "pnl": pnl}})
                        bot.send_message(chat_id=user_id, text=f"🔴 SELL {symbol} @ ₹{ltp} | P&L: ₹{pnl:+.2f}")
            except Exception as e:
                print(e)
        time.sleep(60)

# Main

def main():
    updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    # Conversation handler for Add / Modify Stock
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

    # Register handlers
    dp.add_handler(conv_handler)
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("help", help_cmd))
    dp.add_handler(CommandHandler("setbalance", set_balance))
    dp.add_handler(MessageHandler(Filters.regex("(?i)^1|balance"), view_balance))
    dp.add_handler(MessageHandler(Filters.regex("(?i)^3|portfolio"), portfolio))
    dp.add_handler(MessageHandler(Filters.regex("(?i)^4|delete"), delete_stock))

    # Start background threads
    threading.Thread(target=track, args=(updater.bot,), daemon=True).start()
    threading.Thread(target=background_ltp_updater, daemon=True).start()

    # Flask route
    @app.route("/", methods=["GET"])
    def home():
        return "Bot is Running", 200

    # Start webhook
    updater.start_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TELEGRAM_BOT_TOKEN,
        webhook_url=f"{APP_URL}/{TELEGRAM_BOT_TOKEN}"
    )

    updater.idle()


if __name__ == "__main__":
    main()
