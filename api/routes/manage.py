# مسار الملف: api/routes/manage.py
from aiohttp import web
import json
import random
import traceback
import logging
import asyncio
import bcrypt
import asyncpg  # 🚀 لتوحيد الاستيرادات وتسريع الأداء
import os
import aiohttp
from datetime import datetime, timedelta
from aiogram.types import BufferedInputFile
import jwt
from decimal import Decimal, InvalidOperation # 🚀 الاستيراد الجديد
from functools import partial

# 🛡️ لحماية مهام الإشعارات الخلفية من الحذف (Garbage Collector  )
background_tasks = set()

def create_safe_task(coro):
    task = asyncio.create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return task

# 🚀 الميزة الجديدة: محول JSON ذكي يفهم الـ Decimal والتواريخ تلقائياً
class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return str(obj) # تحويل الأرقام المالية إلى نص لحمايتها في الواجهة
        if isinstance(obj, datetime):
            return obj.strftime('%Y-%m-%d %H:%M')
        return super().default(obj)

def custom_dumps(obj):
    return json.dumps(obj, cls=CustomJSONEncoder)

# 🚀 سحر برمجي: دمج المحول الذكي مع كل استجابات السيرفر بدون تعديل كل سطر!
original_json_response = web.json_response
web.json_response = partial(original_json_response, dumps=custom_dumps)

# 🛡️ دوال التحويل الآمنة
def safe_decimal(value, default='0.0'):
    """تحويل مالي دقيق جداً للمبالغ"""
    try:
        return Decimal(str(value)) if value not in [None, ""] else Decimal(default)
    except (ValueError, TypeError, InvalidOperation):
        return Decimal(default)

def safe_float(value, default=0.0):
    """تحويل للكسور العادية (للأوزان والكميات غير المالية)"""
    try:
        return float(value) if value not in [None, ""] else default
    except (ValueError, TypeError):
        return default

def safe_int(value, default=0):
    try:
        return int(value) if value not in [None, ""] else default
    except (ValueError, TypeError):
        return default

# 👇 الكود الجديد: ضبط التوقيت الموحد 👇
try:
    import zoneinfo
    local_tz = zoneinfo.ZoneInfo('Asia/Aden')
except ImportError:
    import pytz
    local_tz = pytz.timezone('Asia/Aden')

import database
from bot.setup import bot
from config import ADMIN_ID, ARCHIVE_GROUP_ID, JWT_SECRET
from services.notifications import send_push_notification
# بافتراض وجود ملف المحاسبة، نقوم باستيراد الدوال المحصنة
# from services.accounting import process_payment, process_balance_transfer

# 👇 الدالة المساعدة المعمارية الجديدة (The Manus Way) 👇
async def get_real_store_id(conn, request: web.Request, requested_store_id: int):
    safe_id = request.get('safe_telegram_id')
    role = request.get('auth_role')
    
    # 🛡️ تحويل safe_id إلى رقم صحيح (Integer) لتجنب خطأ قاعدة البيانات
    if safe_id is not None:
        safe_id = int(safe_id)
    
    # إذا كان المستخدم هو المدير العام (وضع الظل)، نثق بالـ requested_store_id القادم من الواجهة
    if role == 'admin' and requested_store_id:
        store_id = await conn.fetchval("SELECT id FROM stores WHERE telegram_id = $1 OR id = $1", requested_store_id)
        return store_id
        
    # أما إذا كان صاحب بقالة أو عامل، نتجاهل تماماً ما يرسله الـ Frontend ونعتمد على safe_id الموثوق!
    store_id = await conn.fetchval("SELECT id FROM stores WHERE telegram_id = $1", safe_id)
    if not store_id:
        store_id = await conn.fetchval("SELECT store_id FROM workers WHERE telegram_id = $1", safe_id)
        
    return store_id

async def api_manage_data(request: web.Request):
    store_tid = request.query.get('store_id')
    # 🛡️ حماية من الروابط الفارغة أو التي تحتوي على نصوص بدل الأرقام
    if not store_tid or not str(store_tid).isdigit(): 
        return web.json_response({"error": "Missing or invalid store_id"}, status=400)
    
    try:
        async with database.pool.acquire() as conn:
            store_id = await get_real_store_id(conn, request, int(store_tid))
                
            if not store_id: 
                return web.json_response({"error": "Store not found"}, status=404)
            
            # جلب المنتجات مع سعر التكلفة والعروض وحقول الكرتون والطازج والوحدات
            products = await conn.fetch("SELECT id, custom_name as name, price, cost_price, stock, category, barcode, expires_at, offer_text, carton_qty, carton_price, carton_barcode, is_fresh_item, unit_name, bulk_name FROM store_products WHERE store_id = $1 AND is_deleted = FALSE ORDER BY id DESC LIMIT 50", store_id)

            # 👇 جلب الزبائن مع فحص حالة تفعيل الإشعارات وصلاحية الكروت (مع جلب الفئات المسموحة) 👇
            customers = await conn.fetch("""
                SELECT c.id, c.name, c.phone, c.balance, c.credit_limit, c.can_pull_cards, c.allowed_ecard_amounts,
                       EXISTS(SELECT 1 FROM push_subscriptions ps WHERE ps.user_id = c.id AND ps.user_type = 'customer') as has_push
                FROM customers c 
                WHERE c.store_id = $1 AND c.is_deleted = FALSE AND c.parent_id IS NULL 
                ORDER BY c.id DESC LIMIT 50
            """, store_id)

            try:
                workers = await conn.fetch("SELECT id, name, telegram_id, passcode, permissions FROM workers WHERE store_id = $1 AND is_deleted = FALSE", store_id)
                agents = await conn.fetch("SELECT id, name, phone, is_active, passcode FROM delivery_agents WHERE store_id = $1 AND is_deleted = FALSE", store_id)
            except Exception:
                workers = []
                agents = []
                
            # 👇 الأسطر التي كانت مفقودة وتسبب الخطأ (تمت إعادتها) 👇
            prod_list = [{"id": p['id'], "name": p['name'], "price": float(p['price']), "cost_price": float(p['cost_price'] or 0), "stock": p['stock'], "category": p['category'], "barcode": p['barcode'], "expires_at": str(p['expires_at']) if p['expires_at'] else None, "offer_text": p['offer_text'], "carton_qty": p['carton_qty'], "carton_price": float(p['carton_price']), "carton_barcode": p['carton_barcode'], "is_fresh_item": p['is_fresh_item'], "unit_name": p['unit_name'] or 'حبة', "bulk_name": p['bulk_name'] or 'كرتون'} for p in products]
            
            import json
            cust_list = [{"id": c['id'], "name": c['name'], "phone": c['phone'], "balance": float(c['balance']), "credit_limit": float(c['credit_limit']), "has_push": c['has_push'], "can_pull_cards": c.get('can_pull_cards', False), "allowed_ecard_amounts": json.loads(c.get('allowed_ecard_amounts', '[]')) if isinstance(c.get('allowed_ecard_amounts'), str) else c.get('allowed_ecard_amounts', [])} for c in customers]
            
            agent_list = [{"id": a['id'], "name": a['name'], "phone": a['phone'], "is_active": a['is_active'], "passcode": a.get('passcode', '0000')} for a in agents]
            # 👆 نهاية الأسطر المستعادة 👆
            
            # 🚀 2. الحل السحري: جلب الطلبات المعلقة ومنتجاتها في خطوة واحدة (منع N+1 Query) 🚀
            pending_orders_records = await conn.fetch("""
                SELECT o.id, o.total_amount, o.payment_method, o.status, c.name as customer_name
                FROM orders o
                LEFT JOIN customers c ON o.customer_id = c.id
                WHERE o.store_id = $1 AND o.status IN ('pending', 'preparing', 'delivering')
                ORDER BY o.created_at DESC
            """, store_id)

            pending_orders = []
            if pending_orders_records:
                order_ids = [po['id'] for po in pending_orders_records]
                
                # جلب جميع المنتجات لكل الطلبات في استعلام واحد سريع جداً!
                all_items = await conn.fetch("""
                    SELECT oi.order_id, p.custom_name as name, oi.quantity as qty
                    FROM order_items oi
                    JOIN store_products p ON oi.product_id = p.id
                    WHERE oi.order_id = ANY($1::int[])
                """, order_ids)
                
                # ترتيب المنتجات في الذاكرة (أسرع بكثير من قاعدة البيانات)
                items_by_order = {}
                for item in all_items:
                    oid = item['order_id']
                    if oid not in items_by_order: items_by_order[oid] = []
                    items_by_order[oid].append({"name": item['name'], "qty": item['qty']})
                    
                for po in pending_orders_records:
                    oid = po['id']
                    order_items_list = items_by_order.get(oid, [])
                    items_dict = {str(i): {"name": it['name'], "qty": it['qty']} for i, it in enumerate(order_items_list)}
                    
                    pending_orders.append({
                        "order_id": oid,
                        "customer_name": po['customer_name'] or "زبون نقدي",
                        "total_amount": float(po['total_amount']),
                        "payment_method": po['payment_method'],
                        "items": items_dict
                    })
                       
            history_records = await conn.fetch("""
                SELECT o.id, o.total_amount, o.payment_method, o.status, o.created_at, c.name as customer_name
                FROM orders o
                LEFT JOIN customers c ON o.customer_id = c.id
                WHERE o.store_id = $1 AND o.status != 'pending'
                ORDER BY o.created_at DESC LIMIT 50
            """, store_id)
            
            order_history = []
            if history_records:
                h_order_ids = [h['id'] for h in history_records]
                
                # جلب المنتجات للطلبات السابقة
                h_all_items = await conn.fetch("""
                    SELECT oi.order_id, p.custom_name as name, oi.quantity as qty
                    FROM order_items oi
                    JOIN store_products p ON oi.product_id = p.id
                    WHERE oi.order_id = ANY($1::int[])
                """, h_order_ids)
                
                h_items_by_order = {}
                for item in h_all_items:
                    oid = item['order_id']
                    if oid not in h_items_by_order: h_items_by_order[oid] = []
                    h_items_by_order[oid].append({"name": item['name'], "qty": item['qty']})

                for h in history_records:
                    oid = h['id']
                    h_order_items_list = h_items_by_order.get(oid, [])
                    h_items_dict = {str(i): {"name": it['name'], "qty": it['qty']} for i, it in enumerate(h_order_items_list)}
                    
                    order_history.append({
                        "order_id": oid,
                        "customer_name": h['customer_name'] or "زبون نقدي",
                        "total_amount": float(h['total_amount']),
                        "payment_method": h['payment_method'],
                        "status": h['status'],
                        "created_at": h['created_at'].strftime('%Y-%m-%d %H:%M'),
                        "items": h_items_dict # 👈 السر هنا: إرسال المنتجات للواجهة لكي تُطبع الفاتورة!
                    })

            worker_list = []                
            for w in workers:
                if w['name'] == 'المدير العام': continue
                try: parsed_perms = json.loads(w['permissions'] if w['permissions'] else '["pos"]')
                except: parsed_perms = ["pos"]
                worker_list.append({"id": w['id'], "name": w['name'], "telegram_id": w['telegram_id'], "passcode": w['passcode'], "permissions": parsed_perms})
                
            # جلب بيانات البقالة (مع الحفاظ على alshehab_phone)
            store_info = await conn.fetchrow("SELECT name, status, trial_ends_at, package_type, theme_color, logo_url, alshehab_phone FROM stores WHERE id = $1", store_id)
            store_data = {
                "name": store_info['name'],
                "status": store_info['status'],
                "trial_ends_at": str(store_info['trial_ends_at'])[:10] if store_info['trial_ends_at'] else None,
                "package_type": store_info['package_type'],
                "theme_color": store_info['theme_color'],
                "logo_url": store_info['logo_url'],
                "is_alshehab_linked": bool(store_info['alshehab_phone']) # 🌟 إخبار الواجهة أن الحساب مربوط!
            }
            
            # 👇 الكود الجديد والصحيح لمعرفة دور المستخدم 👇
            safe_id = request.get('safe_telegram_id')
            if safe_id: safe_id = int(safe_id)
            
            user_role = "admin"  # الافتراضي: (صاحب البقالة أو المدير العام)
            worker_permissions = []
            
            if safe_id:
                # 1. هل هذا الشخص هو صاحب البقالة الأساسي؟
                is_owner = await conn.fetchval("SELECT id FROM stores WHERE telegram_id = $1 AND id = $2", safe_id, store_id)
                
                # 2. إذا لم يكن صاحب البقالة (ولم تكن أنت المدير العام في وضع الظل)، نبحث عنه في العمال
                if not is_owner:
                    current_worker = await conn.fetchrow("SELECT permissions FROM workers WHERE telegram_id = $1 AND store_id = $2 AND is_deleted = FALSE", safe_id, store_id)
                    if current_worker:
                        user_role = "staff" # هذا كاشير!
                        try:
                            worker_permissions = json.loads(current_worker['permissions'] or '[]')
                        except:
                            worker_permissions = []
                
            return web.json_response({
                "status": "success",
                "user_role": user_role,                     
                "permissions": worker_permissions,          
                "store_info": store_data,
                "products": prod_list, "customers": cust_list, "workers": worker_list,
                "agents": agent_list, "pending_orders": pending_orders, "order_history": order_history
            })

    except Exception as e:
        logging.error(f"Manage Data Error: {e}")
        return web.json_response({"error": str(e), "products": [], "customers": [], "workers": [], "agents": []})

