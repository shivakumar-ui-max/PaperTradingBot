import os
import datetime
import requests
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ConversationHandler, ContextTypes, filters
from pymongo import MongoClient
from dotenv import load_dotenv
from flask import Flask, request

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
API_KEY = os.getenv("API_KEY")
SECRET_KEY = os.getenv("SECRET_KEY")
APP_URL = os.getenv("APP_URL")
USER_ID = "1145551286"

client = MongoClient(MONGO_URI)
db = client["paper_trading"]

balance_col = db["Balance"]
tracked_stocks_col = db["TrackedStocks"]
trade_logs_col = db["TradeLogs"]

ADD_STOCK, DELETE_STOCK, SET_BALANCE = range(3)

print("Downloading Angel symbol-token mapping...")
symbol_token_map = {}
url = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
response = requests.get(url)
data = response.json()

for item in data:
    if item.get("exchange") == "NSE":
        symbol_token_map[item["symbol"]] = item["token"]

# Helper Functions

def get_balance():
    bal = balance_col.find_one({"user_id": USER_ID})
    return bal["balance"] if bal else 0

def update_balance(amount):
    balance_col.update_one({"user_id": USER_ID}, {"$set": {"balance": amount}}, upsert=True)

def add_stock(symbol, entry, qty, sl, target):
    tracked_stocks_col.insert_one({
        "user_id": USER_ID,
        "symbol": symbol,
        "entry_price": entry,
        "qty": qty,
        "sl": sl,
        "target": target
    })
    bal = get_balance()
    invested = entry * qty
    update_balance(bal - invested)

def modify_stock(symbol, sl, target):
    tracked_stocks_col.update_one({"user_id": USER_ID, "symbol": symbol}, {"$set": {"sl": sl, "target": target}})

def delete_stock(symbol):
    tracked_stocks_col.delete_one({"user_id": USER_ID, "symbol": symbol})

def get_ltp(symbol):
    angel_symbol = symbol.replace(".NS", "-EQ")
    token = symbol_token_map.get(angel_symbol)
    if not token:
        return None

    url = "https://apiconnect.angelbroking.com/rest/secure/angelbroking/order/v1/getLtpData"
    headers = {
        "X-PrivateKey": API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": "192.168.0.1",
        "X-ClientPublicIP": "192.168.0.1",
        "X-MACAddress": "00:0a:95:9d:68:16",
        "X-UserType": "USER",
        "Authorization": SECRET_KEY
    }
    payload = {
        "exchange": "NSE",
        "tradingsymbol": angel_symbol,
        "symboltoken": token
    }
    try:
        res = requests.post(url, json=payload, headers=headers)
        return float(res.json()['data']['ltp'])
    except:
        return None

# Telegram Bot Handlers

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [['Balance', 'Add / Modify Stock'], ['Portfolio', 'Delete Tracking Stock']]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("Welcome to Paper Trading Bot!", reply_markup=reply_markup)
    return ADD_STOCK

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """
Available Commands:
1. Add/Modify Stock - Track or modify a stock.
2. Balance - Show balance.
3. Portfolio - Show holdings & P&L.
4. Delete Tracking Stock - Remove stock.
5. Cancel - Cancel current action.
6. SetBalance - Set initial balance.
"""
    await update.message.reply_text(text)

async def add_modify_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        parts = text.split(',')
        input_symbol = parts[0].strip().upper()
        symbol = input_symbol + ".NS" if not input_symbol.endswith(".NS") else input_symbol
        entry = float(parts[1].strip())
        qty = int(parts[2].strip())
        sl = float(parts[3].strip())
        target = float(parts[4].strip()) if len(parts) > 4 else None

        if tracked_stocks_col.find_one({"symbol": symbol, "user_id": USER_ID}):
            modify_stock(symbol, sl, target)
            await update.message.reply_text(f"{symbol} modified successfully!")
        else:
            add_stock(symbol, entry, qty, sl, target)
            await update.message.reply_text(f"{symbol} added successfully!")
    except:
        await update.message.reply_text("Invalid format. Use: SYMBOL, ENTRY, QTY, SL, [TARGET]")

    return ConversationHandler.END

