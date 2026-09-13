import os
import sys
import re
import json
import time
import shutil
import threading
import subprocess
import logging
import http.server
import socketserver
import concurrent.futures
from datetime import datetime
from typing import Optional, Set, List, Dict

import requests

# ---------------------------------------------------------------------------
# رفع باگ «هیچ لاگی دیده نمی‌شه»: به‌صورت پیش‌فرض logging.basicConfig روی
# stderr می‌نویسه و stdout/stderr پایتون وقتی خروجی به فایل/پایپ هدایت می‌شه
# (مثلاً روی Render، Docker، یا با nohup) ممکنه بافر بشه و دیر/هیچ‌وقت flush
# نشه. اینجا صراحتاً stdout رو انتخاب می‌کنیم (اکثر پنل‌های لاگ هاستینگ فقط
# stdout رو نشون می‌دن) و هم stdout هم stderr رو به‌صورت خط‌به‌خط
# (line-buffered) reconfigure می‌کنیم تا هر لاگ بلافاصله همون لحظه فرستاده بشه.
# ---------------------------------------------------------------------------
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(line_buffering=True)
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger("multi_project_bot")

# ============================ تنظیمات ============================
# توکن ربات تلگرام
# رفع باگ امنیتی: قبلاً این توکن مستقیم و به‌صورت متن خام داخل سورس بود
# (نه از Environment Variable). اگه همین فایل جایی (گیت‌هاب، کانال، یه
# بستهٔ آماده که چندنفر گرفتن) پخش شده باشه، همون توکن قدیمی رو همه دارن —
# و همین باعث خطای "409 Conflict" روی getUpdates می‌شه، چون تلگرام فقط
# اجازهٔ یک polling هم‌زمان روی هر توکن رو می‌ده. حالا توکن هم مثل AI_API_KEY
# اول از env خونده می‌شه؛ ولی حتماً از @BotFather یه توکن تازه بگیر
# (/mybots -> API Token -> Revoke current token) و همون رو ست کن، چون
# توکن قدیمی که در نسخه‌های قبلی این فایل hardcode بود دیگه قابل‌اعتماد نیست.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8968690413:AAGugyR_VUKsOfJvLXX9adTX0g7z2c0dhR8")

# هوش مصنوعی: از طریق کتابخونه رسمی openai (پکیج pip openai) فراخوانی می‌شه؛
# این کتابخونه خودش endpoint رو مدیریت می‌کنه، فقط کلید API و اسم مدل لازمه
# (بدون نیاز به URL جدا). مقادیر اول از Environment Variable خونده می‌شن؛
# اگه ست نشده باشن، از مقدار پیش‌فرض (fallback) استفاده می‌شه.
AI_API_KEY = os.environ.get("AI_API_KEY", "sk-t19anutsf3jd4xj35xjc7t9pzhccj0ed")
AI_MODEL = os.environ.get("AI_MODEL", "qwen3.8-flash")

# پوشه‌ای که همه پروژه‌های پایتون (هر کدوم در یک زیرپوشه) داخلش هستن.
# ساختار فعلی: symbol_projects/BTC/DC-Cr_BTC.py -> symbol_projects/BTC/BTC-MarketData.json
# و همینطور برای BNB, DOGE, ETH, SOL, XAUT, XRP
# هر اسکریپت فقط داده خام تولید می‌کنه (۲۰۰ کندل آخر + FVGهای ۱۰۰ کندل آخر)؛
# هیچ پرامپتی داخل خود اسکریپت‌ها نیست — پرامپت‌های ثابت مرحله ۱ و ۲ همینجا
# در M.py (PROMPT_STAGE_1 / PROMPT_STAGE_2) نگه‌داری می‌شن و برای هر ۷ پروژه
# یکسان هستن.
PROJECTS_BASE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "symbol_projects"
)

def _detect_python_executable() -> str:
    """رفع باگ: روی اکثر سرورهای لینوکسی (مثل Render) دستور "python" اصلاً
    وجود نداره و فقط "python3" نصبه؛ قبلاً این مقدار هاردکد روی "python" بود
    که باعث می‌شد هر ۷ اسکریپت با خطای "command not found" شکست بخورن (این
    خطا لاگ می‌شد، ولی چون هیچ‌وقت فایل JSON ساخته نمی‌شد، هیچ سیگنالی هم
    تولید نمی‌شد و به نظر می‌رسید بات "کاری انجام نمی‌ده"). حالا به‌جای هاردکد،
    اول python3 رو امتحان می‌کنیم (اولویت روی لینوکس/Render)، بعد python،
    و اگه هیچ‌کدوم پیدا نشه از همون مقدار پیش‌فرض "python3" استفاده و در لاگ
    هشدار می‌دیم."""
    for candidate in ("python3", "python"):
        if shutil.which(candidate):
            return candidate
    logger.warning(
        "نه python3 و نه python در PATH پیدا نشد! اجرای اسکریپت‌های پروژه‌ها "
        "شکست می‌خوره. پایتون رو نصب کنید یا PYTHON_EXECUTABLE رو دستی ست کنید."
    )
    return "python3"


PYTHON_EXECUTABLE = os.environ.get("PYTHON_EXECUTABLE") or _detect_python_executable()
logger.info(f"دستور پایتون برای اجرای اسکریپت‌های پروژه‌ها: '{PYTHON_EXECUTABLE}'")
SUBPROCESS_TIMEOUT = None  # هیچ‌وقت timeout نشه؛ هر چقدر طول بکشه صبر می‌کنه
TELEGRAM_MAX_CHARS = 4000

# اگه True باشه، پاسخ خام مرحله ۱ (که برای خروجی نهایی اهمیتی نداره و فقط
# ورودی مرحله ۲ محسوبه) برای دیباگ توی یک فایل txt کنار همون پروژه ذخیره
# می‌شه (هربار بازنویسی می‌شه، به تلگرام فرستاده نمی‌شه).
SAVE_STAGE1_DEBUG_FILE = True

# فاصله زمانی «خودپینگ» به آدرس عمومی سرویس (بر حسب ثانیه)، فقط برای اینکه
# روی پلن رایگان Render سرویس به‌خاطر بی‌کاری نخوابه. پیش‌فرض ۵ دقیقه، طبق
# درخواست؛ چون پلن رایگان Render بعد از ۱۵ دقیقه بی‌فعالیتِ واقعی می‌خوابه،
# هر ۵ دقیقه (به‌جای مثلاً ۱۴ دقیقه) حاشیه‌ی امن بیشتری می‌ده.
PING_INTERVAL_SECONDS = int(os.environ.get("PING_INTERVAL_SECONDS", "300"))

# زمان روشن شدن پروسه؛ برای تشخیص «هنوز تازه بالا اومده، اولین دور کامل
# نشده» در endpoint سلامت، تا این حالت اشتباهاً stale/خراب گزارش نشه.
PROCESS_STARTED_AT = datetime.now()