async def api_manage_product_update(request: web.Request):
    data = await request.json()
    fresh_hours = safe_int(data.get('fresh_hours', 0)) # 🛡️ حماية الوقت من النصوص
    expires_at = datetime.now(local_tz) + timedelta(hours=fresh_hours) if fresh_hours > 0 else None

    # 🛡️ التحويل الآمن للبيانات
    price = safe_decimal(data.get('price'))
    if price <= 0:
        return web.json_response({"status": "error", "message": "يجب أن يكون السعر رقماً صحيحاً أكبر من الصفر"})

    cost_price = safe_decimal(data.get('cost_price', 0))
    carton_price = safe_decimal(data.get('carton_price', 0))
    carton_qty = safe_int(data.get('carton_qty', 0))
    
    raw_stock = data.get('stock')
    final_stock = safe_int(raw_stock) if raw_stock not in [None, ""] else None

    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, safe_int(data.get('store_id')))

        await conn.execute("""
            UPDATE store_products 
            SET custom_name = $1, price = $2, cost_price = $3, stock = $4, category = $5, barcode = $6, expires_at = $7, offer_text = $8, carton_qty = $9, carton_price = $10, carton_barcode = $11, is_fresh_item = $12, unit_name = $15, bulk_name = $16
            WHERE id = $13 AND store_id = $14
        """, data['name'], price, cost_price, final_stock, data.get('category', 'عام'), data.get('barcode'), expires_at, data.get('offer_text'), carton_qty, carton_price, data.get('carton_barcode'), data.get('is_fresh_item', False), safe_int(data.get('id')), store_id, data.get('unit_name', 'حبة'), data.get('bulk_name', 'كرتون'))

    return web.json_response({"status": "success"})

# ==========================================
# ⚠️ رادار المنتجات الميتة والتصفيات
# ==========================================
async def api_manage_dead_stock(request: web.Request):
    store_tid = request.query.get('store_id')
    # 🛡️ حماية من الرابط الفارغ
    if not store_tid or not str(store_tid).isdigit():
        return web.json_response({"status": "error", "message": "معرف البقالة مفقود"}, status=400)
        
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, int(store_tid))

        dead_products = await conn.fetch("""
            SELECT id, custom_name as name, price, stock 
            FROM store_products 
            WHERE store_id = $1 AND is_deleted = FALSE AND stock > 0
            AND id NOT IN (
                SELECT product_id FROM order_items oi
                JOIN orders o ON oi.order_id = o.id
                WHERE o.store_id = $1 AND o.created_at > NOW() - INTERVAL '30 days'
            )
        """, store_id)
        
        # 🌟 الإصلاح هنا: تحويل Decimal إلى float و int لتجنب خطأ JSON 🌟
        formatted_products = [
            {
                "id": p["id"],
                "name": p["name"],
                "price": float(p["price"]) if p["price"] is not None else 0.0,
                "stock": int(p["stock"]) if p["stock"] is not None else 0
            }
            for p in dead_products
        ]
        
    return web.json_response({"status": "success", "dead_stock": formatted_products})

async def api_manage_clearance(request: web.Request):
    data = await request.json()

    store_id = safe_int(data.get('store_id')) # 🛡️ تحويل آمن
    product_ids = data.get('product_ids', [])
    discount_percent = safe_decimal(data.get('discount')) # 🚀 استخدام التحويل المالي الدقيق
    
    if not isinstance(product_ids, list) or len(product_ids) == 0:
        return web.json_response({"status": "error", "message": "يجب تحديد المنتجات بشكل صحيح"}, status=400)
        
    if not store_id or discount_percent <= Decimal('0'):
        return web.json_response({"status": "error", "message": "البيانات غير مكتملة أو نسبة الخصم غير صحيحة"}, status=400)
    
    # 🛡️ التحقق من المدخلات: منع الخصم السالب أو الأكبر من 99%
    if discount_percent <= Decimal('0') or discount_percent >= Decimal('100'):
        return web.json_response({"status": "error", "message": "نسبة الخصم يجب أن تكون بين 1 و 99"})
        
    # 🚀 القسمة الآمنة باستخدام Decimal
    discount = discount_percent / Decimal('100.0')
    
    async with database.pool.acquire() as conn:
        real_store_id = await get_real_store_id(conn, request, store_id)
            
        # 🚀 طرح الخصم من 1.0 باستخدام Decimal أيضاً
        await conn.execute("""
            UPDATE store_products 
            SET price = price * $1, offer_text = '🔥 تصفية شاملة' 
            WHERE store_id = $2 AND id = ANY($3::int[])
        """, (Decimal('1.0') - discount), real_store_id, product_ids)
        
        customers = await conn.fetch("SELECT id FROM customers WHERE store_id = $1", real_store_id)
        
    for c in customers:
        # 🚀 تعديل الضرب هنا ليتوافق مع Decimal
        create_safe_task(send_push_notification(c['id'], "🔥 تصفية كبرى!", f"تم تخفيض أسعار بعض المنتجات بنسبة {int(discount * Decimal('100'))}%، سارع بالطلب!"))
        
    return web.json_response({"status": "success"})

