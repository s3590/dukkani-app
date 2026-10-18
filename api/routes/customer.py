from aiohttp import web
import jwt
import bcrypt
import time # 👈 استيراد مكتبة الوقت
from datetime import datetime, timedelta
import database
from config import JWT_SECRET
from bot.setup import bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import asyncio
from services.notifications import send_push_notification
from services.accounting import process_new_order
from api.routes.websockets import notify_store_new_order

from api.middlewares import get_user_from_token
import json
from config import ADMIN_ID, ADMIN_PASSWORD

# 👇 قاموس لحفظ وقت آخر طلب لكل زبون لمنع التكرار 👇
CUSTOMER_ORDER_COOLDOWN = {}

async def api_customer_login(request: web.Request):
    data = await request.json()
    login_id = data.get('phone') 
    password = data.get('password') 

    # 👇 استخراج الـ IP ونوع الجهاز 👇
    client_ip = request.headers.get('X-Forwarded-For', request.remote)
    if client_ip: client_ip = client_ip.split(',')[0].strip()
    user_agent = request.headers.get('User-Agent', '')
    
    device_name = "جهاز غير معروف"
    if "iPhone" in user_agent: device_name = "آيفون (iPhone)"
    elif "Android" in user_agent: device_name = "أندرويد (Android)"
    elif "Windows" in user_agent: device_name = "كمبيوتر (Windows)"
    elif "Mac" in user_agent: device_name = "ماك (Mac)"

    # 1. فحص المدير العام 👑
    if login_id == str(ADMIN_ID) and password == ADMIN_PASSWORD:
        # إرسال تنبيه أمني للمدير
        try:
            await bot.send_message(ADMIN_ID, f"🚨 **تنبيه أمني:**\nتم تسجيل دخول للقيادة العليا للتو.\n📱 الجهاز: {device_name}\n🌐 الآيبي: {client_ip}\n\nإذا لم تكن أنت، قم بتغيير كلمة المرور فوراً!")
        except: pass
        return web.json_response({"status": "success", "role": "admin"})
        
    from datetime import timezone  # 👈 استيراد timezone هنا لضمان عملها
        
    async with database.pool.acquire() as conn:
        # 2. فحص أصحاب البقالات والعمال 🏪
        worker = await conn.fetchrow("""
            SELECT w.id, w.store_id, w.permissions, w.name 
            FROM workers w 
            JOIN stores s ON w.store_id = s.id 
            WHERE w.passcode = $1 AND (w.telegram_id::text = $2 OR s.telegram_id::text = $2 OR s.phone = $2)
        """, password, login_id)
        
        if worker:                
            perms = json.loads(worker['permissions']) if worker['permissions'] else ["pos"]
            
            # إرسال تنبيه لصاحب البقالة بدخول العامل
            store_tid = await conn.fetchval("SELECT telegram_id FROM stores WHERE id = $1", worker['store_id'])
            if store_tid:
                try:
                    await bot.send_message(store_tid, f"🔐 **تنبيه تسجيل دخول:**\nقام ({worker['name']}) بتسجيل الدخول للنظام.\n📱 الجهاز: {device_name}\n🌐 الآيبي: {client_ip}")
                except: pass

            # 👇 توليد توكن للعامل (تم التعديل لـ timezone.utc) 👇
            token = jwt.encode({
                'role': 'staff',
                'store_id': worker['store_id'],
                'worker_id': worker['id'],
                'admin_id': login_id, 
                'exp': datetime.now(timezone.utc) + timedelta(days=90)
            }, JWT_SECRET, algorithm='HS256')

            return web.json_response({
                "status": "success", 
                "role": "staff",
                "store_id": worker['store_id'],
                "worker_id": worker['id'],
                "permissions": perms,
                "token": token # 👈 إرسال التوكن للواجهة
            })

        # 2.5 فحص المناديب 🛵
        agent = await conn.fetchrow("""
            SELECT a.id, a.store_id, s.name as store_name, s.theme_color, s.logo_url
            FROM delivery_agents a
            JOIN stores s ON a.store_id = s.id
            WHERE a.passcode = $1 AND (a.phone = $2 OR s.telegram_id::text = $2 OR s.phone = $2) AND a.is_active = TRUE AND a.is_deleted = FALSE
        """, password, login_id)
        
        if agent:
            # 👇 تم التعديل لـ timezone.utc 👇
            token = jwt.encode({'agent_id': agent['id'], 'store_id': agent['store_id'], 'exp': datetime.now(timezone.utc) + timedelta(days=90)}, JWT_SECRET, algorithm='HS256')
            return web.json_response({
                "status": "success",
                "role": "agent",
                "store_id": agent['store_id'],
                "agent_id": agent['id'],
                "token": token,
                "store_name": agent['store_name'],
                "theme_color": agent.get('theme_color') or '#0ba360',
                "logo_url": agent.get('logo_url') or '/static/logo_customer.png'
            })
            
        # 3. فحص الزبائن 🛒
        customers = await conn.fetch("""
            SELECT c.id, c.store_id, c.name, c.password, c.status, c.parent_id,
                   s.name as store_name, s.theme_color, s.logo_url
            FROM customers c
            JOIN stores s ON c.store_id = s.id
            WHERE (c.phone = $1 OR c.parent_id IN (SELECT id FROM customers WHERE phone = $1 AND parent_id IS NULL)) 
              AND c.is_deleted = FALSE
        """, login_id)
        
        valid_customers = []
        for c in customers:
            # 🚀 التحقق الآمن في الخلفية لمنع انهيار السيرفر لو كانت كلمة المرور تالفة
            try:
                is_valid = await asyncio.to_thread(bcrypt.checkpw, password.encode('utf-8'), c['password'].encode('utf-8'))
            except Exception:
                is_valid = False
                
            if is_valid:
                if c['status'] in ['pending', 'suspended']:
                    continue 
                    
                # 👇 تم التعديل لـ timezone.utc 👇
                token = jwt.encode({'customer_id': c['id'], 'store_id': c['store_id'], 'exp': datetime.now(timezone.utc) + timedelta(days=90)}, JWT_SECRET, algorithm='HS256')
                valid_customers.append({
                    "store_id": c['store_id'],
                    "store_name": c['store_name'],
                    "name": c['name'],
                    "theme_color": c.get('theme_color') or '#0ba360',
                    "logo_url": c.get('logo_url') or '/static/logo_customer.png',
                    "token": token
                })
        
        if valid_customers:
            return web.json_response({
                "status": "success",
                "role": "customer",
                "stores": valid_customers
            })
            
    return web.json_response({"status": "error", "message": "بيانات الدخول غير صحيحة"})

