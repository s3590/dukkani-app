# مسار الملف: bot/states.py
from aiogram.fsm.state import State, StatesGroup

# ==========================================
# 🏪 حالات المتجر وإدارة العمليات اليومية
# ==========================================
class AddCustomer(StatesGroup):
    """إضافة زبون جديد لحساب البقالة"""
    name = State()
    phone = State()
    password = State()
    balance = State()

class AddProduct(StatesGroup):
    """إضافة منتج ورفع صورته عبر البوت"""
    name = State()
    price = State()
    photo = State()
    stock = State()
    barcode = State()

class ManualOrder(StatesGroup):
    """تسجيل طلب يدوي من البوت (كاشير سريع)"""
    select_customer = State()
    build_cart = State()
    cash_paid = State()

class AddPayment(StatesGroup):
    """تسجيل دفعة سداد دين نقدي"""
    select_customer = State()
    amount = State()

class RangeReport(StatesGroup):
    """استخراج تقرير مبيعات وحركات لفترة محددة"""
    start_date = State()
    end_date = State()

class EditCreditLimit(StatesGroup):
    """تعديل الحد الأقصى لسقف دين الزبون"""
    select_customer = State()
    new_limit = State()

class SendPromo(StatesGroup):
    """إرسال إشعار ترويجي جماعي لزبائن المتجر"""
    waiting_for_promo_msg = State()

class RegisterStore(StatesGroup):
    """طلب تسجيل بقالة جديدة من المستخدمين الجدد"""
    waiting_for_name = State()
    waiting_for_phone = State()

class Support(StatesGroup):
    """نظام التذاكر والدعم الفني المتبادل بين المتجر والإدارة"""
    waiting_for_msg = State()
    waiting_for_reply = State()


# ==========================================
# 👑 حالات المدير العام للمنصة (Super Admin)
# ==========================================
class AdminStates(StatesGroup):
    """إجراءات لوحة تحكم القيادة العليا"""
    waiting_for_broadcast = State()     # رسائل إذاعية لجميع المتاجر
    waiting_for_format_confirm = State() # كلمة سر تأكيد فورمات النظام (لحمايته من الخطأ)
    waiting_for_store_credit = State()  # شحن رصيد رسائل أو باقة لمتجر

class AdminCatalog(StatesGroup):
    """إدارة الكتالوج العام للمنتجات العالمية"""
    waiting_for_product_name = State()
    waiting_for_product_image = State()


# ==========================================
# 👷‍♂️ حالات إدارة الموظفين والعمال (اختياري)
# ==========================================
class AddWorker(StatesGroup):
    """إضافة كاشير أو عامل جديد للمتجر عبر البوت"""
    name = State()
    passcode = State()
    role = State()
