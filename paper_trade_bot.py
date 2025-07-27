import os
import datetime
import asyncio
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, CallbackContext, filters
)
from pymongo import MongoClient
from dotenv import load_dotenv
import yfinance as yf
import socket

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
BALANCE,ADD_STOCK, DELETE_STOCK, PORTFOLIO = range(4)

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


LOG_FILE = "price_logs.txt"

def log_to_file(message):
    with open(LOG_FILE, "a") as file:
        file.write(f"{message}\n")

def get_price(symbol, debug=True):
    try:
        yf_symbol = symbol if symbol.endswith(".NS") else symbol + ".NS"
        ticker = yf.Ticker(yf_symbol)

        # Try 1m interval first (live price)
        data = ticker.history(period='1d', interval='1m')
        if not data.empty and not data['Close'].dropna().empty:
            ltp = data['Close'].dropna().iloc[-1]
            return round(float(ltp), 2)

        # If no intraday data, get last available daily close (up to 10 days back)
        data = ticker.history(period='10d', interval='1d')
        if not data.empty and not data['Close'].dropna().empty:
            ltp = data['Close'].dropna().iloc[-1]
            return round(float(ltp), 2)

        if debug:
            log_to_file(f"❌ No data found for {symbol} (may be illiquid, market closed, or Yahoo issue)")
        return None
    except Exception as e:
        if debug:
            log_to_file(f"❌ Error fetching LTP for {symbol}: {e}")
        return None
    try:
        yf_symbol = symbol if symbol.endswith(".NS") else symbol + ".NS"
        ticker = yf.Ticker(yf_symbol)

        # Try 1m interval first
        data = ticker.history(period='1d', interval='1m')
        if data.empty:
            # Try 5m interval
            data = ticker.history(period='1d', interval='5m')
        if data.empty:
            # Try daily interval
            data = ticker.history(period='5d', interval='1d')

        if debug:
            now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            log_to_file(f"\n🕒 {now} - Fetching {yf_symbol}")
            try:
                hostname = socket.gethostname()
                ip_address = socket.gethostbyname(hostname)
                log_to_file(f"🔍 Hostname: {hostname} | IP: {ip_address}")
            except Exception as ip_err:
                log_to_file(f"⚠️ IP fetch error: {ip_err}")
            log_to_file(f"📈 Raw data head:\n{data.head()}")

        if not data.empty:
            ltp = data['Close'].iloc[-1]
            return round(float(ltp), 1)
        else:
            if debug:
                log_to_file(f"❌ No data found for {symbol} after trying multiple intervals (may be illiquid, market closed, or truly delisted)")
            return None
    except Exception as e:
        if debug:
            log_to_file(f"❌ Error fetching LTP for {symbol}: {e}")
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


async def sell_stock(symbol, entry, qty, sl, target, price, reason):
    pnl = (price - entry) * qty
    new_balance = get_balance()["balance"] + price * qty
    now = datetime.datetime.now()
    today_str = now.strftime("%Y-%m-%d")

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



async def execution(symbol, entry, qty, sl, target):
    try:
        if not symbol.endswith(".NS"):
            symbol += ".NS"

        ticker = yf.Ticker(symbol)
        data = ticker.history(period='1d', interval='1m')

        if data.empty:
            print(f"⚠️ No candle data found for {symbol}")
            return

        latest_candle = data.iloc[-1]
        high = float(latest_candle['High'])
        low = float(latest_candle['Low'])

        tracked = tracked_stocks.find_one({"symbol": symbol})
        if not tracked:
            print(f"⚠️ {symbol} not found in tracking.")
            return

        status = tracked.get("detail")

        # Entry logic: check if price range allows entry
        if status == "tracking":
            if low <= entry <= high:
                tracked_stocks.update_one(
                    {"symbol": symbol},
                    {"$set": {"detail": "holding"}}
                )
                print(f"✅ Order Executed for {symbol} at ₹{entry}")
            else:
                print(f"⏳ Waiting for {symbol} to trigger entry ₹{entry} (Range: {low}-{high})")
            return

        # Exit logic: SL or Target
        if status == "holding":
            if low <= sl:
                await sell_stock(symbol, entry, qty, sl, target, sl, "Stop Loss Hit")
            elif high >= target:
                await sell_stock(symbol, entry, qty, sl, target, target, "Target Hit")

    except Exception as e:
        print(f"❌ Execution Error for {symbol}:", e)


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

async def ask_stock_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📥 Please enter stock in this format:\n"
        "`SYMBOL, ENTRY, QTY, SL, [TARGET]`\n\n"
        "📌 Example:\n`RELIANCE, 2800, 5, 2750, 2900`",
        parse_mode="Markdown"
    )
    return ADD_STOCK


async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bal = get_balance()["balance"]
    await update.message.reply_text(f"Current Balance: ₹{bal}")
    return ConversationHandler.END

