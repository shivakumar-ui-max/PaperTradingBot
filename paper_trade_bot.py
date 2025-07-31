import os
import datetime
import asyncio
import requests
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, CallbackContext, filters
)
from pymongo import MongoClient
from dotenv import load_dotenv
import yfinance as yf
import logging
from flask import Flask
from threading import Thread

# Initialize Flask app for keep-alive
app = Flask(__name__)

@app.route('/ping')
def ping():
    return "Bot is alive", 200

# Load environment variables
load_dotenv()

# Initialize logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Database setup
client = MongoClient(os.getenv("MONGO_URI"))
db = client["PaperTrade"]
balance = db["Balance"]
tracked_stocks = db["TrackedStocks"]
trade_logs = db["TradeLogs"]

# Constants
PORT = int(os.environ.get('PORT', 8443))
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("APP_URL")
MONGO_URI = os.getenv("MONGO_URI")

# Conversation states
BALANCE, ADD_STOCK, DELETE_STOCK, PORTFOLIO = range(4)

# --- Core Functions ---
def update_balance(amt=None):
    """Update balance with proper error handling"""
    try:
        existing_bal = balance.find_one(sort=[("_id", -1)])
        if amt is not None:
            if existing_bal:
                balance.update_one(
                    {"_id": existing_bal["_id"]},
                    {"$set": {"balance": amt, "updated_at": datetime.datetime.now()}}
                )
            else:
                balance.insert_one({
                    "balance": amt,
                    "created_at": datetime.datetime.now(),
                    "updated_at": datetime.datetime.now()
                })
        else:
            latest_trade = trade_logs.find_one(sort=[("_id", -1)])
            if latest_trade and "balance_after" in latest_trade:
                new_balance = latest_trade["balance_after"]
                if existing_bal:
                    balance.update_one(
                        {"_id": existing_bal["_id"]},
                        {"$set": {"balance": new_balance, "updated_at": datetime.datetime.now()}}
                    )
                else:
                    balance.insert_one({
                        "balance": new_balance,
                        "created_at": datetime.datetime.now(),
                        "updated_at": datetime.datetime.now()
                    })
    except Exception as e:
        logger.error(f"Balance update failed: {e}")
# --- Stock Operations ---
async def add_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle adding new stock to track"""
    try:
        text = update.message.text
        symbol, entry, qty, sl, *target = [x.strip() for x in text.split(",")]
        
        tracked_stocks.insert_one({
            "symbol": symbol.upper(),
            "entry_price": float(entry),
            "qty": int(qty),
            "sl": float(sl),
            "target": float(target[0]) if target else None,
            "detail": "tracking",
            "created_at": datetime.datetime.now()
        })
        
        await update.message.reply_text(
            f"✅ Added {symbol.upper()}:\n"
            f"Entry: ₹{entry} | Qty: {qty}\n"
            f"SL: ₹{sl} | Target: ₹{target[0] if target else 'Not Set'}"
        )
        return ConversationHandler.END
        
    except Exception as e:
        logger.error(f"Add stock failed: {e}")
        await update.message.reply_text("❌ Invalid format. Use: SYMBOL, ENTRY, QTY, SL, [TARGET]")
        return ADD_STOCK

async def delete_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle stock deletion"""
    symbol = update.message.text.strip().upper()
    result = tracked_stocks.delete_one({"symbol": symbol})
    
    if result.deleted_count > 0:
        await update.message.reply_text(f"🗑️ Deleted {symbol} from tracking")
    else:
        await update.message.reply_text(f"❌ {symbol} not found in tracking")
    
    return ConversationHandler.END

