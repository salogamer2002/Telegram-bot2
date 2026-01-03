import os
import csv
import math
import asyncio
from datetime import datetime, date
import logging
import traceback
import time

import yfinance as yf
import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters
)
from telegram.request import HTTPXRequest

# ============================================
# TELEGRAM SETTINGS
# ============================================
TELEGRAM_TOKEN = "8449892769:AAEmsjdc33Mk4gvc97Vcm8J5lA4uWnYJP2Q"
CHAT_ID = "7231998450"
KEYWORD = "ابدا"
STORAGE_FILE = "sent_alerts.csv"

# ============================================
# PROXY SETTINGS
# ============================================
# Set USE_PROXY = True to enable proxy
USE_PROXY = False  # Change to True if you want to use proxy

# Choose your proxy type and URL:
# For HTTP proxy: "http://127.0.0.1:8080"
# For SOCKS5 proxy: "socks5://127.0.0.1:1080"
# For Cloudflare WARP: "http://127.0.0.1:40000"
PROXY_URL = "http://127.0.0.1:8080"
# ============================================

# Rate limiting and scanning settings
RATE_LIMIT_DELAY = 5
last_message_time = 0
SCAN_DELAY = 0.5
MAX_EXPIRATIONS = 2
MAX_OPTIONS_PER_SYMBOL = 5

# Cache settings
CACHE_DURATION = 300
price_cache = {}
options_cache = {}
indicators_cache = {}

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# User settings storage
user_contract_type = {}
user_volume_settings = {}
user_states = {}
user_option_type = {}

# Scan criteria
SCAN_CRITERIA = {
    "min_volume": 10000,
    "min_volume_oi_ratio": 1.5,
    "max_strike_distance": 0.10
}

# Symbols to scan
SYMBOLS_TO_SCAN = ["GLD", "GDX", "SPX", "XLE", "USO", "DIA", "NVDA", "AAPL", "TSLA", "UNG", "SPY", "QQQ", "DAX"]

def create_storage_file():
    if not os.path.exists(STORAGE_FILE):
        with open(STORAGE_FILE, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=["symbol", "strike", "date", "contract_type"])
            writer.writeheader()

def reset_if_new_day():
    try:
        if os.path.exists(STORAGE_FILE):
            today_str = str(date.today())
            with open(STORAGE_FILE, 'r', newline='', encoding='utf-8') as infile:
                reader = csv.DictReader(infile)
                rows = [row for row in reader if row["date"] == today_str]
            with open(STORAGE_FILE, 'w', newline='', encoding='utf-8') as outfile:
                writer = csv.DictWriter(outfile, fieldnames=["symbol", "strike", "date", "contract_type"])
                writer.writeheader()
                writer.writerows(rows)
    except Exception as e:
        logger.error(f"Error in reset_if_new_day: {e}")

def store_sent_alert(symbol, strike, contract_type):
    try:
        if not os.path.exists(STORAGE_FILE):
            with open(STORAGE_FILE, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=["symbol", "strike", "date", "contract_type"])
                writer.writeheader()
        
        if is_alert_already_sent(symbol, strike, contract_type):
            logger.info(f"تنبيه مكرر - لم يتم التخزين: {symbol} {strike} {contract_type}")
            return True
        
        with open(STORAGE_FILE, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=["symbol", "strike", "date", "contract_type"])
            writer.writerow({
                "symbol": symbol,
                "strike": str(strike),
                "date": str(date.today()),
                "contract_type": contract_type
            })
        logger.info(f"تم تخزين التنبيه بنجاح: {symbol} {strike} {contract_type}")
        return True
    except Exception as e:
        logger.error(f"Error saving sent alert: {e}")
        return False

def is_alert_already_sent(symbol, strike, contract_type):
    if not os.path.exists(STORAGE_FILE):
        return False
    today_str = str(date.today())
    try:
        with open(STORAGE_FILE, 'r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if (row["symbol"] == symbol and 
                    float(row["strike"]) == float(strike) and
                    row["date"] == today_str and
                    row["contract_type"] == contract_type):
                    logger.info(f"تم العثور على تنبيه سابق: {symbol} {strike} {contract_type}")
                    return True
    except Exception as e:
        logger.error(f"Error reading storage file: {e}")
    return False