async def add_modify_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = update.message.text
        data = [d.strip() for d in text.split(",")]

        if len(data) < 4:
            await update.message.reply_text("❌ Invalid format. Please use:\nSYMBOL, ENTRY, QTY, SL, [TARGET]")
            return ConversationHandler.END

        symbol = data[0].upper()
        entry = float(data[1])
        qty = int(data[2])
        sl = float(data[3])
        target = float(data[4]) if len(data) >= 5 else None

        existing = tracked_stocks.find_one({"symbol": symbol})

        if existing:
            # Only override SL and Target
            update_fields = {
                "sl": sl,
                "last_modified": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            if target is not None:
                update_fields["target"] = target

            tracked_stocks.update_one(
                {"symbol": symbol},
                {"$set": update_fields}
            )

            await update.message.reply_text(
                f"✏️ SL and Target updated!\n\n"
                f"SYMBOL: {symbol}\n"
                f"SL: ₹{sl}\n"
                f"TARGET: {'❌ Not Set' if not target else f'₹{target}'}"
            )
        else:
            # Insert new tracking stock
            tracked_stocks.insert_one({
                "symbol": symbol,
                "entry_price": entry,
                "qty": qty,
                "sl": sl,
                "target": target,
                "status": "tracking",
                "detail": "tracking",
                "date": datetime.datetime.now().strftime("%Y-%m-%d")
            })

            await update.message.reply_text(
                f"✅ Stock added to tracking!\n\n"
                f"SYMBOL: {symbol}\nENTRY: ₹{entry}\nQTY: {qty}\nSL: ₹{sl}\n"
                f"TARGET: {'❌ Not Set' if not target else f'₹{target}'}"
            )

        return ConversationHandler.END

    except Exception as e:
        print("ERROR in add_modify_stock:", e)
        await update.message.reply_text("❌ Failed to add/modify stock. Please try again.")
        return ConversationHandler.END

async def portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = f"📊 Portfolio: 📅 {today_str}\n\n"

    # ✅ HOLDING
    text += "✅ HOLDING\n"
    holdings = tracked_stocks.find({"detail": "holding"})
    for h in holdings:
        ltp = get_price(h["symbol"],debug=True  )
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

    # ⏳ TRACKING
    text += "\👀 TRACKING\n"
    tracking = tracked_stocks.find({"detail": "tracking"})
    for t in tracking:
        ltp = get_price(t['symbol'],debug=True)
        invested = t['entry_price'] * t['qty']
        text += (
            f"⏱️ {t['symbol']} | Entry: ₹{t['entry_price']} | Now: ₹{ltp} | "
            f"SL: {t.get('sl')} | Target: {t.get('target', 'None')} | Qty: {t['qty']} | "
            f"Invested: ₹{round(invested, 2)}\n"
        )

    # 🔴 SOLD
    text += "\n🔴 SOLD\n"
    today_pnl = 0
    sold_logs = trade_logs.find({"detail": "sold"})
    for s in sold_logs:
        text += f"🔴 {s['symbol']} | Sold at: ₹{s['exit_price']} | P&L: ₹{s['pnl']} | Qty: {s['qty']}\n"
        if s.get('date') == today_str:
            today_pnl += s.get('pnl', 0)

    # 📈 P&L Summary
    overall_pnl_cursor = trade_logs.aggregate([
        {"$group": {"_id": None, "total": {"$sum": "$pnl"}}}
    ])
    overall_pnl = next(overall_pnl_cursor, {}).get("total", 0)

    text += f"\n📅 TODAY ({today_str}) P&L: ₹{round(today_pnl, 2)}\n"
    text += "-" * 56 + "\n"
    text += f"📈 Total Realized P&L: ₹{round(overall_pnl, 2)}"

    await update.message.reply_text(text)


async def delete_tracking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🗑️ Please enter the stock *symbol* to delete from tracking:", parse_mode="Markdown")
    return DELETE_STOCK

async def confirm_delete_tracking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    input_symbol = update.message.text.strip().upper()
    symbol = input_symbol if input_symbol.endswith(".NS") else input_symbol + ".NS"

    tracked = tracked_stocks.find_one({"symbol": symbol})

    if tracked:
        if tracked["detail"] == "tracking":
            delete_stock(symbol)
            await update.message.reply_text(f"✅ Deleted {symbol} from tracking.")
        else:
            await update.message.reply_text(f"⚠️ {symbol} is in HOLDING. Cannot delete.")
    else:
        await update.message.reply_text(f"❌ {symbol} not found in tracking list.")

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
                target=stock.get('target')
            )
        await asyncio.sleep(10)

async def on_startup(application):
    # Schedule the monitor_all task after the application starts
    application.create_task(monitor_all(application))

def main():
    import logging
    logging.basicConfig(level=logging.INFO)

    application = Application.builder().token(BOT_TOKEN).build()

    # Conversation handler for /start and menu options
    main_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex("^(1️⃣|1|[Bb]alance)$"), show_balance),
            MessageHandler(filters.Regex("^(2️⃣|2|[Aa]dd.*|[Mm]odify.*)$"), ask_stock_details),
            MessageHandler(filters.Regex("^(3️⃣|3|[Pp]ortfolio)$"), portfolio),
            MessageHandler(filters.Regex("^(4️⃣|4|[Dd]elete.*)$"), delete_tracking),
        ],
        states={
            ADD_STOCK: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_modify_stock)],
            DELETE_STOCK: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_delete_tracking)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Register handlers
    application.add_handler(main_conv_handler)
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("portfolio", portfolio))
    application.add_handler(CommandHandler("balance", show_balance))
    application.add_handler(CommandHandler("setbalance", set_balance))

    # Webhook setup
    application.post_init = on_startup
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,
        webhook_url=f"{APP_URL}/{BOT_TOKEN}"
    )

if __name__ == '__main__':
    main()