async def api_manage_product_upload_image(request: web.Request):
    try:
        reader = await request.multipart()
        store_id, product_id, product_name, image_data = None, None, None, None
        
        async for field in reader:
            if field.name == 'store_id': store_id = safe_int(await field.read())
            elif field.name == 'product_id': product_id = safe_int(await field.read())
            elif field.name == 'product_name': product_name = (await field.read()).decode('utf-8')
            elif field.name == 'image': 
                # 🛡️ التحقق من نوع الملف
                if field.headers.get('Content-Type') not in ['image/jpeg', 'image/png', 'image/jpg']:
                    return web.json_response({"status": "error", "message": "صيغة الصورة غير مدعومة. يرجى رفع JPG أو PNG"})
                
                # 🛡️ قراءة الملف تدريجياً لحماية الذاكرة (الحد الأقصى 5 ميجابايت)
                max_size = 5 * 1024 * 1024
                chunks = []
                current_size = 0
                while True:
                    chunk = await field.read_chunk(8192)  # قراءة 8 كيلوبايت في كل مرة
                    if not chunk:
                        break
                    current_size += len(chunk)
                    if current_size > max_size:
                        return web.json_response({"status": "error", "message": "حجم الصورة كبير جداً. الحد الأقصى 5 ميجابايت"})
                    chunks.append(chunk)
                
                image_data = b''.join(chunks)
        
        if not all([store_id, product_id, product_name, image_data]):
            return web.json_response({"status": "error", "message": "بيانات مفقودة"})
        
        photo = BufferedInputFile(image_data, filename="product.jpg")
        target_chat = ARCHIVE_GROUP_ID if ARCHIVE_GROUP_ID else ADMIN_ID
        
        msg = await bot.send_photo(chat_id=target_chat, photo=photo, caption=f"🖼️ صورة منتج محدثة: {product_name}")
        file_id = msg.photo[-1].file_id
        
        async with database.pool.acquire() as conn:
            real_store_id = await get_real_store_id(conn, request, store_id)
            if not real_store_id:
                return web.json_response({"status": "error", "message": "البقالة غير موجودة"})

            query = """
                UPDATE store_products 
                SET telegram_file_id = $1 
                WHERE store_id = $2 AND id = $3
                RETURNING id
            """
            updated_rows = await conn.fetch(query, file_id, real_store_id, product_id)
            
        return web.json_response({"status": "success", "updated_count": len(updated_rows)})
    except Exception as e:
        logging.error(f"Image Upload Error: {e}")
        return web.json_response({"status": "error", "message": "حدث خطأ أثناء رفع الصورة"})

async def api_manage_product_delete(request: web.Request):
    data = await request.json()
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, safe_int(data.get('store_id'))
)
            
        await conn.execute("UPDATE store_products SET is_deleted = TRUE WHERE id = $1 AND store_id = $2", safe_int(data.get('id')), store_id)
        
        await conn.execute("""
            INSERT INTO fraud_logs (store_id, worker_name, action) 
            VALUES ($1, 'مستخدم النظام', 'قام بحذف منتج رقم ' || $2)
        """, store_id, str(data['id']))
        
    return web.json_response({"status": "success"})

async def api_manage_customer_delete(request: web.Request):
    data = await request.json()
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, safe_int(data.get('store_id'))
)

        # 🛡️ التأكد من نجاح الحذف فعلياً
        result = await conn.execute("UPDATE customers SET is_deleted = TRUE WHERE id = $1 AND store_id = $2 AND balance = 0", safe_int(data.get('id')), store_id)
        
        if result == "UPDATE 0":
            return web.json_response({"status": "error", "message": "لا يمكن حذف الزبون! إما أنه غير موجود أو أن عليه ديون/رصيد."})
            
    return web.json_response({"status": "success"})

# 👇 دالة تحويل الرصيد الجديدة 👇
async def api_manage_customer_transfer(request: web.Request):
    data = await request.json()
    amount = safe_decimal(data.get('amount')) # 🛡️ تحويل آمن
    from_id = safe_int(data.get('from_id'))
    to_id = safe_int(data.get('to_id'))
    
    if amount <= 0 or not from_id or not to_id:
        return web.json_response({"status": "error", "message": "بيانات التحويل غير صحيحة"})
    
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, safe_int(data.get('store_id')))
        if not store_id:
            return web.json_response({"status": "error", "message": "البقالة غير موجودة"})
            
    try:
        # استدعاء الدالة المحصنة من المطبخ المركزي
        from services.accounting import process_balance_transfer
        await process_balance_transfer(store_id, from_id, to_id, amount)
        return web.json_response({"status": "success"})
    except Exception as e:
        logging.error(f"Transfer Error: {e}")
        return web.json_response({"status": "error", "message": str(e)})

async def api_manage_worker_add(request: web.Request):
    data = await request.json()
    store_tid = safe_int(data.get('store_id')) # 🛡️
    worker_name = data['name']
    permissions = json.dumps(data.get('permissions', ['pos']))
    passcode = str(random.randint(10000, 99999))
    
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, store_tid)
        
        # 🚧 القيود البرمجية: الحد الأقصى للعمال في الباقة المجانية
        store_info = await conn.fetchrow("SELECT package_type FROM stores WHERE id = $1", store_id)
        if store_info and store_info['package_type'] == 'free':
            worker_count = await conn.fetchval("SELECT COUNT(*) FROM workers WHERE store_id = $1 AND is_deleted = FALSE", store_id)
            # يسمح بـ 2 فقط (المدير + عامل واحد)
            if worker_count >= 2: 
                return web.json_response({"status": "error", "message": "🚧 وصلت للحد الأقصى! الباقة المجانية تسمح بإضافة كاشير واحد فقط. للترقية إلى VIP تواصل مع الدعم."})
                
        while await conn.fetchval("SELECT id FROM workers WHERE passcode = $1", passcode):
            passcode = str(random.randint(10000, 99999))
            
        await conn.execute("""
            INSERT INTO workers (store_id, name, passcode, permissions) 
            VALUES ($1, $2, $3, $4)
        """, store_id, worker_name, passcode, permissions)
        
    return web.json_response({"status": "success", "passcode": passcode})

async def api_manage_worker_update(request: web.Request):
    data = await request.json()
    permissions = json.dumps(data.get('permissions', []))
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, safe_int(data.get('store_id')))
        await conn.execute("UPDATE workers SET name = $1, permissions = $2 WHERE id = $3 AND store_id = $4", data['name'], permissions, safe_int(data.get('id')), store_id)
    return web.json_response({"status": "success"})

async def api_manage_agent_delete(request: web.Request):
    data = await request.json()
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, safe_int(data.get('store_id')))
        # 🛡️ الحذف الناعم (Soft Delete) لحماية التقارير السابقة وإمكانية الاستعادة
        await conn.execute("UPDATE delivery_agents SET is_deleted = TRUE WHERE id = $1 AND store_id = $2", safe_int(data.get('id')), store_id)
    return web.json_response({"status": "success"})

# ==========================================
# 🛵 مسارات المناديب
# ==========================================
async def api_manage_agent_add(request: web.Request):
    data = await request.json()
    passcode = str(random.randint(1000, 9999))
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, safe_int(data.get('store_id')))
        
        # 🚧 القيود البرمجية: المناديب ميزة VIP فقط
        store_info = await conn.fetchrow("SELECT package_type FROM stores WHERE id = $1", store_id)
        if store_info and store_info['package_type'] == 'free':
            return web.json_response({"status": "error", "message": "🚧 عذراً، ميزة التوصيل والمناديب متاحة فقط في باقة VIP. قم بالترقية لمضاعفة مبيعاتك!"})
            
        while await conn.fetchval("SELECT id FROM delivery_agents WHERE passcode = $1", passcode):
            passcode = str(random.randint(1000, 9999))
            
        await conn.execute("INSERT INTO delivery_agents (store_id, name, phone, passcode) VALUES ($1, $2, $3, $4)", store_id, data['name'], data.get('phone'), passcode)
    return web.json_response({"status": "success", "passcode": passcode})

async def api_manage_agent_update(request: web.Request):
    data = await request.json()
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, safe_int(data.get('store_id')))
        await conn.execute("UPDATE delivery_agents SET name = $1, phone = $2 WHERE id = $3 AND store_id = $4", data['name'], data.get('phone'), safe_int(data.get('id')), store_id)
    return web.json_response({"status": "success"})

async def api_manage_worker_delete(request: web.Request):
    data = await request.json()
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, safe_int(data.get('store_id')))
        await conn.execute("UPDATE workers SET is_deleted = TRUE WHERE id = $1 AND store_id = $2", safe_int(data.get('id')), store_id)
    return web.json_response({"status": "success"})