async def send_telegram_message(message):
    global last_message_time
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    
    try:
        current_time = time.time()
        time_since_last_message = current_time - last_message_time
        
        if time_since_last_message < RATE_LIMIT_DELAY:
            wait_time = RATE_LIMIT_DELAY - time_since_last_message
            await asyncio.sleep(wait_time)
        
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status == 429:
                    retry_after = int(resp.headers.get('Retry-After', RATE_LIMIT_DELAY))
                    await asyncio.sleep(retry_after)
                    async with session.post(url, json=payload) as retry_resp:
                        if retry_resp.status != 200:
                            return False
                        last_message_time = time.time()
                        return True
                elif resp.status != 200:
                    return False
                
                last_message_time = time.time()
                return True
                
    except Exception as e:
        logger.error(f"Error sending telegram message: {e}")
        return False

def is_valid_number(val):
    return val is not None and not (isinstance(val, float) and math.isnan(val))

async def get_market_indicators():
    """Fetch SPX and NDX market indicators"""
    try:
        current_time = time.time()
        
        # Check cache
        if 'indicators' in indicators_cache:
            cache_time, cache_data = indicators_cache['indicators']
            if current_time - cache_time < CACHE_DURATION:
                logger.debug("استخدام مؤشرات السوق المخزنة مؤقتاً")
                return cache_data
        
        # Fetch SPX data
        spx = yf.Ticker("^GSPC")  # S&P 500 Index
        spx_hist = spx.history(period="5d")
        
        if not spx_hist.empty:
            spx_current = spx_hist['Close'].iloc[-1]
            spx_prev = spx_hist['Close'].iloc[-2] if len(spx_hist) > 1 else spx_current
            spx_change = spx_current - spx_prev
            spx_change_pct = (spx_change / spx_prev) * 100
        else:
            spx_current = spx_change = spx_change_pct = None
        
        # Fetch NDX data
        ndx = yf.Ticker("^NDX")  # NASDAQ 100 Index
        ndx_hist = ndx.history(period="5d")
        
        if not ndx_hist.empty:
            ndx_current = ndx_hist['Close'].iloc[-1]
            ndx_prev = ndx_hist['Close'].iloc[-2] if len(ndx_hist) > 1 else ndx_current
            ndx_change = ndx_current - ndx_prev
            ndx_change_pct = (ndx_change / ndx_prev) * 100
        else:
            ndx_current = ndx_change = ndx_change_pct = None
        
        indicators = {
            'spx': {
                'price': spx_current,
                'change': spx_change,
                'change_pct': spx_change_pct
            },
            'ndx': {
                'price': ndx_current,
                'change': ndx_change,
                'change_pct': ndx_change_pct
            }
        }
        
        # Cache the results
        indicators_cache['indicators'] = (current_time, indicators)
        
        return indicators
        
    except Exception as e:
        logger.error(f"خطأ في تحميل مؤشرات السوق: {e}")
        return None

def format_indicators_message(indicators):
    """Format market indicators into a readable message"""
    if not indicators:
        return ""
    
    try:
        spx = indicators['spx']
        ndx = indicators['ndx']
        
        message = "\n\n📊 *مؤشرات السوق:*\n"
        
        # SPX formatting
        if spx['price']:
            spx_emoji = "🟢" if spx['change'] >= 0 else "🔴"
            spx_sign = "+" if spx['change'] >= 0 else ""
            message += (
                f"{spx_emoji} *SPX:* `{spx['price']:.2f}` "
                f"({spx_sign}{spx['change']:.2f} / {spx_sign}{spx['change_pct']:.2f}%`)\n"
            )
        
        # NDX formatting
        if ndx['price']:
            ndx_emoji = "🟢" if ndx['change'] >= 0 else "🔴"
            ndx_sign = "+" if ndx['change'] >= 0 else ""
            message += (
                f"{ndx_emoji} *NDX:* `{ndx['price']:.2f}` "
                f"({ndx_sign}{ndx['change']:.2f} / {ndx_sign}{ndx['change_pct']:.2f}%`)\n"
            )
        
        return message
        
    except Exception as e:
        logger.error(f"خطأ في تنسيق رسالة المؤشرات: {e}")
        return ""

