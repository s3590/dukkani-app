# مسار الملف: config.py
import os
import json
import logging
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials

# تحميل المتغيرات من ملف .env
load_dotenv()

logger = logging.getLogger(__name__)

# ================= 🌐 إعدادات الخادم والرابط العام =================
PORT = int(os.environ.get("PORT") or 8080)
BASE_URL = os.getenv("BASE_URL", "https://dukkani-app.onrender.com").rstrip("/")

# ================= 🗄️ إعدادات قاعدة البيانات (PostgreSQL) =================
RAW_DB_URL = os.getenv("DATABASE_URL")
if not RAW_DB_URL:
    raise ValueError("❌ خطأ حرج: لم يتم العثور على 'DATABASE_URL' في متغيرات البيئة!")

# تصحيح بادئة الرابط لضمان التوافق التام مع مكتبة asyncpg في Render
DATABASE_URL = RAW_DB_URL.strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# ================= 🤖 إعدادات البوت والمدير العام =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ خطأ حرج: لم يتم العثور على 'BOT_TOKEN' في متغيرات البيئة!")
BOT_TOKEN = BOT_TOKEN.strip()

ADMIN_ID_STR = os.getenv("ADMIN_ID")
if not ADMIN_ID_STR or ADMIN_ID_STR.strip() == "0":
    raise ValueError("❌ خطأ حرج: لم يتم العثور على 'ADMIN_ID' أو القيمة تساوي 0!")
ADMIN_ID = int(ADMIN_ID_STR.strip())

ARCHIVE_GROUP_ID = os.getenv("ARCHIVE_GROUP_ID")
if ARCHIVE_GROUP_ID:
    try:
        ARCHIVE_GROUP_ID = int(ARCHIVE_GROUP_ID.strip())
    except ValueError:
        ARCHIVE_GROUP_ID = None

# ================= 🔐 حماية الجلسات والأمان (JWT & Admin) =================
JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    raise ValueError("❌ خطأ حرج: لم يتم العثور على 'JWT_SECRET' في متغيرات البيئة!")
JWT_SECRET = JWT_SECRET.strip()

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
if not ADMIN_PASSWORD:
    raise ValueError("❌ خطأ حرج: لم يتم العثور على 'ADMIN_PASSWORD' في متغيرات البيئة!")
ADMIN_PASSWORD = ADMIN_PASSWORD.strip()

# ================= ⚡ إعدادات رديس (Redis Cache & PubSub) =================
REDIS_URL = os.getenv("REDIS_URL")
if REDIS_URL:
    REDIS_URL = REDIS_URL.strip()

# ================= 🚀 تهيئة Firebase للإشعارات (FCM) =================
firebase_app_initialized = False

# 1. الخيار الأول: فحص وجود متغير بيئة يحتوي على الـ JSON مباشرة
firebase_env_json = os.getenv("FIREBASE_CREDENTIALS_JSON") or os.getenv("FIREBASE_SERVICE_ACCOUNT")
if firebase_env_json:
    try:
        cert_dict = json.loads(firebase_env_json)
        cred = credentials.Certificate(cert_dict)
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        firebase_app_initialized = True
        logger.info("✅ تم تهيئة Firebase بنجاح من متغير البيئة النصي.")
    except Exception as e:
        logger.error("❌ فشلت تهيئة Firebase من متغير البيئة: %s", e)

# 2. الخيار الثاني: فحص المسارات السرية في Render أو المجلد المحلي
if not firebase_app_initialized:
    firebase_paths = [
        "firebase-adminsdk.json",
        "/etc/secrets/firebase-adminsdk.json",
        os.path.join(os.path.dirname(__file__), "firebase-adminsdk.json")
    ]
    
    firebase_cert_path = next((p for p in firebase_paths if os.path.exists(p)), None)
    
    if firebase_cert_path:
        try:
            cred = credentials.Certificate(firebase_cert_path)
            if not firebase_admin._apps:
                firebase_admin.initialize_app(cred)
            firebase_app_initialized = True
            logger.info("✅ تم تهيئة Firebase بنجاح من الملف: %s", firebase_cert_path)
        except Exception as e:
            logger.error("❌ فشلت تهيئة Firebase من الملف: %s", e)
    else:
        logger.warning("⚠️ تحذير: لم يتم العثور على أوراق اعتماد Firebase (ملف أو متغير). الإشعارات لن تعمل!")