async def api_customer_register(request: web.Request):
    """دالة التسجيل الذاتي للزبون (مع دعم نظام العائلة)"""
    data = await request.json()
    phone = data.get('phone')
    name = data.get('name')
    password = data.get('password')
    family_invite = data.get('family_invite') # 👈 جلب كود دعوة العائلة
    
    parent_id = None
    store_id = data.get('store_id')
    
    # 👇 فك تشفير رابط العائلة إن وجد 👇
    if family_invite:
        try:
            decoded = jwt.decode(family_invite, JWT_SECRET, algorithms=['HS256'])
            parent_id = decoded.get('parent_id')
            store_id = decoded.get('store_id') # نأخذ البقالة من رابط الأب إجبارياً
        except:
            return web.json_response({"status": "error", "message": "رابط الدعوة غير صالح أو منتهي."})
            
    phone = str(phone or '').strip()
    name = str(name or '').strip()
    password = str(password or '').strip()

    if not all([store_id, phone, name, password]) or len(password) < 4:
        return web.json_response({"status": "error", "message": "يرجى إدخال جميع الحقول مع كلمة مرور لا تقل عن 4 خانات."})
        
    # 🚀 التشفير في الخلفية 🚀
    hashed_bytes = await asyncio.to_thread(bcrypt.hashpw, password.encode('utf-8'), bcrypt.gensalt())
    hashed_pw = hashed_bytes.decode('utf-8')
    
    try:
        if not store_id or not str(store_id).isdigit():
            return web.json_response({"status": "error", "message": "معرف البقالة غير صالح."})
            
        store_id_int = int(store_id) 
        
        async with database.pool.acquire() as conn:
            exists = await conn.fetchval("SELECT id FROM customers WHERE phone = $1 AND store_id = $2", phone, store_id_int)
            if exists:
                return web.json_response({"status": "error", "message": "لديك حساب مسبق في هذه البقالة. قم بتسجيل الدخول."})
                
            # 👇 إدراج الزبون 👇
            status = 'active' if parent_id else 'pending'
            cust_id = await conn.fetchval("""
                INSERT INTO customers (store_id, phone, name, password, status, parent_id) 
                VALUES ($1, $2, $3, $4, $5, $6) RETURNING id
            """, store_id_int, phone, name, hashed_pw, status, parent_id)
            
            # إذا كان حساب عائلة، لا نرسل إشعاراً للبقال للموافقة
            if parent_id:
                return web.json_response({"status": "success", "message": "تم انضمامك للعائلة بنجاح! يمكنك تسجيل الدخول الآن."})
            
            # إرسال إشعار للبقال عبر الرادار (WebSocket)
            from api.routes.websockets import notify_store_new_order
            import asyncio
            asyncio.create_task(notify_store_new_order(store_id_int, {
                "type": "new_customer",
                "customer_id": cust_id,
                "customer_name": name,
                "phone": phone
            }))
            
            # 👇 إرسال رسالة للبقال عبر تيليجرام 👇
            store_tid = await conn.fetchval("SELECT telegram_id FROM stores WHERE id = $1", store_id_int)
            if store_tid:
                from bot.setup import bot
                from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
                
                msg_text = f"👤 **طلب تسجيل زبون جديد**\n\nالاسم: {name}\nالهاتف: {phone}\n\nهل توافق على انضمامه لبقالتك؟"
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ قبول الزبون", callback_data=f"approve_cust_{cust_id}")],
                    [InlineKeyboardButton(text="❌ رفض وحذف", callback_data=f"reject_cust_{cust_id}")]
                ])
                try:
                    await bot.send_message(store_tid, msg_text, reply_markup=kb)
                except Exception as e:
                    import logging
                    logging.error(f"فشل إرسال إشعار تسجيل الزبون للبقالة: {e}")
            
        return web.json_response({"status": "success", "message": "تم إرسال طلب التسجيل للبقالة بنجاح. يرجى الانتظار حتى يتم تفعيل حسابك."})
    except Exception as e:
        import logging
        logging.error(f"Register Error: {e}")
        # إظهار الخطأ الدقيق على الشاشة لتسهيل حله لو تكرر
        return web.json_response({"status": "error", "message": f"حدث خطأ: {str(e)}"})

async def api_customer_statement(request: web.Request):
    user = get_user_from_token(request)
    if 'customer_id' not in user:
        return web.json_response({"status": "error", "message": "يرجى تسجيل الخروج والدخول مجدداً."})

    async with database.pool.acquire() as conn:
        # جلب بيانات الزبون لمعرفة ما إذا كان حساباً فرعياً
        cust = await conn.fetchrow("SELECT balance, parent_id FROM customers WHERE id = $1", user['customer_id'])
        
        # حماية: التأكد من أن الزبون موجود في قاعدة البيانات
        if not cust:
            return web.json_response({"status": "error", "message": "حساب الزبون غير موجود أو تم حذفه. يرجى تسجيل الدخول مجدداً."})
            
        # إذا كان حساباً فرعياً، نجلب رصيد وعمليات الأب
        target_id = cust['parent_id'] if cust['parent_id'] else user['customer_id']
        
        target_cust = await conn.fetchrow("SELECT COALESCE(balance, 0.0) as balance FROM customers WHERE id = $1", target_id)
        # 🛡️ جلب حالة الطلب بأمان عبر الربط مع جدول orders لتفادي انهيار قاعدة البيانات
        trans = await conn.fetch("""
            SELECT t.amount, t.details, t.created_at, COALESCE(o.status, 'completed') as status 
            FROM transactions t 
            LEFT JOIN orders o ON t.order_id = o.id 
            WHERE t.customer_id = $1 
            ORDER BY t.created_at DESC LIMIT 50
        """, target_id)
        
        # 👇 جلب اسم الأب (مهم جداً لميزة المحادثة العائلية) 👇
        parent_name = ""
        if cust['parent_id']:
            parent_name = await conn.fetchval("SELECT name FROM customers WHERE id = $1", cust['parent_id'])
            
        # 👇 جلب بيانات البقالة (تعديلك الممتاز) في نفس الاتصال لتوفير الموارد 👇
        store_info = await conn.fetchrow("SELECT theme_color, logo_url FROM stores WHERE id = $1", user['store_id'])
    
    transactions = [{
        "amount": float(t['amount']),
        "details": t['details'],
        "created_at": t['created_at'].strftime('%Y-%m-%d %H:%M'),
        "status": t['status']
    } for t in trans]
        
    return web.json_response({
        "status": "success", 
        "balance": float(target_cust['balance']), 
        "transactions": transactions,
        "is_subaccount": bool(cust['parent_id']),
        "parent_name": parent_name, # 👈 تمت إعادته لكي لا يتعطل زر المحادثة
        "theme_color": store_info['theme_color'] if store_info else None,
        "logo_url": store_info['logo_url'] if store_info else None
    })

async def api_customer_products(request: web.Request):
    user = get_user_from_token(request)

    if 'store_id' not in user:
        return web.json_response({"status": "error", "message": "يرجى تسجيل الخروج والدخول مجدداً لتحديث البيانات."})
        
    async with database.pool.acquire() as conn:
        # 👇 التعديل هنا: إضافة شرط العزل التام (إخفاء قسم الطازج إذا انتهى وقته) 👇
        products = await conn.fetch("""
            SELECT sp.id, sp.custom_name as name, sp.price, sp.category, sp.expires_at, sp.offer_text,
                   COALESCE(sp.telegram_file_id, gp.image_url) as telegram_file_id 
            FROM store_products sp 
            LEFT JOIN global_products gp ON sp.product_id = gp.id 
            WHERE sp.store_id = $1 AND sp.is_available=TRUE AND sp.is_deleted = FALSE
            AND (sp.category != 'طازج' OR (sp.category = 'طازج' AND sp.expires_at > NOW()))
        """, user['store_id'])
    
    prod_list = []
    
    # ⏱️ جلب توقيت مطابق لتوقيت قاعدة البيانات (Aden)
    try:
        import zoneinfo
        local_tz = zoneinfo.ZoneInfo('Asia/Aden')
    except ImportError:
        import pytz
        local_tz = pytz.timezone('Asia/Aden')
        
    current_time = datetime.now(local_tz).replace(tzinfo=None)
    
    for p in products:
        is_fresh = False
        expires_str = None
        if p['expires_at']:
            if p['expires_at'] > current_time:
                is_fresh = True
                expires_str = p['expires_at'].isoformat() + 'Z'
                
        prod_list.append({
            "id": p['id'],
            "name": p['name'],
            "price": float(p['price']),
            "category": p['category'] or 'عام',
            "image": p['telegram_file_id'],
            "is_fresh": is_fresh,
            "expires_at": expires_str,
            "offer_text": p['offer_text'] # 👈 إرسال العرض للواجهة
        })
        
    return web.json_response({"status": "success", "products": prod_list})