async def show_indicators(update: Update, context):
    """Show current market indicators"""
    try:
        if hasattr(update, 'message') and update.message:
            message_obj = update.message
        elif hasattr(update, 'callback_query') and update.callback_query:
            message_obj = update.callback_query.message
        else:
            return
        
        await message_obj.reply_text("⏳ جارٍ تحميل مؤشرات السوق...")
        
        indicators = await get_market_indicators()
        
        if indicators:
            spx = indicators['spx']
            ndx = indicators['ndx']
            
            spx_emoji = "🟢" if spx['change'] >= 0 else "🔴"
            ndx_emoji = "🟢" if ndx['change'] >= 0 else "🔴"
            spx_sign = "+" if spx['change'] >= 0 else ""
            ndx_sign = "+" if ndx['change'] >= 0 else ""
            
            message = (
                "📊 *مؤشرات السوق الحالية*\n\n"
                f"{spx_emoji} *S&P 500 (SPX)*\n"
                f"• السعر: `{spx['price']:.2f}`\n"
                f"• التغير: `{spx_sign}{spx['change']:.2f}` (`{spx_sign}{spx['change_pct']:.2f}%`)\n\n"
                f"{ndx_emoji} *NASDAQ 100 (NDX)*\n"
                f"• السعر: `{ndx['price']:.2f}`\n"
                f"• التغير: `{ndx_sign}{ndx['change']:.2f}` (`{ndx_sign}{ndx['change_pct']:.2f}%`)\n\n"
                f"🕐 آخر تحديث: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
        else:
            message = "⚠️ تعذر تحميل مؤشرات السوق. يرجى المحاولة لاحقاً."
        
        await message_obj.reply_text(message, parse_mode='Markdown')
        await show_main_menu(update, context)
        
    except Exception as e:
        logger.error(f"خطأ في show_indicators: {e}")

async def fetch_symbol_data(symbol):
    try:
        current_time = time.time()
        
        if symbol in price_cache:
            cache_time, cache_data = price_cache[symbol]
            if current_time - cache_time < CACHE_DURATION:
                logger.debug(f"استخدام السعر المخزن مؤقتاً لـ {symbol}")
                return cache_data['price'], cache_data['expirations']
        
        ticker = yf.Ticker(symbol)
        info = ticker.info
        current_price = info.get('regularMarketPrice')
        
        if not current_price:
            price_data = ticker.history(period="1d")
            if price_data.empty:
                return None, None
            current_price = price_data['Close'].iloc[-1]
        
        expirations = ticker.options
        
        price_cache[symbol] = (
            current_time,
            {
                'price': current_price,
                'expirations': expirations
            }
        )
        
        return current_price, expirations
        
    except Exception as e:
        logger.error(f"خطأ في تحميل بيانات {symbol}: {e}")
        return None, None

async def fetch_options_chain(symbol, exp_date, contract_type):
    try:
        cache_key = f"{symbol}_{exp_date}_{contract_type}"
        current_time = time.time()
        
        if cache_key in options_cache:
            cache_time, cache_data = options_cache[cache_key]
            if current_time - cache_time < CACHE_DURATION:
                logger.debug(f"استخدام بيانات الخيارات المخزنة مؤقتاً لـ {symbol}")
                return cache_data
        
        ticker = yf.Ticker(symbol)
        chain = ticker.option_chain(exp_date)
        options = chain.puts if contract_type == "put" else chain.calls
        
        options_cache[cache_key] = (current_time, options)
        
        return options
        
    except Exception as e:
        logger.error(f"خطأ في تحميل سلسلة الخيارات لـ {symbol}: {e}")
        return None

async def check_unusual_activity(symbols, contract_type, criteria=None, user_id=None):
    if criteria is None:
        criteria = SCAN_CRITERIA
    
    logger.info(f"بدء عملية الفحص للنشاط غير الاعتيادي (عدد العقود: {MAX_OPTIONS_PER_SYMBOL})")
    reset_if_new_day()
    alerts_found = False
    
    # Get market indicators
    indicators = await get_market_indicators()
    indicators_text = format_indicators_message(indicators)
    
    tasks = [fetch_symbol_data(symbol) for symbol in symbols]
    results = await asyncio.gather(*tasks)
    
    for symbol, (current_price, expirations) in zip(symbols, results):
        if current_price is None or not expirations:
            logger.warning(f"تخطي {symbol} - لا توجد بيانات كافية")
            continue
            
        logger.info(f"جارٍ فحص الرمز: {symbol}")
        options_processed = 0
        
        if user_option_type.get(user_id) == "daily":
            expirations = expirations[:2]
        else:
            weekly_expirations = [exp for exp in expirations if datetime.strptime(exp, '%Y-%m-%d').weekday() == 4]
            expirations = weekly_expirations[:2]
        
        option_tasks = [fetch_options_chain(symbol, exp_date, contract_type) for exp_date in expirations]
        option_chains = await asyncio.gather(*option_tasks)
        
        for exp_date, options in zip(expirations, option_chains):
            if options is None or options.empty:
                continue
                
            valid_options = options[
                (options['volume'] > criteria["min_volume"]) &
                (options['openInterest'] > 0)
            ]
            
            if valid_options.empty:
                continue
            
            for _, row in valid_options.iterrows():
                if options_processed >= MAX_OPTIONS_PER_SYMBOL:
                    break
                    
                try:
                    volume = row['volume']
                    open_interest = row['openInterest']
                    strike = row['strike']
                    
                    ratio = volume / open_interest
                    strike_distance = abs(strike - current_price) / current_price
                    
                    if (ratio > criteria["min_volume_oi_ratio"] and
                        strike_distance < criteria["max_strike_distance"]):
                        
                        if is_alert_already_sent(symbol, strike, contract_type):
                            continue
                            
                        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        message = (
                            f"🚨 *تنبيه نشاط غير اعتيادي*\n\n"
                            f"📊 *تفاصيل العقد:*\n"
                            f"• *الرمز:* `{symbol}`\n"
                            f"• *النوع:* `{contract_type.upper()}`\n"
                            f"• *Strike:* `{strike:.2f}`\n"
                            f"• *السعر الحالي:* `{current_price:.2f}`\n"
                            f"• *المسافة:* `{strike_distance:.2%}`\n\n"
                            f"📈 *مؤشرات النشاط:*\n"
                            f"• *الحجم:* `{volume:,}`\n"
                            f"• *العقود المفتوحة:* `{open_interest:,}`\n"
                            f"• *نسبة الحجم/OI:* `{ratio:.2f}`\n\n"
                            f"📅 *معلومات إضافية:*\n"
                            f"• *تاريخ الانتهاء:* `{exp_date}`\n"
                            f"• *وقت الاكتشاف:* `{now}`\n"
                            f"{indicators_text}\n"
                            f"⚙️ *معايير الفحص:*\n"
                            f"• الحد الأدنى للنسبة: `{criteria['min_volume_oi_ratio']}`\n"
                            f"• الحد الأدنى للحجم: `{criteria['min_volume']:,}`\n"
                            f"• أقصى مسافة: `{criteria['max_strike_distance']:.2%}`"
                        )
                        
                        success = await send_telegram_message(message)
                        if success:
                            if store_sent_alert(symbol, strike, contract_type):
                                alerts_found = True
                                options_processed += 1
                                await asyncio.sleep(SCAN_DELAY)
                                
                except Exception as e:
                    logger.error(f"خطأ في معالجة صف الخيار: {e}")
                    continue
                    
    logger.info("انتهت عملية الفحص")
    return alerts_found

async def start_scan(update, context):
    create_storage_file()
    if hasattr(update, 'message') and update.message:
        user_id = update.message.from_user.id
        message_obj = update.message
    elif hasattr(update, 'callback_query') and update.callback_query:
        user_id = update.callback_query.from_user.id
        message_obj = update.callback_query.message
    else:
        logger.warning("لم يتم العثور على message أو callback_query في update")
        return
    
    if user_id not in user_contract_type:
        await message_obj.reply_text("❌ الرجاء اختيار نوع العقد أولاً (CALL/PUT)")
        await show_main_menu(update, context)
        return
        
    if user_id not in user_option_type:
        await message_obj.reply_text("❌ الرجاء اختيار نوع العقود (يومي/أسبوعي)")
        await show_main_menu(update, context)
        return
    
    contract_type = user_contract_type.get(user_id, "call")
    option_type = user_option_type.get(user_id, "daily")
    volume_settings = user_volume_settings.get(user_id, SCAN_CRITERIA)
    
    temp_criteria = SCAN_CRITERIA.copy()
    temp_criteria.update(volume_settings)
    
    logger.info(f"بدء الفحص بواسطة المستخدم {user_id} لنوع العقد {contract_type} ({option_type}) مع إعدادات الحجم {volume_settings}")
    
    await message_obj.reply_text(
        f"🔍 جارٍ فحص النشاط غير الاعتيادي لعقود {contract_type.upper()} ({option_type})...\n"
        f"⚙️ إعدادات الحجم: الحد الأدنى {volume_settings['min_volume']}\n"
        f"📊 عدد العقود لكل رمز: {MAX_OPTIONS_PER_SYMBOL}"
    )
    
    alerts_found = await check_unusual_activity(SYMBOLS_TO_SCAN, contract_type, temp_criteria, user_id)
    response = ("✅ تم العثور على نشاط غير اعتيادي وإرسال التنبيهات." if alerts_found 
                else "⚠️ لم يتم العثور على نشاط غير اعتيادي وفق المعايير الحالية.")
    await message_obj.reply_text(response)
    await show_main_menu(update, context)

async def handle_message(update: Update, context):
    try:
        message_text = update.message.text.strip()
        user_id = update.message.from_user.id

        if message_text.startswith('/'):
            message_text = message_text[1:]

        if message_text.lower() in ["ابدأ", "ابدا", "start"]:
            user_contract_type[user_id] = None
            user_option_type[user_id] = None
            user_volume_settings[user_id] = None
            user_states[user_id] = None
            
            welcome_message = (
                "🌟 *مرحباً بك في بوت فحص العقود* 🚀\n\n"
                "🤖 *مميزات البوت:*\n"
                "• فحص العقود بشكل مباشر\n"
                "• إعدادات حجم مخصصة\n"
                "• تنبيهات فورية\n"
                "• مؤشرات SPX و NDX\n\n"
                "📊 *اختر من القائمة أدناه:*"
            )
            
            keyboard = [
                [
                    InlineKeyboardButton("📊 نوع العقد", callback_data="select_contract"),
                    InlineKeyboardButton("⚙️ إعدادات الحجم", callback_data="volume_settings")
                ],
                [
                    InlineKeyboardButton("📈 مؤشرات السوق", callback_data="show_indicators"),
                    InlineKeyboardButton("▶️ بدء الفحص", callback_data="start_scan")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                welcome_message,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return

        if user_id in user_states:
            if user_states[user_id] == "waiting_for_volume":
                try:
                    if message_text.lower() == "رجوع":
                        user_states[user_id] = None
                        await show_volume_settings(update, context)
                        return
                    
                    min_volume = int(message_text)
                    if min_volume < 1000:
                        await update.message.reply_text("❌ الحد الأدنى للحجم يجب أن يكون 1000 على الأقل")
                        return
                    
                    user_volume_settings[user_id] = {
                        "min_volume": min_volume,
                        "min_volume_oi_ratio": 1.0
                    }
                    user_states[user_id] = None
                    await update.message.reply_text(f"✅ تم تعيين الحد الأدنى للحجم إلى {min_volume}")
                    await show_main_menu(update, context)
                except ValueError:
                    await update.message.reply_text("❌ الرجاء إدخال رقم صحيح")
    except Exception as e:
        logger.error(f"خطأ في handle_message: {e}")

async def show_main_menu(update: Update, context):
    try:
        keyboard = [
            [
                InlineKeyboardButton("📊 نوع العقد", callback_data="select_contract"),
                InlineKeyboardButton("⚙️ إعدادات الحجم", callback_data="volume_settings")
            ],
            [
                InlineKeyboardButton("📈 مؤشرات السوق", callback_data="show_indicators"),
                InlineKeyboardButton("▶️ بدء الفحص", callback_data="start_scan")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        menu_message = (
            "🌟 *مرحباً بك في بوت فحص العقود* 🚀\n\n"
            "🤖 *مميزات البوت:*\n"
            "• فحص العقود بشكل مباشر\n"
            "• إعدادات حجم مخصصة\n"
            "• تنبيهات فورية\n"
            "• مؤشرات SPX و NDX\n\n"
            "📊 *اختر من القائمة أدناه:*"
        )

        if hasattr(update, 'callback_query'):
            await update.callback_query.edit_message_text(
                menu_message,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                menu_message,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
    except Exception as e:
        logger.error(f"خطأ في show_main_menu: {e}")

async def show_contract_selection(update: Update, context):
    try:
        keyboard = [
            [
                InlineKeyboardButton("📈 CALL", callback_data="contract_call"),
                InlineKeyboardButton("📉 PUT", callback_data="contract_put")
            ],
            [InlineKeyboardButton("↩️ رجوع", callback_data="back_to_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if hasattr(update, 'callback_query'):
            await update.callback_query.edit_message_text(
                "📊 اختر نوع العقد:",
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_text(
                "📊 اختر نوع العقد:",
                reply_markup=reply_markup
            )
    except Exception as e:
        logger.error(f"خطأ في show_contract_selection: {e}")

async def handle_contract_selection(update: Update, context):
    try:
        query = update.callback_query
        user_id = query.from_user.id
        
        if query.data == "contract_call":
            user_contract_type[user_id] = "call"
            await query.answer("✅ تم اختيار عقود CALL")
            await show_option_type_menu(update, context)
        elif query.data == "contract_put":
            user_contract_type[user_id] = "put"
            await query.answer("✅ تم اختيار عقود PUT")
            await show_option_type_menu(update, context)
        elif query.data == "back_to_main":
            await show_main_menu(update, context)
    except Exception as e:
        logger.error(f"خطأ في handle_contract_selection: {e}")

async def show_option_type_menu(update: Update, context):
    try:
        keyboard = [
            [
                InlineKeyboardButton("📅 يومي", callback_data="option_daily"),
                InlineKeyboardButton("📅 أسبوعي", callback_data="option_weekly")
            ],
            [InlineKeyboardButton("↩️ رجوع", callback_data="back_to_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if hasattr(update, 'callback_query'):
            await update.callback_query.edit_message_text(
                "📅 اختر نوع العقود:",
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_text(
                "📅 اختر نوع العقود:",
                reply_markup=reply_markup
            )
    except Exception as e:
        logger.error(f"خطأ في show_option_type_menu: {e}")

async def handle_option_type_selection(update: Update, context):
    try:
        query = update.callback_query
        user_id = query.from_user.id
        
        if query.data == "option_daily":
            user_option_type[user_id] = "daily"
            await query.answer("✅ تم اختيار العقود اليومية")
            await show_main_menu(update, context)
        elif query.data == "option_weekly":
            user_option_type[user_id] = "weekly"
            await query.answer("✅ تم اختيار العقود الأسبوعية")
            await show_main_menu(update, context)
        elif query.data == "back_to_main":
            await show_main_menu(update, context)
    except Exception as e:
        logger.error(f"خطأ في handle_option_type_selection: {e}")

async def show_volume_settings(update: Update, context):
    try:
        keyboard = [
            [
                InlineKeyboardButton("🟢 عالي (20000+)", callback_data="volume_high"),
                InlineKeyboardButton("🟡 متوسط (10000-20000)", callback_data="volume_medium")
            ],
            [
                InlineKeyboardButton("🔴 منخفض (5000-10000)", callback_data="volume_low"),
                InlineKeyboardButton("⚙️ مخصص", callback_data="volume_custom")
            ],
            [InlineKeyboardButton("↩️ رجوع", callback_data="back_to_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        volume_message = (
            "⚙️ *إعدادات الحجم*\n\n"
            "📊 *اختر مستوى الحجم المطلوب:*\n\n"
            "🟢 *عالي:* 20000+ عقود\n"
            "🟡 *متوسط:* 10000-20000 عقود\n"
            "🔴 *منخفض:* 5000-10000 عقود\n"
            "⚙️ *مخصص:* تحديد قيمة مخصصة\n\n"
            "💡 *ملاحظة:* كلما زاد الحجم، زادت دقة النتائج"
        )
        
        if hasattr(update, 'callback_query'):
            await update.callback_query.edit_message_text(
                volume_message,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                volume_message,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
    except Exception as e:
        logger.error(f"خطأ في show_volume_settings: {e}")

async def handle_volume_selection(update: Update, context):
    try:
        query = update.callback_query
        user_id = query.from_user.id
        
        if query.data == "volume_settings":
            await show_volume_settings(update, context)
        elif query.data == "volume_high":
            user_volume_settings[user_id] = {"min_volume": 20000, "min_volume_oi_ratio": 2.0}
            await query.answer("✅ تم تعيين الحجم إلى مستوى عالي (20000+)")
            await show_main_menu(update, context)
        elif query.data == "volume_medium":
            user_volume_settings[user_id] = {"min_volume": 10000, "min_volume_oi_ratio": 1.5}
            await query.answer("✅ تم تعيين الحجم إلى مستوى متوسط (10000-20000)")
            await show_main_menu(update, context)
        elif query.data == "volume_low":
            user_volume_settings[user_id] = {"min_volume": 5000, "min_volume_oi_ratio": 1.2}
            await query.answer("✅ تم تعيين الحجم إلى مستوى منخفض (5000-10000)")
            await show_main_menu(update, context)
        elif query.data == "volume_custom":
            user_states[user_id] = "waiting_for_volume"
            await query.edit_message_text(
                "📝 أدخل الحد الأدنى للحجم المطلوب (مثال: 15000)\n"
                "أو اكتب 'رجوع' للعودة"
            )
        elif query.data == "back_to_main":
            await show_main_menu(update, context)
    except Exception as e:
        logger.error(f"خطأ في handle_volume_selection: {e}")

async def error_handler(update, context):
    logger.error(f"حدث خطأ: {context.error}")
    if hasattr(update, 'message') and update.message:
        await update.message.reply_text("⚠️ حدث خطأ أثناء المعالجة. يرجى المحاولة لاحقًا.")

async def start(update: Update, context):
    try:
        user_id = update.effective_user.id
        user_contract_type[user_id] = None
        user_option_type[user_id] = None
        user_volume_settings[user_id] = None
        user_states[user_id] = None
        
        welcome_message = (
            "🌟 *مرحباً بك في بوت فحص العقود* 🚀\n\n"
            "🤖 *مميزات البوت:*\n"
            "• فحص العقود بشكل مباشر\n"
            "• إعدادات حجم مخصصة\n"
            "• تنبيهات فورية\n"
            "• مؤشرات SPX و NDX\n\n"
            "📊 *اختر من القائمة أدناه:*"
        )
        
        keyboard = [
            [
                InlineKeyboardButton("📊 نوع العقد", callback_data="select_contract"),
                InlineKeyboardButton("⚙️ إعدادات الحجم", callback_data="volume_settings")
            ],
            [
                InlineKeyboardButton("📈 مؤشرات السوق", callback_data="show_indicators"),
                InlineKeyboardButton("▶️ بدء الفحص", callback_data="start_scan")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            welcome_message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"خطأ في start: {e}")

def main():
    """Main function with proxy support"""
    try:
        logger.info("بدء تشغيل البوت...")
        
        # Create application with or without proxy
        if USE_PROXY:
            logger.info(f"استخدام البروكسي: {PROXY_URL}")
            request = HTTPXRequest(
                proxy=PROXY_URL,
                connect_timeout=60.0,
                read_timeout=60.0,
                write_timeout=60.0,
                pool_timeout=60.0
            )
            application = (
                Application.builder()
                .token(TELEGRAM_TOKEN)
                .request(request)
                .connect_timeout(60.0)
                .read_timeout(60.0)
                .write_timeout(60.0)
                .pool_timeout(60.0)
                .build()
            )
        else:
            logger.info("تشغيل بدون بروكسي")
            application = (
                Application.builder()
                .token(TELEGRAM_TOKEN)
                .connect_timeout(60.0)
                .read_timeout(60.0)
                .write_timeout(60.0)
                .pool_timeout(60.0)
                .build()
            )
        
        # Add handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        application.add_handler(CallbackQueryHandler(show_contract_selection, pattern="^select_contract$"))
        application.add_handler(CallbackQueryHandler(handle_contract_selection, pattern="^contract_"))
        application.add_handler(CallbackQueryHandler(handle_option_type_selection, pattern="^option_"))
        application.add_handler(CallbackQueryHandler(handle_volume_selection, pattern="^volume_"))
        application.add_handler(CallbackQueryHandler(start_scan, pattern="^start_scan$"))
        application.add_handler(CallbackQueryHandler(show_indicators, pattern="^show_indicators$"))
        application.add_handler(CallbackQueryHandler(show_main_menu, pattern="^back_to_main$"))
        application.add_error_handler(error_handler)
        
        # Run bot
        logger.info("✅ البوت جاهز للعمل!")
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            timeout=60
        )
    except Exception as e:
        logger.error(f"❌ فشل تشغيل البوت: {e}")
        traceback.print_exc()

if __name__ == '__main__':
    main()