async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bal = get_balance()
    await update.message.reply_text(f"Current Balance: ₹{bal}")
    return ConversationHandler.END

async def portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.datetime.now()
    today_str = now.strftime("%d-%B-%Y")
    text = f"📊 Portfolio: 📅 {today_str}\n\nHOLDING\n"

    holdings = tracked_stocks_col.find({"user_id": USER_ID})
    for h in holdings:
        ltp = get_ltp(h['symbol'])
        if ltp:
            change = ((ltp - h['entry_price']) / h['entry_price']) * 100
            invested = h['entry_price'] * h['qty']
            sign = "🟢" if change >= 0 else "❌"
            text += f"{sign} Holding {h['symbol']} | Entry: ₹{h['entry_price']} | Now: ₹{ltp} | {round(change,2)}% | Qty: {h['qty']} | SL: {h['sl']} | Target: {h.get('target','None')} | Invested: ₹{round(invested,2)}\n"
        else:
            text += f"⚠️ {h['symbol']} | LTP Not Found\n"

    text += "\nSOLD:\n"
    today_pnl = 0
    overall_pnl = 0

    sold_logs = trade_logs_col.find({"user_id": USER_ID})
    for log in sold_logs:
        reason = log.get("reason", "EXIT")
        sell_time = log.get("sell_time", "")
        pnl = log.get("pnl", 0)
        overall_pnl += pnl

        trade_date = sell_time.split(" ")[0]
        if trade_date == now.strftime("%Y-%m-%d"):
            today_pnl += pnl

        text += f"🔴 {log['symbol']} | {reason} | ₹{log['buy_price']} → ₹{log['sell_price']} | Qty: {log['qty']} | P&L: ₹{round(pnl,2)} | {sell_time.split(' ')[1]}\n"

    text += f"\nTODAY {today_str} P&L: ₹{round(today_pnl,2)}\n"
    text += "-" * 56 + "\n"
    text += f"📈 Overall Realized P&L (History): ₹{round(overall_pnl,2)}"

    await update.message.reply_text(text)

async def delete_tracking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    input_symbol = update.message.text.strip().upper()
    symbol = input_symbol + ".NS" if not input_symbol.endswith(".NS") else input_symbol
    delete_stock(symbol)
    await update.message.reply_text(f"Deleted {symbol} from tracking.")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Operation cancelled.")
    return ConversationHandler.END

async def set_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amt = float(update.message.text.strip())
    update_balance(amt)
    await update.message.reply_text(f"Balance set to ₹{amt}")
    return ConversationHandler.END

# Create Flask App for Webhook

flask_app = Flask(__name__)

app = Application.builder().token(BOT_TOKEN).updater(None).build()

conv_handler = ConversationHandler(
    entry_points=[CommandHandler("start", start)],
    states={
        ADD_STOCK: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_modify_stock)],
        DELETE_STOCK: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_tracking)],
        SET_BALANCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_balance)],
    },
    fallbacks=[CommandHandler("cancel", cancel)]
)

app.add_handler(conv_handler)
app.add_handler(CommandHandler("help", help_command))
app.add_handler(CommandHandler("portfolio", portfolio))
app.add_handler(CommandHandler("balance", show_balance))
app.add_handler(CommandHandler("setbalance", set_balance))

@flask_app.post("/webhook")
async def webhook_handler():
    await app.update_queue.put(Update.de_json(await request.get_json(), app.bot))
    return "ok"

if __name__ == "__main__":
    app.run_webhook(
        listen="0.0.0.0",
        port=8000,
        webhook_url=f"{APP_URL}/webhook",
        web_app=flask_app
    )