async def api_customer_order(request: web.Request):
    user = get_user_from_token(request)
    if 'customer_id' not in user:
        return web.json_response({"status": "error", "message": "غير مصرح لك"})
        
    customer_id = user['customer_id']
    import time
    current_time = time.time()
    
    # 👇 حماية من الطلبات المكررة وتسريب الذاكرة 👇
    for k in list(CUSTOMER_ORDER_COOLDOWN.keys()):
        if current_time - CUSTOMER_ORDER_COOLDOWN[k] > 60:
            del CUSTOMER_ORDER_COOLDOWN[k]

    if customer_id in CUSTOMER_ORDER_COOLDOWN:
        if current_time - CUSTOMER_ORDER_COOLDOWN[customer_id] < 10:
            return web.json_response({"status": "error", "message": "الرجاء الانتظار قليلاً قبل إرسال طلب آخر (حماية من التكرار)."})
            
    CUSTOMER_ORDER_COOLDOWN[customer_id] = current_time
    # 👆 نهاية كود الحماية 👆

    data = await request.json()
    items = data.get('items', {})
    delivery_address = data.get('delivery_address', '').strip()
    
    if not items: return web.json_response({"status": "error", "message": "السلة فارغة"})
    
    import asyncio
    from services.accounting import process_new_order
    from services.notifications import send_push_notification
    from bot.setup import bot
    from config import ADMIN_ID # 👈 استيراد آيدي المدير

    async with database.pool.acquire() as conn:
        cust = await conn.fetchrow("SELECT name, balance, credit_limit, parent_id FROM customers WHERE id = $1", user['customer_id'])
        
        real_total = 0.0
        for pid_str, item in items.items():
            qty = int(item.get('qty', 1))
            
            if qty <= 0:
                return web.json_response({"status": "error", "message": "الكمية يجب أن تكون أكبر من الصفر."})
                
            db_price = await conn.fetchval("SELECT price FROM store_products WHERE id = $1 AND store_id = $2", int(pid_str), user['store_id'])
            if db_price: 
                real_total += float(db_price) * qty
                
        if real_total <= 0: return web.json_response({"status": "error", "message": "خطأ في المنتجات."})

        # 👨‍👩‍👧‍👦 حساب فرعي (زوجة/ابن)
        if cust['parent_id']:
            father = await conn.fetchrow("SELECT id, name, balance, credit_limit, family_direct_order FROM customers WHERE id = $1", cust['parent_id'])
            
            if father['family_direct_order']:
                if (float(father['balance']) + real_total > float(father['credit_limit'])):
                    return web.json_response({"status": "error", "message": f"الطلب يتجاوز سقف الدين المسموح لرب الأسرة."})
                    
                try:
                    order_id, _ = await process_new_order(user['store_id'], father['id'], items, 'credit')
                    items_text = "\n".join([f"- {item.get('qty', 1)}x {item.get('name', '')}" for item in items.values()])
                    msg_body = f"قام ({cust['name']}) بطلب مباشر بقيمة {real_total} ريال.\nالتفاصيل:\n{items_text}"
                    asyncio.create_task(send_push_notification(father['id'], "🛒 طلب عائلي مباشر", msg_body))
                    
                    return web.json_response({"status": "success", "message": "تم إرسال الطلب للبقالة بنجاح ✅"})
                except Exception as e:
                    return web.json_response({"status": "error", "message": str(e)})
            else:
                order_id = await conn.fetchval("""
                    INSERT INTO orders (store_id, customer_id, status, payment_method, total_amount)
                    VALUES ($1, $2, 'family_review', 'credit', $3) RETURNING id
                """, user['store_id'], user['customer_id'], real_total)
                
                for pid_str, item in items.items():
                    qty = int(item.get('qty', 1))
                    p_info = await conn.fetchrow("SELECT price, stock, custom_name FROM store_products WHERE id = $1 AND store_id = $2", int(pid_str), user['store_id'])
                    if not p_info:
                        return web.json_response({"status": "error", "message": "أحد المنتجات غير موجود في البقالة."})
                    if p_info['stock'] is not None and p_info['stock'] < qty:
                        return web.json_response({"status": "error", "message": f"عذراً، الكمية المطلوبة من ({p_info['custom_name']}) غير متوفرة."})
                        
                    await conn.execute("INSERT INTO order_items (order_id, product_id, quantity, price_at_time) VALUES ($1, $2, $3, $4)", order_id, int(pid_str), qty, p_info['price'])
                    await conn.execute("UPDATE store_products SET stock = stock - $1 WHERE id = $2 AND stock IS NOT NULL", qty, int(pid_str))
                    
                asyncio.create_task(send_push_notification(cust['parent_id'], "👨‍👩‍👧‍👦 طلب عائلي للمراجعة", f"أرسل {cust['name']} طلباً جديداً بـ {real_total} ريال ينتظر موافقتك."))
                return web.json_response({"status": "success", "message": "تم إرسال الطلب لرب الأسرة للمراجعة 👨‍👩‍👧‍👦"})
            
        # 👨 حساب رئيسي
        else:
            payment_method = data.get('payment_method', 'credit')
            
            if payment_method == 'credit' and (float(cust['balance']) + real_total > float(cust['credit_limit'])):
                return web.json_response({"status": "error", "message": f"الطلب يتجاوز سقف الدين المسموح لك. السقف: {cust['credit_limit']} ريال."})
                
            try:
                order_id, _ = await process_new_order(user['store_id'], user['customer_id'], items, payment_method, 0.0, delivery_address)
                
                store_tid = await conn.fetchval("SELECT telegram_id FROM stores WHERE id = $1", user['store_id'])
                workers = await conn.fetch("SELECT id, telegram_id FROM workers WHERE store_id = $1", user['store_id'])
                
                notify_list = set()
                if store_tid: notify_list.add(store_tid)
                for w in workers:
                    if w['telegram_id']: notify_list.add(w['telegram_id'])
                
                # 🛡️ الحماية الصارمة: إزالة آيدي المدير العام من القائمة إن وجد
                if ADMIN_ID in notify_list:
                    notify_list.discard(ADMIN_ID)
                
                pay_text = "آجل (دين)" if payment_method == 'credit' else "كاش"
                msg = f"🔔 **طلب جديد رقم #{order_id}**\n👤 الزبون: {cust['name']}\n💰 الإجمالي: {real_total} ريال\n💳 الدفع: {pay_text}\n\n👉 افتح النظام لمعالجة الطلب!"
                
                # إرسال رسائل تيليجرام للبقالة والعمال فقط
                for tid in notify_list:
                    asyncio.create_task(bot.send_message(tid, msg))

                # إرسال إشعار منبثق (Push Notification)
                asyncio.create_task(send_push_notification(user['store_id'], f"🛒 طلب جديد #{order_id}", f"الإجمالي: {real_total} ريال", {"action": "open_orders"}, "store"))
                for w in workers:
                    asyncio.create_task(send_push_notification(w['id'], f"🛒 طلب جديد #{order_id}", f"الإجمالي: {real_total} ريال", {"action": "open_orders"}, "worker"))

                return web.json_response({"status": "success", "message": "تم إرسال الطلب للبقالة بنجاح ✅"})
                
            except Exception as e:
                return web.json_response({"status": "error", "message": str(e)})

# ================= 👨‍👩‍👧‍👦 دوال حساب العائلة =================
async def api_customer_family_invite(request: web.Request):
    """توليد رمز دعوة عائلي مشفر ومؤمن لمدة 7 أيام"""
    user = get_user_from_token(request)
    from datetime import timezone
    invite_token = jwt.encode({
        'parent_id': user['customer_id'],
        'store_id': user['store_id'],
        'exp': datetime.now(timezone.utc) + timedelta(days=7)
    }, JWT_SECRET, algorithm='HS256')
    return web.json_response({"status": "success", "invite_code": invite_token})

