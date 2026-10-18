# مسار الملف: bot/handlers/worker.py
import os
import time
import html
import logging
from aiogram import Router, F, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.types.web_app_info import WebAppInfo
import database
from bot.setup import bot

logger = logging.getLogger(__name__)

worker_router = Router()

# =====================================================================
# 🛡️ نظام الحماية من التخمين والقوة الغاشمة (Rate Limiting)
# =====================================================================
# هيكل البيانات: {telegram_id: {"attempts": int, "blocked_until": float}}
FAILED_ATTEMPTS = {}
MAX_ATTEMPTS = 5
BLOCK_DURATION = 600  # حظر لمدة 10 دقائق بعد 5 محاولات خاطئة


def check_rate_limit(user_id: int) -> tuple[bool, int]:
    """التحقق من حالة الحظر للمستخدم لمنع تخمين الأكواد السرية"""
    now = time.time()
    record = FAILED_ATTEMPTS.get(user_id)
    if not record:
        return True, 0

    if record["blocked_until"] > now:
        remaining_minutes = int((record["blocked_until"] - now) // 60) + 1
        return False, remaining_minutes

    # فك الحظر تلقائياً بعد انقضاء الوقت
    if record["blocked_until"] != 0 and record["blocked_until"] <= now:
        FAILED_ATTEMPTS.pop(user_id, None)

    return True, 0


def record_failed_attempt(user_id: int) -> int:
    """تسجيل محاولة فاشلة وحظر المستخدم إذا تجاوز الحد"""
    now = time.time()
    record = FAILED_ATTEMPTS.setdefault(user_id, {"attempts": 0, "blocked_until": 0})
    record["attempts"] += 1

    if record["attempts"] >= MAX_ATTEMPTS:
        record["blocked_until"] = now + BLOCK_DURATION
        return 0  # تم الحظر الآن

    return MAX_ATTEMPTS - record["attempts"]


def clear_failed_attempts(user_id: int):
    """تصفير المحاولات عند تسجيل الدخول الناجح"""
    FAILED_ATTEMPTS.pop(user_id, None)


# =====================================================================
# 👷‍♂️ دخول العمال والمدراء بالكود السري (من 4 إلى 6 أرقام)
# =====================================================================
@worker_router.message(F.chat.type == "private", F.text.regexp(r"^\d{4,6}$"))
async def worker_login(message: types.Message):
    user_id = message.from_user.id
    passcode = message.text.strip()

    # 1. فحص الحماية من التخمين
    allowed, wait_mins = check_rate_limit(user_id)
    if not allowed:
        return await message.answer(
            f"⛔ <b>تم تجميد المحاولات مؤقتاً!</b>\n"
            f"لقد تجاوزت الحد المسموح من المحاولات الخاطئة.\n"
            f"يرجى الانتظار <b>{wait_mins} دقيقة</b> قبل المحاولة مجدداً.",
            parse_mode="HTML",
        )

    try:
        async with database.pool.acquire() as conn:
            # 2. البحث عن العامل والتحقق من حالة البقالة
            workers = await conn.fetch(
                """
                SELECT w.id, w.name, w.store_id, w.role, w.permissions,
                       w.telegram_id AS old_tg_id, 
                       s.name AS store_name, s.telegram_id AS admin_tg_id, s.status AS store_status
                FROM workers w
                JOIN stores s ON w.store_id = s.id
                WHERE w.passcode = $1 AND w.is_deleted = FALSE
            """,
                passcode,
            )

            if not workers:
                remaining = record_failed_attempt(user_id)
                if remaining == 0:
                    return await message.answer(
                        "🚨 <b>تم حظر المحاولات مؤقتاً!</b>\n"
                        "أدخلت كوداً خاطئاً 5 مرات متتالية. تم تجميد الدخول لمدة 10 دقائق لحماية النظام.",
                        parse_mode="HTML",
                    )
                return await message.answer(
                    f"❌ <b>الكود السري غير صحيح أو الحساب معطل.</b>\n"
                    f"⚠️ متبقي لديك: <b>{remaining}</b> محاولات قبل التجميد المؤقت.",
                    parse_mode="HTML",
                )

            # معالجة المطابقة (إذا كان هناك حساب واحد أو مطابق لحساب المستخدم الحالي)
            worker = workers[0]
            if len(workers) > 1:
                # إذا تكرر نفس الكود، نفضّل المتجر الذي كان مسجلاً به نفس حساب التيليجرام سابقاً
                matched = next((w for w in workers if str(w["old_tg_id"]) == str(user_id)), None)
                if matched:
                    worker = matched

            # 3. التحقق من حالة المتجر
            if worker["store_status"] != "active":
                return await message.answer(
                    "❌ <b>عذراً، هذا المتجر موقوف حالياً.</b>\nيرجى مراجعة إدارة المنصة.",
                    parse_mode="HTML",
                )

            # 4. تصفير محاولات التخمين وتحديث تيليجرام العامل
            clear_failed_attempts(user_id)
            await conn.execute(
                """
                UPDATE workers 
                SET telegram_id = $1, last_login_at = NOW() 
                WHERE id = $2
            """,
                user_id,
                worker["id"],
            )

            # 5. تجهيز روابط الـ WebApp المباشرة
            base_url = os.getenv("BASE_URL", "https://dukkani-app.onrender.com").rstrip("/")
            store_id = worker["store_id"]

            buttons = []
            # زر شاشة الكاشير والـ POS
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="🛒 فتح الكاشير ومبيعات البقالة (POS)",
                        web_app=WebAppInfo(url=f"{base_url}/pos?store_id={store_id}"),
                    )
                ]
            )

            # إذا كان مديراً أو مشرفاً، إضافة زر لوحة الإدارة
            if worker.get("role") in ["admin", "manager"]:
                buttons.append(
                    [
                        InlineKeyboardButton(
                            text="⚙️ لوحة إدارة البقالة",
                            web_app=WebAppInfo(url=f"{base_url}/manage?store_id={store_id}"),
                        )
                    ]
                )

            kb = InlineKeyboardMarkup(inline_keyboard=buttons)

            worker_name_escaped = html.escape(worker["name"])
            store_name_escaped = html.escape(worker["store_name"])

            await message.answer(
                f"✅ <b>أهلاً بك يا {worker_name_escaped}!</b>\n"
                f"🏪 تم ربط حسابك بنجاح بكادر متجر: <b>{store_name_escaped}</b>\n\n"
                f"اضغط على الزر أدناه للبدء مباشرة:",
                reply_markup=kb,
                parse_mode="HTML",
            )

            # 6. إشعار أمني للمدير العام إذا تم الدخول من حساب تيليجرام جديد أو مختلف
            if worker["admin_tg_id"] and str(worker["old_tg_id"]) != str(user_id):
                user_display = (
                    f"@{message.from_user.username}"
                    if message.from_user.username
                    else f"الآيدي: {user_id}"
                )
                safe_name = html.escape(worker["name"])
                admin_msg = (
                    f"🚨 <b>إشعار أمني - تسجيل دخول عامل:</b>\n\n"
                    f"👤 <b>العامل:</b> {safe_name}\n"
                    f"📱 <b>حساب التيليجرام الجديد:</b> {user_display}\n"
                    f"🕒 <b>الوقت:</b> الآن\n\n"
                    f"⚠️ <i>إذا لم تقم بتسليم الكود لهذا الموظف، يرجى تغيير الكود السري له فوراً من لوحة الإدارة.</i>"
                )
                try:
                    await bot.send_message(
                        worker["admin_tg_id"],
                        admin_msg,
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.warning("تعذر إرسال الإشعار الأمني لمدير المتجر: %s", e)

    except Exception as e:
        logger.error("خطأ أثناء تسجيل دخول العامل: %s", e, exc_info=True)
        await message.answer("⚠️ حدث خطأ مؤقت في الاتصال بقاعدة البيانات، يرجى المحاولة بعد لحظات.")
