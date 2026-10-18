# مسار الملف: services/accounting.py
import logging
import asyncio
import html
from decimal import Decimal, InvalidOperation
from datetime import datetime, time

# ⏱️ دعم المناطق الزمنية
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from pytz import timezone as ZoneInfo

import database
from api.routes.websockets import notify_store_new_order
from bot.setup import bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from services.notifications import send_push_notification

logger = logging.getLogger(__name__)

# 🛡️ لحماية مهام الإشعارات في الخلفية من الحذف بواسطة الـ Garbage Collector
background_tasks = set()

def create_safe_task(coro):
    """إنشاء مهمة غير متزامنة مع حمايتها من الحذف المفاجئ أثناء التنفيذ"""
    task = asyncio.create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return task


async def process_new_order(store_id: int, customer_id: int, items: dict, payment_method: str, delivery_fee: float = 0.0, delivery_address: str = None):
    """
    المطبخ المركزي لمعالجة الطلبات الجديدة (مضاد للثغرات، سريع، دقيق محاسبياً)
    """
    # 🛡️ الحماية من السلة الفارغة وهجمات حجب الخدمة (DoS)
    if not items:
        raise ValueError("السلة فارغة، لا يمكن إنشاء طلب بدون منتجات.")
    if len(items) > 100:
        raise ValueError("تجاوزت الحد الأقصى للمنتجات في الطلب الواحد (100 منتج) لحماية النظام.")

    try:
        delivery_fee = Decimal(str(delivery_fee))
    except InvalidOperation:
        raise ValueError("رسوم التوصيل غير صالحة.")
        
    if delivery_fee < Decimal('0'):
        raise ValueError("رسوم التوصيل لا يمكن أن تكون بالسالب.")

    # 🛡️ سد ثغرة (الزبون النقدي + الدفع بالآجل)
    if payment_method == 'credit' and customer_id == 0:
        raise ValueError("لا يمكن تسجيل طلب بالآجل (دين) لزبون نقدي.")

    order_id = None
    grand_total = Decimal('0.0')
    cust_name = "زبون نقدي"
    enriched_items = {} 

    async with database.pool.acquire() as conn:
        async with conn.transaction():
            
            # 1. 🛡️ قفل صف الزبون أولاً مع فحص store_id لسد ثغرة التلاعب بين المتاجر
            if customer_id != 0:
                cust = await conn.fetchrow(
                    "SELECT name, balance, credit_limit, status, is_deleted FROM customers WHERE id = $1 AND store_id = $2 FOR UPDATE",
                    customer_id, store_id
                )
                if not cust or cust['is_deleted']:
                    raise ValueError("حساب الزبون غير موجود أو تم حذفه.")
                if payment_method == 'credit' and cust['status'] == 'suspended':
                    raise ValueError("حسابك موقوف مؤقتاً، لا يمكنك الطلب بالآجل.")
                cust_name = cust['name']

            # 2. قفل المنتجات ثانياً بترتيب الـ ID لتجنب Deadlock + حساب الإجمالي
            total_items_price = Decimal('0.0')
            product_ids = [int(pid) for pid in items.keys()]
            
            rows = await conn.fetch("""
                SELECT id, price, custom_name, stock 
                FROM store_products 
                WHERE id = ANY($1) AND store_id = $2 AND is_deleted = FALSE
                ORDER BY id FOR UPDATE
            """, product_ids, store_id)
            
            products_db = {row['id']: row for row in rows}

            for pid_str, item in items.items():
                pid = int(pid_str)
                qty = int(item.get('qty', 1))
                
                if qty <= 0: raise ValueError("الكمية يجب أن تكون أكبر من الصفر.")
                if qty > 1000: raise ValueError("الكمية المطلوبة لمنتج واحد كبيرة جداً، يرجى التواصل مع الإدارة.")
                    
                if pid not in products_db: raise ValueError("عذراً، أحد المنتجات في سلتك لم يعد متوفراً.")
                
                db_product = products_db[pid]
                if db_product['stock'] is not None and db_product['stock'] < qty:
                    raise ValueError(f"عذراً، الكمية المطلوبة من {db_product['custom_name']} غير متوفرة.")
                
                # حساب السعر بدقة Decimal
                db_price = Decimal(str(db_product['price']))
                total_items_price += (db_price * Decimal(str(qty)))
                
                enriched_items[pid] = {
                    'qty': qty,
                    'price': db_price,
                    'name': db_product['custom_name'] or f"منتج #{pid}"
                }
            
            grand_total = total_items_price + delivery_fee

            # 3. التحقق من سقف الدين باستخدام Decimal
            if payment_method == 'credit' and customer_id != 0:
                if (Decimal(str(cust['balance'])) + grand_total) > Decimal(str(cust['credit_limit'])):
                    raise ValueError(f"الطلب يتجاوز سقف الدين. السقف: {cust['credit_limit']} ريال.")

            # 4. إنشاء الطلب وحفظ عنوان التوصيل للمندوب
            order_id = await conn.fetchval("""
                INSERT INTO orders (store_id, customer_id, status, payment_method, total_amount, delivery_fee, delivery_address)
                VALUES ($1, $2, 'pending', $3, $4, $5, $6) RETURNING id
            """, store_id, customer_id if customer_id != 0 else None, payment_method, grand_total, delivery_fee, delivery_address)

            # 5. إدراج تفاصيل الطلب وخصم المخزون بأمان وسرعة فائقة
            order_items_data = []
            stock_update_data = []
            
            for pid, item_data in enriched_items.items():
                order_items_data.append((order_id, pid, item_data['qty'], item_data['price']))
                stock_update_data.append((item_data['qty'], pid))
                
            if order_items_data:
                await conn.executemany(
                    "INSERT INTO order_items (order_id, product_id, quantity, price_at_time) VALUES ($1, $2, $3, $4)",
                    order_items_data
                )
            if stock_update_data:
                await conn.executemany(
                    "UPDATE store_products SET stock = stock - $1 WHERE id = $2 AND stock IS NOT NULL",
                    stock_update_data
                )

            # 6. تسجيل المعاملة المالية
            if payment_method == 'credit' and customer_id != 0:
                await conn.execute("""
                    WITH updated_cust AS (UPDATE customers SET balance = balance + $1 WHERE id = $2 RETURNING id)
                    INSERT INTO transactions (store_id, customer_id, order_id, trans_type, amount, details)
                    VALUES ($3, $2, $4, 'credit_sale', $1, $5)
                """, grand_total, customer_id, store_id, order_id, f'مبيعات آجلة (طلب #{order_id})')
            else:
                await conn.execute("""
                    INSERT INTO transactions (store_id, customer_id, order_id, trans_type, amount, details)
                    VALUES ($1, $2, $3, 'cash_sale', $4, $5)
                """, store_id, customer_id if customer_id != 0 else None, order_id, grand_total, f'مبيعات نقدية (طلب #{order_id})')

            # 7. إصدار سجل الفاتورة
            if customer_id != 0:
                await conn.execute(
                    "INSERT INTO invoices (store_id, customer_id, title, pdf_url) VALUES ($1, $2, $3, $4)",
                    store_id, customer_id, f"فاتورة طلب رقم #{order_id}", f"/api/customer/invoice/{order_id}"
                )
                        
    return order_id, float(grand_total)