# ==========================================
# 🛒 معالجة الطلبات الحية
# ==========================================
async def api_manage_order_action(request: web.Request):
    data = await request.json()
    
    # 🛡️ حماية من البيانات الناقصة (KeyError)
    if 'store_id' not in data or 'order_id' not in data or 'action' not in data:
        return web.json_response({"status": "error", "message": "بيانات الطلب غير مكتملة"}, status=400)
        
    store_id = safe_int(data.get('store_id'))

    order_id = safe_int(data.get('order_id'))
    action = data['action']
    agent_id = data.get('agent_id')
    
    try:
        async with database.pool.acquire() as conn:
            real_store_id = await get_real_store_id(conn, request, store_id)
                
            order = await conn.fetchrow("SELECT customer_id, total_amount, payment_method, status FROM orders WHERE id = $1 AND store_id = $2", order_id, real_store_id)
            if not order:
                return web.json_response({"status": "error", "message": "الطلب غير موجود"})
                
            if action == 'accept':
                if agent_id:
                    await conn.execute("UPDATE orders SET status = 'preparing', agent_id = $1 WHERE id = $2", safe_int(agent_id), order_id)
                else:
                    await conn.execute("UPDATE orders SET status = 'preparing' WHERE id = $1", order_id)
                    
                if order['customer_id']:
                    msg = "تم قبول طلبك وتعيين مندوب لتوصيله 🛵." if agent_id else "البقالة تقوم بتجهيز طلبك الآن ⏳."
                    create_safe_task(send_push_notification(order['customer_id'], "✅ تم قبول طلبك!", msg))
                    
            elif action == 'delivering':
                agent_id = data.get('agent_id')
                # 🛡️ استخدام التحويل الآمن لحماية السيرفر من القيم الفارغة
                delivery_fee = safe_decimal(data.get('delivery_fee', 0.0))
                
                if agent_id:
                    await conn.execute("UPDATE orders SET status = 'delivering', agent_id = $1, delivery_fee = $2 WHERE id = $3", safe_int(agent_id), delivery_fee, order_id)

                else:
                    await conn.execute("UPDATE orders SET status = 'delivering', delivery_fee = $1 WHERE id = $2", delivery_fee, order_id)
                    
                if order['customer_id']:
                    create_safe_task(send_push_notification(order['customer_id'], "🛵 طلبك في الطريق!", "المندوب في طريقه إليك، استعد لاستلام الطلب."))
                    
            elif action == 'completed':
                await conn.execute("UPDATE orders SET status = 'completed' WHERE id = $1", order_id)
                if order['customer_id']:
                    create_safe_task(send_push_notification(order['customer_id'], "✅ اكتمل الطلب", "بالعافية عليك! تم تسليم الطلب بنجاح."))
                    
            elif action == 'completed_as_credit':
                if not order['customer_id']:
                    return web.json_response({"status": "error", "message": "لا يمكن تحويل طلب الزبون النقدي إلى آجل"})
                
                async with conn.transaction():
                    # تحويل الطلب إلى آجل وإضافة الدين
                    await conn.execute("UPDATE orders SET status = 'completed', payment_method = 'credit' WHERE id = $1", order_id)
                    await conn.execute("UPDATE customers SET balance = balance + $1 WHERE id = $2", order['total_amount'], order['customer_id'])
                    await conn.execute("""
                        INSERT INTO transactions (store_id, customer_id, order_id, trans_type, amount, details)
                        VALUES ($1, $2, $3, 'credit_sale', $4, 'تحويل طلب كاش إلى آجل عند التسليم')
                    """, real_store_id, order['customer_id'], order_id, order['total_amount'])
                    
                if order['customer_id']:
                    create_safe_task(send_push_notification(order['customer_id'], "📝 تم التقييد كآجل", f"تم تسليم طلبك وتسجيل {order['total_amount']} ريال على حسابك."))
                    
            elif action == 'reject':
                # 🚀 استدعاء المطبخ المركزي لمعالجة الرفض بأمان
                from services.accounting import process_order_rejection
                worker_id_val = safe_int(data.get('worker_id')) if data.get('worker_id') not in [None, ""] else None
                await process_order_rejection(real_store_id, order_id, worker_id_val)
                    
        return web.json_response({"status": "success"})
    except Exception as e:
        logging.error(f"Order Action Error: {e}")
        return web.json_response({"status": "error", "message": "حدث خطأ أثناء معالجة الطلب"})

# ==========================================
# 📢 إشعار المنتجات الطازجة وسوق اليوم
# ==========================================
async def api_manage_promo_fresh(request: web.Request):
    data = await request.json()
    store_tid = safe_int(data.get('store_id'))

    
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, store_tid)
            
        customers = await conn.fetch("SELECT id FROM customers WHERE store_id = $1", store_id)
        
    for c in customers:
        create_safe_task(send_push_notification(
            c['id'], 
            "🥬 وصل الطازج!", 
            "وصلت الآن منتجات طازجة (خضار، فواكه، لحوم) للبقالة، الكمية محدودة سارع بالطلب! 🔥"
        ))
        
    return web.json_response({"status": "success"})

async def api_manage_promo_launch_fresh(request: web.Request):
    """إطلاق سوق اليوم وتحديث الأسعار والكميات وتواريخ الانتهاء"""
    data = await request.json()
    store_tid = safe_int(data.get('store_id'))

    items = data.get('items', []) # 🛡️ حماية من الـ KeyError
    if not isinstance(items, list) or len(items) == 0:
        return web.json_response({"status": "error", "message": "لا توجد منتجات محددة"}, status=400)
        
    hours = safe_int(data.get('hours', 24))
    
    expires_at = datetime.now(local_tz) + timedelta(hours=hours)
    
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, store_tid)

        # 🛡️ تحديث كل منتج بسعره وكميته الجديدة، وتحويل قسمه إلى "طازج" ليعمل عليه نظام الإخفاء
        async with conn.transaction():
            for item in items:
                item_price = safe_decimal(item.get('price'))
                item_stock = safe_int(item.get('stock'))
                await conn.execute("""
                    UPDATE store_products 
                    SET expires_at = $1, price = $2, stock = $3, category = 'طازج'
                    WHERE store_id = $4 AND id = $5
                """, expires_at, item_price, item_stock, store_id, int(item['id']))
        
        customers = await conn.fetch("SELECT id FROM customers WHERE store_id = $1", store_id)
        
    for c in customers:
        import asyncio
        from services.notifications import send_push_notification
        create_safe_task(send_push_notification(
            c['id'], 
            "🍅🐟 سوق اليوم بدأ!", 
            "تم إطلاق عروض طازجة جديدة في البقالة، سارع بالطلب قبل نفاد الكمية! 🔥"
        ))
        
    return web.json_response({"status": "success"})

# ==========================================
# 🎨 هوية البقالة (Branding)
# ==========================================
async def api_manage_branding_update(request: web.Request):
    try:
        reader = await request.multipart()
        store_id, store_name, theme_color, logo_data = None, None, None, None
        
        async for field in reader:
            if field.name == 'store_id': store_id = safe_int(await field.read()) # 🛡️ إضافة الحماية هنا أيضاً
            elif field.name == 'store_name': store_name = (await field.read()).decode('utf-8')
            elif field.name == 'theme_color': theme_color = (await field.read()).decode('utf-8')
            elif field.name == 'logo': 
                # 🛡️ التحقق من نوع الملف
                if field.headers.get('Content-Type') not in ['image/jpeg', 'image/png', 'image/jpg']:
                    return web.json_response({"status": "error", "message": "صيغة الشعار غير مدعومة. يرجى رفع JPG أو PNG"})
                
                # 🛡️ قراءة الملف تدريجياً لحماية الذاكرة (الحد الأقصى 5 ميجابايت)
                max_size = 5 * 1024 * 1024
                chunks = []
                current_size = 0
                while True:
                    chunk = await field.read_chunk(8192)  # قراءة 8 كيلوبايت في كل مرة
                    if not chunk:
                        break
                    current_size += len(chunk)
                    if current_size > max_size:
                        return web.json_response({"status": "error", "message": "حجم الشعار كبير جداً. الحد الأقصى 5 ميجابايت"})
                    chunks.append(chunk)
                
                logo_data = b''.join(chunks)
        
        if not store_id or not theme_color or not store_name:
            return web.json_response({"status": "error", "message": "بيانات مفقودة"})
        
        async with database.pool.acquire() as conn:
            real_store_id = await get_real_store_id(conn, request, store_id)
            
            if logo_data:
                photo = BufferedInputFile(logo_data, filename="logo.jpg")
                target_chat = ARCHIVE_GROUP_ID if ARCHIVE_GROUP_ID else ADMIN_ID
                msg = await bot.send_photo(chat_id=target_chat, photo=photo, caption=f"🎨 شعار جديد للبقالة: {store_name}")
                file_id = msg.photo[-1].file_id
                logo_url = f"/api/image/{file_id}"
                
                await conn.execute("UPDATE stores SET name = $1, theme_color = $2, logo_url = $3 WHERE id = $4", store_name, theme_color, logo_url, real_store_id)
            else:
                await conn.execute("UPDATE stores SET name = $1, theme_color = $2 WHERE id = $3", store_name, theme_color, real_store_id)
                
        return web.json_response({"status": "success"})
    except Exception as e:
        logging.error(f"Branding Update Error: {e}")
        return web.json_response({"status": "error", "message": "حدث خطأ أثناء تحديث الهوية"})

async def api_manage_products_page(request: web.Request):
    """جلب المنتجات بنظام الدفعات مع دعم البحث التقريبي"""
    store_tid = safe_int(request.query.get('store_id'))
    page = safe_int(request.query.get('page', 1))
    if page < 1: page = 1
    search_query = request.query.get('search', '').strip()
    limit = 50
    offset = (page - 1) * limit

    products = [] # 🛡️ تعريف مسبق لتجنب خطأ UnboundLocalError
    
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, store_tid)
        
        if store_id: # 🛡️ التأكد من وجود البقالة أولاً
            if search_query:
                fuzzy = f"%{search_query.replace(' ', '%')}%"
                products = await conn.fetch("""
                    SELECT id, custom_name as name, price, cost_price, stock, category, barcode, expires_at, offer_text, carton_qty, carton_price, carton_barcode, is_fresh_item, unit_name, bulk_name 
                    FROM store_products 
                    WHERE store_id = $1 AND is_deleted = FALSE 
                    AND (custom_name ILIKE $4 OR category ILIKE $4 OR barcode ILIKE $4)
                    ORDER BY id DESC LIMIT $2 OFFSET $3
                """, store_id, limit, offset, fuzzy)
            else:
                products = await conn.fetch("""
                    SELECT id, custom_name as name, price, cost_price, stock, category, barcode, expires_at, offer_text, carton_qty, carton_price, carton_barcode, is_fresh_item, unit_name, bulk_name 
                    FROM store_products 
                    WHERE store_id = $1 AND is_deleted = FALSE
                    ORDER BY id DESC LIMIT $2 OFFSET $3
                """, store_id, limit, offset)
        
    prod_list = [{"id": p['id'], "name": p['name'], "price": float(p['price']), "cost_price": float(p['cost_price'] or 0), "stock": p['stock'], "category": p['category'], "barcode": p['barcode'], "expires_at": str(p['expires_at']) if p['expires_at'] else None, "offer_text": p['offer_text'], "carton_qty": p['carton_qty'], "carton_price": float(p['carton_price']), "carton_barcode": p['carton_barcode'], "is_fresh_item": p['is_fresh_item'], "unit_name": p['unit_name'] or 'حبة', "bulk_name": p['bulk_name'] or 'كرتون'} for p in products]
    return web.json_response({"status": "success", "products": prod_list})

