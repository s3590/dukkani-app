# مسار الملف: bot/keyboards.py
import re
import json
import urllib.parse
from aiogram.types import (
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    ReplyKeyboardMarkup, 
    KeyboardButton
)
from config import ADMIN_ID


# =====================================================================
# 🛠️ دوال مساعدة لفحص الصلاحيات
# =====================================================================
def _normalize_permissions(permissions) -> set:
    """تحويل الصلاحيات إلى مجموعة (Set) سواء كانت نص JSON أو قائمة"""
    if permissions is None:
        return {"all"}
    if isinstance(permissions, str):
        try:
            parsed = json.loads(permissions)
            if isinstance(parsed, list):
                return set(parsed)
        except Exception:
            return {permissions}
    if isinstance(permissions, (list, tuple, set)):
        return set(permissions)
    return set()


# =====================================================================
# 📋 لوحة التحكم الشفافة الرئيسية (Inline Keyboard)
# =====================================================================
def get_main_keyboard(permissions=None) -> InlineKeyboardMarkup:
    """
    إنشاء أزرار لوحة التحكم حسب صلاحيات المستخدم (مدير عام أو كاشير/عامل)
    """
    perms = _normalize_permissions(permissions)
    is_owner = "all" in perms

    def has_perm(p: str) -> bool:
        return is_owner or p in perms

    kb = []

    # الصف 1: إدارة الزبائن
    row1 = []
    if has_perm("manage_customers"):
        row1.append(InlineKeyboardButton(text="👥 إضافة زبون", callback_data="add_customer"))
    if row1:
        kb.append(row1)

    # الصف 2: المبيعات والسداد (POS)
    row2 = []
    if has_perm("pos"):
        row2.append(InlineKeyboardButton(text="🛒 تسجيل طلب يدوي", callback_data="manual_order"))
    if has_perm("add_payment"):
        row2.append(InlineKeyboardButton(text="💰 تسجيل سداد", callback_data="add_payment"))
    if row2:
        kb.append(row2)

    # الصف 3: التقارير والمبيعات
    row3 = []
    if has_perm("view_reports"):
        row3.append(InlineKeyboardButton(text="📊 مبيعات اليوم", callback_data="daily_report"))
        row3.append(InlineKeyboardButton(text="📅 مبيعات فترة", callback_data="range_report"))
    if row3:
        kb.append(row3)

    # الصف 4: كشف الحساب والرابط
    row4 = []
    if has_perm("view_reports"):
        row4.append(InlineKeyboardButton(text="👤 كشف حساب زبون", callback_data="customer_statement"))
    if is_owner:
        row4.append(InlineKeyboardButton(text="🔗 رابط تطبيقي", callback_data="my_app_link"))
    if row4:
        kb.append(row4)

    # الصف 5: تعديل سقف الديون
    row5 = []
    if has_perm("manage_customers"):
        row5.append(InlineKeyboardButton(text="⚙️ تعديل سقف الدين", callback_data="edit_credit_limit"))
    if row5:
        kb.append(row5)

    # الصف 6: التعبئة التلقائية للمخزون
    if has_perm("manage_products"):
        kb.append([InlineKeyboardButton(text="🪄 تعبئة المتجر تلقائياً", callback_data="auto_fill_products")])

    # الصف 7: إرسال العروض والإعلانات (خاص بصاحب المتجر فقط)
    if is_owner:
        kb.append([InlineKeyboardButton(text="📢 إرسال عرض للزبائن", callback_data="send_promo")])

    return InlineKeyboardMarkup(inline_keyboard=kb)


# =====================================================================
# ⌨️ الأزرار السفلية الدائمة (Reply Keyboard)
# =====================================================================
def get_bottom_keyboard(user_id=None, permissions=None) -> ReplyKeyboardMarkup:
    """
    إنشاء القائمة السفلية الثابتة في تطبيق التيليجرام
    """
    # 1. أزرار المدير العام للمنصة (Super Admin) 👑
    if user_id == ADMIN_ID:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="🎛️ غرفة العمليات"), KeyboardButton(text="📱 دخول التطبيق")],
                [KeyboardButton(text="🧪 اختبار الضغط"), KeyboardButton(text="☢️ فورمات النظام")]
            ],
            resize_keyboard=True,
            is_persistent=True
        )

    # 2. أزرار أصحاب البقالات والكادر 🏪
    perms = _normalize_permissions(permissions)
    has_manage_products = ("all" in perms) or ("manage_products" in perms)

    keyboard = [
        [KeyboardButton(text="📱 بيانات الدخول للتطبيق")]
    ]

    row2 = []
    if has_manage_products:
        row2.append(KeyboardButton(text="📦 رفع صورة منتج"))
    row2.append(KeyboardButton(text="🎧 الدعم الفني"))
    keyboard.append(row2)

    keyboard.append([
        KeyboardButton(text="🏠 الرئيسية"), 
        KeyboardButton(text="❌ إلغاء")
    ])

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        is_persistent=True
    )


# =====================================================================
# 📲 زر الواتساب الذكي للزبائن
# =====================================================================
def get_whatsapp_btn(phone: str, message: str) -> InlineKeyboardMarkup | None:
    """
    توليد زر مراسلة عبر واتساب بعد تنظيف الرقم وإضافة كود الدولة المناسب
    """
    if not phone or str(phone).strip().lower() in ['none', 'null', '']:
        return None

    # استخراج الأرقام فقط والتخلص من الرموز والمسافات
    digits = re.sub(r'\D', '', str(phone))
    if not digits:
        return None

    # إزالة الصفرين الدوليين 00 إذا وُجدت
    if digits.startswith("00"):
        digits = digits[2:]

    # المعالجة الذكية للأرقام المحلية (مثل أرقام اليمن: 7XXXXXXXX أو 07XXXXXXXX)
    if digits.startswith("0") and len(digits) == 10:
        digits = "967" + digits[1:]
    elif len(digits) == 9 and digits.startswith("7"):
        digits = "967" + digits

    encoded_msg = urllib.parse.quote(message)
    url = f"https://wa.me/{digits}?text={encoded_msg}"

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📲 إرسال إشعار للزبون عبر واتساب", url=url)]
    ])
