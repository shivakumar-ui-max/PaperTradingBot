from dotenv import load_dotenv
load_dotenv()

import os
import yfinance as yf
import time
import threading
import requests
from datetime import datetime
from pymongo import MongoClient
from telegram.ext import Updater, CommandHandler, CallbackContext, ConversationHandler, MessageHandler, Filters
from telegram import Update
from apscheduler.schedulers.background import BackgroundScheduler

# === ENV CONFIG ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
NEWS_API_KEY = os.getenv("NEWS_API_KEY")
MONGO_URI = os.getenv("MONGO_URI") or "mongodb://localhost:27017"

# === MONGO SETUP ===
client = MongoClient(MONGO_URI, tls=True, tlsAllowInvalidCertificates=True)


db = client["PaperTrade"]
logs_collection = db["TradeLogs"]
stocks_collection = db["TrackedStocks"]
balance_collection = db["Balance"]

# === STATES ===
ASK_BALANCE, ADD_SYMBOL, ADD_ENTRY, ADD_SL, ADD_QTY, ADD_TARGET = range(6)

# === DATA STRUCTURES ===
stocks = []
balance = {"value": 100000}
sent_messages = []
temp_stock = {}

# === TELEGRAM HELPER ===
def send_message(context, text):
    try:
        message = context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
        sent_messages.append(message.message_id)
    except Exception:
        pass

def trade_log(symbol, action, price, qty, pnl, reason, bal):
    try:
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
    except Exception as e:
        print(f"❌ Failed to log to MongoDB: {e}")

# === SENTIMENT CHECK ===
POSITIVE_KEYWORDS = ["gain", "surge", "rise", "increase", "record high", "profit", "growth"]
NEGATIVE_KEYWORDS = ["loss", "fall", "drop", "decline", "cut", "slump", "plunge"]

def sentiment_icon(text):
    text = text.lower()
    if any(w in text for w in POSITIVE_KEYWORDS): return "✅"
    if any(w in text for w in NEGATIVE_KEYWORDS): return "❌"
    return "➖"

# === NEWS FETCHER ===
def fetch_news(query):
    try:
        url = "https://newsapi.org/v2/top-headlines"
        params = {
            "q": query,
            "language": "en",
            "category": "business",
            "sortBy": "publishedAt",
            "apiKey": NEWS_API_KEY
        }
        response = requests.get(url, params=params).json()
        articles = response.get("articles", [])[:5]
        if not articles:
            return "📭 No relevant news found."

        message = f"📰 Top Business News for '{query}':\n\n"
        for art in articles:
            title = art.get("title", "")
            url = art.get("url", "")
            sentiment = sentiment_icon(title)
            message += f"{sentiment} {title}\n🔗 {url}\n\n"
        return message
    except Exception as e:
        return f"❌ Error fetching news: {e}"

# === COMMAND HANDLERS ===
def stock_news(update: Update, context: CallbackContext):
    msg = fetch_news("india")
    sent = update.message.reply_text(msg)
    sent_messages.append(sent.message_id)

def global_news(update: Update, context: CallbackContext):
    msg = fetch_news("global")
    sent = update.message.reply_text(msg)
    sent_messages.append(sent.message_id)

def view_balance(update: Update, context: CallbackContext):
    bal_text = f"💰 Current Balance: ₹{balance['value']:.2f}"
    if not stocks:
        msg = f"{bal_text}\n📉 You are not holding or tracking any stocks."
    else:
        msg = f"{bal_text}\n📊 You are currently tracking/holding {len(stocks)} stocks."
    sent = update.message.reply_text(msg)
    sent_messages.append(sent.message_id)

def ask_balance(update: Update, context: CallbackContext):
    update.message.reply_text("💸 Please enter the amount to set your balance:")
    return ASK_BALANCE

def receive_balance(update: Update, context: CallbackContext):
    try:
        amount = float(update.message.text.replace(",", "").strip())
        if amount <= 0:
            raise ValueError("Amount must be positive.")
        balance['value'] = amount
        balance_collection.delete_many({})
        balance_collection.insert_one({"value": amount})
        msg = f"✅ Your balance is now set to ₹{amount:,.2f}"
    except:
        msg = "❌ Invalid amount. Please enter a valid number."
    update.message.reply_text(msg)
    return ConversationHandler.END

def cancel(update: Update, context: CallbackContext):
    update.message.reply_text("❌ Operation cancelled.")
    return ConversationHandler.END

def add_stock_start(update: Update, context: CallbackContext):
    temp_stock.clear()
    update.message.reply_text("📌 Please enter the stock symbol (e.g., TCS.NS):")
    return ADD_SYMBOL

def add_stock_symbol(update: Update, context: CallbackContext):
    temp_stock["symbol"] = update.message.text.upper()
    update.message.reply_text("✏️ Enter Entry Price:")
    return ADD_ENTRY

def add_stock_entry(update: Update, context: CallbackContext):
    try:
        temp_stock["entry"] = float(update.message.text)
        update.message.reply_text("🛑 Enter Stop Loss:")
        return ADD_SL
    except:
        update.message.reply_text("❌ Invalid entry price. Please enter a number:")
        return ADD_ENTRY

def add_stock_sl(update: Update, context: CallbackContext):
    try:
        temp_stock["sl"] = float(update.message.text)
        update.message.reply_text("🎯 Enter Target Price (or type 'skip'):")
        return ADD_TARGET
    except:
        update.message.reply_text("❌ Invalid stop loss. Please enter a number:")
        return ADD_SL