async def api_manage_customers_page(request: web.Request):
    """جلب العملاء بنظام الدفعات مع دعم البحث التقريبي (وإخفاء العائلة)"""
    import json
    store_tid = safe_int(request.query.get('store_id'))
    page = safe_int(request.query.get('page', 1))
    if page < 1: page = 1
    search_query = request.query.get('search', '').strip()
    limit = 50
    offset = (page - 1) * limit
    
    customers = [] # 🛡️ تعريف مسبق لتجنب الأخطاء

    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, store_tid)
        
        if store_id: # 🛡️ التأكد من وجود البقالة أولاً
            if search_query:
                fuzzy = f"%{search_query.replace(' ', '%')}%"
                customers = await conn.fetch("""
                    SELECT c.id, c.name, c.phone, c.balance, c.credit_limit, c.can_pull_cards, c.allowed_ecard_amounts,
                           EXISTS(SELECT 1 FROM push_subscriptions ps WHERE ps.user_id = c.id AND ps.user_type = 'customer') as has_push
                    FROM customers c 
                    WHERE c.store_id = $1 AND c.is_deleted = FALSE AND c.parent_id IS NULL
                    AND (c.name ILIKE $4 OR c.phone ILIKE $4)
                    ORDER BY c.id DESC LIMIT $2 OFFSET $3
                """, store_id, limit, offset, fuzzy)
            else:
                customers = await conn.fetch("""
                    SELECT c.id, c.name, c.phone, c.balance, c.credit_limit, c.can_pull_cards, c.allowed_ecard_amounts,
                           EXISTS(SELECT 1 FROM push_subscriptions ps WHERE ps.user_id = c.id AND ps.user_type = 'customer') as has_push
                    FROM customers c 
                    WHERE c.store_id = $1 AND c.is_deleted = FALSE AND c.parent_id IS NULL
                    ORDER BY c.id DESC LIMIT $2 OFFSET $3
                """, store_id, limit, offset)
        
    cust_list = [
        {
            "id": c['id'], 
            "name": c['name'], 
            "phone": c['phone'], 
            "balance": float(c['balance']), 
            "credit_limit": float(c['credit_limit']), 
            "has_push": c['has_push'], 
            "can_pull_cards": c.get('can_pull_cards', False),
            "allowed_ecard_amounts": json.loads(c.get('allowed_ecard_amounts', '[]')) if isinstance(c.get('allowed_ecard_amounts'), str) else c.get('allowed_ecard_amounts', [])
        } for c in customers
    ]
    return web.json_response({"status": "success", "customers": cust_list})

async def api_manage_product_add(request: web.Request):
    data = await request.json()
    fresh_hours = safe_int(data.get('fresh_hours', 0)) # 🛡️ حماية الوقت من النصوص
    expires_at = datetime.now(local_tz) + timedelta(hours=fresh_hours) if fresh_hours > 0 else None
    prod_name = data['name'].strip()
    category = data.get('category', 'عام').strip()

    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, safe_int(data.get('store_id'))
)

        # 1. الذكاء الجماعي: التحقق هل المنتج موجود في الكتالوج المركزي؟
        global_pid = await conn.fetchval("SELECT id FROM global_products WHERE name = $1", prod_name)
        
        # إذا لم يكن موجوداً، نقوم بإضافته للكتالوج المركزي لكي تستفيد منه البقالات الأخرى لاحقاً!
        if not global_pid:
            global_pid = await conn.fetchval("""
                INSERT INTO global_products (name, category) 
                VALUES ($1, $2) RETURNING id
            """, prod_name, category)

        # 2. إضافة المنتج لبقالة المستخدم وربطه بالكتالوج المركزي (مع التحويل الآمن 🛡️)
        price = safe_decimal(data.get('price'))
        cost_price = safe_decimal(data.get('cost_price'))
        stock = safe_int(data.get('stock')) if data.get('stock') not in [None, ""] else None
        carton_qty = safe_int(data.get('carton_qty'))
        carton_price = safe_decimal(data.get('carton_price'))

        new_id = await conn.fetchval("""
            INSERT INTO store_products (store_id, product_id, custom_name, price, cost_price, stock, category, barcode, expires_at, offer_text, carton_qty, carton_price, carton_barcode, is_fresh_item, unit_name, bulk_name)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16) RETURNING id
        """, store_id, global_pid, prod_name, price, cost_price, stock, category, data.get('barcode'), expires_at, data.get('offer_text'), carton_qty, carton_price, data.get('carton_barcode'), data.get('is_fresh_item', False), data.get('unit_name', 'حبة'), data.get('bulk_name', 'كرتون'))
        
    return web.json_response({"status": "success", "product_id": new_id})

async def api_manage_catalog_search(request: web.Request):
    query = request.query.get('q', '').strip()
    if len(query) < 2:
        return web.json_response({"status": "success", "results": []})
        
    async with database.pool.acquire() as conn:
        fuzzy = f"%{query.replace(' ', '%')}%"
        results = await conn.fetch("""
            SELECT id, name, category 
            FROM global_products 
            WHERE name ILIKE $1 
            ORDER BY name LIMIT 10
        """, fuzzy)
        
    return web.json_response({
        "status": "success", 
        "results": [{"id": r['id'], "name": r['name'], "category": r['category']} for r in results]
    })

async def api_manage_customer_add(request: web.Request):
    data = await request.json()
    
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, safe_int(data.get('store_id'))
)
        can_pull = bool(data.get('can_pull_cards', False)) # 👈 استقبال صلاحية الكروت
        
        # 🚧 القيود البرمجية: الحد الأقصى للزبائن في الباقة المجانية
        store_info = await conn.fetchrow("SELECT package_type FROM stores WHERE id = $1", store_id)
        if store_info and store_info['package_type'] == 'free':
            cust_count = await conn.fetchval("SELECT COUNT(*) FROM customers WHERE store_id = $1 AND is_deleted = FALSE AND parent_id IS NULL", store_id)
            if cust_count >= 50:
                return web.json_response({"status": "error", "message": "🚧 وصلت للحد الأقصى! الباقة المجانية تسمح بـ 50 زبون فقط. يرجى الترقية لـ VIP لإضافة عدد غير محدود من الزبائن."})
        
        try:
            # 🚀 توليد كلمة المرور في الخلفية لمنع تجميد السيرفر 🚀
            default_pass = "1234"
            hashed_bytes = await asyncio.to_thread(bcrypt.hashpw, default_pass.encode('utf-8'), bcrypt.gensalt())
            hashed_pw = hashed_bytes.decode('utf-8')
            
            # 🌟 جلب الفئات المسموحة وتحويلها إلى JSON
            allowed_amounts = data.get('allowed_ecard_amounts', [])
            allowed_amounts_json = json.dumps(allowed_amounts)
            
            await conn.execute("""
                INSERT INTO customers (store_id, name, phone, credit_limit, balance, password, can_pull_cards, allowed_ecard_amounts)
                VALUES ($1, $2, $3, $4, 0, $5, $6, $7::jsonb)
            """, store_id, data['name'], data.get('phone', ''), float(data.get('credit_limit', 0)), hashed_pw, can_pull, allowed_amounts_json)
            
        except asyncpg.exceptions.UniqueViolationError:
            return web.json_response({"status": "error", "message": "رقم الهاتف مسجل مسبقاً لعميل آخر في بقالتك!"})
            
    return web.json_response({"status": "success"})

async def api_manage_customer_update(request: web.Request):
    data = await request.json()
    
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, safe_int(data.get('store_id'))
)
        can_pull = bool(data.get('can_pull_cards', False)) # 👈 استقبال صلاحية الكروت
        
        # 🌟 جلب الفئات المسموحة وتحويلها إلى JSON
        allowed_amounts = data.get('allowed_ecard_amounts', [])
        allowed_amounts_json = json.dumps(allowed_amounts)

        try:
            await conn.execute("""
                UPDATE customers 
                SET name = $1, phone = $2, credit_limit = $3, can_pull_cards = $6, allowed_ecard_amounts = $7::jsonb
                WHERE id = $4 AND store_id = $5
            """, data['name'], data.get('phone', ''), safe_decimal(data.get('credit_limit', 0)), safe_int(data.get('id')), store_id, can_pull, allowed_amounts_json)
            
        except asyncpg.exceptions.UniqueViolationError:
            return web.json_response({"status": "error", "message": "رقم الهاتف مسجل مسبقاً لعميل آخر في بقالتك!"})
            
    return web.json_response({"status": "success"})

# ==========================================
# 🚨 سجل التلاعبات والنشاط المشبوه (أمني)
# ==========================================
async def api_manage_fraud_log(request: web.Request):
    store_tid = request.query.get('store_id')
    if not store_tid:
        return web.json_response({"status": "error", "message": "Missing store_id"})

    try:
        async with database.pool.acquire() as conn:
            store_id = await get_real_store_id(conn, request, int(store_tid))

            if not store_id:
                return web.json_response({"status": "error", "message": "Store not found"})

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS fraud_logs (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER,
                    worker_name TEXT,
                    action TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            logs = await conn.fetch("""
                SELECT worker_name, action, created_at 
                FROM fraud_logs 
                WHERE store_id = $1 
                ORDER BY created_at DESC 
                LIMIT 50
            """, store_id)

            log_list = []
            for log in logs:
                log_list.append({
                    "worker_name": log['worker_name'] or "مجهول",
                    "action": log['action'],
                    "created_at": log['created_at'].strftime('%Y-%m-%d %H:%M')
                })

        return web.json_response({"status": "success", "logs": log_list})
    except Exception as e:
        logging.error(f"Fraud Log Error: {e}")
        return web.json_response({"status": "error", "message": "حدث خطأ أثناء جلب السجل"})

# =====================================================================
# 💰 تسجيل سداد وكشف حساب من تطبيق الإدارة
# =====================================================================
async def api_manage_customer_pay(request: web.Request):
    data = await request.json()
    amount = safe_decimal(data.get('amount')) # 🚀 استخدام التحويل المالي الدقيق
    cid = safe_int(data.get('customer_id'))
    
    if amount <= Decimal('0') or not cid:
        return web.json_response({"status": "error", "message": "بيانات الدفع غير صحيحة"})
    
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, safe_int(data.get('store_id'))
)
        if not store_id:
            return web.json_response({"status": "error", "message": "البقالة غير موجودة"})
            
    try:
        # استدعاء الدالة المحصنة من المطبخ المركزي
        from services.accounting import process_payment
        new_balance = await process_payment(store_id, cid, amount)
        return web.json_response({"status": "success", "new_balance": new_balance})
    except Exception as e:
        logging.error(f"Payment Error: {e}")
        return web.json_response({"status": "error", "message": str(e)})