async def process_pos_transaction(store_id: int, worker_id: int, customer_id: int, client_total: float, discount: float, is_return: bool, items: dict):
    """
    المطبخ المركزي لمعالجة طلبات الكاشير السريع (مضاد للاختراق، دقيق محاسبياً، وفائق السرعة)
    """
    if not items:
        raise ValueError("الفاتورة فارغة، يرجى إضافة منتجات أولاً.")
    if len(items) > 150:
        raise ValueError("حجم الفاتورة كبير جداً، يرجى تقسيم المنتجات على فاتورتين.")

    try:
        discount = Decimal(str(discount))
    except InvalidOperation:
        raise ValueError("قيمة الخصم غير صالحة.")
        
    if discount < Decimal('0'):
        raise ValueError("قيمة الخصم لا يمكن أن تكون بالسالب.")
        
    new_balance = Decimal('0.0')
    cust_name = "زبون نقدي"
    fraud_alert_triggered = False
    store_tid = None

    async with database.pool.acquire() as conn:
        async with conn.transaction():
            # 1. 🛡️ الترتيب الذهبي: قفل الزبون أولاً مع فحص store_id
            if customer_id != 0:
                cust = await conn.fetchrow(
                    "SELECT name, balance, credit_limit, status, is_deleted FROM customers WHERE id = $1 AND store_id = $2 FOR UPDATE",
                    customer_id, store_id
                )
                if not cust or cust['is_deleted']: raise ValueError("حساب الزبون غير موجود أو تم حذفه.")
                if cust['status'] == 'suspended': raise ValueError("حساب الزبون موقوف مؤقتاً.")
                cust_name = cust['name']

            # 2. قفل المنتجات وحساب الإجمالي الفعلي من السيرفر
            product_ids = [int(pid) for pid in items.keys()]
            stock_updates = []
            backend_calculated_total = Decimal('0.0')
            
            if product_ids:
                db_products = await conn.fetch("""
                    SELECT id, custom_name, stock, price 
                    FROM store_products 
                    WHERE id = ANY($1) AND store_id = $2
                    ORDER BY id FOR UPDATE
                """, product_ids, store_id)
                db_products_map = {row['id']: row for row in db_products}
                
                for pid_str, item in items.items():
                    pid = int(pid_str)
                    qty = int(item.get('qty', 1))
                    
                    if qty <= 0: raise ValueError("الكمية يجب أن تكون أكبر من الصفر.")
                    if qty > 5000: raise ValueError("الكمية المدخلة غير منطقية، يرجى المراجعة.")
                        
                    db_p = db_products_map.get(pid)
                    if not db_p: raise ValueError(f"المنتج رقم #{pid} غير موجود.")
                    
                    if not is_return and db_p['stock'] is not None and db_p['stock'] < qty:
                        raise ValueError(f"الكمية المطلوبة من {db_p['custom_name']} غير متوفرة في المخزون.")
                    
                    item_price = Decimal(str(db_p['price']))
                    backend_calculated_total += (item_price * Decimal(str(qty)))
                    stock_updates.append((qty, pid))
                
                if stock_updates:
                    if is_return:
                        await conn.executemany("UPDATE store_products SET stock = stock + $1 WHERE id = $2 AND stock IS NOT NULL", stock_updates)
                    else:
                        await conn.executemany("UPDATE store_products SET stock = stock - $1 WHERE id = $2 AND stock IS NOT NULL", stock_updates)

            # 3. تطبيق الخصم بالدقة المطلقة
            final_total = backend_calculated_total - discount
            if final_total < Decimal('0'):
                raise ValueError("الإجمالي بعد الخصم لا يمكن أن يكون بالسالب.")

            # 4. فحص سقف الدين بالـ Decimal
            if customer_id != 0 and not is_return:
                if (Decimal(str(cust['balance'])) + final_total) > Decimal(str(cust['credit_limit'])):
                    raise ValueError(f"الطلب يتجاوز سقف الدين. السقف: {cust['credit_limit']} ريال.")

            # 5. تجهيز التفاصيل وتحديث الرصيد
            details = "\n".join([f"- {item['qty']}x {item.get('name', f'منتج #{pid}')}" for pid, item in items.items()])
            if discount > 0:
                details += f"\n🎁 الخصم: {discount} ريال"
                
            trans_type = 'pos_return' if is_return else 'pos_order'
            action_name = "مرتجع" if is_return else "طلب"
            
            if customer_id != 0:
                details_full = f"{action_name} كاشير (آجل)\n{details}\nالإجمالي النهائي: {final_total}"
                amount_to_insert = -final_total if is_return else final_total
                
                trans_id = await conn.fetchval("""
                    WITH updated_cust AS (UPDATE customers SET balance = balance + $1 WHERE id = $2 RETURNING id)
                    INSERT INTO transactions (store_id, customer_id, trans_type, amount, details) 
                    VALUES ($3, $2, $4, $1, $5) RETURNING id
                """, amount_to_insert, customer_id, store_id, trans_type, details_full)
                new_balance = Decimal(str(cust['balance'])) + amount_to_insert
            else:
                details_full = f"{action_name} كاشير (نقدي)\n{details}\nالإجمالي النهائي: {final_total}"
                cash_amount = -final_total if is_return else final_total
                await conn.execute("""
                    INSERT INTO transactions (store_id, customer_id, trans_type, amount, details) 
                    VALUES ($1, NULL, $2, $3, $4)
                """, store_id, trans_type, cash_amount, details_full)

            # 6. نظام كشف التلاعب (Fraud Detection)
            if is_return:
                returns_count = await conn.fetchval("""
                    SELECT COUNT(*) FROM transactions 
                    WHERE store_id = $1 AND trans_type = 'pos_return' AND created_at >= NOW() - INTERVAL '1 day'
                """, store_id)
                if returns_count >= 3:
                    worker_name = (await conn.fetchval("SELECT name FROM workers WHERE id = $1", worker_id)) if worker_id else "مجهول"
                    await conn.execute("""
                        INSERT INTO fraud_logs (store_id, worker_name, action) 
                        VALUES ($1, $2, 'تم رصد 3 عمليات مرتجع/إلغاء خلال 24 ساعة!')
                    """, store_id, worker_name or "مجهول")
                    fraud_alert_triggered = True
                    store_tid = await conn.fetchval("SELECT telegram_id FROM stores WHERE id = $1", store_id)

    # ==========================================
    # إرسال الإشعارات خارج المعاملة
    if customer_id != 0:
        create_safe_task(send_push_notification(
            customer_id, 
            "🔄 مرتجع كاشير" if is_return else "🛒 طلب كاشير", 
            f"تم تسجيل {action_name} بقيمة {final_total} ريال. رصيدك الحالي: {float(new_balance)} ريال."
        ))

    if fraud_alert_triggered and store_tid:
        async def send_fraud_alert():
            try:
                await bot.send_message(
                    store_tid, 
                    "🚨 <b>تنبيه أمني!</b> 🚨\nتم رصد 3 عمليات مرتجع في بقالتك خلال 24 ساعة. يرجى مراجعة الصندوق!", 
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.warning("تعذر إرسال تنبيه الأمان للتيليجرام: %s", e)
        create_safe_task(send_fraud_alert())

    return float(new_balance), cust_name


async def process_payment(store_id: int, customer_id: int, amount: float):
    """
    المطبخ المركزي لمعالجة سداد الديون وتوليد سندات القبض (سريعة وآمنة محاسبياً)
    """
    try:
        amount = Decimal(str(amount))
    except InvalidOperation:
        raise ValueError("قيمة المبلغ غير صالحة وتم رفضها أمنياً.")
        
    if amount <= Decimal('0'):
        raise ValueError("المبلغ يجب أن يكون أكبر من الصفر.")

    async with database.pool.acquire() as conn:
        async with conn.transaction():
            # 1. قفل صف الزبون
            cust = await conn.fetchrow(
                "SELECT name, balance, is_deleted FROM customers WHERE id = $1 AND store_id = $2 FOR UPDATE",
                customer_id, store_id
            )
            
            if not cust or cust['is_deleted']:
                raise ValueError("حساب الزبون غير موجود أو تم حذفه.")
                
            new_balance = Decimal(str(cust['balance'])) - amount
            
            # 2. تحديث الرصيد وتسجيل العملية مدمجة باستخدام CTE
            trans_id = await conn.fetchval("""
                WITH updated_cust AS (
                    UPDATE customers SET balance = balance - $1 WHERE id = $2 RETURNING id
                )
                INSERT INTO transactions (store_id, customer_id, trans_type, amount, details) 
                VALUES ($3, $2, 'payment', $4, 'سداد دفعة نقدية') 
                RETURNING id
            """, amount, customer_id, store_id, -amount)
            
            # 3. توليد سند قبض
            receipt_title = f"💵 سند قبض: {amount} ريال"
            receipt_url = f"/api/customer/receipt/{trans_id}"
            await conn.execute("""
                INSERT INTO invoices (store_id, customer_id, title, pdf_url)
                VALUES ($1, $2, $3, $4)
            """, store_id, customer_id, receipt_title, receipt_url)

    # إرسال الإشعار
    create_safe_task(send_push_notification(
        customer_id, 
        "💰 تم استلام دفعتك", 
        f"تم خصم {amount} ريال من حسابك. المتبقي عليك: {float(new_balance)} ريال. شكراً لك!",
        {"action": "open_inbox"}
    ))
    
    return float(new_balance)


async def process_balance_transfer(store_id: int, from_id: int, to_id: int, amount: float):
    """
    المطبخ المركزي لمعالجة تحويل الرصيد بين الزبائن بأمان تام ومنع الـ Deadlocks
    """
    try:
        amount = Decimal(str(amount))
    except InvalidOperation:
        raise ValueError("قيمة المبلغ غير صالحة.")
        
    if amount <= Decimal('0'): raise ValueError("مبلغ التحويل يجب أن يكون أكبر من الصفر.")
    if from_id == to_id: raise ValueError("لا يمكن التحويل لنفس الزبون.")

    async with database.pool.acquire() as conn:
        async with conn.transaction():
            # 1. قفل كلا الزبونين بترتيب الـ ID لمنع التعليق
            customers = await conn.fetch("""
                SELECT id, name, balance, credit_limit, status 
                FROM customers 
                WHERE id IN ($1, $2) AND store_id = $3 AND is_deleted = FALSE 
                ORDER BY id FOR UPDATE
            """, from_id, to_id, store_id)
            
            if len(customers) != 2: raise ValueError("أحد الزبائن غير موجود أو تم حذفه.")
            
            from_cust = next(c for c in customers if c['id'] == from_id)
            to_cust = next(c for c in customers if c['id'] == to_id)
            
            if from_cust['status'] == 'suspended' or to_cust['status'] == 'suspended':
                raise ValueError("لا يمكن إتمام التحويل، أحد الحسابين موقوف.")

            # 2. سد ثغرة تخطي سقف الدين
            if (Decimal(str(from_cust['balance'])) + amount) > Decimal(str(from_cust['credit_limit'])):
                raise ValueError("لا يمكنك التحويل! رصيدك سيتجاوز سقف الدين المسموح لك.")

            # 3. تحديث الأرصدة وتسجيل القيود
            await conn.execute("UPDATE customers SET balance = balance + $1 WHERE id = $2", amount, from_id)
            await conn.execute("""
                INSERT INTO transactions (store_id, customer_id, trans_type, amount, details) 
                VALUES ($1, $2, 'transfer_out', $3, $4)
            """, store_id, from_id, amount, f"تحويل رصيد (سداد دين) لصالح الزبون: {to_cust['name']}")
            
            await conn.execute("UPDATE customers SET balance = balance - $1 WHERE id = $2", amount, to_id)
            await conn.execute("""
                INSERT INTO transactions (store_id, customer_id, trans_type, amount, details) 
                VALUES ($1, $2, 'transfer_in', $3, $4)
            """, store_id, to_id, -amount, f"استلام رصيد (سداد دين) محول من الزبون: {from_cust['name']}")

    # إرسال الإشعارات
    create_safe_task(send_push_notification(
        from_id, "💸 تم التحويل", f"تم تسجيل {amount} ريال على حسابك لصالح {to_cust['name']}."
    ))
    create_safe_task(send_push_notification(
        to_id, "💸 رصيد مضاف", f"قام {from_cust['name']} بسداد {amount} ريال من دينك."
    ))


async def process_order_rejection(store_id: int, order_id: int, worker_id: int = None):
    """
    المطبخ المركزي لمعالجة رفض الطلبات واسترجاع الأموال والمخزون
    """
    customer_id = None

    async with database.pool.acquire() as conn:
        async with conn.transaction():
            # 1. جلب بيانات الطلب والتحقق من المتجر
            order_info = await conn.fetchrow(
                "SELECT customer_id, total_amount, payment_method, status FROM orders WHERE id = $1 AND store_id = $2",
                order_id, store_id
            )
            if not order_info: raise ValueError("الطلب غير موجود.")
            if order_info['status'] != 'pending': raise ValueError("لا يمكن رفض طلب قيد المعالجة أو مكتمل.")
            
            customer_id = order_info['customer_id']
            total_amount = Decimal(str(order_info['total_amount']))

            # 2. قفل حساب الزبون أولاً إذا كان الطلب آجلاً
            if order_info['payment_method'] == 'credit' and customer_id:
                await conn.execute("SELECT id FROM customers WHERE id = $1 AND store_id = $2 FOR UPDATE", customer_id, store_id)
            
            # 3. قفل الطلب ثانياً
            order = await conn.fetchrow("SELECT id FROM orders WHERE id = $1 AND status = 'pending' FOR UPDATE", order_id)
            if not order: raise ValueError("حالة الطلب تغيرت، لا يمكن الإلغاء الآن.")

            # 4. تحديث حالة الطلب
            await conn.execute("UPDATE orders SET status = 'rejected' WHERE id = $1", order_id)
            
            # 5. استرجاع المخزون
            items = await conn.fetch("SELECT product_id, quantity FROM order_items WHERE order_id = $1 ORDER BY product_id", order_id)
            if items:
                stock_updates = [(item['quantity'], item['product_id']) for item in items]
                await conn.executemany("UPDATE store_products SET stock = stock + $1 WHERE id = $2 AND stock IS NOT NULL", stock_updates)
            
            # 6. استرجاع الرصيد
            if order_info['payment_method'] == 'credit' and customer_id:
                await conn.execute("""
                    WITH updated_cust AS (UPDATE customers SET balance = balance - $1 WHERE id = $2 RETURNING id)
                    INSERT INTO transactions (store_id, customer_id, order_id, trans_type, amount, details)
                    VALUES ($3, $2, $4, 'refund', $5, 'إلغاء طلب واسترجاع الرصيد')
                """, total_amount, customer_id, store_id, order_id, -total_amount)
                
            # 7. تسجيل حركة الإلغاء
            worker_name = (await conn.fetchval("SELECT name FROM workers WHERE id = $1", worker_id)) if worker_id else "مستخدم النظام"
            await conn.execute(
                "INSERT INTO fraud_logs (store_id, worker_name, action) VALUES ($1, $2, 'قام برفض وإلغاء طلب رقم ' || $3)",
                store_id, worker_name or "مستخدم النظام", str(order_id)
            )

    if customer_id:
        create_safe_task(send_push_notification(customer_id, "❌ نعتذر منك", "تم رفض طلبك من قبل البقالة. تم استرجاع رصيدك."))


async def process_family_order_action(parent_id: int, order_id: int, action: str, modified_items: dict):
    """
    المطبخ المركزي لمعالجة طلبات العائلة (سريعة، دقيقة محاسبياً، وفعالة في استخدام الاتصالات)
    """
    child_id = None
    store_id = None
    store_tid = None
    new_total = Decimal('0.0')
    final_items_for_store = {}
    father_name = ""

    async with database.pool.acquire() as conn:
        async with conn.transaction():
            # 0. 🚀 جلب بيانات الطلب ورقم تيليجرام المتجر بضربة واحدة لتوفير اتصال إضافي
            order_info = await conn.fetchrow("""
                SELECT o.store_id, o.customer_id as child_id, s.telegram_id as store_tid 
                FROM orders o
                LEFT JOIN stores s ON s.id = o.store_id
                WHERE o.id = $1 AND o.status = 'family_review'
            """, order_id)
            
            if not order_info: raise ValueError("الطلب غير موجود أو تمت معالجته مسبقاً.")
            store_id = order_info['store_id']
            child_id = order_info['child_id']
            store_tid = order_info['store_tid']

            # 1. الترتيب الذهبي: قفل حساب الأب والتأكد من المتجر
            father = await conn.fetchrow(
                "SELECT name, balance, credit_limit, status FROM customers WHERE id = $1 AND store_id = $2 FOR UPDATE",
                parent_id, store_id
            )
            if not father: raise ValueError("حساب رب الأسرة غير موجود في هذه البقالة.")
            if father['status'] == 'suspended': raise ValueError("حساب رب الأسرة موقوف مؤقتاً.")
            father_name = father['name']

            # 2. قفل الطلب ثانياً
            order = await conn.fetchrow("SELECT id FROM orders WHERE id = $1 AND status = 'family_review' FOR UPDATE", order_id)
            if not order: raise ValueError("حالة الطلب تغيرت، يرجى المحاولة لاحقاً.")
            
            if action == 'approve':
                if not modified_items:
                    raise ValueError("لا يمكن اعتماد طلب فارغ.")
                if len(modified_items) > 100:
                    raise ValueError("الطلب يحتوي على عدد منتجات ضخم جداً لحماية النظام.")

                original_items = await conn.fetch("SELECT product_id, quantity, price_at_time FROM order_items WHERE order_id = $1", order_id)
                product_ids = [item['product_id'] for item in original_items]
                db_products = {}
                
                if product_ids:
                    rows = await conn.fetch("""
                        SELECT id, custom_name, stock FROM store_products 
                        WHERE id = ANY($1) AND store_id = $2 
                        ORDER BY id FOR UPDATE
                    """, product_ids, store_id)
                    db_products = {row['id']: row for row in rows}

                stock_updates = []
                order_item_updates = []
                order_item_deletes = []
                
                for orig_item in original_items:
                    pid = orig_item['product_id']
                    pid_str = str(pid)
                    old_qty = orig_item['quantity']
                    price = Decimal(str(orig_item['price_at_time']))
                    
                    new_qty = int(modified_items.get(pid_str, old_qty))
                    if new_qty < 0: new_qty = 0
                    
                    if new_qty != old_qty:
                        diff = old_qty - new_qty
                        db_p = db_products.get(pid)
                        if diff < 0 and db_p and db_p['stock'] is not None:
                            if db_p['stock'] + diff < 0: 
                                p_name = db_p['custom_name'] or f"منتج #{pid}"
                                raise ValueError(f"عذراً، الكمية الإضافية المطلوبة من ({p_name}) غير متوفرة في المخزون.")
                        
                        stock_updates.append((diff, pid))
                        if new_qty == 0:
                            order_item_deletes.append((order_id, pid))
                        else:
                            order_item_updates.append((new_qty, order_id, pid))
                    
                    if new_qty > 0:
                        new_total += (price * Decimal(str(new_qty)))
                        p_name = db_products.get(pid, {}).get('custom_name', f"منتج #{pid}")
                        final_items_for_store[pid_str] = {"qty": new_qty, "name": p_name}

                # التحديثات المجمعة السريعة
                if stock_updates: await conn.executemany("UPDATE store_products SET stock = stock + $1 WHERE id = $2 AND stock IS NOT NULL", stock_updates)
                if order_item_deletes: await conn.executemany("DELETE FROM order_items WHERE order_id = $1 AND product_id = $2", order_item_deletes)
                if order_item_updates: await conn.executemany("UPDATE order_items SET quantity = $1 WHERE order_id = $2 AND product_id = $3", order_item_updates)
                
                if new_total == Decimal('0.0'):
                    await conn.execute("UPDATE orders SET status = 'rejected' WHERE id = $1", order_id)
                    action = 'rejected_empty'
                else:
                    if (Decimal(str(father['balance'])) + new_total) > Decimal(str(father['credit_limit'])):
                        raise ValueError("الطلب يتجاوز سقف الدين المسموح لك.")
                        
                    await conn.execute("""
                        WITH updated_order AS (UPDATE orders SET status = 'pending', total_amount = $1, customer_id = $2 WHERE id = $3 RETURNING id),
                             updated_cust  AS (UPDATE customers SET balance = balance + $1 WHERE id = $2 RETURNING id)
                        INSERT INTO transactions (store_id, customer_id, order_id, trans_type, amount, details)
                        VALUES ($4, $2, $3, 'credit_sale', $1, 'طلب عائلي معتمد')
                    """, new_total, parent_id, order_id, store_id)
                    
            else:
                await conn.execute("UPDATE orders SET status = 'rejected' WHERE id = $1", order_id)
                items = await conn.fetch("SELECT product_id, quantity FROM order_items WHERE order_id = $1", order_id)
                if items:
                    stock_updates = [(item['quantity'], item['product_id']) for item in items]
                    await conn.executemany("UPDATE store_products SET stock = stock + $1 WHERE id = $2 AND stock IS NOT NULL", stock_updates)

    # ==========================================
    # إرسال الإشعارات والتليجرام خارج الـ Transaction
    if action == 'approve':
        create_safe_task(send_push_notification(child_id, "✅ تم الاعتماد", "تم اعتماد طلبك وإرساله للبقالة."))
        order_data = {
            "order_id": order_id, 
            "customer_name": father_name, 
            "total_amount": float(new_total), 
            "payment_method": "credit", 
            "items": final_items_for_store
        }
        create_safe_task(notify_store_new_order(store_id, order_data))
        
        if store_tid:
            async def send_order_telegram():
                items_text = "\n".join([f"- {item['qty']}x {item['name']}" for item in final_items_for_store.values()])
                if len(items_text) > 3000:
                    items_text = items_text[:3000] + "\n... (تم إخفاء باقي المنتجات لكبر حجم الطلب)."
                    
                safe_father_name = html.escape(father_name or "مجهول")
                safe_items_text = html.escape(items_text)
                msg_text = (
                    f"🔔 <b>طلب جديد رقم #{order_id}</b>\n"
                    f"👤 الزبون: {safe_father_name}\n"
                    f"💰 الإجمالي: {float(new_total)} ريال\n"
                    f"💳 الدفع: آجل (دين)\n\n"
                    f"<b>التفاصيل:</b>\n{safe_items_text}"
                )
                
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ قبول وتجهيز", callback_data=f"accept_order_{order_id}")],
                    [InlineKeyboardButton(text="❌ رفض الطلب", callback_data=f"reject_order_{order_id}")]
                ])
                try:
                    await bot.send_message(store_tid, msg_text, reply_markup=kb, parse_mode="HTML")
                except Exception as e:
                    logger.warning("فشل إرسال رسالة التليجرام للبقالة: %s", e)

            create_safe_task(send_order_telegram())
            
    elif action == 'rejected_empty':
        create_safe_task(send_push_notification(child_id, "❌ تم الإلغاء", "تم إلغاء الطلب بالكامل لعدم وجود منتجات."))
    else:
        create_safe_task(send_push_notification(child_id, "❌ تم الرفض", "تم رفض طلبك من قبل رب الأسرة."))


