# مسار الملف: run.py
import os
import logging
from aiohttp import web
import config  # تهيئة الإعدادات وفايربيس والمتغيرات مبكراً
from api.app import create_app

# 🛡️ إعداد سجل الأخطاء (تم إصلاح المسافة في asctime)
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# إنشاء تطبيق الويب (متاح للموديول ليدعم Gunicorn والتشغيل المباشر)
app = create_app()

if __name__ == "__main__":
    logger.info("🚀 جاري إشعال محركات منصة دُكّاني...")
    
    # جلب المنفذ بأمان تام
    port = int(os.environ.get("PORT") or 8080)
    logger.info(f"🌐 السيرفر يعمل الآن بنجاح على المنفذ: {port}")
    
    # تشغيل خادم aiohttp
    web.run_app(app, host="0.0.0.0", port=port)