async def api_customer_family_add(request: web.Request):
    """إضافة فرد للعائلة مباشرة باسم مستعار ورمز سري (مصححة ومحمية)"""
    import random
    user = get_user_from_token(request)
    data = await request.json()
    name = str(data.get('name', '')).strip()
    pin = str(data.get('pin', '')).strip()
    
    if not name or len(pin) < 4:
        return web.json_response({"status": "error", "message": "يرجى إدخال الاسم ورمز سري من 4 أرقام على الأقل."})
        
    # 🚀 التشفير الصحيح للرمز السري pin في الخلفية
    hashed_bytes = await asyncio.to_thread(bcrypt.hashpw, pin.encode('utf-8'), bcrypt.gensalt())
    hashed_pin = hashed_bytes.decode('utf-8')

    async with database.pool.acquire() as conn:
        father = await conn.fetchrow("SELECT phone FROM customers WHERE id = $1", user['customer_id'])
        if not father:
            return web.json_response({"status": "error", "message": "حساب الأب غير موجود."})
            
        pseudo_phone = f"{father['phone']}#{random.randint(10000, 99999)}"
        
        await conn.execute("""
            INSERT INTO customers (store_id, parent_id, phone, name, password, status)
            VALUES ($1, $2, $3, $4, $5, 'active')
        """, user['store_id'], user['customer_id'], pseudo_phone, name, hashed_pin)
        
    return web.json_response({"status": "success", "message": f"تم إضافة ({name}) بنجاح! يمكنه الدخول الآن برقم هاتفك والرمز السري: {pin}"})

async def api_customer_family_settings(request: web.Request):
    """تحديث إعدادات الطلب المباشر للعائلة"""
    user = get_user_from_token(request)
    data = await request.json()
    direct_order = bool(data.get('direct_order', False))
    
    async with database.pool.acquire() as conn:
        await conn.execute("UPDATE customers SET family_direct_order = $1 WHERE id = $2", direct_order, user['customer_id'])
    return web.json_response({"status": "success", "message": "تم تحديث إعدادات العائلة بنجاح"})

async def api_customer_family_members(request: web.Request):
    """جلب قائمة أفراد العائلة"""
    user = get_user_from_token(request)
    async with database.pool.acquire() as conn:
        members = await conn.fetch("SELECT id, name FROM customers WHERE parent_id = $1 AND is_deleted = FALSE", user['customer_id'])
    return web.json_response({"status": "success", "members": [{"id": m['id'], "name": m['name']} for m in members]})

async def api_customer_family_member_update(request: web.Request):
    """تعديل الرمز السري لفرد من العائلة (مصححة)"""
    user = get_user_from_token(request)
    data = await request.json()
    member_id = int(data.get('member_id', 0))
    new_pin = str(data.get('new_pin', '')).strip()
    
    if len(new_pin) < 4:
        return web.json_response({"status": "error", "message": "الرمز السري يجب أن يكون 4 أرقام على الأقل"})
        
    # 🚀 تشفير new_pin الصحيح
    hashed_bytes = await asyncio.to_thread(bcrypt.hashpw, new_pin.encode('utf-8'), bcrypt.gensalt())
    hashed_pin = hashed_bytes.decode('utf-8')
    
    async with database.pool.acquire() as conn:
        updated = await conn.execute("UPDATE customers SET password = $1 WHERE id = $2 AND parent_id = $3", hashed_pin, member_id, user['customer_id'])
        if updated == "UPDATE 0":
            return web.json_response({"status": "error", "message": "غير مصرح لك بتعديل هذا الحساب"})
            
    return web.json_response({"status": "success", "message": "تم تحديث الرمز السري بنجاح"})

async def api_customer_family_member_delete(request: web.Request):
    """حذف فرد من العائلة"""
    user = get_user_from_token(request)
    data = await request.json()
    member_id = int(data['member_id'])
    
    async with database.pool.acquire() as conn:
        updated = await conn.execute("UPDATE customers SET is_deleted = TRUE WHERE id = $1 AND parent_id = $2", member_id, user['customer_id'])
        if updated == "UPDATE 0":
            return web.json_response({"status": "error", "message": "غير مصرح لك بحذف هذا الحساب"})
            
    return web.json_response({"status": "success", "message": "تم حذف الحساب بنجاح"})

async def api_customer_family_orders(request: web.Request):
    user = get_user_from_token(request)
    async with database.pool.acquire() as conn:
        orders = await conn.fetch("""
            SELECT o.id, o.total_amount, c.name as member_name
            FROM orders o JOIN customers c ON o.customer_id = c.id
            WHERE c.parent_id = $1 AND o.status = 'family_review'
        """, user['customer_id'])
        
        orders_data = []
        for o in orders:
            items = await conn.fetch("""
                SELECT oi.product_id, oi.quantity, oi.price_at_time, sp.custom_name as name
                FROM order_items oi JOIN store_products sp ON oi.product_id = sp.id
                WHERE oi.order_id = $1
            """, o['id'])
            orders_data.append({
                "id": o['id'], "member_name": o['member_name'], "total": float(o['total_amount']),
                "items": [{"id": i['product_id'], "qty": i['quantity'], "price": float(i['price_at_time']), "name": i['name']} for i in items]
            })
    return web.json_response({"status": "success", "orders": orders_data})

async def api_customer_family_action(request: web.Request):
    """
    معالجة قرار رب الأسرة بخصوص طلبات العائلة (اعتماد أو تعديل أو رفض)
    مفوضة للمطبخ المحاسبي المركزي لحماية سقف الدين وسلامة المخزون ومنع الـ Deadlocks
    """
    user = get_user_from_token(request)
    if 'customer_id' not in user:
        return web.json_response({"status": "error", "message": "غير مصرح لك"})
        
    data = await request.json()
    order_id = data.get('order_id')
    action = data.get('action')
    
    if not order_id or not action:
        return web.json_response({"status": "error", "message": "رقم الطلب والإجراء مطلوبان."})
        
    order_id = int(order_id)
    modified_items = data.get('items', {})  # {product_id: new_qty}
    
    # 🚀 استدعاء المطبخ المحاسبي المركزي المعتمد في services/accounting.py
    from services.accounting import process_family_order_action
    try:
        await process_family_order_action(user['customer_id'], order_id, action, modified_items)
        
        msg = "تم اعتماد الطلب وإرساله للبقالة بنجاح ✅" if action == 'approve' else "تم رفض الطلب واسترجاع المخزون بنجاح."
        return web.json_response({"status": "success", "message": msg})
        
    except ValueError as e:
        # التقاط أخطاء سقف الدين ونفاد المخزون وإرجاعها للواجهة بلطف
        return web.json_response({"status": "error", "message": str(e)})
    except Exception as e:
        import logging
        logging.error("خطأ غير متوقع أثناء معالجة طلب العائلة: %s", e)
        return web.json_response({"status": "error", "message": "حدث خطأ أثناء معالجة الطلب، يرجى المحاولة لاحقاً."})