async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display current balance"""
    bal = balance.find_one(sort=[("_id", -1)]) or {"balance": 0}
    await update.message.reply_text(f"💰 Current Balance: ₹{bal['balance']:,}")
    return ConversationHandler.END

def get_price(symbol, max_retries=3):
    """Robust price fetching with retries"""
    symbol = symbol if symbol.endswith(".NS") else f"{symbol}.NS"
    for attempt in range(max_retries):
        try:
            ticker = yf.Ticker(symbol)
            data = ticker.history(period='1d', interval='1m')
            if not data.empty:
                return round(float(data['Close'].iloc[-1]), 2)
        except Exception as e:
            if attempt == max_retries - 1:
                logger.error(f"Price fetch failed for {symbol}: {e}")
            continue
    return None

# --- Trading Operations ---
async def execute_trade(symbol, entry, qty, sl, target, price, reason):
    """Atomic trade execution with error handling"""
    try:
        pnl = (price - entry) * qty
        current_bal = balance.find_one(sort=[("_id", -1)]) or {"balance": 0}
        new_balance = current_bal["balance"] + (price * qty)
        
        trade_data = {
            "symbol": symbol,
            "entry_price": entry,
            "qty": qty,
            "sl": sl,
            "target": target,
            "exit_price": price,
            "pnl": pnl,
            "entry_time": datetime.datetime.now(),
            "exit_time": datetime.datetime.now(),
            "balance_after": new_balance,
            "status": reason,
            "detail": "sold"
        }
        
        trade_logs.insert_one(trade_data)
        tracked_stocks.delete_one({"symbol": symbol})
        update_balance(new_balance)
        
        logger.info(f"Trade executed: {symbol} {reason} at {price}")
        return True
    except Exception as e:
        logger.error(f"Trade execution failed for {symbol}: {e}")
        return False

async def check_and_execute(symbol, stock_data):
    """Enhanced execution logic"""
    try:
        symbol_ns = symbol if symbol.endswith(".NS") else f"{symbol}.NS"
        price = get_price(symbol_ns)
        
        if price is None:
            logger.warning(f"No price data for {symbol_ns}")
            return False
            
        entry = stock_data['entry_price']
        qty = stock_data['qty']
        sl = stock_data['sl']
        target = stock_data.get('target')
        status = stock_data.get('detail', 'tracking')
        
        ticker = yf.Ticker(symbol_ns)
        data = ticker.history(period='5m', interval='1m')
        
        if data.empty:
            logger.warning(f"No recent data for {symbol_ns}")
            return False
            
        recent_low = data['Low'].min()
        recent_high = data['High'].max()
        current_price = data['Close'].iloc[-1]
        
        # Entry logic
        if status == "tracking":
            if recent_low <= entry <= recent_high:
                tracked_stocks.update_one(
                    {"symbol": symbol_ns},
                    {"$set": {"detail": "holding"}}
                )
                logger.info(f"Entry triggered for {symbol_ns} at ~{entry}")
                return True
                
        # Exit logic
        elif status == "holding":
            if recent_low <= sl or current_price <= sl:
                return await execute_trade(symbol_ns, entry, qty, sl, target, sl, "SL Hit")
            elif target and (recent_high >= target or current_price >= target):
                return await execute_trade(symbol_ns, entry, qty, sl, target, target, "Target Hit")
                
        return False
        
    except Exception as e:
        logger.error(f"Error in check_and_execute for {symbol}: {e}")
        return False

# --- Portfolio Display ---
async def portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enhanced portfolio display"""
    try:
        today = datetime.datetime.now()
        today_str = today.strftime("%d %b %Y")
        
        # Fetch data
        holdings = list(tracked_stocks.find({"detail": "holding"}))
        tracking = list(tracked_stocks.find({"detail": "tracking"}))
        recent_closed = list(trade_logs.find({"detail": "sold"})
                           .sort("exit_time", -1)
                           .limit(5))
        
        # Calculate values
        current_balance = balance.find_one(sort=[("_id", -1)]) or {"balance": 0}
        holdings_value = sum(
            get_price(h['symbol']) * h['qty'] 
            for h in holdings 
            if get_price(h['symbol'])
        )
        net_worth = current_balance["balance"] + holdings_value
        
        # Calculate P&L
        today_pnl_cursor = trade_logs.aggregate([
            {"$match": {"date": today.strftime("%Y-%m-%d")}},
            {"$group": {"_id": None, "total": {"$sum": "$pnl"}}}
        ])
        today_pnl = next(today_pnl_cursor, {}).get("total", 0)
        
        overall_pnl_cursor = trade_logs.aggregate([
            {"$group": {"_id": None, "total": {"$sum": "$pnl"}}}
        ])
        overall_pnl = next(overall_pnl_cursor, {}).get("total", 0)
        
        # Build portfolio message
        message = [
            f"📊 PORTFOLIO SUMMARY • {today_str}",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"💰 Net Worth: ₹{net_worth:,}  •  📈 Today's P&L: ₹{today_pnl:,}",
            f"🏆 Total Profit: ₹{overall_pnl:,}",
            ""
        ]
        
        # Holdings section
        if holdings:
            message.extend([
                f"✅ HOLDINGS ({len(holdings)})",
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            ])
            for h in holdings:
                ltp = get_price(h['symbol'])
                if ltp:
                    change_pct = (ltp - h['entry_price']) / h['entry_price'] * 100
                    sl_pct = (h['sl'] - ltp) / ltp * 100
                    target_pct = (h.get('target', ltp) - ltp) / ltp * 100 if h.get('target') else 0
                    
                    emoji = "🟢" if change_pct >= 0 else "🔴"
                    message.extend([
                        f"{emoji} {h['symbol'].replace('.NS','')}  •  ₹{ltp:,} ({change_pct:+.2f}%)",
                        f"   ├─ Entry: ₹{h['entry_price']:,}  •  Qty: {h['qty']}",
                        f"   ├─ Invested: ₹{h['entry_price']*h['qty']:,}  •  Value: ₹{ltp*h['qty']:,}",
                        f"   ├─ SL: ₹{h['sl']:,} ({sl_pct:.2f}%)  •  Target: ₹{h.get('target','N/A')} ({target_pct:+.2f}%)",
                        ""
                    ])
        
        # Tracking section
        if tracking:
            message.extend([
                f"⏳ TRACKING ({len(tracking)})",
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            ])
            for t in tracking:
                ltp = get_price(t['symbol'])
                if ltp:
                    change_pct = (ltp - t['entry_price']) / t['entry_price'] * 100
                    sl_pct = (t['sl'] - ltp) / ltp * 100
                    target_pct = (t.get('target', ltp) - ltp) / ltp * 100 if t.get('target') else 0
                    
                    message.extend([
                        f"🟡 {t['symbol'].replace('.NS','')}  •  ₹{ltp:,} ({change_pct:+.2f}%)",
                        f"   ├─ Entry: ₹{t['entry_price']:,}  •  Qty: {t['qty']}",
                        f"   ├─ Invested: ₹{t['entry_price']*t['qty']:,}  •  Value: ₹{ltp*t['qty']:,}",
                        f"   ├─ SL: ₹{t['sl']:,} ({sl_pct:.2f}%)  •  Target: ₹{t.get('target','N/A')} ({target_pct:+.2f}%)",
                        ""
                    ])
        
        # Recent closed trades
        if recent_closed:
            message.extend([
                f"🗓 RECENT CLOSED ({len(recent_closed)})",
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            ])
            for trade in recent_closed:
                pnl_pct = (trade['exit_price'] - trade['entry_price']) / trade['entry_price'] * 100
                emoji = "🟢" if trade['pnl'] >= 0 else "🔴"
                message.append(
                    f"{emoji} {trade['symbol'].replace('.NS','')}  •  "
                    f"Closed at ₹{trade['exit_price']:,}  •  "
                    f"P&L: ₹{trade['pnl']:+,} ({pnl_pct:+.2f}%)"
                )
        
        await update.message.reply_text("\n".join(message))
        
    except Exception as e:
        logger.error(f"Portfolio display error: {e}")
        await update.message.reply_text("❌ Error generating portfolio. Please try again.")