# ============================ پرامپت‌های ثابت دو مرحله ============================
# این دو پرامپت کاملاً ثابت هستن و بین هر ۷ پروژه مشترک؛ فقط دیتای JSON بعد
# از هرکدوم (به‌صورت متن) اضافه می‌شه. مرحله ۱ فقط یک گزارش تحلیلی خام
# می‌ده (بدون سیگنال) که جواب خودش برامون مهم نیست ولی به‌عنوان ورودی مرحله ۲
# استفاده می‌شه. مرحله ۲ همون گزارش + دیتای FVG صد کندل آخر رو می‌گیره و
# سیگنال نهایی (یا HOLD) رو تولید می‌کنه — این خروجیه که فیلتر وین‌ریت روش
# اعمال و به تلگرام فرستاده می‌شه.

PROMPT_STAGE_1 = """# ROLE AND DIRECTIVE
You are an Elite Institutional Smart Money Concepts (SMC) Strategist and Market Analyst. Your sole objective is to deconstruct, analyze, and map the provided JSON market data with algorithmic precision.

# CORE METHODOLOGY
1. Your primary analytical engine is strictly Smart Money Concepts (SMC) and Read The Market (RTM) principles.
2. Use traditional Price Action (PA) strictly as a secondary tool to confirm momentum, candlestick behavior, and the nature of price approach (e.g., compression, spikes) toward critical zones.
3. DO NOT output any trading signals (No Buy, Sell, or Hold commands). Your output must be a pure, objective, and deeply detailed analytical report designed to be processed by a secondary risk-management algorithm.

# DATA INGESTION GUIDELINES
Analyze the provided JSON file containing market data (OHLC, Swings, Sessions, Imbalances, etc.) and execute the following checks step-by-step:

1. Market Structure (Mapping): Identify the dominant trend. Pinpoint the most recent Break of Structure (BOS) and any potential Change of Character (CHoCH). Differentiate between internal (minor) and external (major) structure.
2. Liquidity Engineering (The Engine): Locate swept and un-swept liquidity pools (BSL/SSL). Pay special attention to session highs/lows (e.g., Asian Session manipulation) and identify any engineered liquidity (Inducement / IDM) before key Points of Interest.
3. Supply & Demand (POI Selection): Identify high-probability, unmitigated Order Blocks (OB), Breaker Blocks, and Fair Value Gaps (FVG).
4. Price Approach (PA Confirmation): Evaluate HOW price is approaching or reacting to the POI. Look for signs of Compression (CP), Liquidity Voids, or exhaustion spikes.

# REQUIRED OUTPUT FORMAT
You must format your response EXACTLY according to the following structure. Do not add introductory or concluding conversational text. Be concise, objective, and use professional institutional trading terminology.

### 1. MARKET STRUCTURE (SMC)
* Dominant Bias: [Bullish / Bearish / Ranging]
* External Structure: [Last major BOS/CHoCH coordinates and direction]
* Internal Structure: [Current minor swing behavior]

### 2. LIQUIDITY & INDUCEMENT (ENGINEERING)
* Swept Liquidity: [Identify specifically which BSL/SSL or Session High/Low was recently taken]
* Un-swept Liquidity (Targets): [Where is the most obvious resting liquidity?]
* Inducement (IDM): [Has IDM been created and swept? State Yes/No with exact level]

### 3. POINTS OF INTEREST (POIs) & IMBALANCE
* Primary POI: [Exact price range of the highest probability Unmitigated Order Block or Demand/Supply zone]
* Imbalance (FVG): [Exact price range of nearest FVG, state if it aligns with the POI]

### 4. PRICE ACTION CONFIRMATION (APPROACH)
* Approach Behavior: [Describe the nature of the move toward the POI: Impulsive, Corrective, Compression (CP), etc.]
* Reaction: [Any candlestick rejection, exhaustion, or engulfing patterns at the POI?]

### 5. CONFLUENCE WEIGHTING
* Bullish Confluences: [List factors supporting a move up]
* Bearish Confluences: [List factors supporting a move down]

### 6. INVALIDATION LEVELS
* Structural Failure: [Exact price level where the current structural bias becomes completely invalid]

# json data:

"""

PROMPT_STAGE_2 = """Role: Secondary Confluence Analyst & Final Execution Algorithm.

Mission: You are the final strict quality-control layer. You will receive the primary SMC (Smart Money Concepts) analysis from Stage 1 and a JSON dataset containing Fair Value Gaps (FVG) from the last 100 candles. Your objective is to rigorously validate or reject the Stage 1 analysis using pure Price Action principles and algorithmic FVG data. You must output a highly precise trade signal or a strict HOLD command.

Core Processing Rules (Non-Negotiable):

Pure Price Action Validation: Do not blindly accept Stage 1's SMC analysis. You must cross-verify its findings (BOS, CHoCH, Order Blocks, Liquidity Sweeps) against raw Price Action mechanics. Ensure current market momentum, structural shifts, and candlestick context fully support the SMC narrative. If Price Action contradicts the SMC setup, you must reject the trade.

FVG Algorithmic Matching: Indicators are entirely removed. Your primary confirmation tool is the provided FVG JSON data. The proposed entry zone must perfectly align with a valid, unmitigated FVG within the correct structural leg.

Realistic Limit Orders: If issuing a BUY_LIMIT or SELL_LIMIT, the ENTRY price must be mathematically logical and proximal to the current price action (e.g., at the proximal line or 50% equilibrium of the confirmed FVG). Absolutely no distant, statistically improbable, or disconnected limit orders.

Quality Control & Win Rate: Only approve setups with a flawless confluence of SMC structure + Price Action validation + FVG alignment. If the algorithmic Win Rate is evaluated below 50% due to conflicting structural data, immediately issue a HOLD.

Absolute Zero-Noise Output: You are strictly forbidden from generating thought processes, reasoning blocks, introductory text, or any XML/HTML tags. Do not explain your analysis. Output ONLY the final signal block exactly as formatted below.

Required Output Format(in persian):
Symbol: [Symbol Name]
ACTION: [BUY_LIMIT / SELL_LIMIT / HOLD]
ENTRY: [Exact Price / N/A]
SL: [Exact Price (must include spread/risk buffer) / N/A]
TP1: [Exact Price / N/A]
TP2: [Exact Price / N/A]
TP3: [Exact Price / N/A]
RR: [Exact Decimal / N/A]
WIN RATE: [Exact Percentage / N/A]
HOLD REASON: [If HOLD, write one concise Persian sentence explaining the Price Action/FVG conflict / N/A]
RETRY CONDITION: [If HOLD, write one concise Persian sentence defining the exact price trigger needed to re-evaluate / N/A]

data:

"""