async def api_manage_customer_statement(request: web.Request):
    store_tid = request.query.get('store_id')
    cid = request.query.get('customer_id')
    
    # 🛡️ حماية من الروابط الناقصة
    if not store_tid or not cid or not str(store_tid).isdigit() or not str(cid).isdigit():
        return web.json_response({"status": "error", "message": "بيانات الرابط غير مكتملة أو خاطئة"}, status=400)
        
    store_tid, cid = int(store_tid), int(cid)
    
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, store_tid)
            
        cust = await conn.fetchrow("SELECT name, balance FROM customers WHERE id = $1 AND store_id = $2", cid, store_id)
        if not cust: return web.json_response({"status": "error"})
        
        trans = await conn.fetch("SELECT id, trans_type, amount, details, created_at FROM transactions WHERE customer_id = $1 ORDER BY created_at DESC", cid)
        
    t_list = [{"id": t['id'], "type": t['trans_type'], "amount": float(t['amount']), "details": t['details'], "date": t['created_at'].strftime('%Y-%m-%d %H:%M')} for t in trans]
    
    return web.json_response({"status": "success", "customer": dict(cust), "transactions": t_list})

# =====================================================================
# 📊 التقارير وإرسال العروض للزبائن
# =====================================================================
async def api_manage_reports(request: web.Request):
    store_tid = request.query.get('store_id')
    start_date = request.query.get('start_date')
    end_date = request.query.get('end_date')
    
    # 🛡️ حماية من الروابط الناقصة أو التواريخ الخاطئة
    if not store_tid or not str(store_tid).isdigit() or not start_date or not end_date:
        return web.json_response({"status": "error", "message": "بيانات التقرير غير مكتملة"}, status=400)
        
    try:
        datetime.strptime(start_date, '%Y-%m-%d')
        datetime.strptime(end_date, '%Y-%m-%d')
    except ValueError:
        return web.json_response({"status": "error", "message": "صيغة التاريخ غير صحيحة، يجب أن تكون YYYY-MM-DD"}, status=400)
        
    store_tid = int(store_tid)
    
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, store_tid)
            
        sales = await conn.fetch("""
            SELECT c.name, t.amount, t.details, t.created_at 
            FROM transactions t 
            LEFT JOIN customers c ON t.customer_id = c.id 
            WHERE t.store_id = $1 AND DATE(t.created_at) >= $2::DATE AND DATE(t.created_at) <= $3::DATE
            ORDER BY t.created_at DESC
        """, store_id, start_date, end_date)
        
    s_list = [{"name": s['name'] or "زبون نقدي", "amount": float(s['amount']), "details": s['details'], "date": s['created_at'].strftime('%Y-%m-%d %H:%M')} for s in sales]
    return web.json_response({"status": "success", "sales": s_list})

async def api_manage_send_promo(request: web.Request):
    data = await request.json()
    store_tid = safe_int(data.get('store_id'))

    msg_text = data['message']
    
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, store_tid)
            
        customers = await conn.fetch("SELECT id FROM customers WHERE store_id = $1", store_id)
        
    for c in customers:
        create_safe_task(send_push_notification(c['id'], "📢 عرض خاص من البقالة!", msg_text))
        
    return web.json_response({"status": "success", "count": len(customers)})

# =====================================================================
# 🪄 التعبئة التلقائية للمتجر (للبقالات الجديدة)
# =====================================================================
async def api_manage_auto_fill(request: web.Request):
    data = await request.json()
    store_tid = safe_int(data.get('store_id'))

    
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
        store_id = await get_real_store_id(conn, request, store_tid)
            
        count = await conn.fetchval("SELECT COUNT(*) FROM store_products WHERE store_id = $1 AND is_deleted = FALSE", store_id)
        if count > 5:
            return web.json_response({"status": "error", "message": "متجرك يحتوي على منتجات بالفعل. لا يمكن استخدام التعبئة التلقائية لتجنب التكرار."})
            
        for p in default_products:
            pid = await conn.fetchval("SELECT id FROM global_products WHERE name = $1", p['name'])
            if not pid:
                pid = await conn.fetchval("INSERT INTO global_products (name, category) VALUES ($1, $2) RETURNING id", p['name'], p['category'])
            
            await conn.execute("""
                INSERT INTO store_products (store_id, product_id, custom_name, price, category)
                VALUES ($1, $2, $3, $4, $5)
            """, store_id, pid, p['name'], p['price'], p['category'])
            
    return web.json_response({"status": "success", "message": f"تم إضافة {len(default_products)} منتج أساسي بنجاح!"})

async def api_manage_change_passcode(request: web.Request):
    data = await request.json()
    store_tid = safe_int(data.get('store_id'))

    new_passcode = data['new_passcode']
    
    async with database.pool.acquire() as conn:
        await conn.execute("UPDATE workers SET passcode = $1 WHERE telegram_id = $2 AND name = 'المدير العام'", new_passcode, store_tid)
        
    return web.json_response({"status": "success", "message": "تم تغيير الكود السري بنجاح!"})

# =====================================================================
# 🔑 تصفير كلمة المرور وإرسال كشف الحساب للصندوق
# =====================================================================
async def api_manage_customer_reset_pass(request: web.Request):
    data = await request.json()
    cid = safe_int(data.get('customer_id'))
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, safe_int(data.get('store_id'))
)
        
        new_pass = "1234"
        # 🚀 التشفير في الخلفية 🚀
        hashed_bytes = await asyncio.to_thread(bcrypt.hashpw, new_pass.encode('utf-8'), bcrypt.gensalt())
        hashed_pw = hashed_bytes.decode('utf-8')
        await conn.execute("UPDATE customers SET password = $1 WHERE id = $2 AND store_id = $3", hashed_pw, cid, store_id)
        
    return web.json_response({"status": "success", "new_password": new_pass})

async def api_manage_customer_send_statement(request: web.Request):
    data = await request.json()
    cid = safe_int(data.get('customer_id'))
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, safe_int(data.get('store_id'))
)
        
        await conn.execute("""
            INSERT INTO invoices (store_id, customer_id, title, pdf_url)
            VALUES ($1, $2, '📜 كشف حساب شامل', '#statement')
        """, store_id, cid)
        
    create_safe_task(send_push_notification(cid, "📜 كشف حساب جديد", "تم إرسال كشف حسابك التفصيلي إلى صندوق الوارد.", {"action": "open_inbox"}))
    return web.json_response({"status": "success"})

# ==========================================
# 🛵 تطبيق المندوب (Agent App)
# ==========================================
async def api_agent_data(request: web.Request):
    auth_header = request.headers.get('Authorization', '')
    parts = auth_header.split(' ')
    if not auth_header.startswith('Bearer ') or len(parts) < 2 or not parts[1]:
        return web.json_response({"status": "error", "message": "غير مصرح"}, status=401)
    
    token = parts[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
        agent_id = payload['agent_id']
    except Exception:
        return web.json_response({"status": "error", "message": "جلسة غير صالحة"}, status=401)

    async with database.pool.acquire() as conn:
        # 1. جلب الطلبات الحالية المسندة للمندوب مع عنوان التوصيل الدقيق
        orders_records = await conn.fetch("""
            SELECT o.id, o.total_amount, o.payment_method, o.delivery_fee, 
                   c.name as customer_name, c.phone, 
                   COALESCE(o.delivery_address, c.address) as address, 
                   o.customer_id
            FROM orders o
            LEFT JOIN customers c ON o.customer_id = c.id
            WHERE o.agent_id = $1 AND o.status = 'delivering'
        """, agent_id)
        
        orders = []
        for o in orders_records:
            orders.append({
                "id": o['id'],
                "customer_id": o['customer_id'],
                "customer_name": o['customer_name'] or "زبون نقدي",
                "phone": o['phone'],
                "address": o['address'],
                "total_amount": float(o['total_amount']),
                "payment_method": o['payment_method'],
                "delivery_fee": float(o['delivery_fee'] or 0.0)
            })
            
        # 2. حساب المحفظة (أرباح اليوم والعهدة)
        # الأرباح = مجموع رسوم التوصيل للطلبات المكتملة اليوم
        earnings = await conn.fetchval("""
            SELECT SUM(delivery_fee) FROM orders 
            WHERE agent_id = $1 AND status = 'completed' AND DATE(created_at) = CURRENT_DATE
        """, agent_id) or 0.0
        
        # العهدة = مجموع الكاش المستلم اليوم (الطلبات النقدية فقط)
        cash_to_return = await conn.fetchval("""
            SELECT SUM(total_amount) FROM orders 
            WHERE agent_id = $1 AND status = 'completed' AND payment_method = 'cash' AND DATE(created_at) = CURRENT_DATE
        """, agent_id) or 0.0
        
    return web.json_response({
        "status": "success",
        "orders": orders,
        "wallet": {
            "earnings": float(earnings),
            "cash_to_return": float(cash_to_return)
        }
    })

# ================= 💬 نظام المحادثة الفورية (البقالة) =================
async def api_manage_chat_list(request: web.Request):
    store_tid = safe_int(request.query.get('store_id'))
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, store_tid)
            
        # جلب قائمة الزبائن الذين لديهم محادثات، مع عدد الرسائل غير المقروءة وآخر رسالة
        chats = await conn.fetch("""
            SELECT c.id, c.name, 
                   (SELECT COUNT(*) FROM chat_messages WHERE customer_id = c.id AND store_id = $1 AND sender_type = 'customer' AND is_read = FALSE) as unread_count,
                   (SELECT message_text FROM chat_messages WHERE customer_id = c.id AND store_id = $1 ORDER BY created_at DESC LIMIT 1) as last_message,
                   (SELECT created_at FROM chat_messages WHERE customer_id = c.id AND store_id = $1 ORDER BY created_at DESC LIMIT 1) as last_time
            FROM customers c
            WHERE EXISTS (SELECT 1 FROM chat_messages WHERE customer_id = c.id AND store_id = $1)
            ORDER BY last_time DESC
        """, store_id)
        
    chat_list = [{"id": c['id'], "name": c['name'], "unread": c['unread_count'], "last_message": c['last_message'], "time": c['last_time'].strftime('%Y-%m-%d %I:%M %p') if c['last_time'] else ''} for c in chats]
    return web.json_response({"status": "success", "chats": chat_list})

