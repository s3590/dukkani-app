# مسار الملف: bot/handlers/store.py
import os
import csv
import json
import html
import random
import bcrypt
import asyncio
import logging
from io import StringIO
from datetime import datetime, timedelta, date

from aiogram import Router, F, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.types.web_app_info import WebAppInfo
from firebase_admin import messaging as fcm_messaging

import database
from bot.setup import bot
from config import ADMIN_ID
from bot.states import (
    AddCustomer, AddProduct, ManualOrder, AddPayment, 
    Support, SendPromo, RangeReport, EditCreditLimit, RegisterStore
)
from bot.keyboards import get_main_keyboard, get_bottom_keyboard, get_whatsapp_btn
from services.notifications import send_push_notification

logger = logging.getLogger(__name__)
store_router = Router()

async def get_user_store_context(conn, telegram_id: int):
    """دعم أصحاب المتاجر والموظفين تلقائياً"""
    store = await conn.fetchrow("SELECT id, name, status, archive_group_id FROM stores WHERE telegram_id = $1", telegram_id)
    if store:
        return {"store_id": store['id'], "store_name": store['name'], "is_owner": True, "status": store['status'], "permissions": ["all"], "worker_name": "المدير العام"}
        
    worker = await conn.fetchrow("""
        SELECT w.id, w.name, w.store_id, w.permissions, s.name as store_name, s.status as store_status
        FROM workers w JOIN stores s ON w.store_id = s.id
        WHERE w.telegram_id = $1 AND w.is_deleted = FALSE
    """, telegram_id)
    if worker:
        try:
            perms = json.loads(worker['permissions']) if worker['permissions'] else ["pos"]
        except Exception:
            perms = ["pos"]
        return {"store_id": worker['store_id'], "store_name": worker['store_name'], "is_owner": False, "status": worker['store_status'], "permissions": perms, "worker_name": worker['name']}
    return None

# =====================================================================
# 🤖 أوامر البداية والإلغاء
# =====================================================================
@store_router.message(CommandStart())
async def start_cmd(message: types.Message, state: FSMContext):
    tid = message.from_user.id
    async with database.pool.acquire() as conn:
        store = await conn.fetchrow("SELECT id, name, status FROM stores WHERE telegram_id = $1", tid)
        if store:
            if store['status'] == 'suspended':
                return await message.answer("⛔ حسابك موقوف. تواصل مع الإدارة.", reply_markup=get_bottom_keyboard(tid))
            await message.answer("تم فتح القائمة الرئيسية 👇", reply_markup=get_bottom_keyboard(tid))
            return await message.answer(f"مرحباً بك في {store['name']} 🏪", reply_markup=get_main_keyboard())
        
        worker = await conn.fetchrow("SELECT store_id, name, permissions FROM workers WHERE telegram_id = $1", tid)
        if worker:
            store_name = await conn.fetchval("SELECT name FROM stores WHERE id = $1", worker['store_id'])
            # 🛡️ حماية من انهيار البوت إذا كانت الصلاحيات فارغة أو غير صالحة
            try:
                perms = json.loads(worker['permissions']) if worker['permissions'] else ["pos"]
            except Exception:
                perms = ["pos"]
                
            await message.answer("تم فتح قائمة العامل 👇", reply_markup=get_bottom_keyboard(tid, perms))
            return await message.answer(f"مرحباً بك يا {worker['name']} (عامل في {store_name}) 👷‍♂️", reply_markup=get_main_keyboard(perms))
        
        # 🚀 إذا لم يكن مسجلاً، نعرض له رسالة الترحيب وزر طلب الاشتراك
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏪 طلب تسجيل بقالة جديدة", callback_data="request_new_store")]
        ])
        await message.answer("أهلاً بك في نظام دُكّاني! 🚀\nالنظام السحابي الأذكى لإدارة البقالات.\n\nيبدو أنك غير مسجل لدينا. هل ترغب في تسجيل بقالتك؟", reply_markup=kb)

