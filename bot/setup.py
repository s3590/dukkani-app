# مسار الملف: bot/setup.py
import time
import html
import logging
import traceback
from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from config import BOT_TOKEN, ADMIN_ID

# إعداد تسجيل الأخطاء القياسي
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# تهيئة البوت مع تعيين HTML كنمط افتراضي للنصوص
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

# متغير لمنع إغراق حساب المدير بالرسائل في حال تكرار الأخطاء السريعة (Flood Protection)
LAST_ERROR_ALERT_TIME = 0
ERROR_ALERT_COOLDOWN = 30  # إرسال تنبيه واحد كل 30 ثانية كحد أقصى


# =====================================================================
# 🛡️ صائد الأخطاء الشامل للبوت (Global Error Handler)
# =====================================================================
@dp.error()
async def global_error_handler(event: types.ErrorEvent):
    global LAST_ERROR_ALERT_TIME
    
    # 1. استخراج وطباعة الخطأ في سجلات السيرفر (Console / Logs) دائماً
    err_str = "".join(traceback.format_exception(type(event.exception), event.exception, event.exception.__traceback__))
    logger.error("🚨 [Bot Global Error]: %s", err_str)
    
    now = time.time()
    # 2. الحماية من الحظر: لا نرسل تيليجرام إذا أرسلنا تنبيهاً خلال آخر 30 ثانية
    if now - LAST_ERROR_ALERT_TIME < ERROR_ALERT_COOLDOWN:
        return
        
    try:
        LAST_ERROR_ALERT_TIME = now
        # استخدام html.escape لتعقيم < و > و & لمنع أي انهيار في Parse Mode
        safe_err = html.escape(err_str)
        
        await bot.send_message(
            ADMIN_ID, 
            f"🚨 <b>خطأ برمجي في البوت:</b>\n\n<pre><code class='language-python'>{safe_err[:3500]}</code></pre>"
        )
    except Exception as e:
        logger.warning("تعذر إرسال تنبيه الخطأ لتيليجرام المدير: %s", e)


# =====================================================================
# 🔄 استيراد وتسجيل الراوترات (لتجنب الاستدعاء الدائري Circular Import)
# =====================================================================
from bot.handlers.admin import admin_router
from bot.handlers.worker import worker_router
from bot.handlers.store import store_router

# ترتيب الأولويات في الموزع: المدير أولاً، ثم العامل، ثم المتجر العام
dp.include_router(admin_router)
dp.include_router(worker_router)
dp.include_router(store_router)