# حداقل وین‌ریت لازم برای اینکه سیگنال به تلگرام ارسال بشه (بر حسب درصد).
# هر سیگنالی که وین‌ریتش کمتر از این مقدار باشه (یا اصلاً وین‌ریت نداشته باشه،
# مثل HOLD یا N/A) فرستاده نمی‌شه. این فیلتر داخل کد اعمال می‌شه، نه با تغییر پرامپت.
MIN_WIN_RATE = 60.0

# فاصله زمانی بین شروع هر دو دور تحلیل متوالی (بر حسب ثانیه). به‌جای اجرای
# چرخه‌ای/پشت‌سرهم بدون وقفه، حالا دقیقاً هر ۱۰ دقیقه یک‌بار همه‌ی سیمبل‌ها
# به‌صورت هم‌زمان (موازی، نه پشت‌سرهم) تحلیل می‌شن. اگه خود پردازش یک دور
# بیشتر از این مقدار طول بکشه، دور بعدی بلافاصله (بدون تاخیر اضافه) شروع
# می‌شه؛ در غیر این صورت main_loop فقط به اندازه زمان باقی‌مونده صبر می‌کنه.
CYCLE_INTERVAL_SECONDS = 10 * 60

# وضعیت مشترک آخرین دور تحلیل، برای گزارش در endpoint سلامت (/health).
# صرفاً «پروسه زنده‌ست» تضمین نمی‌کنه که main_loop واقعاً داره کار می‌کنه
# (مثلاً ممکنه یک ترد هنگ کرده باشه)؛ با ثبت زمان شروع/پایان هر دور، سرویس
# مانیتورینگ بیرونی می‌تونه واقعاً تشخیص بده که تحلیل به‌روز هست یا نه.
_cycle_status_lock = threading.Lock()
_last_cycle_started_at: Optional[datetime] = None
_last_cycle_finished_at: Optional[datetime] = None


def _mark_cycle_started() -> None:
    global _last_cycle_started_at
    with _cycle_status_lock:
        _last_cycle_started_at = datetime.now()


def _mark_cycle_finished() -> None:
    global _last_cycle_finished_at
    with _cycle_status_lock:
        _last_cycle_finished_at = datetime.now()


def get_cycle_status() -> Dict:
    """یک عکس فوری (snapshot) از وضعیت آخرین دور تحلیل برمی‌گردونه، به‌همراه
    یک فیلد «stale» که True می‌شه اگه خیلی وقته دوری کامل نشده (نشونه‌ی
    هنگ‌کردن یا کرش خاموشِ main_loop)."""
    with _cycle_status_lock:
        started = _last_cycle_started_at
        finished = _last_cycle_finished_at
    now = datetime.now()
    # آستانه: ۲ برابر فاصله دور + ۲ دقیقه حاشیه (برای دورهایی که کمی بیشتر
    # از ۱۰ دقیقه طول می‌کشن)، تا false-positive نده.
    stale_threshold = 2 * CYCLE_INTERVAL_SECONDS + 120
    if finished is not None:
        stale = (now - finished).total_seconds() > stale_threshold
    else:
        # هنوز هیچ دوری کامل نشده؛ فقط اگه از استارت پروسه هم بیشتر از
        # آستانه گذشته باشه stale در نظر می‌گیریم (وگرنه یعنی تازه بالا اومده).
        stale = (now - PROCESS_STARTED_AT).total_seconds() > stale_threshold
    return {
        "process_started_at": PROCESS_STARTED_AT.isoformat(),
        "last_cycle_started_at": started.isoformat() if started else None,
        "last_cycle_finished_at": finished.isoformat() if finished else None,
        "cycle_interval_seconds": CYCLE_INTERVAL_SECONDS,
        "stale": stale,
    }


KNOWN_CHATS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "known_chats.json"
)

_known_chats_lock = threading.Lock()


# ============================ کشف خودکار پروژه‌ها ============================
def discover_projects(base_dir: str) -> List[Dict]:
    """
    هر زیرپوشه‌ی base_dir رو به‌عنوان یک پروژه در نظر می‌گیره:
    - اولین فایل .py داخل اون پوشه رو به‌عنوان اسکریپت اجرایی انتخاب می‌کنه
    - طبق الگوی مشاهده‌شده در پروژه‌های شما، خروجی هر پروژه یک فایل JSON با نام
      "<NAME>-MarketData.json" داخل همون پوشه‌ست (مسیر نسبی داخل خود اسکریپت)،
      شامل ۲۰۰ کندل آخر (مرحله ۱) و FVGهای ۱۰۰ کندل آخر (مرحله ۲)
    """
    projects: List[Dict] = []
    if not os.path.isdir(base_dir):
        logger.error(f"پوشه پروژه‌ها پیدا نشد: {base_dir}")
        return projects

    for entry in sorted(os.listdir(base_dir)):
        project_dir = os.path.join(base_dir, entry)
        if not os.path.isdir(project_dir):
            continue
        py_files = [f for f in os.listdir(project_dir) if f.endswith(".py")]
        if not py_files:
            continue
        script_name = py_files[0]
        output_file = f"{entry}-MarketData.json"
        projects.append({
            "name": entry,
            "dir": project_dir,
            "script": script_name,
            "output_file": output_file,
        })

    if not projects:
        logger.warning(f"هیچ پروژه‌ای در {base_dir} پیدا نشد.")
    else:
        names = ", ".join(p["name"] for p in projects)
        logger.info(f"{len(projects)} پروژه شناسایی شد: {names}")

    return projects


PROJECTS: List[Dict] = discover_projects(PROJECTS_BASE_DIR)