async def api_customer_pay_intent(request: web.Request):
    """إرسال إشعار سداد دين للبقالة"""
    user = get_user_from_token(request)
    data = await request.json()
    amount = data.get('amount')
    note = data.get('note', '')

    async with database.pool.acquire() as conn:
        cust = await conn.fetchrow("SELECT name FROM customers WHERE id = $1", user['customer_id'])
        store_tid = await conn.fetchval("SELECT telegram_id FROM stores WHERE id = $1", user['store_id'])

        # 1. إرسال إشعار تيليجرام لصاحب البقالة
        msg = f"💵 **إشعار سداد قادم!**\n👤 الزبون: {cust['name']}\n💰 المبلغ المتوقع: {amount} ريال\n📝 ملاحظة: {note}\n\n(يرجى استلام المبلغ وتسجيله في النظام عند وصوله)"
        if store_tid:
            asyncio.create_task(bot.send_message(store_tid, msg))

        # 2. إرسال إشعار فايربيس (Push) لعمال الكاشير في هذه البقالة
        workers = await conn.fetch("SELECT id FROM workers WHERE store_id = $1", user['store_id'])
        for w in workers:
            asyncio.create_task(send_push_notification(w['id'], "💵 إشعار سداد قادم", f"الزبون {cust['name']} سيرسل {amount} ريال. ({note})"))

    return web.json_response({"status": "success", "message": "تم إرسال إشعار السداد للبقالة بنجاح ✅"})

# 👇 المسار الجديد: جلب الإشعارات الديناميكية للزبون 👇
async def api_customer_notifications(request: web.Request):
    user = get_user_from_token(request)
    if 'customer_id' not in user:
        return web.json_response({"status": "error", "message": "غير مصرح"})
        
    async with database.pool.acquire() as conn:
        # جلب آخر 10 عمليات كإشعارات
        trans = await conn.fetch("""
            SELECT amount, details, created_at 
            FROM transactions 
            WHERE customer_id = $1 
            ORDER BY created_at DESC LIMIT 10
        """, user['customer_id'])
        
    notifs = []
    for t in trans:
        amount = float(t['amount'])
        if amount < 0:
            title = "💸 دفعة مسددة"
            body = f"تم تسجيل سداد مبلغ {abs(amount)} ريال بنجاح."
        else:
            title = "🛍️ عملية شراء جديدة"
            body = f"تم تسجيل مشتريات بقيمة {amount} ريال. ({t['details']})"
            
        notifs.append({
            "title": title,
            "body": body,
            "created_at": t['created_at'].strftime('%Y-%m-%d %I:%M %p')
        })
        
    return web.json_response({"status": "success", "notifications": notifs})

# 👇 المسار الجديد: جلب فواتير صندوق الوارد 📥 👇
async def api_customer_invoices(request: web.Request):
    user = get_user_from_token(request)
    if 'customer_id' not in user:
        return web.json_response({"status": "error", "message": "غير مصرح"})
        
    async with database.pool.acquire() as conn:
        # جلب الفواتير الخاصة بالزبون
        invoices = await conn.fetch("""
            SELECT id, title, pdf_url, is_read, created_at 
            FROM invoices 
            WHERE customer_id = $1 
            ORDER BY created_at DESC LIMIT 50
        """, user['customer_id'])
        
        # تحديث حالة الفواتير غير المقروءة إلى "مقروءة" بمجرد فتح الصندوق
        unread_exists = any(not inv['is_read'] for inv in invoices)
        if unread_exists:
            await conn.execute("UPDATE invoices SET is_read = TRUE WHERE customer_id = $1 AND is_read = FALSE", user['customer_id'])
            
    invoices_list = []
    for inv in invoices:
        invoices_list.append({
            "id": inv['id'],
            "title": inv['title'],
            "pdf_url": inv['pdf_url'],
            "is_read": inv['is_read'],
            "created_at": inv['created_at'].strftime('%Y-%m-%d %I:%M %p')
        })
        
    return web.json_response({"status": "success", "invoices": invoices_list})

# 👇 المسار الجديد: التوليد اللحظي للفاتورة (Zero Storage) 🧾 👇
async def api_customer_invoice_view(request: web.Request):
    # يمكن للزبون فتح الرابط من المتصفح مباشرة، لذا نأخذ التوكن من الرابط أو الكوكيز أو الهيدر
    token = request.query.get('token') or request.cookies.get('customer_token') or request.headers.get('Authorization', '').replace('Bearer ', '')
    
    try:
        user = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
    except:
        return web.Response(text="غير مصرح لك بعرض هذه الفاتورة.", status=401)

    order_id = int(request.match_info['order_id'])
    
    async with database.pool.acquire() as conn:
        # جلب بيانات الطلب والبقالة والزبون
        order = await conn.fetchrow("""
            SELECT o.id, o.created_at, o.total_amount, o.delivery_fee, o.payment_method, 
                   s.name as store_name, s.phone as store_phone, s.logo_url, 
                   c.name as customer_name, c.phone as customer_phone
            FROM orders o 
            JOIN stores s ON o.store_id = s.id 
            JOIN customers c ON o.customer_id = c.id 
            WHERE o.id = $1 AND o.customer_id = $2
        """, order_id, user['customer_id'])
        
        if not order:
            return web.Response(text="الفاتورة غير موجودة.", status=404)
            
        # جلب المنتجات
        items = await conn.fetch("""
            SELECT oi.quantity, oi.price_at_time, sp.custom_name as name 
            FROM order_items oi 
            JOIN store_products sp ON oi.product_id = sp.id 
            WHERE oi.order_id = $1
        """, order_id)

    import html # 🛡️ استيراد مكتبة الحماية

    # تصميم الفاتورة (HTML/CSS)
    items_html = ""
    for i, item in enumerate(items, 1):
        total_price = float(item['price_at_time']) * item['quantity']
        safe_item_name = html.escape(item['name'] or '') # 🛡️ حماية اسم المنتج
        items_html += f"""
        <tr>
            <td>{i}</td>
            <td>{safe_item_name}</td>
            <td>{item['quantity']}</td>
            <td>{float(item['price_at_time'])}</td>
            <td>{total_price}</td>
        </tr>
        """
        
    pay_method = "آجل (على الحساب)" if order['payment_method'] == 'credit' else "نقدي (كاش)"
    date_str = order['created_at'].strftime('%Y-%m-%d %I:%M %p')

    # 🛡️ حماية اسم البقالة واسم الزبون
    safe_store_name = html.escape(order['store_name'] or '')
    safe_customer_name = html.escape(order['customer_name'] or '')

    html_content = f"""
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>فاتورة رقم #{order['id']}</title>
        <link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;700;900&display=swap" rel="stylesheet">
        <style>
            body {{ font-family: 'Cairo', sans-serif; background: #f4f7f6; margin: 0; padding: 20px; color: #1e293b; }}
            .invoice-box {{ max-width: 800px; margin: auto; background: #fff; padding: 30px; border-radius: 15px; box-shadow: 0 10px 30px rgba(0,0,0,0.1  ); }}
            .header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #e2e8f0; padding-bottom: 20px; margin-bottom: 20px; }}
            .header img {{ max-height: 80px; border-radius: 10px; }}
            .info-section {{ display: flex; justify-content: space-between; margin-bottom: 30px; }}
            .info-box {{ background: #f8fafc; padding: 15px; border-radius: 10px; width: 48%; border: 1px solid #e2e8f0; }}
            table {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; }}
            th, td {{ padding: 12px; text-align: right; border-bottom: 1px solid #e2e8f0; }}
            th {{ background: #0ba360; color: white; font-weight: bold; }}
            .total-section {{ text-align: left; background: #f8fafc; padding: 20px; border-radius: 10px; border: 1px solid #e2e8f0; }}
            .total-row {{ font-size: 16px; margin-bottom: 10px; }}
            .grand-total {{ font-size: 22px; font-weight: 900; color: #0ba360; border-top: 2px solid #e2e8f0; padding-top: 10px; }}
            .btn-print {{ display: block; width: 100%; background: #0ba360; color: white; text-align: center; padding: 15px; border-radius: 10px; text-decoration: none; font-weight: bold; font-size: 18px; margin-top: 20px; cursor: pointer; border: none; }}
            @media print {{
                body {{ background: #fff; padding: 0; }}
                .invoice-box {{ box-shadow: none; padding: 0; }}
                .no-print {{ display: none !important; }}
            }}
        </style>
    </head>
    <body>
        <div class="invoice-box">
            <div class="header">
                <div>
                    <h1 style="margin: 0; color: #0ba360;">فاتورة ضريبية مبسطة</h1>
                    <p style="margin: 5px 0 0; color: #64748b;">رقم الفاتورة: #{order['id']}</p>
                    <p style="margin: 5px 0 0; color: #64748b;">التاريخ: {date_str}</p>
                </div>
                <div style="text-align: left;">
                    <img src="{order['logo_url']}" alt="شعار البقالة">
                    <h3 style="margin: 10px 0 0;">{safe_store_name}</h3>
                </div>
            </div>
            
            <div class="info-section">
                <div class="info-box">
                    <h4 style="margin-top: 0; color: #0ba360;">بيانات العميل:</h4>
                    <p><strong>الاسم:</strong> {safe_customer_name}</p>
                    <p><strong>الجوال:</strong> {order['customer_phone']}</p>
                </div>
                <div class="info-box">
                    <h4 style="margin-top: 0; color: #0ba360;">تفاصيل الدفع:</h4>
                    <p><strong>طريقة الدفع:</strong> {pay_method}</p>
                    <p><strong>حالة الطلب:</strong> مكتمل</p>
                </div>
            </div>

            <table>
                <thead>
                    <tr>
                        <th>م</th>
                        <th>الصنف</th>
                        <th>الكمية</th>
                        <th>سعر الوحدة</th>
                        <th>الإجمالي</th>
                    </tr>
                </thead>
                <tbody>
                    {items_html}
                </tbody>
            </table>

            <div class="total-section">
                <div class="total-row">رسوم التوصيل: {float(order['delivery_fee'])} ريال</div>
                <div class="grand-total">الإجمالي النهائي: {float(order['total_amount'])} ريال</div>
            </div>

            <button class="btn-print no-print" onclick="window.print()">🖨️ طباعة / حفظ كـ PDF</button>
        </div>
    </body>
    </html>
    """
    return web.Response(text=html_content, content_type='text/html')