@store_router.message(F.text == "❌ إلغاء")
async def cancel_action(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("تم إلغاء العملية الحالية ❌", reply_markup=get_bottom_keyboard(message.from_user.id))

@store_router.message(F.text == "🏠 الرئيسية")
async def go_home(message: types.Message, state: FSMContext):
    await state.clear()
    await start_cmd(message, state)

# =====================================================================
# 📦 إضافة منتج (عبر البوت لرفع الصورة)
# =====================================================================
@store_router.message(F.text == "📦 رفع صورة منتج")
async def add_prod_start(message: types.Message, state: FSMContext):
    await message.answer("أرسل اسم المنتج:")
    await state.set_state(AddProduct.name)

@store_router.message(AddProduct.name)
async def add_prod_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("أرسل سعر المنتج (بالريال):")
    await state.set_state(AddProduct.price)

@store_router.message(AddProduct.price)
async def add_prod_price(message: types.Message, state: FSMContext):
    try:
        price = float(message.text)
        await state.update_data(price=price)
        await message.answer("أرسل صورة للمنتج (أو أرسل 'تخطي'):")
        await state.set_state(AddProduct.photo)
    except:
        await message.answer("الرجاء إرسال رقم صحيح للسعر.")

@store_router.message(AddProduct.photo)
async def add_prod_photo(message: types.Message, state: FSMContext):
    file_id = message.photo[-1].file_id if message.photo else None
    await state.update_data(photo=file_id)
    await message.answer("كم حبة متوفرة في المخزن؟ (أو أرسل 'تخطي'):")
    await state.set_state(AddProduct.stock)

@store_router.message(AddProduct.stock)
async def add_prod_stock(message: types.Message, state: FSMContext):
    stock = int(message.text) if message.text.isdigit() else None
    await state.update_data(stock=stock)
    await message.answer("🏷️ أرسل رقم الباركود للمنتج (أو أرسل 'تخطي'):")
    await state.set_state(AddProduct.barcode)

@store_router.message(AddProduct.barcode)
async def add_prod_barcode(message: types.Message, state: FSMContext):
    barcode = message.text.strip() if message.text != 'تخطي' else None
    data = await state.get_data()
    
    async with database.pool.acquire() as conn:
        store_id = await conn.fetchval("SELECT id FROM stores WHERE telegram_id = $1", message.from_user.id)
        pid = await conn.fetchval("INSERT INTO global_products (name, category) VALUES ($1, 'أخرى') RETURNING id", data['name'])
        await conn.execute("""
            INSERT INTO store_products (store_id, product_id, custom_name, price, telegram_file_id, stock, barcode)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
        """, store_id, pid, data['name'], data['price'], data['photo'], data['stock'], barcode)
        
    await message.answer(f"✅ تم إضافة المنتج ({data['name']}) بنجاح!", reply_markup=get_main_keyboard())
    await state.clear()

# =====================================================================
# 👥 إضافة زبون
# =====================================================================
@store_router.callback_query(F.data == "add_customer")
async def add_cust_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer("أرسل اسم الزبون:")
    await state.set_state(AddCustomer.name)

@store_router.message(AddCustomer.name)
async def add_cust_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("أرسل رقم هاتفه:")
    await state.set_state(AddCustomer.phone)

@store_router.message(AddCustomer.phone)
async def add_cust_phone(message: types.Message, state: FSMContext):
    await state.update_data(phone=message.text)
    await message.answer("أرسل كلمة مرور بسيطة (مثال: 1234):")
    await state.set_state(AddCustomer.password)

@store_router.message(AddCustomer.password)
async def add_cust_pass(message: types.Message, state: FSMContext):
    await state.update_data(password=message.text)
    await message.answer("كم عليه دين سابق؟ (اكتب 0 إذا لا يوجد):")
    await state.set_state(AddCustomer.balance)

@store_router.message(AddCustomer.balance)
async def add_cust_balance(message: types.Message, state: FSMContext):
    try:
        balance = float(message.text)
        data = await state.get_data()
        
        # 🚀 التشفير في الخلفية لمنع تجميد البوت
        hashed_bytes = await asyncio.to_thread(bcrypt.hashpw, data['password'].encode('utf-8'), bcrypt.gensalt())
        hashed_pw = hashed_bytes.decode('utf-8')
        
        async with database.pool.acquire() as conn:
            store_id = await conn.fetchval("SELECT id FROM stores WHERE telegram_id = $1", message.from_user.id)
            cid = await conn.fetchval("INSERT INTO customers (store_id, phone, name, password, balance) VALUES ($1, $2, $3, $4, $5) RETURNING id", store_id, data['phone'], data['name'], hashed_pw, balance)
            if balance > 0:
                await conn.execute("INSERT INTO transactions (store_id, customer_id, trans_type, amount, details) VALUES ($1, $2, 'opening', $3, 'رصيد افتتاحي')", store_id, cid, balance)
                
        await message.answer(f"✅ تم إضافة الزبون {data['name']} بنجاح!", reply_markup=get_main_keyboard())
        await state.clear()
    except Exception as e:
        await message.answer("❌ خطأ في الإدخال.")
        await state.clear()

# =====================================================================
# 💰 تسجيل سداد
# =====================================================================
@store_router.callback_query(F.data == "add_payment")
async def add_payment_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    async with database.pool.acquire() as conn:
        store_id = await conn.fetchval("SELECT id FROM stores WHERE telegram_id = $1", callback.from_user.id)
        customers = await conn.fetch("SELECT id, name, balance FROM customers WHERE store_id = $1 AND balance > 0", store_id)
        
    if not customers: return await callback.message.answer("لا يوجد زبائن عليهم ديون.")
        
    kb = [[InlineKeyboardButton(text=f"{c['name']} (عليه {c['balance']:.0f})", callback_data=f"paycust_{c['id']}")] for c in customers]
    await callback.message.answer("اختر الزبون الذي سدد الدفعة:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.set_state(AddPayment.select_customer)

@store_router.callback_query(AddPayment.select_customer, F.data.startswith("paycust_"))
async def add_payment_amount(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    cid = int(callback.data.split("_")[1])
    await state.update_data(customer_id=cid)
    await callback.message.edit_text("كم المبلغ الذي سدده الزبون؟")
    await state.set_state(AddPayment.amount)

@store_router.message(AddPayment.amount)
async def add_payment_process(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text.strip())
        if amount <= 0:
            return await message.answer("الرجاء إدخال مبلغ سداد أكبر من الصفر.")
            
        data = await state.get_data()
        cid = data['customer_id']
        
        async with database.pool.acquire() as conn:
            ctx = await get_user_store_context(conn, message.from_user.id)
            if not ctx:
                await state.clear()
                return await message.answer("❌ المتجر غير مسجل.")
                
            store_id = ctx['store_id']
            async with conn.transaction():
                cust = await conn.fetchrow("SELECT phone, balance FROM customers WHERE id = $1 AND store_id = $2 FOR UPDATE", cid, store_id)
                if not cust:
                    return await message.answer("الزبون غير موجود.")
                    
                new_balance = float(cust['balance']) - amount
                await conn.execute("UPDATE customers SET balance = $1 WHERE id = $2", new_balance, cid)
                await conn.execute("""
                    INSERT INTO transactions (store_id, customer_id, trans_type, amount, details) 
                    VALUES ($1, $2, 'payment', $3, 'سداد دفعة نقدية')
                """, store_id, cid, -amount)
                
            c_phone = cust['phone']
            
        wa_msg = f"تم استلام دفعة نقدية بقيمة {amount:.0f} ريال. رصيد دينك المتبقي: {new_balance:.0f} ريال. شكراً لك!"
        await message.answer(
            f"✅ تم تسجيل السداد بنجاح!\nالمبلغ: {amount:.0f} ريال | المتبقي: {new_balance:.0f} ريال", 
            reply_markup=get_whatsapp_btn(c_phone, wa_msg)
        )
        asyncio.create_task(
            send_push_notification(cid, "💰 تم استلام دفعتك", f"تم قيد دفعة بقيمة {amount:.0f} ريال. المتبقي عليك: {new_balance:.0f} ريال.")
        )
        await state.clear()
    except ValueError:
        await message.answer("❌ الرجاء إدخال رقم صحيح للمبلغ.")
    except Exception as e:
        logger.error("خطأ في تسجيل السداد: %s", e)
        await message.answer("❌ حدث خطأ أثناء تسجيل السداد.")

# =====================================================================
# 📊 التقارير وكشف الحساب
# =====================================================================
@store_router.callback_query(F.data == "daily_report")
async def generate_daily_report(callback: types.CallbackQuery):
    await callback.answer("جاري تجهيز التقرير...", show_alert=False)
    async with database.pool.acquire() as conn:
        store_id = await conn.fetchval("SELECT id FROM stores WHERE telegram_id = $1", callback.from_user.id)
        sales = await conn.fetch("SELECT c.name, t.amount, t.details, t.created_at FROM transactions t JOIN customers c ON t.customer_id = c.id WHERE t.store_id = $1 AND DATE(t.created_at) = CURRENT_DATE", store_id)
        
    if not sales: return await callback.message.answer("لا توجد حركات مسجلة اليوم.")
        
    csv_file = StringIO()
    writer = csv.writer(csv_file)
    writer.writerow(['الزبون', 'المبلغ', 'التفاصيل', 'الوقت'])
    for s in sales: writer.writerow([s['name'], s['amount'], s['details'], str(s['created_at'])[:16]])
    
    csv_bytes = csv_file.getvalue().encode('utf-8-sig')
    document = BufferedInputFile(csv_bytes, filename=f"daily_report_{date.today()}.csv")
    await bot.send_document(callback.from_user.id, document, caption=f"📊 تقرير حركات اليوم ({date.today()})")

@store_router.callback_query(F.data == "customer_statement")
async def cust_statement_start(callback: types.CallbackQuery):
    await callback.answer()
    async with database.pool.acquire() as conn:
        store_id = await conn.fetchval("SELECT id FROM stores WHERE telegram_id = $1", callback.from_user.id)
        customers = await conn.fetch("SELECT id, name, balance FROM customers WHERE store_id = $1", store_id)
        
    if not customers: return await callback.message.answer("لا يوجد زبائن مسجلين.")
        
    kb = [[InlineKeyboardButton(text=f"{c['name']} (دين: {c['balance']:.0f})", callback_data=f"stmt_{c['id']}")] for c in customers]
    await callback.message.answer("👤 اختر الزبون لعرض كشف حسابه:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@store_router.callback_query(F.data.startswith("stmt_"))
async def cust_statement_generate(callback: types.CallbackQuery):
    await callback.answer("جاري تجهيز كشف الحساب...", show_alert=False)
    cid = int(callback.data.split("_")[1])
    
    async with database.pool.acquire() as conn:
        cust = await conn.fetchrow("SELECT name, balance FROM customers WHERE id = $1", cid)
        trans = await conn.fetch("SELECT trans_type, amount, details, created_at FROM transactions WHERE customer_id = $1 ORDER BY created_at DESC", cid)
        
    html_content = f"""
    <html dir="rtl" lang="ar">
    <head><meta charset="utf-8"><title>كشف حساب - {cust['name']}</title></head>
    <body style="font-family: Arial; padding: 20px;">
        <h2>كشف حساب العميل: {cust['name']}</h2>
        <h3>إجمالي الدين الحالي: {cust['balance']:.0f} ريال</h3>
        <table border="1" width="100%" style="border-collapse: collapse; text-align: center;">
            <tr style="background: #eee;"><th>نوع العملية</th><th>المبلغ</th><th>التفاصيل</th><th>التاريخ</th></tr>
    """
    for t in trans:
        t_type = "سداد" if t['amount'] < 0 else "دين/طلب"
        html_content += f"<tr><td>{t_type}</td><td>{abs(t['amount']):.0f}</td><td>{t['details']}</td><td>{t['created_at'].strftime('%Y-%m-%d %H:%M')}</td></tr>"
    html_content += "</table></body></html>"
    
    html_bytes = html_content.encode('utf-8')
    document = BufferedInputFile(html_bytes, filename=f"statement_{cid}.html")
    await bot.send_document(callback.from_user.id, document, caption=f"👤 كشف حساب: {cust['name']}")

# =====================================================================
# 🔗 روابط وإعدادات أخرى
# =====================================================================
@store_router.callback_query(F.data == "my_app_link")
async def send_my_app_link(callback: types.CallbackQuery):
    await callback.answer()
    async with database.pool.acquire() as conn:
        store_id = await conn.fetchval("SELECT id FROM stores WHERE telegram_id = $1", callback.from_user.id)
        
    # 👇 جلب الرابط من المتغيرات 👇
    base_url = os.getenv("BASE_URL", "https://dukkani-app.onrender.com" )
    link = f"{base_url}/?store={store_id}"
    await callback.message.answer(f"📲 **رابط تطبيق البقالة الخاص بك:**\n\n`{link}`\n\n👆 انسخ هذا الرابط وأرسله لزبائنك!", parse_mode="Markdown" )

@store_router.callback_query(F.data == "edit_credit_limit")
async def edit_limit_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    async with database.pool.acquire() as conn:
        store_id = await conn.fetchval("SELECT id FROM stores WHERE telegram_id = $1", callback.from_user.id)
        customers = await conn.fetch("SELECT id, name, credit_limit FROM customers WHERE store_id = $1", store_id)
        
    if not customers: return await callback.message.answer("لا يوجد زبائن مسجلين.")
        
    kb = [[InlineKeyboardButton(text=f"{c['name']} (السقف: {c['credit_limit']})", callback_data=f"limitcust_{c['id']}")] for c in customers]
    await callback.message.answer("اختر الزبون لتعديل سقف دينه:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.set_state(EditCreditLimit.select_customer)

@store_router.callback_query(EditCreditLimit.select_customer, F.data.startswith("limitcust_"))
async def edit_limit_amount(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    cid = int(callback.data.split("_")[1])
    await state.update_data(customer_id=cid)
    await callback.message.edit_text("أرسل سقف الدين الجديد (بالريال):")
    await state.set_state(EditCreditLimit.new_limit)

@store_router.message(EditCreditLimit.new_limit)
async def edit_limit_save(message: types.Message, state: FSMContext):
    try:
        new_limit = float(message.text)
        data = await state.get_data()
        async with database.pool.acquire() as conn:
            await conn.execute("UPDATE customers SET credit_limit = $1 WHERE id = $2", new_limit, data['customer_id'])
        await message.answer(f"✅ تم تحديث سقف الدين بنجاح إلى {new_limit} ريال.", reply_markup=get_bottom_keyboard(message.from_user.id))
        await state.clear()
    except:
        await message.answer("❌ الرجاء إدخال رقم صحيح.")

@store_router.callback_query(F.data == "send_promo")
async def promo_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer("📢 **إرسال عرض للزبائن:**\nأرسل الآن نص العرض أو الرسالة:")
    await state.set_state(SendPromo.waiting_for_promo_msg)

@store_router.message(SendPromo.waiting_for_promo_msg)
async def promo_send(message: types.Message, state: FSMContext):
    await message.answer("⏳ جاري إرسال الإشعارات لجميع الزبائن...")
    async with database.pool.acquire() as conn:
        store_id = await conn.fetchval("SELECT id FROM stores WHERE telegram_id = $1", message.from_user.id)
        customers = await conn.fetch("SELECT id FROM customers WHERE store_id = $1", store_id)
    
    if not customers: return await message.answer("❌ لا يوجد لديك زبائن مسجلين.")
        
    customer_ids = [c['id'] for c in customers]
    if customer_ids:
        # 👇 تم إصلاح طريقة تمرير المصفوفة لقاعدة البيانات 👇
        tokens = await conn.fetch("SELECT subscription_json FROM push_subscriptions WHERE user_id = ANY($1::int[]) AND user_type = 'customer'", customer_ids)
        fcm_tokens = [t['subscription_json'] for t in tokens if t['subscription_json']]
        
        if fcm_tokens:
            try:
                # 🛡️ تقسيم التوكنات إلى دفعات (كل دفعة 500 كحد أقصى) لتجنب خطأ Firebase
                chunk_size = 500
                for i in range(0, len(fcm_tokens), chunk_size):
                    chunk = fcm_tokens[i:i + chunk_size]
                    msg = fcm_messaging.MulticastMessage(
                        notification=fcm_messaging.Notification(title="📢 عرض خاص من البقالة!", body=message.text), 
                        tokens=chunk
                    )
                    # 👇 استخدام to_thread لمنع تجميد البوت أثناء إرسال آلاف الإشعارات 👇
                    await asyncio.to_thread(fcm_messaging.send_multicast, msg)
            except Exception as e: pass

    await message.answer(f"✅ تم إرسال العرض بنجاح إلى {len(customer_ids)} زبون!")
    await state.clear()

@store_router.message(F.text == "🎧 الدعم الفني")
async def support_start(message: types.Message, state: FSMContext):
    await message.answer("✍️ اكتب رسالتك أو مشكلتك وسنقوم بالرد عليك في أقرب وقت:")
    await state.set_state(Support.waiting_for_msg)

@store_router.message(Support.waiting_for_msg)
async def support_receive(message: types.Message, state: FSMContext):
    async with database.pool.acquire() as conn:
        store = await conn.fetchrow("SELECT name, phone FROM stores WHERE telegram_id = $1", message.from_user.id)
    
    msg_to_admin = f"📩 **رسالة دعم فني جديدة:**\n🏪 البقالة: {store['name']}\n💬 الرسالة:\n{message.text}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ رد على البقالة", callback_data=f"reply_{message.from_user.id}")]])
    
    await bot.send_message(ADMIN_ID, msg_to_admin, reply_markup=kb)
    await message.answer("✅ تم إرسال رسالتك للإدارة بنجاح.")
    await state.clear()

@store_router.callback_query(F.data.startswith("reply_"))
async def admin_reply_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.from_user.id != ADMIN_ID: return
    store_tid = int(callback.data.split("_")[1])
    await state.update_data(reply_to=store_tid)
    await callback.message.answer("✍️ اكتب ردك الآن:")
    await state.set_state(Support.waiting_for_reply)

@store_router.message(Support.waiting_for_reply)
async def admin_reply_send(message: types.Message, state: FSMContext):
    data = await state.get_data()
    store_tid = data['reply_to']
    await bot.send_message(store_tid, f"👨‍💻 **رد من الإدارة:**\n\n{message.text}")  
    await message.answer("✅ تم إرسال الرد للبقالة.")
    await state.clear()

# =====================================================================
# 🪄 التعبئة التلقائية للمتجر
# =====================================================================
@store_router.callback_query(F.data == "auto_fill_products")
async def auto_fill_products_handler(callback: types.CallbackQuery):
    await callback.answer("⏳ جاري تعبئة المتجر... الرجاء الانتظار", show_alert=False)
    
    default_products = [
        {"name": "سكر السعيد 10 كيلو", "price": 8500, "category": "مواد غذائية"},
        {"name": "دقيق السنابل 10 كيلو", "price": 7000, "category": "مواد غذائية"},
        {"name": "زيت شيف 1 لتر", "price": 2500, "category": "مواد غذائية"},
        {"name": "شاي الكبوس 227 جرام", "price": 1800, "category": "مواد غذائية"},
        {"name": "صلصة المدهش", "price": 400, "category": "معلبات"},
        {"name": "تونة المكلا", "price": 900, "category": "معلبات"},
        {"name": "زبادي الهناء", "price": 250, "category": "ألبان وأجبان"},
        {"name": "بيبسي عائلي 2.25 لتر", "price": 1200, "category": "عصائر ومشروبات"},
        {"name": "بسكويت أبو ولد", "price": 150, "category": "بسكويت وحلويات"},
        {"name": "صابون كريستال", "price": 300, "category": "منظفات"}
    ]

    async with database.pool.acquire() as conn:
        # 👇 التحقق مما إذا كان صاحب بقالة أو عامل 👇
        store_id = await conn.fetchval("SELECT id FROM stores WHERE telegram_id = $1", callback.from_user.id)
        if not store_id:
            store_id = await conn.fetchval("SELECT store_id FROM workers WHERE telegram_id = $1", callback.from_user.id)
            
        if not store_id:
            return await callback.message.answer("❌ عذراً، لم يتم العثور على بقالتك.")
            
        count = await conn.fetchval("SELECT COUNT(*) FROM store_products WHERE store_id = $1", store_id)

        if count > 5:
            return await callback.message.answer("⚠️ متجرك يحتوي على منتجات بالفعل. لا يمكن استخدام التعبئة التلقائية لتجنب التكرار.")
        
        for p in default_products:
            pid = await conn.fetchval("INSERT INTO global_products (name, category) VALUES ($1, $2) ON CONFLICT DO NOTHING RETURNING id", p['name'], p['category'])
            if not pid:
                pid = await conn.fetchval("SELECT id FROM global_products WHERE name = $1", p['name'])
            
            await conn.execute("""
                INSERT INTO store_products (store_id, product_id, custom_name, price, category, stock, barcode)
                VALUES ($1, $2, $3, $4, $5, NULL, $6)
            """, store_id, pid, p['name'], p['price'], p['category'], str(random.randint(10000000, 99999999))) # 🛡️ إضافة باركود عشوائي للفرز
            
    await callback.message.answer(f"✅ تم إضافة {len(default_products)} منتج إلى متجرك بنجاح!\nيمكنك تعديل أسعارها من (إدارة البقالة).")

# =====================================================================
# 📅 تقرير مبيعات فترة محددة
# =====================================================================
@store_router.callback_query(F.data == "range_report")
async def range_report_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer("📅 أرسل تاريخ البداية بصيغة YYYY-MM-DD\n(مثال: 2026-08-01):")
    await state.set_state(RangeReport.start_date)

@store_router.message(RangeReport.start_date)
async def range_report_start_date(message: types.Message, state: FSMContext):
    await state.update_data(start_date=message.text)
    await message.answer("📅 أرسل تاريخ النهاية بصيغة YYYY-MM-DD\n(مثال: 2026-08-31):")
    await state.set_state(RangeReport.end_date)

@store_router.message(RangeReport.end_date)
async def range_report_end_date(message: types.Message, state: FSMContext):
    start_date = (await state.get_data())['start_date']
    end_date = message.text
    await message.answer("⏳ جاري تجهيز التقرير...")
    
    try:
        async with database.pool.acquire() as conn:
            store_id = await conn.fetchval("SELECT id FROM stores WHERE telegram_id = $1", message.from_user.id)
            sales = await conn.fetch("""
                SELECT c.name, t.amount, t.details, t.created_at 
                FROM transactions t JOIN customers c ON t.customer_id = c.id 
                WHERE t.store_id = $1 AND DATE(t.created_at) >= $2 AND DATE(t.created_at) <= $3
                ORDER BY t.created_at DESC
            """, store_id, datetime.strptime(start_date, "%Y-%m-%d").date(), datetime.strptime(end_date, "%Y-%m-%d").date())
            
        if not sales:
            return await message.answer("لا توجد حركات في هذه الفترة.")
            
        csv_file = StringIO()
        writer = csv.writer(csv_file)
        writer.writerow(['الزبون', 'المبلغ', 'التفاصيل', 'الوقت'])
        for s in sales: writer.writerow([s['name'], s['amount'], s['details'], str(s['created_at'])[:16]])
        
        file_path = f"range_report_{store_id}.csv"
        with open(file_path, "w", encoding="utf-8-sig") as f: f.write(csv_file.getvalue())
        await bot.send_document(message.from_user.id, FSInputFile(file_path), caption=f"📊 تقرير المبيعات من {start_date} إلى {end_date}")
        os.remove(file_path)
        await state.clear()
    except Exception as e:
        await message.answer("❌ خطأ في صيغة التاريخ. تأكد من كتابته هكذا: 2026-08-01")
        await state.clear()

# =====================================================================
# 🛒 تسجيل طلب يدوي (عبر البوت)
# =====================================================================
@store_router.callback_query(F.data == "manual_order")
async def manual_order_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    async with database.pool.acquire() as conn:
        store_id = await conn.fetchval("SELECT id FROM stores WHERE telegram_id = $1", callback.from_user.id)
        customers = await conn.fetch("SELECT id, name FROM customers WHERE store_id = $1", store_id)
        
    if not customers: return await callback.message.answer("لا يوجد زبائن مسجلين.")
        
    kb = [[InlineKeyboardButton(text=c['name'], callback_data=f"selcust_{c['id']}")] for c in customers]
    await callback.message.answer("اختر الزبون:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.set_state(ManualOrder.select_customer)

@store_router.callback_query(ManualOrder.select_customer, F.data.startswith("selcust_"))
async def manual_order_customer(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    cid = int(callback.data.split("_")[1])
    await state.update_data(customer_id=cid, cart={}, total=0.0)
    
    async with database.pool.acquire() as conn:
        store_id = await conn.fetchval("SELECT id FROM stores WHERE telegram_id = $1", callback.from_user.id)
        products = await conn.fetch("SELECT id, custom_name, price FROM store_products WHERE store_id = $1 AND is_available=TRUE", store_id)
        
    kb = [[InlineKeyboardButton(text=f"{p['custom_name']} ({p['price']} ريال)", callback_data=f"additem_{p['id']}_{p['price']}_{p['custom_name']}")] for p in products]
    kb.append([InlineKeyboardButton(text="✅ إنهاء الطلب", callback_data="finish_cart")])
    
    await callback.message.edit_text("🛒 اختر المنتجات:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.set_state(ManualOrder.build_cart)

@store_router.callback_query(ManualOrder.build_cart, F.data.startswith("additem_"))
async def manual_order_add_item(callback: types.CallbackQuery, state: FSMContext):
    _, pid, price, name = callback.data.split("_")
    data = await state.get_data()
    cart = data.get('cart', {})
    
    if pid in cart: cart[pid]['qty'] += 1
    else: cart[pid] = {'name': name, 'price': float(price), 'qty': 1}
    
    total = sum(item['price'] * item['qty'] for item in cart.values())
    await state.update_data(cart=cart, total=total)
    await callback.answer(f"تم إضافة {name}. الإجمالي: {total}")

@store_router.callback_query(ManualOrder.build_cart, F.data == "finish_cart")
async def manual_order_finish(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    if not data.get('cart'): return await callback.answer("السلة فارغة!", show_alert=True)
    await callback.message.edit_text(f"💰 إجمالي الفاتورة: {data['total']} ريال.\nكم دفع الزبون كاش؟ (اكتب 0 إذا كان كله آجل)")
    await state.set_state(ManualOrder.cash_paid)

@store_router.message(ManualOrder.cash_paid)
async def manual_order_process(message: types.Message, state: FSMContext):
    try:
        cash_paid = float(message.text.strip())
        if cash_paid < 0:
            return await message.answer("المبلغ المدفوع لا يمكن أن يكون سالباً.")
            
        data = await state.get_data()
        total = data['total']
        credit = total - cash_paid
        
        details = "\n".join([f"- {item['qty']}x {item['name']}" for item in data['cart'].values()])
        details_full = f"طلب يدوي:\n{details}\nالإجمالي: {total:.0f} | مدفوع: {cash_paid:.0f} | آجل: {credit:.0f}"
        
        async with database.pool.acquire() as conn:
            ctx = await get_user_store_context(conn, message.from_user.id)
            if not ctx: return
            store_id = ctx['store_id']
            cid = data['customer_id']
            
            # حماية ACID Transaction كاملة لضمان سلامة المخزون والدين معاً
            async with conn.transaction():
                cust = await conn.fetchrow("SELECT phone, balance FROM customers WHERE id = $1 AND store_id = $2 FOR UPDATE", cid, store_id)
                if not cust:
                    raise ValueError("الزبون غير موجود.")
                    
                for pid, item in data['cart'].items():
                    res = await conn.execute(
                        "UPDATE store_products SET stock = stock - $1 WHERE id = $2 AND (stock IS NULL OR stock >= $1)", 
                        item['qty'], int(pid)
                    )
                    if res == "UPDATE 0":
                        raise ValueError(f"عذراً، كمية ({item['name']}) غير متوفرة في المخزن.")
                
                new_balance = float(cust['balance'])
                if credit > 0:
                    new_balance += credit
                    await conn.execute("UPDATE customers SET balance = balance + $1 WHERE id = $2", credit, cid)
                    
                await conn.execute("""
                    INSERT INTO transactions (store_id, customer_id, trans_type, amount, details) 
                    VALUES ($1, $2, 'manual_order', $3, $4)
                """, store_id, cid, credit, details_full)
                
            c_phone = cust['phone']
            
        wa_msg = f"تم تسجيل طلبك بقيمة {total:.0f} ريال. المدفوع: {cash_paid:.0f} ريال، المتبقي آجل: {credit:.0f} ريال. رصيدك الحالي: {new_balance:.0f} ريال."
        await message.answer(
            f"✅ تم تسجيل الطلب وتحديث المخزون بنجاح!\nالإجمالي: {total:.0f} ريال | الآجل: {credit:.0f} ريال", 
            reply_markup=get_whatsapp_btn(c_phone, wa_msg)
        )
        if credit > 0:
            asyncio.create_task(
                send_push_notification(cid, "🛒 فاتورة جديدة (آجل)", f"تم تسجيل طلب بقيمة {total:.0f} ريال. رصيدك: {new_balance:.0f} ريال.")
            )
        await state.clear()
    except ValueError as ve:
        await message.answer(f"⚠️ {ve}")
    except Exception as e:
        logger.error("خطأ في الطلب اليدوي: %s", e)
        await message.answer("❌ حدث خطأ أثناء تسجيل الطلب.")

# =====================================================================
# 🛒 معالجة الطلبات المباشرة من التطبيق (أزرار البوت)
# =====================================================================
@store_router.callback_query(F.data.startswith("accept_order_"))
async def accept_online_order(callback: types.CallbackQuery):
    order_id = int(callback.data.split("_")[2])
    
    async with database.pool.acquire() as conn:
        store_id = await conn.fetchval("SELECT id FROM stores WHERE telegram_id = $1", callback.from_user.id)
        # تحديث ذري يمنع المعالجة المزدوجة إذا نقر الكاشير مرتين بسرعة
        order = await conn.fetchrow("""
            UPDATE orders SET status = 'preparing' 
            WHERE id = $1 AND store_id = $2 AND status = 'pending' 
            RETURNING customer_id
        """, order_id, store_id)
        
        if not order:
            return await callback.answer("تمت معالجة هذا الطلب مسبقاً أو غير موجود.", show_alert=True)
        
        if order['customer_id']:
            import asyncio
            asyncio.create_task(send_push_notification(order['customer_id'], "✅ تم قبول طلبك!", "البقالة تقوم بتجهيز طلبك الآن ⏳."))
            
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛵 إرسال الطلب (في الطريق)", callback_data=f"status_delivering_{order_id}")],
        [InlineKeyboardButton(text="✅ تم التسليم للزبون", callback_data=f"status_completed_{order_id}")]
    ])
    
    new_text = callback.message.text + "\n\n✅ **تم قبول الطلب (قيد التجهيز ⏳)**"
    await callback.message.edit_text(new_text, reply_markup=kb)

@store_router.callback_query(F.data.startswith("status_"))
async def update_order_status(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    new_status = parts[1]
    order_id = int(parts[2])
    
    async with database.pool.acquire() as conn:
        await conn.execute("UPDATE orders SET status = $1 WHERE id = $2", new_status, order_id)
        customer_id = await conn.fetchval("SELECT customer_id FROM orders WHERE id = $1", order_id)
        
    if new_status == 'delivering':
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ تم التسليم للزبون", callback_data=f"status_completed_{order_id}")]])
        new_text = callback.message.text.replace("قيد التجهيز ⏳", "في الطريق 🛵")
        await callback.message.edit_text(new_text, reply_markup=kb)
        if customer_id:
            await send_push_notification(customer_id, "🛵 طلبك في الطريق!", "المندوب في طريقه إليك، استعد لاستلام الطلب.")
        
    elif new_status == 'completed':
        new_text = callback.message.text.replace("في الطريق 🛵", "مكتمل ✅").replace("قيد التجهيز ⏳", "مكتمل ✅")
        await callback.message.edit_text(new_text, reply_markup=None)
        if customer_id:
            await send_push_notification(customer_id, "✅ اكتمل الطلب", "بالعافية عليك! تم تسليم الطلب بنجاح.")

@store_router.callback_query(F.data.startswith("reject_order_"))
async def reject_online_order(callback: types.CallbackQuery):
    order_id = int(callback.data.split("_")[2])
    
    async with database.pool.acquire() as conn:
        store_id = await conn.fetchval("SELECT id FROM stores WHERE telegram_id = $1", callback.from_user.id)
        order = await conn.fetchrow("SELECT customer_id, total_amount, payment_method, status FROM orders WHERE id = $1 AND store_id = $2", order_id, store_id)
        
        if not order or order['status'] != 'pending':
            return await callback.answer("الطلب غير متاح أو تمت معالجته.", show_alert=True)
            
        async with conn.transaction():
            await conn.execute("UPDATE orders SET status = 'rejected' WHERE id = $1", order_id)
            
            # استرجاع المخزون
            items = await conn.fetch("SELECT product_id, quantity FROM order_items WHERE order_id = $1", order_id)
            for item in items:
                await conn.execute("UPDATE store_products SET stock = stock + $1 WHERE id = $2 AND stock IS NOT NULL", item['quantity'], item['product_id'])
            
            # استرجاع الرصيد
            if order['payment_method'] == 'credit' and order['customer_id']:
                await conn.execute("UPDATE customers SET balance = balance - $1 WHERE id = $2", order['total_amount'], order['customer_id'])
                await conn.execute("""
                    INSERT INTO transactions (store_id, customer_id, order_id, trans_type, amount, details)
                    VALUES ($1, $2, $3, 'refund', $4, 'إلغاء طلب واسترجاع الرصيد')
                """, store_id, order['customer_id'], order_id, -order['total_amount'])
                
        if order['customer_id']:
            import asyncio
            asyncio.create_task(send_push_notification(order['customer_id'], "❌ نعتذر منك", "تم رفض طلبك من قبل البقالة. تم استرجاع رصيدك."))
            
    new_text = callback.message.text + "\n\n❌ **تم رفض الطلب وإلغاء القيود.**"
    await callback.message.edit_text(new_text, reply_markup=None)

# =====================================================================
# 👤 معالجة طلبات تسجيل الزبائن الجدد من البوت
# =====================================================================
@store_router.callback_query(F.data.startswith("approve_cust_"))
async def approve_new_customer(callback: types.CallbackQuery):
    cust_id = int(callback.data.split("_")[2])
    
    async with database.pool.acquire() as conn:
        store_id = await conn.fetchval("SELECT id FROM stores WHERE telegram_id = $1", callback.from_user.id)
        cust = await conn.fetchrow("SELECT name, status FROM customers WHERE id = $1 AND store_id = $2", cust_id, store_id)
        
        if not cust:
            return await callback.answer("الزبون غير موجود.", show_alert=True)
        if cust['status'] == 'active':
            return await callback.answer("تم قبول هذا الزبون مسبقاً.", show_alert=True)
            
        await conn.execute("UPDATE customers SET status = 'active' WHERE id = $1", cust_id)
        
        # إرسال إشعار للزبون بأن حسابه تم تفعيله
        import asyncio
        asyncio.create_task(send_push_notification(cust_id, "🎉 تم تفعيل حسابك!", "وافق صاحب البقالة على انضمامك. يمكنك الآن تسجيل الدخول وطلب المنتجات."))
        
    new_text = callback.message.text + "\n\n✅ **تم قبول الزبون وتفعيل حسابه بنجاح.**"
    await callback.message.edit_text(new_text, reply_markup=None)

@store_router.callback_query(F.data.startswith("reject_cust_"))
async def reject_new_customer(callback: types.CallbackQuery):
    cust_id = int(callback.data.split("_")[2])
    
    async with database.pool.acquire() as conn:
        store_id = await conn.fetchval("SELECT id FROM stores WHERE telegram_id = $1", callback.from_user.id)
        # حذف الزبون المرفوض نهائياً
        await conn.execute("DELETE FROM customers WHERE id = $1 AND store_id = $2 AND status = 'pending'", cust_id, store_id)
        
    new_text = callback.message.text + "\n\n❌ **تم رفض الزبون وحذف طلبه.**"
    await callback.message.edit_text(new_text, reply_markup=None)

# =====================================================================
# 📱 استخراج بيانات الدخول لتطبيق الإدارة (VIP)
# =====================================================================
@store_router.message(F.text == "📱 بيانات الدخول للتطبيق")
async def get_app_login_info(message: types.Message):
    tid = message.from_user.id
    
    async with database.pool.acquire() as conn:
        # 1. فحص هل هو صاحب بقالة؟
        store = await conn.fetchrow("SELECT id, name FROM stores WHERE telegram_id = $1", tid)
        if store:
            # البحث عن الكود السري (Passcode) الخاص به في جدول العمال (لأنه يعتبر المدير الأول)
            worker = await conn.fetchrow("SELECT passcode FROM workers WHERE store_id = $1 AND telegram_id = $2", store['id'], tid)
            
            if not worker:
                # إذا لم يكن له كود سري (حالة نادرة للقدامى)، نقوم بإنشاء واحد له
                import random
                new_passcode = str(random.randint(10000, 99999))
                await conn.execute("""
                    INSERT INTO workers (store_id, name, telegram_id, passcode, permissions) 
                    VALUES ($1, 'المدير العام', $2, $3, '["pos", "manage_products", "manage_customers", "add_payment", "view_reports"]')
                """, store['id'], tid, new_passcode)
                passcode = new_passcode
            else:
                passcode = worker['passcode']
                
            role_name = "المدير العام 👑"
            
        else:
            # 2. فحص هل هو عامل؟ (مع التأكد أنه لم يتم طرده)
            worker = await conn.fetchrow("SELECT store_id, name, passcode FROM workers WHERE telegram_id = $1 AND is_deleted = FALSE", tid)
            if worker:
                store = await conn.fetchrow("SELECT id, name FROM stores WHERE id = $1", worker['store_id'])
                passcode = worker['passcode']
                role_name = f"عامل ({worker['name']}) 👷‍♂️"
            else:
                return await message.answer("❌ عذراً، لم يتم العثور على حسابك في النظام." )

    # 👇 جلب الرابط من متغيرات البيئة ليدعم تغيير الدومين مستقبلاً 👇
    app_link = os.getenv("BASE_URL", "https://dukkani-app.onrender.com" )
    
    msg = f"""
🔐 **بيانات الدخول لتطبيق الإدارة**
🏪 البقالة: {store['name']}
👤 الصلاحية: {role_name}

🌐 **رابط التطبيق:**
{app_link}

📱 **رقم الدخول (الآيدي ):**
`{tid}`

🔑 **الكود السري:**
`{passcode}`

*(اضغط على الرقم أو الكود لنسخه مباشرة)*
    """
    
    # إضافة زر شفاف لفتح التطبيق مباشرة من داخل تيليجرام (Web App)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 فتح التطبيق الآن", web_app=types.WebAppInfo(url=app_link))]
    ])
    
    await message.answer(msg, reply_markup=kb, parse_mode="Markdown")

# =====================================================================
# 🚀 نظام تسجيل البقالات (B2B SaaS)
# =====================================================================
@store_router.callback_query(F.data == "request_new_store")
async def request_new_store_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer("📝 ما هو اسم بقالتك؟")
    await state.set_state(RegisterStore.waiting_for_name)

@store_router.message(RegisterStore.waiting_for_name)
async def request_new_store_name(message: types.Message, state: FSMContext):
    await state.update_data(store_name=message.text)
    await message.answer("📱 ما هو رقم هاتف البقالة (للتواصل)؟")
    await state.set_state(RegisterStore.waiting_for_phone)

@store_router.message(RegisterStore.waiting_for_phone)
async def request_new_store_phone(message: types.Message, state: FSMContext):
    store_name = (await state.get_data())['store_name']
    phone = message.text
    tid = message.from_user.id
    username = message.from_user.username or "لا يوجد"
    
    # إرسال الطلب للمدير العام
    msg_to_admin = f"🆕 **طلب اشتراك بقالة جديدة:**\n\n🏪 الاسم: {store_name}\n📱 الهاتف: {phone}\n👤 معرف تيليجرام: {tid}\n🔗 يوزرنيم: @{username}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ موافقة وإنشاء حساب", callback_data=f"admin_approve_store_{tid}")],
        [InlineKeyboardButton(text="❌ رفض", callback_data=f"admin_reject_store_{tid}")]
    ])
    
    try:
        await bot.send_message(ADMIN_ID, msg_to_admin, reply_markup=kb)
        await message.answer("✅ تم إرسال طلبك للإدارة بنجاح! سيتم الرد عليك قريباً.")
    except Exception as e:
        await message.answer("❌ حدث خطأ أثناء إرسال الطلب.")
    
    await state.clear()

# =====================================================================
# 📁 ربط مجموعة الأرشيف
# =====================================================================
@store_router.message(Command("set_archive"))
async def set_archive_group(message: types.Message):
    # يجب أن يرسل صاحب البقالة هذا الأمر داخل المجموعة التي أنشأها
    if message.chat.type in ['group', 'supergroup']:
        async with database.pool.acquire() as conn:
            # التحقق من أن من أرسل الأمر هو صاحب بقالة
            store_id = await conn.fetchval("SELECT id FROM stores WHERE telegram_id = $1", message.from_user.id)
            if store_id:
                await conn.execute("UPDATE stores SET archive_group_id = $1 WHERE id = $2", message.chat.id, store_id)
                await message.answer("✅ تم ربط هذه المجموعة بنجاح كأرشيف لبقالتك!\nسيتم إرسال النسخ الاحتياطية وتقارير الوردية إلى هنا.")
            else:
                await message.answer("❌ عذراً، يجب أن تكون صاحب بقالة مسجلة لربط الأرشيف.")
    else:
        await message.answer("⚠️ يرجى إضافة البوت إلى مجموعة جديدة، ثم إرسال الأمر /set_archive داخل تلك المجموعة لربطها كأرشيف.")