def add_stock_target(update: Update, context: CallbackContext):
    text = update.message.text.strip().lower()
    if text == "skip":
        temp_stock["target"] = None
    else:
        try:
            temp_stock["target"] = float(text)
        except:
            update.message.reply_text("❌ Invalid target. Enter a number or 'skip':")
            return ADD_TARGET
    update.message.reply_text("📦 Enter Quantity:")
    return ADD_QTY

def add_stock_qty(update: Update, context: CallbackContext):
    try:
        temp_stock["qty"] = int(update.message.text)
        temp_stock["entry_price"] = None
        temp_stock["position"] = 0
        stocks.append(temp_stock.copy())
        stocks_collection.insert_one(temp_stock.copy())
        msg = (
            f"✅ Stock added successfully:\n"
            f"Symbol: {temp_stock['symbol']}\n"
            f"Entry: ₹{temp_stock['entry']} | SL: ₹{temp_stock['sl']} | Qty: {temp_stock['qty']}"
        )
        if temp_stock["target"]:
            msg += f"\nTarget: ₹{temp_stock['target']}"
        update.message.reply_text(msg)
        return ConversationHandler.END
    except:
        update.message.reply_text("❌ Invalid quantity. Please enter a number:")
        return ADD_QTY

def reset_stocks(update: Update, context: CallbackContext):
    stocks.clear()
    stocks_collection.delete_many({})
    msg = "♻️ All tracked stocks have been reset successfully."
    sent = update.message.reply_text(msg)
    sent_messages.append(sent.message_id)

def portfolio(update: Update, context: CallbackContext):
    try:
        if not stocks:
            msg = "📉 Your portfolio is empty. Use /addstock to track a stock."
        else:
            lines = ["📊 Your Portfolio:"]
            for stock in stocks:
                status = "🟢 Holding" if stock["position"] > 0 else "🕒 Tracking"
                lines.append(f"{status} {stock['symbol']} | Entry: ₹{stock['entry']} | SL: ₹{stock['sl']} | Qty: {stock['qty']}")
            msg = "\n".join(lines)
    except Exception as e:
        msg = f"❌ Error displaying portfolio: {e}"
    sent = update.message.reply_text(msg)
    sent_messages.append(sent.message_id)
def pnl_summary(update: Update, context: CallbackContext):
    try:
        logs = logs_collection.find()
        total_pnl = 0
        trades_exist = False

        for log in logs:
            if log["action"] != "SELL":
                continue
            trades_exist = True
            total_pnl += log.get("pnl", 0)

        if not trades_exist:
            msg = "📭 No trades yet.\n💡 Use /addstock to start trading and track your P&L."
        else:
            emoji = "✅" if total_pnl >= 0 else "❌"
            msg = f"{emoji} Your total P&L is ₹{total_pnl:.2f}"

    except Exception as e:
        msg = f"❌ Failed to get P&L summary: {e}"

    update.message.reply_text(msg)


def delete_old_messages(context):
    for msg_id in sent_messages:
        try:
            context.bot.delete_message(chat_id=TELEGRAM_CHAT_ID, message_id=msg_id)
        except:
            pass
    sent_messages.clear()

def track_stocks(bot):
    while True:
        for stock in stocks:
            try:
                data = yf.download(stock["symbol"], period="1d", interval="1m", progress=False)
                if data.empty: continue
                price = float(data["Close"].dropna().iloc[-1])

                if stock["position"] == 0 and price >= stock["entry"]:
                    cost = stock["qty"] * price
                    if balance["value"] < cost:
                        continue
                    stock["entry_price"] = price
                    stock["position"] = stock["qty"]
                    balance["value"] -= cost
                    send_message(bot, f"🟢 BUY {stock['symbol']} Qty: {stock['qty']} @ ₹{price:.2f}\nRemaining: ₹{balance['value']:.2f}")
                    trade_log(stock["symbol"], "BUY", price, stock["qty"], "", "ENTRY", balance["value"])

                elif stock["position"] > 0:
                    sell_reason = None
                    if price <= stock["sl"]:
                        sell_reason = "STOP LOSS"
                    elif stock["target"] and price >= stock["target"]:
                        sell_reason = "TARGET"

                    if sell_reason:
                        pnl = (price - stock["entry_price"]) * stock["qty"]
                        balance["value"] += price * stock["qty"]
                        stock["position"] = 0
                        send_message(bot, f"🔴 SELL {stock['symbol']} ({sell_reason}) @ ₹{price:.2f} | P&L: ₹{pnl:.2f}")
                        trade_log(stock["symbol"], "SELL", price, stock["qty"], pnl, sell_reason, balance["value"])
            except Exception as e:
                print(f"❌ Error in tracking {stock['symbol']}: {e}")
        time.sleep(60)

def main():
    updater = Updater(token=TELEGRAM_BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("news", stock_news))
    dp.add_handler(CommandHandler("global", global_news))
    dp.add_handler(CommandHandler("balance", view_balance))
    dp.add_handler(CommandHandler("reset", reset_stocks))
    dp.add_handler(CommandHandler("portfolio", portfolio))
    dp.add_handler(CommandHandler("pnl", pnl_summary))

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
    print("✅ Bot is running... Waiting for Telegram commands.")

        # Restore balance from DB
    last_balance = balance_collection.find_one()
    if last_balance:
        balance["value"] = float(last_balance["value"])

    # Restore tracked stocks
    stocks.clear()
    for doc in stocks_collection.find():
        stocks.append(doc)

    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
