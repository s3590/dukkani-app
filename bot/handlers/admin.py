# مسار الملف: bot/handlers/admin.py
import os
import csv
import asyncio
from io import StringIO
from datetime import datetime, timedelta
from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.fsm.context import FSMContext

import database
from bot.states import AdminStates, AdminCatalog
from bot.setup import bot
from config import ADMIN_ID
import random
import json
from decimal import Decimal
from datetime import datetime, date

# 👇 هذا هو المتغير الذي يبحث عنه السيرفر 👇
admin_router = Router()

# =====================================================================
# 👑 لوحة تحكم المدير العام (God Mode)
# =====================================================================
@admin_router.message(F.text == "📱 دخول التطبيق")
async def admin_app_login(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    from config import ADMIN_PASSWORD
    await message.answer(f"👑 **بيانات دخول القيادة العليا:**\n\n🌐 الرابط: https://dukkani-app.onrender.com\n🔑 كلمة المرور: `{ADMIN_PASSWORD}`" )

@admin_router.message(F.text == "🎛️ غرفة العمليات")
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 إحصائيات النظام", callback_data="admin_stats")],
        [InlineKeyboardButton(text="🏪 إدارة البقالات", callback_data="admin_manage")],
        [InlineKeyboardButton(text="📢 إذاعة رسالة للجميع", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="💾 نسخة احتياطية (Excel)", callback_data="admin_backup")],
        [InlineKeyboardButton(text="🌍 إدارة صور الكتالوج", callback_data="admin_global_catalog")]
    ])
    await message.answer("👑 **غرفة العمليات (المدير العام)**\nمرحباً بك يا زعيم، ماذا تريد أن تفعل؟", reply_markup=kb)

@admin_router.callback_query(F.data == "back_to_admin")
async def back_to_admin_menu(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 إحصائيات النظام", callback_data="admin_stats")],
        [InlineKeyboardButton(text="🏪 إدارة البقالات", callback_data="admin_manage")],
        [InlineKeyboardButton(text="📢 إذاعة رسالة للجميع", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="💾 نسخة احتياطية (Excel)", callback_data="admin_backup")],
        [InlineKeyboardButton(text="🌍 إدارة صور الكتالوج", callback_data="admin_global_catalog")]
    ])
    await callback.message.edit_text("👑 **غرفة العمليات (المدير العام)**\nمرحباً بك يا زعيم، ماذا تريد أن تفعل؟", reply_markup=kb)