# ============================ مدیریت چت‌های شناخته‌شده ============================
def load_known_chats() -> Set[str]:
    if os.path.exists(KNOWN_CHATS_FILE):
        try:
            with open(KNOWN_CHATS_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_known_chats(chats: Set[str]) -> None:
    try:
        with open(KNOWN_CHATS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(chats), f)
    except Exception as e:
        logger.error(f"خطا در ذخیره لیست چت‌ها: {e}")


known_chats: Set[str] = load_known_chats()


def add_known_chat(chat_id) -> None:
    chat_id = str(chat_id)
    with _known_chats_lock:
        if chat_id not in known_chats:
            known_chats.add(chat_id)
            save_known_chats(known_chats)
            logger.info(f"چت جدید ثبت شد و به لیست گیرنده‌ها اضافه شد: {chat_id}")


def poll_telegram_updates() -> None:
    """
    به‌صورت مداوم (long polling) پیام‌های جدید بات رو چک می‌کنه.
    هر کاربر یا گروهی که پیام بده به لیست گیرنده‌ها اضافه می‌شه.

    نکته: برای اینکه بات پیام‌های عادی گروه (نه فقط کامندها) رو ببینه،
    باید Privacy Mode بات رو در BotFather با دستور /setprivacy خاموش کنید.
    """
    offset = None
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    while True:
        try:
            params = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset
            resp = requests.get(url, params=params, timeout=40)
            resp.raise_for_status()
            data = resp.json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message") or update.get("channel_post")
                if message and "chat" in message:
                    add_known_chat(message["chat"]["id"])
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 409:
                # رفع/تشخیص باگ 409 Conflict: تلگرام فقط اجازه یک مصرف‌کننده
                # getUpdates هم‌زمان روی هر توکن رو می‌ده. اگه اینجا افتادیم
                # یعنی run_startup_checks یک Webhook فعال پیدا نکرده (چون اگه
                # پیدا می‌کرد پاکش می‌کرد)، پس تقریباً قطعاً یک نمونه دیگه از
                # همین بات (با همین توکن) جای دیگه‌ای (یا حتی یک ترد/پردازش
                # قبلی که کامل بسته نشده) هم‌زمان داره اجرا می‌شه. به‌جای
                # اسپم کردن این خطا هر ۵ ثانیه، طولانی‌تر صبر می‌کنیم و فقط
                # هر چند بار یک‌بار پیام رو تکرار می‌کنیم.
                logger.error(
                    "❌ 409 Conflict روی getUpdates: یک نمونه دیگه از همین بات "
                    "(همین TELEGRAM_BOT_TOKEN) هم‌زمان جای دیگه‌ای در حال اجراست "
                    "(مثلاً هم روی Render دیپلویه هم لوکال داری اجراش می‌کنی، یا "
                    "دیپلوی قبلی درست متوقف نشده). فقط یک نمونه می‌تونه فعال "
                    "باشه؛ بقیه نمونه‌ها رو متوقف کنید. ۲۰ ثانیه صبر می‌کنیم..."
                )
                time.sleep(20)
                continue
            logger.error(f"خطا در دریافت آپدیت‌های تلگرام: {e}")
            time.sleep(5)
        except Exception as e:
            logger.error(f"خطا در دریافت آپدیت‌های تلگرام: {e}")
            time.sleep(5)


# ============================ ارسال پیام تلگرام ============================
def send_telegram_message(text: str, chat_id: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = [text[i:i + TELEGRAM_MAX_CHARS] for i in range(0, len(text), TELEGRAM_MAX_CHARS)] or [""]
    for chunk in chunks:
        try:
            resp = requests.post(url, data={"chat_id": chat_id, "text": chunk}, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"خطا در ارسال پیام به چت {chat_id}: {e}")


def broadcast_telegram_message(text: str) -> None:
    with _known_chats_lock:
        chats = list(known_chats)
    if not chats:
        logger.warning("هنوز هیچ چتی با بات تعامل نداشته؛ پیامی ارسال نشد.")
        return
    for chat_id in chats:
        send_telegram_message(text, chat_id)


# ============================ اجرای پروژه‌های پایتون ============================
def run_python_project(script_name: str, project_dir: str, output_path: str) -> Optional[str]:
    """اسکریپت رو با cwd روی پوشه خودش اجرا می‌کنه تا فایل txt خروجی (که با
    مسیر نسبی نوشته می‌شه) داخل همون پوشه ساخته بشه.

    برای اینکه هیچ‌وقت یه فایل txt قدیمی/باقی‌مونده از اجرای قبلی به اشتباه
    خونده نشه و به هوش مصنوعی داده نشه، قبل از اجرای اسکریپت، فایل خروجی
    قبلی (اگه وجود داشته باشه) حذف می‌شه. اینطوری اگه اسکریپت به هر دلیلی
    فایل رو دوباره نسازه، این تابع خطا برمی‌گردونه به‌جای اینکه بی‌سروصدا
    محتوای قدیمی رو معتبر جا بزنه.

    خروجی: None یعنی موفق بود و فایل txt همین الان (تازه) ساخته شده.
    هر مقدار دیگه (رشته) یعنی خطا رخ داده و همون رشته توضیح/خلاصه خطاست."""
    try:
        if os.path.exists(output_path):
            os.remove(output_path)
    except Exception as e:
        logger.error(f"خطا در حذف فایل قدیمی {output_path}: {e}")

    try:
        result = subprocess.run(
            [PYTHON_EXECUTABLE, script_name],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
        if result.returncode != 0:
            error_text = (result.stderr or result.stdout or "بدون پیام خطا").strip()
            logger.error(f"اسکریپت {script_name} با خطا تموم شد:\n{error_text}")
            return error_text

        if not os.path.exists(output_path):
            msg = "اسکریپت با موفقیت اجرا شد ولی فایل txt تازه‌ای نساخت."
            logger.error(f"{script_name}: {msg}")
            return msg

        return None
    except subprocess.TimeoutExpired:
        msg = "اجرای اسکریپت بیش از حد طول کشید (timeout)."
        logger.error(f"{script_name}: {msg}")
        return msg
    except Exception as e:
        logger.error(f"خطا در اجرای {script_name}: {e}")
        return str(e)


def read_market_data_json(path: str) -> Optional[Dict]:
    """فایل <NAME>-MarketData.json تازه‌ساخته‌شده رو می‌خونه و پارس می‌کنه.
    این فایل فقط داده خامه (کندل‌ها + FVGها)، نه پرامپت؛ پرامپت‌ها همیشه از
    PROMPT_STAGE_1 / PROMPT_STAGE_2 اضافه می‌شن."""
    if not os.path.exists(path):
        logger.error(f"فایل خروجی پیدا نشد: {path}")
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"خطا در خواندن/پارس JSON فایل {path}: {e}")
        return None


# ============================ تماس با هوش مصنوعی ============================
# رفع درخواست: کل سیستم مربوط به AI_API_URL و timeout دستی درخواست AI حذف
# شد. حالا با کتابخونه رسمی openai کار می‌کنیم که فقط به AI_API_KEY و
# AI_MODEL نیاز داره (بدون URL جدا)؛ خود کتابخونه هم به‌صورت پیش‌فرض بدون
# timeout صبر می‌کنه (به همین دلیل دیگه هیچ متغیر/پارامتر timeout جداگانه‌ای
# اینجا ست نمی‌شه).
#
# نکته مهم: این import عمداً در try/except هست، نه مستقیم بالای فایل؛ چون
# اگه پکیج openai نصب نباشه و این import مستقیم/بدون محافظت در سطح ماژول
# باشه، کل M.py همون لحظه import با ModuleNotFoundError کرش می‌کنه (قبل از
# اینکه هیچ logging.basicConfig یا run_startup_checks ای اجرا بشه) — دقیقاً
# همون باگ "هیچ لاگی نمی‌فرسته" که قبلاً برای ccxt/pandas رفع کردیم.
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

_openai_client = None
_openai_client_lock = threading.Lock()


def _get_openai_client():
    """رفع باگ احتمالی race condition: حالا که همه سیمبل‌ها هم‌زمان (هر کدوم
    در ترد خودش) پردازش می‌شن، ممکنه چند ترد دقیقاً هم‌زمان و برای اولین بار
    این تابع رو صدا بزنن؛ بدون قفل، ممکنه چند نمونه OpenAI client بی‌مورد
    ساخته بشه یا مقدار _openai_client به‌صورت ناهم‌زمان بین تردها overwrite
    بشه. با قفل، ساخت client فقط یک‌بار (توسط اولین تردی که می‌رسه) انجام
    می‌شه و بقیه‌ی تردها همون نمونه‌ی مشترک رو می‌گیرن."""
    global _openai_client
    if OpenAI is None:
        logger.error(
            "پکیج 'openai' نصب نیست. دستور زیر رو اجرا کنید:\n"
            "    pip install -r requirements.txt --break-system-packages"
        )
        return None
    if _openai_client is None:
        with _openai_client_lock:
            if _openai_client is None:
                if not AI_API_KEY:
                    logger.error("AI_API_KEY خالیه؛ کلید API رو داخل M.py یا Environment Variable پر کنید.")
                    return None
                _openai_client = OpenAI(api_key=AI_API_KEY)
    return _openai_client


def ask_ai(prompt_text: str) -> Optional[str]:
    """ارسال پرامپت به هوش مصنوعی از طریق کتابخونه openai، با مدل AI_MODEL."""
    client = _get_openai_client()
    if client is None:
        return None

    try:
        response = client.chat.completions.create(
            model=AI_MODEL,
            messages=[{"role": "user", "content": prompt_text}],
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"خطا در تماس با API هوش مصنوعی: {e}")
        return None


# ============================ فیلتر وین‌ریت (کد-محور، نه پرامپت‌محور) ============================
_WIN_RATE_PATTERN = re.compile(r"WIN\s*RATE\s*:\s*([\d]+(?:\.[\d]+)?)\s*%?", re.IGNORECASE)


def extract_win_rate(ai_response: str) -> Optional[float]:
    """
    خط "WIN RATE: XX%" رو از متن پاسخ هوش مصنوعی با ریجکس پیدا می‌کنه و عدد
    رو به float برمی‌گردونه. اگه خط پیدا نشه یا مقدارش N/A (یا هر چیز غیرعددی)
    باشه، None برمی‌گردونه. این فیلتر کاملاً در کد پایتون انجام می‌شه، نه با
    تغییر پرامپت هوش مصنوعی.
    """
    if not ai_response:
        return None
    match = _WIN_RATE_PATTERN.search(ai_response)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def should_send_signal(ai_response: str) -> bool:
    """فقط سیگنال‌هایی که وین‌ریت عددی و >= MIN_WIN_RATE دارن اجازه ارسال دارن."""
    win_rate = extract_win_rate(ai_response)
    if win_rate is None:
        return False
    return win_rate >= MIN_WIN_RATE


# ============================ پردازش یک پروژه کامل ============================
def process_single_project(project: Dict) -> None:
    """اجرای کامل یک پروژه/سیمبل شامل دو مرحله هوش مصنوعی:

    مرحله ۱ (تحلیل): PROMPT_STAGE_1 + دیتای ۲۰۰ کندل آخر -> یک گزارش تحلیلی
    خام SMC/RTM. جواب این مرحله هیچ‌وقت به تلگرام فرستاده نمی‌شه و فقط به
    عنوان ورودی مرحله ۲ استفاده می‌شه.

    مرحله ۲ (بررسی نهایی و تریگر): PROMPT_STAGE_2 + خروجی مرحله ۱ + دیتای
    FVGهای ۱۰۰ کندل آخر -> سیگنال نهایی (یا HOLD). فقط همین خروجی، و فقط
    وقتی وین‌ریتش >= MIN_WIN_RATE باشه، به تلگرام فرستاده می‌شه.
    """
    name = project["name"]
    project_dir = project["dir"]
    logger.info(f"شروع اجرای {name} ...")

    # فایل JSON همیشه همین الان (تازه، در همین اجرا) ساخته می‌شه؛ چون
    # run_python_project قبل از اجرای اسکریپت نسخه‌ی قبلی رو حذف می‌کنه و
    # بعد از اجرا هم وجودش رو تایید می‌کنه، پس دیتایی که چند خط پایین‌تر
    # خونده می‌شه همیشه آپدیته و مال همین دوره‌ست؛ نه یه فایل قدیمی.
    # نکته: از این به بعد هیچ متنی (خطا، هشدار، وضعیت خالی بودن فایل، خروجی
    # خام مرحله ۱ و ...) به تلگرام فرستاده نمی‌شه؛ فقط توی لاگ سرور (و در صورت
    # فعال بودن SAVE_STAGE1_DEBUG_FILE، یک فایل دیباگ محلی) ثبت می‌شه. تنها
    # چیزی که ممکنه به تلگرام بره، سیگنال نهایی معتبریه که از فیلتر وین‌ریت
    # رد شده باشه.
    output_path = os.path.join(project_dir, project["output_file"])
    error_text = run_python_project(project["script"], project_dir, output_path)
    if error_text is not None:
        short_error = error_text[-600:]
        logger.error(f"اجرای اسکریپت {name} ({project['script']}) با خطا مواجه شد:\n{short_error}")
        return

    market_data = read_market_data_json(output_path)
    if not market_data:
        logger.error(f"فایل خروجی {name} خالی، نامعتبر یا پیدا نشد.")
        return

    symbol_name = market_data.get("symbol", name)

    # ------------------------- مرحله ۱: تحلیل SMC/RTM -------------------------
    stage1_payload = {
        "symbol": symbol_name,
        "timeframe": market_data.get("timeframe"),
        "candles_count": market_data.get("stage1_candles_count"),
        "candles": market_data.get("stage1_candles", []),
    }
    stage1_prompt = PROMPT_STAGE_1 + json.dumps(stage1_payload, indent=2, ensure_ascii=False)

    stage1_response = ask_ai(stage1_prompt)
    if stage1_response is None:
        logger.error(f"{name}: دریافت پاسخ هوش مصنوعی در مرحله ۱ (تحلیل) با خطا مواجه شد.")
        return
    logger.info(f"{name}: مرحله ۱ (تحلیل) تمام شد ({len(stage1_response)} کاراکتر) — به مرحله ۲ داده می‌شه.")

    if SAVE_STAGE1_DEBUG_FILE:
        try:
            debug_path = os.path.join(project_dir, f"{name}-Stage1-LastAnalysis.txt")
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(stage1_response)
        except Exception as e:
            logger.error(f"{name}: خطا در ذخیره فایل دیباگ مرحله ۱: {e}")

    # ------------------- مرحله ۲: بررسی نهایی + تریگر (FVG) -------------------
    stage2_fvg_payload = {
        "symbol": symbol_name,
        "fvg_window_candles": market_data.get("stage2_fvg_window_candles"),
        "fvg_count": market_data.get("stage2_fvg_count"),
        "fvg_list": market_data.get("stage2_fvg_list", []),
    }
    stage2_prompt = (
        PROMPT_STAGE_2
        + "### STAGE 1 ANALYSIS (Primary SMC Report):\n"
        + stage1_response
        + "\n\n### FVG JSON DATA (last 100 candles):\n"
        + json.dumps(stage2_fvg_payload, indent=2, ensure_ascii=False)
    )

    stage2_response = ask_ai(stage2_prompt)
    if stage2_response is None:
        logger.error(f"{name}: دریافت پاسخ هوش مصنوعی در مرحله ۲ (نهایی/تریگر) با خطا مواجه شد.")
        return

    win_rate = extract_win_rate(stage2_response)

    if not should_send_signal(stage2_response):
        if win_rate is None:
            logger.info(f"{name}: سیگنال بدون وین‌ریت معتبر (مثلاً HOLD/N/A) — ارسال نشد.")
        else:
            logger.info(f"{name}: وین‌ریت {win_rate}% کمتر از حد نصاب {MIN_WIN_RATE}% — ارسال نشد.")
        return

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    message = f"✅ {name}\n🕒 {timestamp}\n\n{stage2_response}"
    broadcast_telegram_message(message)
    logger.info(f"{name}: وین‌ریت {win_rate}% >= {MIN_WIN_RATE}% — سیگنال به همه چت‌ها ارسال شد.")


def run_all_projects_once() -> None:
    """اجرای هم‌زمان (موازی) همه پروژه‌های شناسایی‌شده.

    قبلاً اینجا یک حلقه‌ی for ساده بود که سیمبل‌ها رو یکی‌یکی و پشت‌سرهم
    (ترتیبی) پردازش می‌کرد؛ یعنی مثلاً تا BTC کامل تموم نمی‌شد (هر دو مرحله
    هوش مصنوعی + احتمالاً ارسال تلگرام)، اصلاً درخواستی برای ETH فرستاده
    نمی‌شد. حالا با ThreadPoolExecutor همه‌ی سیمبل‌ها (هر کدوم در ترد
    جداگانه‌ی خودش) دقیقاً هم‌زمان با هم اجرا و تحلیل می‌شن؛ هر ترد مستقل
    اسکریپت خودش رو اجرا می‌کنه و مستقل هم به هوش مصنوعی درخواست می‌ده،
    بدون اینکه منتظر تمام شدن بقیه بمونه.

    خطای هر پروژه (چه داخل خود process_single_project و چه یک استثنای
    مدیریت‌نشده) جدا گرفته و لاگ می‌شه تا شکست یک سیمبل روی بقیه سیمبل‌ها یا
    دورهای بعدی اثر نذاره — دقیقاً همون تضمینی که نسخه‌ی ترتیبی قبلی هم
    داشت، فقط حالا موازی."""
    if not PROJECTS:
        logger.error("هیچ پروژه‌ای برای اجرا پیدا نشد؛ مسیر symbol_projects رو چک کنید.")
        return

    logger.info(f"شروع تحلیل هم‌زمان {len(PROJECTS)} سیمبل (هر کدوم در ترد جداگانه)...")
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=len(PROJECTS), thread_name_prefix="symbol"
    ) as executor:
        future_to_project = {
            executor.submit(process_single_project, project): project
            for project in PROJECTS
        }
        for future in concurrent.futures.as_completed(future_to_project):
            project = future_to_project[future]
            try:
                future.result()
            except Exception:
                logger.exception(
                    f"خطای غیرمنتظره و مدیریت‌نشده هنگام پردازش {project.get('name')}؛ رد شدن از این سیمبل."
                )
    logger.info("تحلیل هم‌زمان همه سیمبل‌ها تمام شد.")


# ============================ حلقه اصلی ============================
def main_loop() -> None:
    """به‌جای اجرای چرخه‌ای/پشت‌سرهمِ بدون وقفه، حالا دقیقاً هر
    CYCLE_INTERVAL_SECONDS (پیش‌فرض ۱۰ دقیقه) یک‌بار، یک دور تحلیل هم‌زمان
    روی همه‌ی سیمبل‌ها اجرا می‌شه (نگاه کن به run_all_projects_once).

    زمان‌بندی نسبت به «زمان شروع» هر دور حساب می‌شه، نه نسبت به «زمان پایان»:
    اگه یک دور مثلاً ۴۰ ثانیه طول بکشه، فقط ۹ دقیقه و ۲۰ ثانیه صبر می‌کنیم تا
    فاصله‌ی بین شروع دو دور متوالی دقیقاً ۱۰ دقیقه بشه. اگه دور بیشتر از ۱۰
    دقیقه طول بکشه (مثلاً هوش مصنوعی کند جواب بده)، دور بعدی بدون تاخیر
    اضافه بلافاصله شروع می‌شه (به‌جای عقب افتادن تجمعی زمان‌بندی)."""
    while True:
        cycle_start = time.monotonic()
        _mark_cycle_started()
        try:
            logger.info("=== شروع دور جدید (تحلیل هم‌زمان همه سیمبل‌ها) ===")
            run_all_projects_once()
            logger.info("=== دور تمام شد ===")
        except Exception:
            # لایه محافظتی نهایی: حتی اگه یک باگ کاملاً پیش‌بینی‌نشده اینجا
            # برسه، به‌جای مرگ خاموش کل حلقه، خطا کامل لاگ و دور بعدی امتحان می‌شه.
            logger.exception("خطای غیرمنتظره در main_loop.")
        finally:
            _mark_cycle_finished()

        elapsed = time.monotonic() - cycle_start
        remaining = CYCLE_INTERVAL_SECONDS - elapsed
        if remaining > 0:
            logger.info(
                f"این دور {elapsed:.1f} ثانیه طول کشید؛ {remaining:.1f} ثانیه صبر تا شروع دور بعدی "
                f"(فاصله ثابت {CYCLE_INTERVAL_SECONDS} ثانیه = ۱۰ دقیقه)."
            )
            time.sleep(remaining)
        else:
            logger.warning(
                f"این دور {elapsed:.1f} ثانیه طول کشید که از فاصله {CYCLE_INTERVAL_SECONDS} "
                "ثانیه‌ای (۱۰ دقیقه) بیشتره؛ دور بعدی بدون تاخیر اضافه بلافاصله شروع می‌شه."
            )


# ============================ بررسی سلامت استارتاپ ============================
def run_startup_checks() -> None:
    """یک‌بار در ابتدای اجرا چند چیز حیاتی رو چک و در لاگ گزارش می‌کنه، تا
    اگه چیزی خرابه (پکیج نصب نشده، توکن تلگرام نامعتبره، کلید AI هنوز
    مقدار نمونه/پیش‌فرضه) بلافاصله و واضح توی لاگ دیده بشه؛ نه اینکه کاربر
    مجبور باشه بین ده‌ها خط خطای تکراریِ هر دور، دلیل اصلی رو حدس بزنه."""
    logger.info("=== بررسی‌های اولیه استارتاپ ===")

    # ۱) بررسی نصب بودن پکیج‌های لازم برای اسکریپت‌های symbol_projects + openai
    check = subprocess.run(
        [PYTHON_EXECUTABLE, "-c", "import ccxt, pandas, requests, openai"],
        capture_output=True,
        text=True,
    )
    if check.returncode != 0:
        logger.error(
            "❌ حداقل یکی از پکیج‌های ccxt/pandas/requests/openai با دستور "
            f"'{PYTHON_EXECUTABLE}' قابل import نیست؛ اسکریپت‌ها یا تماس با "
            "هوش مصنوعی شکست می‌خوره. دستور زیر رو اجرا کنید:\n"
            "    pip install -r requirements.txt --break-system-packages\n"
            f"خطای دقیق:\n{(check.stderr or check.stdout).strip()[-500:]}"
        )
    else:
        logger.info("✅ پکیج‌های ccxt/pandas/requests/openai با موفقیت import شدن.")

    # ۲) بررسی معتبر بودن توکن تلگرام (فراخوانی getMe)
    if os.environ.get("TELEGRAM_BOT_TOKEN") is None:
        logger.warning(
            "⚠️ TELEGRAM_BOT_TOKEN به‌صورت Environment Variable ست نشده و از "
            "توکن قدیمیِ hardcode-شده داخل کد استفاده می‌شه. این توکن ممکنه "
            "جای دیگه‌ای (یا نسخه‌های قبلی/کپی‌شدهٔ همین فایل) هم استفاده بشه و "
            "باعث خطای 409 Conflict بشه. پیشنهاد می‌شه از @BotFather توکن رو "
            "Revoke و توکن تازه رو در Environment Variable با نام "
            "TELEGRAM_BOT_TOKEN ست کنی."
        )
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe", timeout=15
        )
        data = resp.json()
        if resp.status_code == 200 and data.get("ok"):
            bot_username = data["result"].get("username")
            logger.info(f"✅ توکن تلگرام معتبره — بات: @{bot_username}")
        else:
            logger.error(f"❌ توکن تلگرام نامعتبره یا رد شد (HTTP {resp.status_code}): {data}")
    except Exception as e:
        logger.error(f"❌ نشد به API تلگرام وصل شد تا توکن چک بشه: {e}")

    # ۲.۵) رفع باگ «409 Conflict روی getUpdates»: این خطا یعنی تلگرام یک
    # مصرف‌کننده دیگه (Webhook یا یک نمونه دیگه از همین بات) رو برای همین
    # توکن ثبت‌شده می‌بینه. یکی از دو دلیلش (Webhook قدیمی) رو اینجا خودکار
    # پاک می‌کنیم؛ دلیل دیگه‌ش (دو نمونه هم‌زمان از بات) رو نمی‌شه از کد خود
    # بات رفع کرد، فقط می‌تونیم واضح گزارشش کنیم.
    try:
        info_resp = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getWebhookInfo", timeout=15
        )
        info = info_resp.json()
        webhook_url = (info.get("result") or {}).get("url") if info.get("ok") else None
        if webhook_url:
            logger.warning(f"⚠️ روی این بات یک Webhook فعال ثبت‌شده بود ('{webhook_url}') که با polling تداخل داره و باعث خطای 409 می‌شه؛ در حال حذفش هستیم...")
            del_resp = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook",
                params={"drop_pending_updates": "false"},
                timeout=15,
            )
            if del_resp.ok and del_resp.json().get("ok"):
                logger.info("✅ Webhook قدیمی حذف شد؛ حالا polling باید بدون خطای 409 کار کنه.")
            else:
                logger.error(f"❌ حذف Webhook ناموفق بود: {del_resp.text}")
        else:
            logger.info("✅ هیچ Webhook فعالی روی این بات ثبت نیست (این علتِ احتمالی 409 منتفیه).")
    except Exception as e:
        logger.error(f"❌ نشد وضعیت Webhook چک بشه: {e}")

    # ۳) هشدار در صورتی که کلید هوش مصنوعی هنوز مقدار نمونه پیش‌فرضه
    if AI_API_KEY == "sk-t19anutsf3jd4xj35xjc7t9pzhccj0ed":
        logger.warning(
            "⚠️ به نظر می‌رسه AI_API_KEY هنوز Environment Variable واقعی "
            "نیست و از مقدار نمونه پیش‌فرض داخل کد استفاده می‌شه؛ اگه این "
            "مقدار واقعی نباشه، هر دو مرحله تحلیل هوش مصنوعی شکست می‌خورن."
        )
    else:
        logger.info("✅ AI_API_KEY از مقدار پیش‌فرض داخل کد متفاوته (به‌نظر از env ست شده).")

    logger.info("=== پایان بررسی‌های اولیه؛ شروع اجرای اصلی ===")