# --- Keep Alive Function ---
async def keep_alive():
    """Pings the app every 5 minutes to prevent Render sleep"""
    while True:
        try:
            requests.get(f"https://{APP_URL}/ping")
            logger.info("Keep-alive ping sent")
        except Exception as e:
            logger.error(f"Keep-alive failed: {e}")
        await asyncio.sleep(300)  # 5 minutes < Render's 15-minute timeout

# --- Startup Function ---
async def on_startup(application: Application):
    """Startup tasks"""
    # Start the keep-alive task
    application.create_task(keep_alive())
    
    # Set webhook
    await application.bot.set_webhook(f"{APP_URL}/{BOT_TOKEN}")
    logger.info("Webhook set and keep-alive started")

# --- Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["Balance", "Add Stock"],
        ["Portfolio", "Delete Stock"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "📈 Welcome to Paper Trading Bot\n\nSelect an action:",
        reply_markup=reply_markup
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Operation cancelled.")
    return ConversationHandler.END

# --- Main Application ---
# --- Main Application ---
def main():
    # Initialize bot
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Register command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("balance", show_balance))
    application.add_handler(CommandHandler("portfolio", portfolio))
    
    # Conversation handler for add/delete stocks
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("add", lambda update, context: update.message.reply_text(
                "Enter stock details:\nSYMBOL, ENTRY, QTY, SL, [TARGET]\n"
                "Example: RELIANCE, 2800, 5, 2750, 2900"
            ) or ADD_STOCK)
        ],
        states={
            ADD_STOCK: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_stock)],
            DELETE_STOCK: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_stock)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    application.add_handler(conv_handler)
    
    # Production (Render) - Webhook mode
    if os.getenv('ENVIRONMENT') == 'production':
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=f"{APP_URL}/{BOT_TOKEN}",
            cert='cert.pem' if os.path.exists('cert.pem') else None
        )
    # Development - Polling mode
    else:
        application.run_polling()

if __name__ == '__main__':
    main()