async def api_manage_chat_history(request: web.Request):
    store_tid = request.query.get('store_id')
    customer_id = request.query.get('customer_id')
    
    # 🛡️ حماية من الروابط الفارغة
    if not store_tid or not customer_id or not str(store_tid).isdigit() or not str(customer_id).isdigit():
        return web.json_response({"status": "error", "message": "بيانات الرابط غير صحيحة"})
        
    store_tid, customer_id = int(store_tid), int(customer_id)
    
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, store_tid)
            
        # تحديث الرسائل غير المقروءة من الزبون إلى مقروءة
        await conn.execute("UPDATE chat_messages SET is_read = TRUE WHERE store_id = $1 AND customer_id = $2 AND sender_type = 'customer' AND is_read = FALSE", store_id, customer_id)
        
        messages = await conn.fetch("""
            SELECT sender_type, message_text, created_at 
            FROM chat_messages 
            WHERE store_id = $1 AND customer_id = $2 
            ORDER BY created_at ASC
        """, store_id, customer_id)
        
    msg_list = [{"sender": m['sender_type'], "text": m['message_text'], "time": m['created_at'].strftime('%Y-%m-%d %I:%M %p')} for m in messages]
    return web.json_response({"status": "success", "messages": msg_list})

async def api_manage_chat_send(request: web.Request):
    data = await request.json()
    store_tid = safe_int(data.get('store_id'))

    customer_id = safe_int(data.get('customer_id'))
    text = str(data.get('message', '')).strip() # 🛡️ حماية من الـ NoneType
    
    if not text or text == 'None': 
        return web.json_response({"status": "error", "message": "رسالة فارغة"})
    
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, store_tid)
            
        await conn.execute("""
            INSERT INTO chat_messages (store_id, customer_id, sender_type, message_text)
            VALUES ($1, $2, 'store', $3)
        """, store_id, customer_id, text)
        
    # إرسال إشعار للزبون مع التوجيه الذكي
    import asyncio
    from services.notifications import send_push_notification
    create_safe_task(send_push_notification(
        customer_id, 
        "💬 رسالة جديدة من البقالة", 
        text,
        {"action": "open_chat"} # 👈 التوجيه الذكي
    ))
        
    return web.json_response({"status": "success"})

# ==========================================
# 💬 نظام المحادثة مع الإدارة (الدعم الفني المباشر)
# ==========================================
async def api_manage_admin_chat_history(request: web.Request):
    store_tid = safe_int(request.query.get('store_id'))
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, store_tid)
            
        await conn.execute("UPDATE admin_store_chats SET is_read = TRUE WHERE store_id = $1 AND sender = 'admin'", store_id)
        messages = await conn.fetch("SELECT sender, message, created_at FROM admin_store_chats WHERE store_id = $1 ORDER BY created_at ASC", store_id)
        
    msg_list = [{"sender": m['sender'], "text": m['message'], "time": m['created_at'].strftime('%H:%M')} for m in messages]
    return web.json_response({"status": "success", "messages": msg_list})

async def api_manage_admin_chat_send(request: web.Request):
    data = await request.json()
    store_tid = safe_int(data.get('store_id'))

    message = str(data.get('message', '')).strip() # 🛡️ حماية
    if not message or message == 'None':
        return web.json_response({"status": "error", "message": "لا يمكن إرسال رسالة فارغة"}, status=400)
    
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, store_tid)
            
        await conn.execute("INSERT INTO admin_store_chats (store_id, sender, message) VALUES ($1, 'store', $2)", store_id, message)
        store_name = await conn.fetchval("SELECT name FROM stores WHERE id = $1", store_id)
        
    # 1. إرسال إشعار حي للمدير ليظهر في الـ Terminal
    from api.routes.websockets import notify_admin_new_log
    import asyncio
    create_safe_task(notify_admin_new_log({
        "action": "رسالة دعم فني 💬",
        "details": f"رسالة جديدة من {store_name}: {message}",
        "time": datetime.now(local_tz).strftime('%H:%M')
    }))

    # 2. 🚀 الجديد: إرسال إشعار (Push Notification) حقيقي لهاتف المدير!
    from services.notifications import send_push_notification
    from config import ADMIN_ID
    create_safe_task(send_push_notification(
        ADMIN_ID, 
        f"🎧 دعم فني: {store_name}", 
        message,
        {"action": "open_support"}, 
        "admin"
    ))
    
    return web.json_response({"status": "success"})

async def api_manage_product_add_stock(request: web.Request):
    """دالة تزويد المخزون لمنتج موجود"""
    data = await request.json()
    added_qty = safe_int(data.get('added_qty'))
    
    # 🛡️ التحقق من المدخلات: منع القيم السالبة أو الصفر
    if added_qty <= 0:
        return web.json_response({"status": "error", "message": "الكمية المضافة يجب أن تكون أكبر من صفر"})
        
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, safe_int(data.get('store_id'))
)

        await conn.execute("""
            UPDATE store_products 
            SET stock = stock + $1 
            WHERE id = $2 AND store_id = $3 AND stock IS NOT NULL
        """, added_qty, safe_int(data.get('id')), store_id)
        
    return web.json_response({"status": "success"})

# =====================================================================
# 🔄 نظام القيود العكسية (التراجع عن العمليات)
# =====================================================================
async def api_manage_transaction_revert(request: web.Request):
    data = await request.json()
    trans_id = safe_int(data.get('transaction_id')) # 🛡️ تحويل آمن
    store_tid = safe_int(data.get('store_id'))
    
    if not trans_id or not store_tid:
        return web.json_response({"status": "error", "message": "رقم العملية أو البقالة مفقود"})
    
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, store_tid)
        if not store_id:
            return web.json_response({"status": "error", "message": "البقالة غير موجودة"})
            
        async with conn.transaction():
            orig = await conn.fetchrow("SELECT * FROM transactions WHERE id = $1 AND store_id = $2", trans_id, store_id)
            if not orig:
                return web.json_response({"status": "error", "message": "العملية غير موجودة"})
                
            if orig['trans_type'] == 'reversal' or 'قيد عكسي' in orig['details']:
                return web.json_response({"status": "error", "message": "لا يمكن التراجع عن قيد عكسي!"})
                
            already_reverted = await conn.fetchval("SELECT id FROM transactions WHERE store_id = $1 AND details LIKE $2", store_id, f"%قيد عكسي للعملية #{trans_id}%")
            if already_reverted:
                return web.json_response({"status": "error", "message": "تم التراجع عن هذه العملية مسبقاً!"})
                
            rev_amount = -float(orig['amount'])
            rev_details = f"قيد عكسي للعملية #{trans_id} ({orig['details']})"
            
            await conn.execute("""
                INSERT INTO transactions (store_id, customer_id, order_id, trans_type, amount, details) 
                VALUES ($1, $2, $3, 'reversal', $4, $5)
            """, store_id, orig['customer_id'], orig['order_id'], rev_amount, rev_details)
            
            if orig['customer_id']:
                await conn.execute("UPDATE customers SET balance = balance + $1 WHERE id = $2", rev_amount, orig['customer_id'])
                
    return web.json_response({"status": "success", "message": "تم عمل قيد عكسي وتصحيح الرصيد بنجاح!"})

# ==========================================
# 📦 نظام الجرد الذكي بالكاميرا (Smart Inventory)
# ==========================================
async def api_manage_inventory_sync(request: web.Request):
    data = await request.json()
    barcode = str(data.get('barcode')).strip()
    actual_stock = safe_int(data.get('actual_stock', 0)) # 🛡️ تحويل آمن
    
    # 🛡️ منع الكميات السالبة
    if actual_stock < 0:
        return web.json_response({"status": "error", "message": "لا يمكن أن يكون المخزون بالسالب!"})
        
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, safe_int(data.get('store_id'))
)
        
        # البحث عن المنتج بالباركود (للحبة أو الكرتون) أو بآيدي المنتج
        product = await conn.fetchrow("""
            SELECT id, custom_name, stock FROM store_products 
            WHERE store_id = $1 AND (barcode = $2 OR carton_barcode = $2 OR id::text = $2)
            AND is_deleted = FALSE LIMIT 1
        """, store_id, barcode)
        
        if not product:
            return web.json_response({"status": "error", "message": "لم يتم العثور على المنتج، تأكد من الباركود."})
            
        old_stock = product['stock']
        # إذا لم يكن مجروداً مسبقاً نعتبره 0 لتسجيل الفرق
        diff = actual_stock - (old_stock if old_stock is not None else 0)
        
        async with conn.transaction():
            await conn.execute("UPDATE store_products SET stock = $1 WHERE id = $2", actual_stock, product['id'])
            await conn.execute("""
                INSERT INTO inventory_checks (store_id, product_id, old_stock, new_stock, difference)
                VALUES ($1, $2, $3, $4, $5)
            """, store_id, product['id'], old_stock, actual_stock, diff)
            
    return web.json_response({
        "status": "success", 
        "product_name": product['custom_name'],
        "old_stock": old_stock if old_stock is not None else 'غير محدد',
        "new_stock": actual_stock,
        "diff": diff
    })