async def generate_and_send_z_report(store_id: int, worker_name: str, is_auto: bool = False):
    """توليد تقرير الوردية وإرساله للأرشيف أو صاحب البقالة (سريع، ويستفيد من فهرس التاريخ)"""
    local_tz = ZoneInfo('Asia/Aden')
    now_local = datetime.now(local_tz)
    
    # ⏱️ حساب بداية ونهاية اليوم بتوقيت المتجر لاستخدام Index بشكل كامل
    start_of_day = datetime.combine(now_local.date(), time.min).replace(tzinfo=local_tz)
    end_of_day = datetime.combine(now_local.date(), time.max).replace(tzinfo=local_tz)

    async with database.pool.acquire() as conn:
        store = await conn.fetchrow("SELECT name, telegram_id, archive_group_id FROM stores WHERE id = $1", store_id)
        if not store: return

        # 🚀 استعلام فائق السرعة عبر B-Tree Index (store_id, created_at)
        sales = await conn.fetchrow("""
            SELECT 
                COALESCE(SUM(CASE WHEN trans_type IN ('pos_order', 'cash_sale', 'credit_sale') THEN amount ELSE 0 END), 0) as total_sales,
                COALESCE(SUM(CASE WHEN trans_type IN ('pos_return', 'refund', 'reversal') THEN amount ELSE 0 END), 0) as total_returns,
                COALESCE(SUM(CASE WHEN trans_type = 'payment' THEN ABS(amount) ELSE 0 END), 0) as total_payments
            FROM transactions 
            WHERE store_id = $1 
              AND created_at >= $2 
              AND created_at <= $3
        """, store_id, start_of_day, end_of_day)

        if is_auto and sales['total_sales'] == 0 and sales['total_payments'] == 0 and sales['total_returns'] == 0:
            return

        safe_store_name = html.escape(store['name'] or "مجهول")
        safe_worker_name = html.escape(worker_name or "مجهول")
        
        msg = f"📊 <b>تقرير نهاية الوردية (Z-Report)</b>\n"
        msg += f"🏪 البقالة: {safe_store_name}\n"
        msg += f"👷‍♂️ الموظف: {safe_worker_name}\n"
        msg += f"📅 التاريخ: {now_local.strftime('%Y-%m-%d %I:%M %p')}\n\n"
        msg += f"🛒 المبيعات: {sales['total_sales']} ريال\n"
        msg += f"🔄 المرتجعات: {abs(sales['total_returns'])} ريال\n"
        msg += f"💰 السدادات المستلمة: {sales['total_payments']} ريال\n"
        
        if is_auto:
            msg += "\n⚠️ <i>ملاحظة: العامل لم يقم بتقفيل الوردية، تم التقفيل وإرسال التقرير تلقائياً بواسطة النظام لحفظ الحسابات.</i>"

        target_chat = store['archive_group_id'] if store['archive_group_id'] else store['telegram_id']
        
        if target_chat:
            try:
                await bot.send_message(target_chat, msg, parse_mode="HTML")
            except Exception as e:
                logger.warning("تعذر إرسال تقرير Z عبر التيليجرام: %s", e)