async def api_customer_change_password(request: web.Request):
    user = get_user_from_token(request)
    data = await request.json()
    old_pass = data.get('old_password')
    new_pass = data.get('new_password')

    async with database.pool.acquire() as conn:
        current_hash = await conn.fetchval("SELECT password FROM customers WHERE id = $1", user['customer_id'])
        import bcrypt
        import asyncio # 🛡️ لضمان عمل الخلفية
        
        # 🚀 1. التحقق من الكلمة القديمة في الخلفية (عدم تجميد السيرفر)
        is_valid = await asyncio.to_thread(bcrypt.checkpw, old_pass.encode('utf-8'), current_hash.encode('utf-8'))
        if not is_valid:
            return web.json_response({"status": "error", "message": "كلمة المرور القديمة غير صحيحة"})
            
        # 🚀 2. تشفير الكلمة الجديدة في الخلفية
        hashed_bytes = await asyncio.to_thread(bcrypt.hashpw, new_pass.encode('utf-8'), bcrypt.gensalt())
        new_hash = hashed_bytes.decode('utf-8')
        
        await conn.execute("UPDATE customers SET password = $1 WHERE id = $2", new_hash, user['customer_id'])
        
    return web.json_response({"status": "success", "message": "تم تغيير كلمة المرور بنجاح!"})

# 👇 المسار الجديد: التوليد اللحظي لسند القبض 💵 👇
async def api_customer_receipt_view(request: web.Request):
    token = request.query.get('token') or request.cookies.get('customer_token') or request.headers.get('Authorization', '').replace('Bearer ', '')
    try:
        user = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
    except:
        return web.Response(text="غير مصرح لك بعرض هذا السند.", status=401)

    trans_id = int(request.match_info['trans_id'])
    
    async with database.pool.acquire() as conn:
        trans = await conn.fetchrow("""
            SELECT t.amount, t.created_at, c.name as customer_name, c.balance, s.name as store_name, s.logo_url
            FROM transactions t
            JOIN customers c ON t.customer_id = c.id
            JOIN stores s ON t.store_id = s.id
            WHERE t.id = $1 AND t.customer_id = $2
        """, trans_id, user['customer_id'])
        
        if not trans: return web.Response(text="السند غير موجود.", status=404)

    import html # 🛡️ استيراد مكتبة الحماية

    date_str = trans['created_at'].strftime('%Y-%m-%d %I:%M %p')
    amount_paid = abs(float(trans['amount']))
    
    # 🛡️ حماية اسم البقالة واسم الزبون
    safe_store_name = html.escape(trans['store_name'] or '')
    safe_customer_name = html.escape(trans['customer_name'] or '')
    
    html_content = f"""
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>سند قبض #{trans_id}</title>
        <link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;700;900&display=swap" rel="stylesheet">
        <style>
            body {{ font-family: 'Cairo', sans-serif; background: #f4f7f6; margin: 0; padding: 20px; color: #1e293b; }}
            .receipt-box {{ max-width: 500px; margin: auto; background: #fff; padding: 30px; border-radius: 20px; box-shadow: 0 10px 30px rgba(0,0,0,0.1  ); text-align: center; border-top: 10px solid #0ba360; }}
            .logo {{ width: 80px; height: 80px; border-radius: 15px; margin-bottom: 15px; object-fit: cover; }}
            .amount {{ font-size: 40px; font-weight: 900; color: #0ba360; margin: 10px 0; }}
            .details-box {{ background: #f8fafc; padding: 15px; border-radius: 15px; text-align: right; margin-top: 20px; border: 1px solid #e2e8f0; }}
            .details-row {{ display: flex; justify-content: space-between; margin-bottom: 10px; font-size: 15px; border-bottom: 1px dashed #cbd5e1; padding-bottom: 5px; }}
            .btn-print {{ display: block; width: 100%; background: #1e293b; color: white; text-align: center; padding: 15px; border-radius: 12px; text-decoration: none; font-weight: bold; font-size: 16px; margin-top: 20px; cursor: pointer; border: none; }}
        </style>
    </head>
    <body>
        <div class="receipt-box">
            <img src="{trans['logo_url']}" class="logo" alt="شعار البقالة">
            <h2 style="margin: 0; color: #1e293b;">سند قبض إلكتروني</h2>
            <p style="color: #64748b; margin-top: 5px;">{safe_store_name}</p>
            
            <div class="amount">{amount_paid} ريال</div>
            <div style="background: #dcfce7; color: #0ba360; display: inline-block; padding: 5px 15px; border-radius: 20px; font-weight: bold; font-size: 14px;">تم الاستلام بنجاح ✅</div>
            
            <div class="details-box">
                <div class="details-row"><span>رقم السند:</span> <b>#{trans_id}</b></div>
                <div class="details-row"><span>التاريخ:</span> <b>{date_str}</b></div>
                <div class="details-row"><span>استلمنا من السيد/ة:</span> <b>{safe_customer_name}</b></div>
                <div class="details-row" style="border: none; margin-bottom: 0;"><span>الرصيد المتبقي:</span> <b style="color: #ff4757;">{float(trans['balance'])} ريال</b></div>
            </div>
            <button class="btn-print" onclick="window.print()">🖨️ طباعة / حفظ كـ PDF</button>
        </div>
    </body>
    </html>
    """
    return web.Response(text=html_content, content_type='text/html')