# ==========================================
# 💸 نظام المصروفات (لحساب صافي الربح)
# ==========================================
async def api_manage_expense_add(request: web.Request):
    data = await request.json()
    amount = safe_decimal(data.get('amount')) # 🛡️ تحويل آمن
    category = data.get('category', 'عام')
    details = data.get('details', '')
    
    if amount <= 0:
        return web.json_response({"status": "error", "message": "المبلغ غير صحيح"})
        
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, safe_int(data.get('store_id'))
)
        await conn.execute("""
            INSERT INTO expenses (store_id, amount, category, details)
            VALUES ($1, $2, $3, $4)
        """, store_id, amount, category, details)
        
    return web.json_response({"status": "success", "message": "تم تسجيل المصروف بنجاح"})

# ==========================================
# 🌉 جسر المصافحة مع الشهاب برو (B2B Handshake)
# ==========================================
async def api_manage_link_alshehab(request: web.Request):
    from api.middlewares import get_user_from_token
    user = get_user_from_token(request)
    if 'store_id' not in user:
        return web.json_response({"status": "error", "message": "غير مصرح"})
        
    data = await request.json()
    alshehab_phone = data.get('alshehab_phone')
    alshehab_password = data.get('alshehab_password')
    
    if not alshehab_phone or not alshehab_password:
        return web.json_response({"status": "error", "message": "البيانات ناقصة"})
        
    import os
    import aiohttp
    
    ALSHEHAB_URL = os.getenv("ALSHEHAB_URL", "https://my-bot-ehio.onrender.com" ) 
    B2B_SECRET_KEY = os.getenv("B2B_SECRET_KEY", "SuperSecretDukkaniBridge2026")
    
    try:
        async with aiohttp.ClientSession( ) as session:
            async with session.post(
                f"{ALSHEHAB_URL}/api/b2b/verify_store",
                json={"phone": str(alshehab_phone), "password": str(alshehab_password)},
                headers={"Authorization": f"Bearer {B2B_SECRET_KEY}"},
                timeout=15
            ) as resp:
                result = await resp.json()
                
                if result.get('status') != 'success':
                    return web.json_response({"status": "error", "message": result.get('message', 'فشل الربط')})
                    
    except Exception as e:
        import logging
        logging.error(f"Alshehab Handshake Error: {e}")
        return web.json_response({"status": "error", "message": "فشل الاتصال بسيرفر الشهاب برو."})
        
    async with database.pool.acquire() as conn:
        await conn.execute("""
            UPDATE stores 
            SET alshehab_phone = $1, alshehab_password = $2 
            WHERE id = $3
        """, str(alshehab_phone), str(alshehab_password), user['store_id'])
        
    return web.json_response({
        "status": "success", 
        "message": "تم ربط بقالتك بنجاح!",
        "sso_token": result.get('sso_token') # 👈 جلب التوكن السري وإرساله للواجهة
    })

# 👇 الدالة الجديدة: توليد الرابط الآمن عند كل فتح للشهاب برو 👇
async def api_manage_alshehab_sso(request: web.Request):
    from api.middlewares import get_user_from_token
    user = get_user_from_token(request)
    
    async with database.pool.acquire() as conn:
        store = await conn.fetchrow("SELECT alshehab_phone, alshehab_password FROM stores WHERE id = $1", user['store_id'])
        
    if not store or not store['alshehab_phone'] or not store['alshehab_password']:
        return web.json_response({"status": "not_linked"})
        
    import os, aiohttp
    ALSHEHAB_URL = os.getenv("ALSHEHAB_URL", "https://my-bot-ehio.onrender.com" ) 
    B2B_SECRET_KEY = os.getenv("B2B_SECRET_KEY", "SuperSecretDukkaniBridge2026")
    
    try:
        async with aiohttp.ClientSession( ) as session:
            async with session.post(
                f"{ALSHEHAB_URL}/api/b2b/verify_store",
                json={"phone": store['alshehab_phone'], "password": store['alshehab_password']},
                headers={"Authorization": f"Bearer {B2B_SECRET_KEY}"},
                timeout=15
            ) as resp:
                result = await resp.json()
                if result.get('status') == 'success':
                    sso_token = result.get('sso_token')
                    return web.json_response({"status": "success", "sso_url": f"{ALSHEHAB_URL}/?sso_token={sso_token}"})
                else:
                    return web.json_response({"status": "error", "message": "بيانات الربط لم تعد صالحة. يرجى الربط مجدداً."})
    except Exception as e:
        return web.json_response({"status": "error", "message": "فشل الاتصال بسيرفر الشهاب."})

# ==========================================
# 🚪 نظام فتح وإغلاق البقالة (الدالة المفقودة)
# ==========================================
async def api_manage_store_status(request: web.Request):
    data = await request.json()
    store_tid = safe_int(data.get('store_id'))
    is_open = bool(data.get('is_open', True))
    
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, store_tid)
        if not store_id:
            return web.json_response({"status": "error", "message": "البقالة غير موجودة"})
            
        status_val = 'active' if is_open else 'closed'
        await conn.execute("UPDATE stores SET status = $1 WHERE id = $2", status_val, store_id)
        
    return web.json_response({"status": "success"})

def setup_manage_routes(app: web.Application):
    """تسجيل مسارات الإدارة في التطبيق"""
    app.router.add_post('/api/manage/inventory/sync', api_manage_inventory_sync) # 👈 مسار الجرد الجديد
    app.router.add_post('/api/manage/expense/add', api_manage_expense_add) # 👈 مسار المصروفات الجديد
    
    app.router.add_post('/api/manage/promo/fresh', api_manage_promo_fresh)
    app.router.add_post('/api/manage/promo/launch_fresh', api_manage_promo_launch_fresh)
    app.router.add_post('/api/manage/branding/update', api_manage_branding_update)
    app.router.add_get('/api/manage/data', api_manage_data)
    
    app.router.add_get('/api/manage/products/page', api_manage_products_page)
    app.router.add_get('/api/manage/customers/page', api_manage_customers_page)
    
    app.router.add_post('/api/manage/product/add', api_manage_product_add)
    app.router.add_post('/api/manage/product/update', api_manage_product_update)
    app.router.add_post('/api/manage/product/add_stock', api_manage_product_add_stock)
    app.router.add_post('/api/manage/product/upload_image', api_manage_product_upload_image)
    app.router.add_post('/api/manage/product/delete', api_manage_product_delete)
    
    app.router.add_post('/api/manage/customer/add', api_manage_customer_add)
    app.router.add_post('/api/manage/customer/update', api_manage_customer_update)
    app.router.add_post('/api/manage/customer/delete', api_manage_customer_delete)
    app.router.add_post('/api/manage/customer/transfer', api_manage_customer_transfer)
    app.router.add_post('/api/manage/worker/add', api_manage_worker_add)
    app.router.add_post('/api/manage/worker/update', api_manage_worker_update)
    app.router.add_post('/api/manage/worker/delete', api_manage_worker_delete)
    app.router.add_post('/api/manage/order/action', api_manage_order_action)
    
    # مسارات المناديب
    app.router.add_post('/api/manage/agent/add', api_manage_agent_add)
    app.router.add_post('/api/manage/agent/update', api_manage_agent_update)
    app.router.add_post('/api/manage/agent/delete', api_manage_agent_delete)
    # مسارات الرادار والتصفيات
    app.router.add_get('/api/manage/dead_stock', api_manage_dead_stock)
    app.router.add_post('/api/manage/clearance', api_manage_clearance)
    
    # مسار سجل التلاعبات
    app.router.add_get('/api/manage/fraud_log', api_manage_fraud_log)
    app.router.add_get('/api/manage/catalog/search', api_manage_catalog_search)
    app.router.add_post('/api/manage/customer/pay', api_manage_customer_pay)
    app.router.add_get('/api/manage/customer/statement', api_manage_customer_statement)
    app.router.add_get('/api/manage/reports', api_manage_reports)
    app.router.add_post('/api/manage/send_promo', api_manage_send_promo)
    app.router.add_post('/api/manage/auto_fill', api_manage_auto_fill)
    app.router.add_post('/api/manage/change_passcode', api_manage_change_passcode)
    app.router.add_post('/api/manage/customer/reset_pass', api_manage_customer_reset_pass)
    app.router.add_post('/api/manage/customer/send_statement', api_manage_customer_send_statement)
    app.router.add_get('/api/agent/data', api_agent_data)
    app.router.add_get('/api/manage/chat/list', api_manage_chat_list)
    app.router.add_get('/api/manage/chat/history', api_manage_chat_history)
    app.router.add_post('/api/manage/chat/send', api_manage_chat_send)
    app.router.add_get('/api/manage/admin_chat/history', api_manage_admin_chat_history)
    app.router.add_post('/api/manage/admin_chat/send', api_manage_admin_chat_send)
    app.router.add_post('/api/manage/transaction/revert', api_manage_transaction_revert)

    # 🌉 مسارات دمج الشهاب برو
    app.router.add_post('/api/manage/link_alshehab', api_manage_link_alshehab)    
    app.router.add_get('/api/manage/alshehab_sso', api_manage_alshehab_sso)
    
    app.router.add_post('/api/manage/store/status', api_manage_store_status) # 🚪 مسار الإغلاق الفعلي
