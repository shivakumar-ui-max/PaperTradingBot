import os
import datetime
import requests
import asyncio
import json
from flask import Flask
from threading import Thread
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, CallbackContext, filters
)
from pymongo import MongoClient
from dotenv import load_dotenv
from bson import ObjectId
import yfinance as yf

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
API_KEY = os.getenv("API_KEY")
SECRET_KEY = os.getenv("SECRET_KEY")
APP_URL = os.getenv("APP_URL")
PORT = int(os.environ.get('PORT', 8443))
MY_CHAT_ID =os.getenv("MY_CHAT_ID")

client = MongoClient(MONGO_URI)
db = client["PaperTrade"]

balance = db["Balance"]
tracked_stocks = db["TrackedStocks"]
trade_logs = db["TradeLogs"]

now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
today_str = datetime.datetime.now().strftime("%Y-%m-%d")

latest_doc = balance.find_one({}, sort=[("_id", -1)])

# Constants for conversation handler
ADD_STOCK, DELETE_STOCK, PORTFOLIO = range(3)

# --- Utility Functions ---

def updateBal(amt=None):
    existing_bal = balance.find_one()
    
    if amt is not None:
        # Manually set balance
        if existing_bal:
            balance.update_one(
                {"_id": existing_bal["_id"]},
                {"$set": {"balance": amt}}
            )
        else:
            balance.insert_one({
                "balance": amt,
                "date": today_str
            })
    else:
        # Auto-update from latest trade log
        if trade_logs.count_documents({}) > 0:
            latest_doc = trade_logs.find_one({}, sort=[("_id", -1)])
            balance_after = latest_doc.get("balance_after")
            if balance_after is not None:
                if existing_bal:
                    balance.update_one(
                        {"_id": existing_bal["_id"]},
                        {"$set": {"balance": balance_after}}
                    )
                else:
                    balance.insert_one({
                        "balance": balance_after,
                        "date": today_str
                    })


def get_balance():
    return balance.find_one({}, sort=[("_id", -1)]) or {"balance": 0}


def get_price(symbol):
    try:
        ticker = yf.Ticker(symbol)
        data = ticker.history(period='1d', interval='1m')
        if not data.empty:
            ltp = data['Close'].iloc[-1]
            return float(ltp)
        else:
            print(f"❌ No data found for {symbol}")
            return None
    except Exception as e:
        print(f"❌ Error fetching LTP: {e}")
        return None

# --- Core Trading Logic ---

def add_stock(symbol, entry, qty, sl, target):
    tracked_stocks.insert_one({
        "symbol": symbol,
        "entry_price": entry,
        "qty": qty,
        "sl": sl,
        "pnl": None,
        "target": target,
        "invested": entry*qty,
        "balance_after": get_balance()["balance"] - entry * qty,
        "date": today_str,
        "detail": "tracking"
    })
    updateBal()

def modify_stock(symbol, sl, target):
    tracked_stocks.update_one({"symbol": symbol}, {"$set": {"sl": sl, "target": target}})

def delete_stock(symbol):
    tracked_stocks.delete_one({"symbol": symbol})

async def sell_stock(symbol, entry, qty, sl, target, price, reason, context=None, chat_id=None):
    pnl = (price - entry) * qty
    new_balance = get_balance()["balance"] + price * qty

    trade_logs.insert_one({
        "symbol": symbol,
        "entry_price": entry,
        "qty": qty,
        "sl": sl,
        "target": target,
        "exit_price": price,
        "pnl": pnl,
        "entry_time": now,
        "exit_time": now,
        "balance_after": new_balance,
        "status": reason,
        "date": today_str,
        "detail": "sold"
    })

    tracked_stocks.delete_one({"symbol": symbol})
    updateBal(new_balance)

    if context and chat_id:
        message = (
            f"🚨 Trade Executed:\n"
            f"{symbol}\n"
            f"Quantity: {qty}\n"
            f"Price: ₹{price}\n"
            f"Reason: {reason}\n"
            f"P&L: ₹{round(pnl, 2)}\n"
            f"New Balance: ₹{round(new_balance, 2)}"
        )
        await context.bot.send_message(chat_id=chat_id, text=message)



async def execution(symbol, entry, qty, sl, target, context=None, chat_id=None):
    price = get_price(symbol)
    if price is None:
        print("⚠️ No price data found for:", symbol)
        return

    if abs(price - entry) < 0.2:
        tracked_stocks.update_one(
            {"symbol": symbol, "detail": "tracking"},
            {"$set": {"detail": "holding"}}
        )
    elif price <= sl:
        await sell_stock(symbol, entry, qty, sl, target, price, "Stop Loss Hit", context, chat_id)
    elif price >= target:
        await sell_stock(symbol, entry, qty, sl, target, price, "Target Hit", context, chat_id)



# --- Telegram Command Handlers ---

async def start(update: Update, context: CallbackContext):
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
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)