# ================= 💬 نظام المحادثة الفورية (الزبون) =================
async def api_customer_chat_history(request: web.Request):
    user = get_user_from_token(request)
    async with database.pool.acquire() as conn:
        # تحديث الرسائل غير المقروءة من البقالة إلى مقروءة
        await conn.execute("UPDATE chat_messages SET is_read = TRUE WHERE store_id = $1 AND customer_id = $2 AND sender_type = 'store' AND is_read = FALSE", user['store_id'], user['customer_id'])
        
        messages = await conn.fetch("""
            SELECT sender_type, message_text, created_at 
            FROM chat_messages 
            WHERE store_id = $1 AND customer_id = $2 
            ORDER BY created_at ASC
        """, user['store_id'], user['customer_id'])
        
    msg_list = [{"sender": m['sender_type'], "text": m['message_text'], "time": m['created_at'].strftime('%Y-%m-%d %I:%M %p')} for m in messages]
    return web.json_response({"status": "success", "messages": msg_list})

async def api_customer_chat_send(request: web.Request):
    user = get_user_from_token(request)
    data = await request.json()
    text = data.get('message', '').strip()
    if not text: return web.json_response({"status": "error", "message": "رسالة فارغة"})
    
    async with database.pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO chat_messages (store_id, customer_id, sender_type, message_text)
            VALUES ($1, $2, 'customer', $3)
        """, user['store_id'], user['customer_id'], text)
        
        cust_name = await conn.fetchval("SELECT name FROM customers WHERE id = $1", user['customer_id'])
        
        # 👇 إرسال الرسالة فوراً عبر الرادار (WebSockets) 👇
        from api.routes.websockets import notify_store_new_chat_message
        from datetime import datetime
        import asyncio
        
        time_now = datetime.now().strftime('%Y-%m-%d %I:%M %p')
        chat_data = {
            "customer_id": user['customer_id'],
            "customer_name": cust_name,
            "text": text,
            "time": time_now,
            "sender": "customer"
        }
        asyncio.create_task(notify_store_new_chat_message(user['store_id'], chat_data))
        
        # 👇 إرسال إشعار لحظي (Push Notification) لتطبيق البقالة (PWA/APK) 👇
        from services.notifications import send_push_notification
        asyncio.create_task(send_push_notification(
            user['store_id'], 
            f"💬 رسالة من ({cust_name})", 
            text, 
            extra_data={"action": "open_chat"}, 
            user_type='staff' # إرسالها لصاحب البقالة والكاشير
        ))
        
        # إرسال إشعار للبقالة عبر التيليجرام
        store_tid = await conn.fetchval("SELECT telegram_id FROM stores WHERE id = $1", user['store_id'])
        if store_tid:
            from bot.setup import bot
            try:
                await bot.send_message(store_tid, f"💬 **رسالة جديدة من الزبون ({cust_name}):**\n\n{text}\n\n👉 افتح (إدارة البقالة) للرد عليه.")
            except: pass
            
    return web.json_response({"status": "success"})

# ================= 💬 نظام المحادثة العائلية =================
async def api_customer_family_chat_history(request: web.Request):
    user = get_user_from_token(request)
    sub_id = int(request.query.get('sub_id', 0)) # إذا كان الأب هو من يفتح المحادثة، سيرسل آيدي الابن
    
    async with database.pool.acquire() as conn:
        cust = await conn.fetchrow("SELECT parent_id FROM customers WHERE id = $1", user['customer_id'])
        
        if cust['parent_id']:
            # هذا حساب فرعي (زوجة/ابن)
            main_id = cust['parent_id']
            sub_customer_id = user['customer_id']
            # تحديث الرسائل غير المقروءة
            await conn.execute("UPDATE family_chat_messages SET is_read = TRUE WHERE main_customer_id = $1 AND sub_customer_id = $2 AND sender_id = $1 AND is_read = FALSE", main_id, sub_customer_id)
        else:
            # هذا حساب رئيسي (الأب)
            main_id = user['customer_id']
            sub_customer_id = sub_id
            await conn.execute("UPDATE family_chat_messages SET is_read = TRUE WHERE main_customer_id = $1 AND sub_customer_id = $2 AND sender_id = $2 AND is_read = FALSE", main_id, sub_customer_id)
            
        messages = await conn.fetch("""
            SELECT sender_id, message_text, created_at 
            FROM family_chat_messages 
            WHERE main_customer_id = $1 AND sub_customer_id = $2 
            ORDER BY created_at ASC
        """, main_id, sub_customer_id)
        
    msg_list = [{"sender_id": m['sender_id'], "text": m['message_text'], "time": m['created_at'].strftime('%I:%M %p')} for m in messages]
    return web.json_response({"status": "success", "messages": msg_list, "my_id": user['customer_id']})

async def api_customer_family_chat_send(request: web.Request):
    user = get_user_from_token(request)
    data = await request.json()
    text = data.get('message', '').strip()
    sub_id = int(data.get('sub_id', 0))
    
    if not text: return web.json_response({"status": "error", "message": "رسالة فارغة"})
    
    async with database.pool.acquire() as conn:
        cust = await conn.fetchrow("SELECT name, parent_id FROM customers WHERE id = $1", user['customer_id'])
        
        if cust['parent_id']:
            main_id = cust['parent_id']
            sub_customer_id = user['customer_id']
            receiver_id = main_id
            title = f"💬 رسالة من ({cust['name']})"
        else:
            main_id = user['customer_id']
            sub_customer_id = sub_id
            receiver_id = sub_customer_id
            title = f"💬 رسالة من ({cust['name']})"
            
        await conn.execute("""
            INSERT INTO family_chat_messages (main_customer_id, sub_customer_id, sender_id, message_text)
            VALUES ($1, $2, $3, $4)
        """, main_id, sub_customer_id, user['customer_id'], text)
        
    # إرسال إشعار للطرف الآخر
    import asyncio
    from services.notifications import send_push_notification
    asyncio.create_task(send_push_notification(receiver_id, title, text))
        
    return web.json_response({"status": "success"})

# ================= 💳 الخدمة الذاتية لسحب الكروت (الزبون) =================
import aiohttp
import os
import logging

ALSHEHAB_API_URL = os.getenv("ALSHEHAB_API_URL", "https://my-bot-ehio.onrender.com" )
B2B_SECRET_KEY = os.getenv("B2B_SECRET_KEY", "SuperSecretDukkaniBridge2026")

async def api_customer_pull_ecard(request: web.Request):
    user = get_user_from_token(request)
    if 'customer_id' not in user:
        return web.json_response({"status": "error", "message": "غير مصرح لك"})
        
    data = await request.json()
    category = data.get('category')
    amount = data.get('amount')
    
    if not category or not amount:
        return web.json_response({"status": "error", "message": "بيانات غير مكتملة"})

    async with database.pool.acquire() as conn:
        # 🌟 جلب الفئات المسموحة للزبون
        cust = await conn.fetchrow("SELECT name, balance, credit_limit, can_pull_cards, allowed_ecard_amounts FROM customers WHERE id = $1", user['customer_id'])
        if not cust:
            return web.json_response({"status": "error", "message": "الزبون غير موجود"})
            
        if not cust['can_pull_cards']:
            return web.json_response({"status": "error", "message": "عذراً، خدمة السحب الذاتي للكروت غير مفعلة لحسابك."})

        # 🌟 الحماية الفولاذية: منع الزبون من سحب فئة غير مصرح له بها 🌟
        allowed_amounts_json = cust.get('allowed_ecard_amounts')
        import json
        allowed_amounts = json.loads(allowed_amounts_json) if isinstance(allowed_amounts_json, str) else (allowed_amounts_json or [])
        
        if allowed_amounts and str(amount) not in allowed_amounts:
            return web.json_response({"status": "error", "message": f"عذراً، غير مصرح لك بسحب فئة {amount}. تواصل مع البقالة لإضافتها لحسابك."})
            
        store = await conn.fetchrow("SELECT alshehab_phone FROM stores WHERE id = $1", user['store_id'])
        if not store or not store['alshehab_phone']:
            return web.json_response({"status": "error", "message": "البقالة غير مرتبطة بنظام الكروت حالياً."})
            
        alshehab_phone = store['alshehab_phone']

    async with aiohttp.ClientSession(  ) as session:
        headers = {"Authorization": f"Bearer {B2B_SECRET_KEY}", "Content-Type": "application/json"}
        payload = {"store_telegram_id": alshehab_phone, "category": category, "amount": amount}
        async with session.post(f"{ALSHEHAB_API_URL}/api/b2b/pull_card", headers=headers, json=payload) as resp:
            result = await resp.json()

    if result.get("status") != "success":
        return web.json_response({"status": "error", "message": result.get("message", "فشل سحب الكرت من الخادم المركزي")})

    card_data = result['card']
    sell_price = float(card_data['sell_price'])

    async with database.pool.acquire() as conn:
        async with conn.transaction():
            # 🛡️ قفل صف الزبون والتحقق الذري من سقف الدين لمنع التلاعب المتزامن
            locked_cust = await conn.fetchrow("SELECT balance, credit_limit FROM customers WHERE id = $1 FOR UPDATE", user['customer_id'])
            if (float(locked_cust['balance']) + sell_price) > float(locked_cust['credit_limit']):
                return web.json_response({"status": "error", "message": "لا يمكن إتمام العملية! رصيدك سيتجاوز سقف الدين المسموح."})

            await conn.execute("UPDATE customers SET balance = balance + $1 WHERE id = $2", sell_price, user['customer_id'])
            await conn.execute("""
                INSERT INTO transactions (store_id, customer_id, trans_type, amount, details) 
                VALUES ($1, $2, 'شراء_كرت', $3, $4)
            """, user['store_id'], user['customer_id'], sell_price, f"شراء كرت {category} فئة {amount} (خدمة ذاتية)")
            
            card_details = f"الشبكة: {category}\nالفئة: {amount}\nرقم التعبئة (الرمز السري):\n{card_data['pin_code']}"
            await conn.execute("""
                INSERT INTO invoices (store_id, customer_id, title, pdf_url)
                VALUES ($1, $2, $3, $4)
            """, user['store_id'], user['customer_id'], f"💳 كرت {category} فئة {amount}", card_details)

        store_tid = await conn.fetchval("SELECT telegram_id FROM stores WHERE id = $1", user['store_id'])
        if store_tid:
            try:
                await bot.send_message(store_tid, f"💳 **سحب ذاتي لكرت!**\n\nالزبون: {cust['name']}\nسحب كرت {category} فئة {amount}\nتم تقييد {sell_price} ريال على حسابه كدين.")
            except: pass

    asyncio.create_task(send_push_notification(
        user['customer_id'], 
        f"💳 استلمت كرت {category} فئة {amount}", 
        f"الرقم السري موجود في صندوق الوارد. تم خصم {sell_price} ريال.",
        {"action": "open_inbox"}
    ))
    
    return web.json_response({"status": "success", "message": "تم سحب الكرت بنجاح! تجده الآن في صندوق الوارد 📥"})

# ================= 💳 جلب الكروت المتوفرة للزبون (مع فلترة الصلاحيات) =================
async def api_customer_available_ecards(request: web.Request):
    user = get_user_from_token(request)
    if 'customer_id' not in user:
        return web.json_response({"status": "error", "message": "غير مصرح لك"})
        
    async with database.pool.acquire() as conn:
        store = await conn.fetchrow("SELECT alshehab_phone FROM stores WHERE id = $1", user['store_id'])
        if not store or not store['alshehab_phone']:
            return web.json_response({"status": "error", "message": "البقالة غير مرتبطة بنظام الكروت حالياً."})
            
        alshehab_phone = store['alshehab_phone']
        
        # جلب الفئات المسموحة لهذا الزبون
        cust = await conn.fetchrow("SELECT allowed_ecard_amounts FROM customers WHERE id = $1", user['customer_id'])
        import json
        allowed_json = cust.get('allowed_ecard_amounts')
        allowed_amounts = json.loads(allowed_json) if isinstance(allowed_json, str) else (allowed_json or [])

    import aiohttp
    import os
    import logging
    ALSHEHAB_API_URL = os.getenv("ALSHEHAB_API_URL", "https://my-bot-ehio.onrender.com" )
    B2B_SECRET_KEY = os.getenv("B2B_SECRET_KEY", "SuperSecretDukkaniBridge2026")

    try:
        async with aiohttp.ClientSession( ) as session:
            headers = {"Authorization": f"Bearer {B2B_SECRET_KEY}", "Content-Type": "application/json"}
            payload = {"store_telegram_id": alshehab_phone}
            async with session.post(f"{ALSHEHAB_API_URL}/api/b2b/available_cards", headers=headers, json=payload) as resp:
                result = await resp.json()
                
        # 🌟 الفلترة الذكية: إخفاء الفئات التي لم يصرح بها صاحب البقالة للزبون 🌟
        if result.get("status") == "success":
            if allowed_amounts:
                filtered_cards = [c for c in result['available_cards'] if str(c['amount']) in allowed_amounts]
                result['available_cards'] = filtered_cards
            else:
                # إذا لم يحدد البقال أي فئة، نعتبرها كلها ممنوعة (أو يمكنك جعلها كلها مسموحة حسب رغبتك)
                result['available_cards'] = []
                
        return web.json_response(result)
        
    except Exception as e:
        logging.error(f"Customer Get Cards Error: {e}")
        return web.json_response({"status": "error", "message": "فشل الاتصال بالخادم المركزي"})

def setup_customer_routes(app: web.Application):
    app.router.add_post('/api/customer/login', api_customer_login)
    app.router.add_post('/api/customer/register', api_customer_register)
    app.router.add_get('/api/customer/statement', api_customer_statement)
    app.router.add_get('/api/customer/products', api_customer_products)
    app.router.add_post('/api/customer/order', api_customer_order)
    app.router.add_get('/api/customer/family/invite', api_customer_family_invite)
    app.router.add_post('/api/customer/family/add', api_customer_family_add)
    app.router.add_post('/api/customer/family/settings', api_customer_family_settings)
    app.router.add_get('/api/customer/family/orders', api_customer_family_orders)
    app.router.add_post('/api/customer/family/action', api_customer_family_action)
    app.router.add_get('/api/customer/family/members', api_customer_family_members)
    app.router.add_post('/api/customer/family/member/update', api_customer_family_member_update)
    app.router.add_post('/api/customer/family/member/delete', api_customer_family_member_delete)
    app.router.add_post('/api/customer/pay_intent', api_customer_pay_intent)
    app.router.add_get('/api/customer/notifications', api_customer_notifications)
    app.router.add_get('/api/customer/invoices', api_customer_invoices)
    app.router.add_get('/api/customer/invoice/{order_id}', api_customer_invoice_view)
    app.router.add_post('/api/customer/change_password', api_customer_change_password)
    app.router.add_get('/api/customer/receipt/{trans_id}', api_customer_receipt_view)
    app.router.add_get('/api/customer/chat', api_customer_chat_history)
    app.router.add_post('/api/customer/chat/send', api_customer_chat_send)
    app.router.add_get('/api/customer/family/chat', api_customer_family_chat_history)
    app.router.add_post('/api/customer/family/chat/send', api_customer_family_chat_send)
    
    # 🌉 مسارات سحب الكروت ذاتياً للزبون
    app.router.add_get('/api/customer/available_ecards', api_customer_available_ecards)
    app.router.add_post('/api/customer/pull_ecard', api_customer_pull_ecard)