@admin_router.callback_query(F.data == "admin_stats")
async def admin_show_stats(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    async with database.pool.acquire() as conn:
        stores_count = await conn.fetchval("SELECT COUNT(*) FROM stores")
        active_stores = await conn.fetchval("SELECT COUNT(*) FROM stores WHERE status = 'active'")
        customers_count = await conn.fetchval("SELECT COUNT(*) FROM customers")
        total_debt = await conn.fetchval("SELECT SUM(balance) FROM customers") or 0.0
        
    text = (
        "📊 **إحصائيات إمبراطوريتك الحية:**\n\n"
        f"🏪 إجمالي البقالات المسجلة: **{stores_count}**\n"
        f"✅ البقالات النشطة حالياً: **{active_stores}**\n"
        f"👥 إجمالي الزبائن في النظام: **{customers_count}**\n"
        f"💰 إجمالي الديون المسجلة: **{total_debt} ريال**\n"
    )
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 رجوع", callback_data="back_to_admin")]]))

@admin_router.callback_query(F.data == "admin_manage")
async def admin_manage_stores(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    async with database.pool.acquire() as conn:
        stores = await conn.fetch("SELECT id, name, status FROM stores")
        
    if not stores: return await callback.answer("لا توجد بقالات مسجلة بعد.", show_alert=True)
        
    kb = [[InlineKeyboardButton(text=f"{'✅' if s['status']=='active' else '❌'} {s['name']}", callback_data=f"toggle_{s['id']}_{'suspended' if s['status']=='active' else 'active'}")] for s in stores]
    kb.append([InlineKeyboardButton(text="🔙 رجوع", callback_data="back_to_admin")])
    await callback.message.edit_text("🏪 **إدارة البقالات:**\n(اضغط على البقالة لإيقافها أو تفعيلها)", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@admin_router.callback_query(F.data.startswith("toggle_"))
async def toggle_store(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    _, sid, status = callback.data.split("_")
    async with database.pool.acquire() as conn:
        await conn.execute("UPDATE stores SET status = $1 WHERE id = $2", status, int(sid))
    await callback.answer("تم تحديث حالة البقالة!", show_alert=True)
    await admin_manage_stores(callback)

@admin_router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await callback.message.edit_text("📢 **نظام الإذاعة:**\nأرسل الآن الرسالة التي تريد إرسالها لجميع أصحاب البقالات (أو أرسل 'إلغاء'):")
    await state.set_state(AdminStates.waiting_for_broadcast)

async def background_bot_broadcast(text: str, admin_id: int):
    """مهمة خلفية لإرسال الإذاعة من البوت دون تجميده"""
    async with database.pool.acquire() as conn:
        stores = await conn.fetch("SELECT telegram_id FROM stores WHERE telegram_id IS NOT NULL")
        
    success = 0
    for store in stores:
        try:
            await bot.send_message(store['telegram_id'], f"📢 **رسالة من الإدارة:**\n\n{text}")
            success += 1
        except: pass
        await asyncio.sleep(0.05) # لتجنب حظر تيليجرام
        
    try:
        await bot.send_message(admin_id, f"✅ **اكتملت الإذاعة:** تم إرسال الرسالة بنجاح إلى {success} بقالة.")
    except: pass

@admin_router.message(AdminStates.waiting_for_broadcast)
async def admin_broadcast_send(message: types.Message, state: FSMContext):
    if message.text == 'إلغاء':
        await message.answer("تم الإلغاء.")
        return await state.clear()
        
    await message.answer("⏳ تم استلام الرسالة وجاري إرسالها في الخلفية... سيصلك إشعار عند الانتهاء.")
    
    # 🛡️ تشغيل الإرسال عبر create_safe_task لحمايته من الحذف أثناء الإرسال الجماعي
    from database import create_safe_task
    create_safe_task(background_bot_broadcast(message.text, message.from_user.id))
    await state.clear()

def json_serial(obj):
    """دالة مساعدة لتحويل التواريخ والأرقام العشرية إلى نصوص لكي يقبلها ملف JSON"""
    if isinstance(obj, (datetime, date)): return obj.isoformat()
    if isinstance(obj, Decimal): return float(obj)
    raise TypeError("Type not serializable")

@admin_router.callback_query(F.data == "admin_backup")
async def admin_backup_db(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    await callback.answer("جاري تجهيز النسخة الاحتياطية الشاملة...", show_alert=False)
    
    # 🛡️ تصحيح اسم fraud_logs وإضافة الجداول الجديدة للنسخة الشاملة
    tables = [
        "stores", "global_products", "store_products", "customers", 
        "workers", "delivery_agents", "orders", "order_items", 
        "transactions", "chat_messages", "family_chat_messages", 
        "admin_logs", "fraud_logs", "invoices", "support_tickets", 
        "push_subscriptions", "expenses", "inventory_checks", "subscription_payments"
    ]
    
    backup_data = {}
    file_path = None # 🛡️ تعريف المتغير هنا لتجنب أخطاء الحذف
    try:
        async with database.pool.acquire() as conn:
            for table in tables:
                records = await conn.fetch(f"SELECT * FROM {table}")
                backup_data[table] = [dict(r) for r in records]
                
        file_path = f"dukkani_full_backup_{datetime.now().strftime('%Y%m%d%H%M%S')}.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(backup_data, f, default=json_serial, ensure_ascii=False, indent=2)
            
        await bot.send_document(ADMIN_ID, FSInputFile(file_path), caption="💾 **نسخة احتياطية شاملة (JSON)**\nتحتوي على جميع البقالات، الزبائن، المنتجات، الديون، والمحادثات.")
    except Exception as e:
        await callback.message.answer(f"❌ فشل إنشاء النسخة الاحتياطية:\n`{e}`")
    finally:
        # 🛡️ ضمان حذف الملف من السيرفر في كل الحالات لتجنب امتلاء الذاكرة (Storage Leak)
        if file_path and os.path.exists(file_path):
            os.remove(file_path)

@admin_router.callback_query(F.data == "admin_global_catalog")
async def admin_catalog_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    
    async with database.pool.acquire() as conn:
        products = await conn.fetch("SELECT id, name FROM global_products ORDER BY category, name")
        
    if not products:
        return await callback.message.edit_text("الكتالوج فارغ.")
        
    kb = []
    row = []
    for p in products:
        row.append(InlineKeyboardButton(text=p['name'], callback_data=f"setgimg_{p['id']}"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row: kb.append(row)
    kb.append([InlineKeyboardButton(text="🔙 رجوع", callback_data="back_to_admin")])
    
    await callback.message.edit_text("🌍 **إدارة صور الكتالوج المركزي:**\n\nاختر المنتج الذي تريد تغيير صورته:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@admin_router.callback_query(F.data.startswith("setgimg_"))
async def admin_catalog_select(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    prod_id = int(callback.data.split("_")[1])
    
    async with database.pool.acquire() as conn:
        prod_name = await conn.fetchval("SELECT name FROM global_products WHERE id = $1", prod_id)
        
    await state.update_data(global_prod_id=prod_id, global_prod_name=prod_name)
    await callback.message.answer(f"✅ اخترت المنتج: **{prod_name}**\n\n🖼️ أرسل الآن الصورة الجديدة لهذا المنتج:")
    await state.set_state(AdminCatalog.waiting_for_product_image)

@admin_router.message(AdminCatalog.waiting_for_product_image, F.photo)
async def admin_catalog_image(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    file_id = message.photo[-1].file_id
    data = await state.get_data()
    
    async with database.pool.acquire() as conn:
        await conn.execute("UPDATE global_products SET image_url = $1 WHERE id = $2", file_id, data['global_prod_id'])
        
    await message.answer(f"✅ تم تحديث الصورة الافتراضية للمنتج **{data['global_prod_name']}** بنجاح!", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 رجوع للوحة التحكم", callback_data="back_to_admin")]]))
    await state.clear()

# =====================================================================
# ⚠️ أمر الفورمات الشامل (تصفير النظام بالكامل)
# =====================================================================
@admin_router.message(F.text == "☢️ فورمات النظام")
async def format_system_cmd(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    
    await message.answer(
        "⚠️ **تحذير خطير جداً!** ⚠️\n\n"
        "أنت على وشك حذف **جميع بيانات النظام بلا رجعة** (بقالات، زبائن، ديون، منتجات).\n\n"
        "للتأكيد، يرجى نسخ العبارة التالية وإرسالها كما هي تماماً:\n\n"
        "`تأكيد فورمات 9999`",
        parse_mode="Markdown"
    )

@admin_router.message(F.text == "تأكيد فورمات 9999")
async def execute_format_system(message: types.Message):
    if message.from_user.id != ADMIN_ID: return

    msg = await message.answer("⏳ جاري عمل فورمات للنظام... الرجاء الانتظار.")
    
    try:
        async with database.pool.acquire() as conn:
            await conn.execute('''
                TRUNCATE TABLE 
                    transactions, order_items, orders, delivery_agents,
                    store_products, push_subscriptions, workers, customers,
                    stores, global_products, admin_logs, fraud_logs,
                    chat_messages, family_chat_messages, invoices, support_tickets,
                    expenses, inventory_checks, subscription_payments
                RESTART IDENTITY CASCADE;
            ''')
        await msg.edit_text("✅ **تمت عملية الفورمات بنجاح!**\nالنظام الآن جديد تماماً وكأنه تم تشغيله لأول مرة.")
    except Exception as e:
        await msg.edit_text(f"❌ حدث خطأ أثناء الفورمات:\n`{e}`")

# =====================================================================
# 🚀 معالجة طلبات اشتراك البقالات الجديدة (B2B SaaS)
# =====================================================================
@admin_router.callback_query(F.data.startswith("admin_approve_store_"))
async def admin_approve_store(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    tid = int(callback.data.split("_")[3])
    
# 🛡️ استخراج ذكي وآمن يمنع خطأ IndexError مهما تغيرت أسطر الرسالة
    store_name = "بقالة جديدة"
    phone = ""
    for line in callback.message.text.split('\n'):
        if "الاسم:" in line:
            store_name = line.split("الاسم:")[1].strip()
        elif "الهاتف:" in line:
            phone = line.split("الهاتف:")[1].strip()

    async with database.pool.acquire() as conn:
        exists = await conn.fetchval("SELECT id FROM stores WHERE telegram_id = $1", tid)
        if exists:
            return await callback.answer("تم تسجيل هذه البقالة مسبقاً.", show_alert=True)
            
        # 👇 ⏱️ توحيد التوقيت ليتطابق مع قاعدة البيانات 👇
        try:
            import zoneinfo
            local_tz = zoneinfo.ZoneInfo('Asia/Aden')
        except ImportError:
            import pytz
            local_tz = pytz.timezone('Asia/Aden')
            
        sub_end = datetime.now(local_tz).replace(tzinfo=None) + timedelta(days=30) # اشتراك مجاني 30 يوم
        
        store_id = await conn.fetchval("INSERT INTO stores (telegram_id, name, phone, subscription_end) VALUES ($1, $2, $3, $4) RETURNING id", tid, store_name, phone, sub_end)
        
        # إنشاء كود سري لصاحب البقالة
        import random
        passcode = str(random.randint(10000, 99999))
        await conn.execute("""
            INSERT INTO workers (store_id, name, telegram_id, passcode, permissions) 
            VALUES ($1, 'المدير العام', $2, $3, '["pos", "manage_products", "manage_customers", "add_payment", "view_reports"]')
        """, store_id, tid, passcode)
        
    # 👇 🌐 جلب الرابط من الإعدادات لسهولة تغييره مستقبلاً 👇
    app_link = os.getenv("BASE_URL", "https://dukkani-app.onrender.com" )
    success_msg = f"🎉 **تمت الموافقة على طلبك!**\n\n🏪 بقالة: {store_name}\n\n🌐 **رابط التطبيق:**\n{app_link}\n\n📱 **رقم الدخول (الآيدي ):**\n`{tid}`\n\n🔑 **الكود السري:**\n`{passcode}`\n\n*(اضغط على الرقم أو الكود لنسخه)*"
    try:
        await bot.send_message(tid, success_msg, parse_mode="Markdown")
    except: pass
    
    await callback.message.edit_text(callback.message.text + "\n\n✅ **تمت الموافقة وإنشاء الحساب.**", reply_markup=None)

@admin_router.callback_query(F.data.startswith("admin_reject_store_"))
async def admin_reject_store(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    tid = int(callback.data.split("_")[3])
    
    try:
        await bot.send_message(tid, "❌ نعتذر منك، تم رفض طلب اشتراكك من قبل الإدارة.")
    except: pass
    
    await callback.message.edit_text(callback.message.text + "\n\n❌ **تم الرفض.**", reply_markup=None)
    
# =====================================================================
# 🧪 أمر ضخ البيانات الوهمية لاختبار الضغط (مخصص للمدير فقط)
# ==========================================

@admin_router.message(F.text == "🧪 اختبار الضغط")
async def seed_test_data_cmd(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    
    await message.answer("⏳ **بدأ اختبار الضغط...**\nجاري ضخ البيانات الوهمية في قاعدة البيانات، الرجاء الانتظار وعدم إرسال أي أمر حتى أنتهي.")
    
    try:
        async with database.pool.acquire() as conn:
            # 1. إضافة 10 بقالات وهمية
            store_ids = []
            for i in range(1, 11):
                store_id = await conn.fetchval(
                    "INSERT INTO stores (telegram_id, name, phone, status) VALUES ($1, $2, $3, 'active') RETURNING id",
                    1000000 + i, f"بقالة الاختبار رقم {i}", f"7770000{i:02d}"
                )
                store_ids.append(store_id)
            await message.answer(f"✅ تم إضافة {len(store_ids)} بقالات وهمية.")

            # 2. إضافة 50 منتج لكل بقالة (الإجمالي 500 منتج)
            product_ids = []
            for sid in store_ids:
                for j in range(1, 51):
                    pid = await conn.fetchval(
                        "INSERT INTO store_products (store_id, custom_name, price, cost_price, stock, category) VALUES ($1, $2, $3, $4, $5, 'عام') RETURNING id",
                        sid, f"منتج تجريبي {j} للبقالة {sid}", round(random.uniform(100, 5000), 2), round(random.uniform(50, 4000), 2), random.randint(10, 100)
                    )
                    product_ids.append(pid)
            await message.answer(f"✅ تم إضافة {len(product_ids)} منتج وهمي.")

            # 3. إضافة 20 زبون لكل بقالة (الإجمالي 200 زبون)
            customer_ids = []
            for sid in store_ids:
                for k in range(1, 21):
                    cid = await conn.fetchval(
                        "INSERT INTO customers (store_id, name, phone, password, balance, credit_limit, status) VALUES ($1, $2, $3, '1234', 0, 50000, 'active') RETURNING id",
                        sid, f"زبون تجريبي {k}", f"733000{sid:02d}{k:02d}"
                    )
                    customer_ids.append(cid)
            await message.answer(f"✅ تم إضافة {len(customer_ids)} زبون وهمي.")

            # 4. توليد 1000 طلب عشوائي
            await message.answer("⏳ جاري توليد 1000 طلب وعملية مالية (هذا قد يستغرق بضع ثوانٍ)...")
            for _ in range(1000):
                sid = random.choice(store_ids)
                # محاولة إيجاد زبون تابع لنفس البقالة
                valid_customers = [c for c in customer_ids if c % sid == 0]
                cid = random.choice(valid_customers) if valid_customers else random.choice(customer_ids)
                total = round(random.uniform(500, 15000), 2)
                
                oid = await conn.fetchval(
                    "INSERT INTO orders (store_id, customer_id, status, payment_method, total_amount) VALUES ($1, $2, 'completed', 'credit', $3) RETURNING id",
                    sid, cid, total
                )
                await conn.execute(
                    "INSERT INTO transactions (store_id, customer_id, order_id, trans_type, amount, details) VALUES ($1, $2, $3, 'credit_sale', $4, 'طلب تجريبي')",
                    sid, cid, oid, total
                )
                await conn.execute("UPDATE customers SET balance = balance + $1 WHERE id = $2", total, cid)
            await message.answer("✅ تم توليد 1000 طلب وعملية مالية.")

            # 5. توليد 2000 رسالة محادثة
            await message.answer("⏳ جاري توليد 2000 رسالة محادثة...")
            for _ in range(2000):
                sid = random.choice(store_ids)
                cid = random.choice(customer_ids)
                sender = random.choice(['store', 'customer'])
                await conn.execute(
                    "INSERT INTO chat_messages (store_id, customer_id, sender_type, message_text) VALUES ($1, $2, $3, 'رسالة اختبار للضغط على النظام')",
                    sid, cid, sender
                )
            
            await message.answer("🎉 **اكتمل اختبار الضغط وتعبئة البيانات بنجاح!**\nيمكنك الآن فتح التطبيق لتفقد السرعة والأداء مع هذه البيانات الضخمة.")

    except Exception as e:
        await message.answer(f"❌ حدث خطأ أثناء ضخ البيانات:\n`{e}`")

from aiogram import F, types
from config import ADMIN_ID

# 👇 كود سري للمدير: أرسل أي صورة للبوت وسيعطيك رابطها المباشر! 👇
@admin_router.message(F.photo)
async def generate_image_link(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        file_id = message.photo[-1].file_id
        base_url = os.getenv("BASE_URL", "https://dukkani-app.onrender.com")
        link = f"{base_url}/api/image/{file_id}"
        
        # 🛡️ استخدام HTML لمنع انهيار الرسالة عند وجود شرطات سفلية (_) في file_id
        await message.reply(
            f"✅ <b>تم إنشاء رابط الشعار بنجاح!</b>\n\n"
            f"انسخ هذا الرابط وضعه في النظام:\n\n<code>{link}</code>", 
            parse_mode="HTML"
        )