async def help_command(update: Update, context: CallbackContext):
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
    await update.message.reply_text(text, parse_mode="Markdown")

async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bal = get_balance()["balance"]
    await update.message.reply_text(f"Current Balance: ₹{bal}")
    return ConversationHandler.END

async def add_modify_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        parts = text.split(',')
        input_symbol = parts[0].strip().upper()
        symbol = input_symbol if input_symbol.endswith(".NS") else input_symbol + ".NS"
        entry = float(parts[1].strip())
        qty = int(parts[2].strip())
        sl = float(parts[3].strip())
        target = float(parts[4].strip()) if len(parts) > 4 else None

        if tracked_stocks.find_one({"symbol": symbol}):
            modify_stock(symbol, sl, target)
            await update.message.reply_text(f"{symbol} modified successfully!")
        else:
            add_stock(symbol, entry, qty, sl, target,)
            await execution(symbol, entry, qty, sl, target, context=context, chat_id=update.effective_chat.id)
            await update.message.reply_text(f"{symbol} added successfully!")
    except:
        await update.message.reply_text("Invalid format. Use: SYMBOL, ENTRY, QTY, SL, [TARGET]")

    return ConversationHandler.END

async def portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = f"📊 Portfolio: 📅 {today_str}\n\n"

    text += "✅ HOLDING\n"
    holdings = tracked_stocks.find({"detail": "holding"})
    for h in holdings:
        ltp = get_price(h["symbol"])
        if ltp:
            change = ((ltp - h['entry_price']) / h['entry_price']) * 100
            invested = h['entry_price'] * h['qty']
            sign = "🟢" if change >= 0 else "❌"
            text += (
                f"{sign} {h['symbol']} | Entry: ₹{h['entry_price']} | Now: ₹{ltp} | "
                f"{round(change, 2)}% | Qty: {h['qty']} | SL: {h['sl']} | "
                f"Target: {h.get('target', 'None')} | Invested: ₹{round(invested, 2)}\n"
            )
        else:
            text += f"⚠️ {h['symbol']} | LTP Not Found\n"

    text += "\n👀 TRACKING\n"
    tracking = tracked_stocks.find({"detail": "tracking"})
    for t in tracking:
        ltp = get_price(t['symbol'])
        text += (
            f"👁️ {t['symbol']} | Entry: ₹{t['entry_price']} | Now: ₹{ltp if ltp else 'N/A'} | "
            f"SL: {t.get('sl')} | Target: {t.get('target', 'None')} | Qty: {t['qty']}\n"
        )

    text += "\n🔴 SOLD\n"
    today_pnl = 0

    sold_logs = trade_logs.find({"detail": "sold"})
    for s in sold_logs:
        text += f"🔴 {s['symbol']} | Sold at: ₹{s['exit_price']} | P&L: ₹{s['pnl']} | Qty: {s['qty']}\n"
        if s.get('date') == today_str:
            today_pnl += s.get('pnl', 0)

    # MongoDB aggregation for total realized P&L
    overall_pnl_cursor = trade_logs.aggregate([
        {"$group": {"_id": None, "total": {"$sum": "$pnl"}}}
    ])
    overall_pnl = next(overall_pnl_cursor, {}).get("total", 0)

    # Append P&L summaries
    text += f"\n📅 TODAY ({today_str}) P&L: ₹{round(today_pnl, 2)}\n"
    text += "-" * 56 + "\n"
    text += f"📈 Total Realized P&L: ₹{round(overall_pnl, 2)}"


    await update.message.reply_text(text)

async def delete_tracking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    input_symbol = update.message.text.strip().upper()
    symbol = input_symbol if input_symbol.endswith(".NS") else input_symbol + ".NS"
    delete_stock(symbol)
    await update.message.reply_text(f"Deleted {symbol} from tracking.")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Operation cancelled.")
    return ConversationHandler.END

async def set_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amt = float(update.message.text.strip())
    updateBal(amt)
    await update.message.reply_text(f"Balance set to ₹{amt}")
    return ConversationHandler.END



async def monitor_all(application):
    while True:
        stocks = tracked_stocks.find({"detail": "tracking"})
        for stock in stocks:
            await execution(
                symbol=stock['symbol'],
                entry=stock['entry_price'],
                qty=stock['qty'],
                sl=stock['sl'],
                target=stock.get('target'),
                context=application.bot,
                chat_id=MY_CHAT_ID
            )
        await asyncio.sleep(10)


def main():
    import logging
    logging.basicConfig(level=logging.INFO)

    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ADD_STOCK: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_modify_stock)],
            DELETE_STOCK: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_tracking)],
            PORTFOLIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, portfolio)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("portfolio", portfolio))
    application.add_handler(CommandHandler("balance", show_balance))
    application.add_handler(CommandHandler("setbalance", set_balance))

    application.create_task(monitor_all(application))


    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,
        webhook_url=f"{APP_URL}/{BOT_TOKEN}"
    )

if __name__ == '__main__':
    main()