# ============================ وب‌سرور سلامت (برای Render.com) ============================
def start_health_server() -> None:
    """
    یک وب‌سرور خیلی ساده بالا میاره با دو مسیر:
    - GET /        -> فقط متن "Bot is running." (برای خودپینگ و پینگ‌های ساده)
    - GET /health  -> یک JSON با وضعیت واقعیِ آخرین دور تحلیل (نه فقط اینکه
                      پروسه زنده‌ست)؛ اگه خیلی وقته دوری کامل نشده، status
                      code آن 503 برمی‌گرده تا ابزار مانیتورینگ بیرونی
                      (UptimeRobot/cron-job.org و ...) واقعاً متوجه هنگ یا
                      کرش خاموش main_loop بشه، نه فقط زنده‌بودن وب‌سرور.

    دلیل وجود این وب‌سرور:
    1) پلن رایگان Render.com فقط از نوع «Web Service» پشتیبانی می‌کنه (نه Worker)،
       و Web Service باید حتماً روی یک پورت HTTP گوش بده وگرنه دیپلوی fail می‌شه.
    2) از همین آدرس (مسیر /) هم برای خودپینگ داخلی (keep_alive_ping) و هم برای
       مانیتورینگ بیرونی هر ۵ دقیقه استفاده می‌شه تا Render سرویس رو به‌خاطر
       بی‌کاری نخوابونه.
    منطق اصلی بات (اجرای پروژه‌ها + تلگرام) در تردهای جداگانه در پس‌زمینه
    اجرا می‌شه و کاری به این سرور نداره.
    """
    port = int(os.environ.get("PORT", 10000))

    class HealthHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/health"):
                status = get_cycle_status()
                body = json.dumps(status, ensure_ascii=False).encode("utf-8")
                self.send_response(503 if status["stale"] else 200)
                self.send_header("Content-type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
                return

            self.send_response(200)
            self.send_header("Content-type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Bot is running.".encode("utf-8"))

        def log_message(self, format, *args):
            # جلوگیری از شلوغ شدن لاگ‌ها با هر پینگ سلامت
            return

    with socketserver.ThreadingTCPServer(("0.0.0.0", port), HealthHandler) as httpd:
        logger.info(f"وب‌سرور سلامت روی پورت {port} بالا اومد (مسیرها: / و /health).")
        httpd.serve_forever()


# ============================ خودپینگ (جلوگیری از خوابیدن روی Render) ============================
def keep_alive_ping() -> None:
    """
    هدف: هر PING_INTERVAL_SECONDS (پیش‌فرض ۳۰۰ ثانیه = ۵ دقیقه) یک درخواست
    HTTP واقعی به آدرس عمومیِ خودِ همین سرویس می‌فرسته، تا پلن رایگان
    Render.com که بعد از ۱۵ دقیقه بی‌فعالیتِ واقعی (بدون هیچ ترافیک ورودیِ
    بیرونی) سرویس رو می‌خوابونه (spin down)، همیشه ترافیک تازه ببینه و
    نخوابه. چون این درخواست به آدرس عمومی (نه localhost) می‌ره، از دید
    Render دقیقاً مثل یک بازدید واقعی حساب می‌شه.

    Render خودش به‌صورت خودکار متغیر RENDER_EXTERNAL_URL رو با آدرس عمومی
    سرویس ست می‌کنه؛ نیازی به کار دستی نیست. اگه این پروژه جای دیگه‌ای
    (نه Render) دیپلوی شده، می‌شه به‌جاش KEEP_ALIVE_URL رو دستی ست کرد؛ اگه
    هیچ‌کدوم ست نشده باشن (مثلاً روی لوکال)، این ترد فقط یک بار هشدار می‌ده
    و خودپینگ رو غیرفعال می‌کنه (چون آدرس عمومی معتبری نداره که پینگ کنه).

    نکته مهم: این خودپینگ فقط جلوی خوابیدن به‌خاطر بی‌کاری رو می‌گیره. اگه
    پروسه کامل کرش کنه، این ترد هم با آن از بین می‌ره. برای پوشش اون حالت،
    علاوه بر این، آدرس عمومی/health رو با یک سرویس مانیتورینگ بیرونی (مثل
    UptimeRobot یا cron-job.org، با فاصله هر ۵ دقیقه) هم چک کنید؛ این تابع
    جایگزین مانیتورینگ بیرونی نیست، مکملشه.
    """
    external_url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("KEEP_ALIVE_URL")
    if not external_url:
        logger.info(
            "نه RENDER_EXTERNAL_URL و نه KEEP_ALIVE_URL ست نشده؛ ترد خودپینگ غیرفعال می‌مونه "
            "(روی Render این متغیر خودکار ست می‌شه، نیازی به کار دستی نیست)."
        )
        return

    ping_url = external_url.rstrip("/") + "/"
    logger.info(f"ترد خودپینگ فعال شد: هر {PING_INTERVAL_SECONDS} ثانیه به '{ping_url}' پینگ می‌زنیم تا سرویس نخوابه.")
    while True:
        time.sleep(PING_INTERVAL_SECONDS)
        try:
            resp = requests.get(ping_url, timeout=20)
            logger.info(f"خودپینگ انجام شد: {ping_url} -> HTTP {resp.status_code}")
        except Exception as e:
            logger.warning(f"خودپینگ ناموفق بود ({ping_url}): {e}")


def _run_thread_safely(target, name: str) -> None:
    """رفع باگ: قبلاً تردهای poll_telegram_updates و main_loop مستقیم اجرا
    می‌شدن؛ اگه یکی‌شون با یک استثنای مدیریت‌نشده کرش می‌کرد، ترد daemon
    بی‌صدا می‌مرد (Python فقط traceback رو یک بار توی stderr چاپ می‌کنه) و
    دیگه هیچ‌وقت دوباره اجرا نمی‌شد، بدون اینکه هیچ نشونه واضحی توی لاگ‌های
    بعدی باقی بمونه. این wrapper هر تابعی که قراره «همیشگی» اجرا بشه رو در
    یک حلقه محافظت‌شده اجرا می‌کنه: اگه به هر دلیلی برگرده یا کرش کنه، کامل
    لاگ می‌کنه و بعد از ۵ ثانیه دوباره استارتش می‌زنه."""
    while True:
        try:
            target()
            logger.warning(f"ترد '{name}' بدون خطا خارج شد ولی قرار بود همیشگی باشه؛ ۵ ثانیه دیگه دوباره اجرا می‌شه.")
        except Exception:
            logger.exception(f"خطای غیرمنتظره باعث توقف ترد '{name}' شد؛ ۵ ثانیه دیگه دوباره اجرا می‌شه.")
        time.sleep(5)


def main() -> None:
    run_startup_checks()

    threading.Thread(
        target=_run_thread_safely, args=(poll_telegram_updates, "poll_telegram_updates"), daemon=True
    ).start()
    threading.Thread(
        target=_run_thread_safely, args=(main_loop, "main_loop"), daemon=True
    ).start()
    # توجه: keep_alive_ping با _run_thread_safely رپ نمی‌شه چون اگه
    # RENDER_EXTERNAL_URL/KEEP_ALIVE_URL ست نشده باشه، عمداً و بدون خطا
    # زود خارج می‌شه (نه کرش)؛ رپش با _run_thread_safely باعث می‌شد این
    # پیام هشدار هر ۵ ثانیه بی‌مورد تکرار بشه.
    threading.Thread(target=keep_alive_ping, name="keep_alive_ping", daemon=True).start()
    logger.info("بات راه‌اندازی شد. منتظر پیام از کاربران/گروه‌ها برای ثبت چت هستیم...")
    # این تابع بلاک می‌کنه و ترد اصلی رو زنده نگه می‌داره؛ روی Render لازمه
    # سرویس یک پورت HTTP باز کنه تا هم دیپلوی موفق بشه و هم پینگ‌های
    # بیرونی (برای جلوگیری از خواب رفتن سرویس رایگان) جواب بگیرن.
    start_health_server()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # آخرین خط دفاعی: حتی خطای کشنده‌ای که کل main() رو (نه یک ترد
        # پس‌زمینه، بلکه ترد اصلی مثل خود وب‌سرور health) متوقف می‌کنه هم
        # قبل از خروج کامل و واضح لاگ بشه.
        logger.exception("خطای کشنده باعث توقف کامل برنامه شد.")
        raise
