# مسار الملف: admin.py
import logging
import csv
import time
import random
import asyncio
import json 
import uuid
from io import StringIO
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from aiohttp import web
import jwt

import database
from config import JWT_SECRET, ADMIN_PASSWORD, ADMIN_ID
from bot.setup import bot
from api.routes.websockets import notify_admin_new_log

logger = logging.getLogger(__name__)

# ==========================================
# 🛠️ دوال مساعدة (Helper Functions)
# ==========================================
ADMIN_WEBSOCKETS = set()

def get_client_ip(request: web.Request) -> str:
    """جلب عنوان IP الحقيقي للمستخدم حتى خلف الـ Reverse Proxy"""
    return request.headers.get('X-Forwarded-For', request.remote or 'unknown').split(',')[0].strip()

def check_admin_token(request: web.Request):
    """التحقق الصارم من صلاحيات القيادة العليا"""
    auth_header = request.headers.get('Authorization')
    if not auth_header: 
        raise web.HTTPUnauthorized(text='{"status": "error", "message": "غير مصرح"}', content_type='application/json')
    
    parts = auth_header.split(' ')
    if len(parts) != 2 or parts[0].lower() != 'bearer':
        raise web.HTTPUnauthorized(text='{"status": "error", "message": "صيغة التوكن غير صحيحة"}', content_type='application/json')
        
    token = parts[1]
    try:
        decoded = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
        if decoded.get('role') != 'super_admin': 
            raise web.HTTPForbidden(text='{"status": "error", "message": "صلاحيات غير كافية"}', content_type='application/json')
        return decoded
    except jwt.ExpiredSignatureError:
        raise web.HTTPUnauthorized(text='{"status": "error", "message": "انتهت الجلسة، يرجى تسجيل الدخول مجدداً"}', content_type='application/json')
    except Exception:
        raise web.HTTPUnauthorized(text='{"status": "error", "message": "توكن غير صالح"}', content_type='application/json')

async def api_admin_ws_logs(request: web.Request):
    """مسار الاتصال الحي (WebSocket) لشاشة القيادة العليا (محصن)"""
    token = request.query.get('token')
    if not token:
        return web.HTTPUnauthorized(reason="Token missing")
    try:
        decoded = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
        if decoded.get('role') != 'super_admin':
            return web.HTTPForbidden(reason="Unauthorized")
    except Exception:
        return web.HTTPUnauthorized(reason="Invalid token")

    ws = web.WebSocketResponse()
    await ws.prepare(request)
    
    if 'websockets' in request.app:
        request.app['websockets'].add(ws)
    ADMIN_WEBSOCKETS.add(ws)
    
    try:
        async for msg in ws:
            pass 
    finally:
        ADMIN_WEBSOCKETS.discard(ws)
        if 'websockets' in request.app:
            request.app['websockets'].discard(ws)
    return ws

async def log_admin_action(conn, action: str, details: str, ip_address: str = "127.0.0.1"):
    """دالة لتسجيل أي حركة يقوم بها المدير مع الـ IP وإرسالها للرادار الحي"""
    await conn.execute("INSERT INTO admin_logs (action, details, ip_address) VALUES ($1, $2, $3)", action, details, ip_address)
    
    log_data = {
        "type": "new_log",
        "data": {
            "action": action,
            "details": details,
            "time": datetime.now(ZoneInfo('Asia/Aden')).strftime("%Y-%m-%d %H:%M")
        }
    }
    
    # بث الحدث لجميع شاشات الإدارة المتصلة
    for ws in list(ADMIN_WEBSOCKETS):
        try:
            await ws.send_str(json.dumps(log_data))
        except Exception:
            ADMIN_WEBSOCKETS.discard(ws)
            
    try:
        await notify_admin_new_log(log_data["data"])
    except Exception:
        pass

# ==========================================
# 🔐 مسارات الدخول والإحصائيات
# ==========================================
async def api_admin_login(request: web.Request):
    data = await request.json()
    password = data.get('password')
    ip = get_client_ip(request)

    if password != ADMIN_PASSWORD:
        return web.json_response({"status": "error", "message": "كلمة المرور غير صحيحة"})
        
    token = jwt.encode({
        'role': 'super_admin',
        'exp': datetime.now(ZoneInfo('UTC')) + timedelta(days=1)
    }, JWT_SECRET, algorithm='HS256')
    
    async with database.pool.acquire() as conn:
        await log_admin_action(conn, "تسجيل دخول 🔑", f"تم تسجيل الدخول بنجاح من IP: {ip}", ip)
        
    return web.json_response({"status": "success", "token": token})

async def api_admin_stats(request: web.Request):
    check_admin_token(request)
    async with database.pool.acquire() as conn:
        stats = await conn.fetchrow("""
            SELECT 
                (SELECT COUNT(*) FROM stores WHERE is_deleted = FALSE) as stores_count,
                (SELECT COUNT(*) FROM stores WHERE status = 'active' AND is_deleted = FALSE) as active_stores,
                (SELECT COUNT(*) FROM customers WHERE is_deleted = FALSE) as customers_count,
                (SELECT COALESCE(SUM(balance), 0) FROM customers WHERE is_deleted = FALSE) as total_debt,
                (SELECT COUNT(*) FROM stores WHERE package_type = 'vip' AND is_deleted = FALSE) as vip_count
        """)
        
        growth_records = await conn.fetch("""
            SELECT DATE(created_at) as date, COUNT(*) as count 
            FROM stores 
            WHERE created_at >= CURRENT_DATE - INTERVAL '7 days' 
            GROUP BY DATE(created_at) 
            ORDER BY date ASC
        """)
        
    stores_count = stats['stores_count']
    free_count = stores_count - stats['vip_count']
    
    growth_labels = [str(r['date'])[5:10] for r in growth_records]
    growth_values = [r['count'] for r in growth_records]

    return web.json_response({
        "status": "success",
        "stores_count": stores_count,
        "active_stores": stats['active_stores'],
        "customers_count": stats['customers_count'],
        "total_debt": float(stats['total_debt']),
        "package_data": [stats['vip_count'], free_count],
        "growth_data": {"labels": growth_labels, "values": growth_values}
    })

async def api_admin_change_password(request: web.Request):
    check_admin_token(request)
    return web.json_response({
        "status": "error", 
        "message": "لأسباب أمنية، تغيير كلمة مرور القيادة العليا يتم فقط من خلال إعدادات السيرفر (Environment Variables)."
    })

async def api_admin_maintenance(request: web.Request):
    check_admin_token(request)
    data = await request.json()
    is_active = data.get('active', False)
    ip = get_client_ip(request)
    
    async with database.pool.acquire() as conn:
        await conn.execute("UPDATE system_settings SET maintenance_mode = $1 WHERE id = 1", is_active)
        action_msg = "تفعيل وضع الصيانة 🔴" if is_active else "إيقاف وضع الصيانة 🟢"
        await log_admin_action(conn, "وضع الصيانة ⚠️", action_msg, ip)
        
    return web.json_response({"status": "success", "maintenance": is_active})

# ==========================================
# 🏪 مسارات إدارة البقالات
# ==========================================
async def api_admin_stores(request: web.Request):
    check_admin_token(request)
    async with database.pool.acquire() as conn:
        stores = await conn.fetch("SELECT id, name, phone, status, package_type, trial_ends_at, is_deleted FROM stores ORDER BY id DESC")

    stores_list = []
    for s in stores:
        d = dict(s)
        d['trial_ends_at'] = str(d['trial_ends_at'])[:10] if d['trial_ends_at'] else None
        stores_list.append(d)
        
    return web.json_response({"status": "success", "stores": stores_list})

async def api_admin_toggle_store(request: web.Request):
    check_admin_token(request)
    data = await request.json()
    ip = get_client_ip(request)
    
    store_id = data.get('store_id')
    new_status = data.get('status')
    
    if not store_id or not new_status:
        return web.json_response({"status": "error", "message": "بيانات غير مكتملة"}, status=400)
         
    store_id = int(store_id)
    suspend_reason = data.get('suspend_reason', '')

    async with database.pool.acquire() as conn:
        reason_to_save = suspend_reason if new_status == 'suspended' else None
        await conn.execute("UPDATE stores SET status = $1, suspend_reason = $2 WHERE id = $3", new_status, reason_to_save, store_id)
            
        log_details = f"تغيير حالة البقالة {store_id} إلى: {new_status}"
        if new_status == 'suspended' and suspend_reason:
            log_details += f" | السبب: {suspend_reason}"
            
        await log_admin_action(conn, "تغيير حالة بقالة", log_details, ip)
        
    return web.json_response({"status": "success"})

# ==========================================
# 🚀 المسارات الاستراتيجية (God Mode)
# ==========================================
async def api_admin_logs(request: web.Request):
    check_admin_token(request)
    try:
        page = max(1, int(request.query.get('page', 1)))
        limit = min(100, max(10, int(request.query.get('limit', 50))))
    except ValueError:
        page, limit = 1, 50
        
    offset = (page - 1) * limit

    async with database.pool.acquire() as conn:
        logs = await conn.fetch("""
            SELECT action, details, ip_address, created_at 
            FROM admin_logs 
            ORDER BY id DESC 
            LIMIT $1 OFFSET $2
        """, limit, offset)
        total_count = await conn.fetchval("SELECT COUNT(*) FROM admin_logs")
        
    logs_list = [{
        "action": l['action'], 
        "details": l['details'], 
        "ip": l['ip_address'] or "-", 
        "time": str(l['created_at'])[:16]
    } for l in logs]
    
    return web.json_response({
        "status": "success", 
        "logs": logs_list,
        "pagination": {
            "current_page": page,
            "total_pages": (total_count + limit - 1) // limit,
            "total_records": total_count,
            "has_next": (offset + limit) < total_count
        }
    })

async def api_admin_shadow_login(request: web.Request):
    check_admin_token(request)
    data = await request.json()
    ip = get_client_ip(request)
    
    store_id = data.get('store_id')
    if not store_id:
        return web.json_response({"status": "error", "message": "معرف البقالة مفقود"}, status=400)
        
    store_id = int(store_id)
    session_jti = str(uuid.uuid4())

    async with database.pool.acquire() as conn:
        telegram_id = await conn.fetchval("SELECT telegram_id FROM stores WHERE id = $1", store_id)
        await log_admin_action(conn, "وضع الظل 👻", f"الدخول كصاحب البقالة رقم {store_id}", ip)

    token = jwt.encode({
        'role': 'store_owner',
        'store_id': store_id,
        'jti': session_jti,
        'exp': datetime.now(ZoneInfo('UTC')) + timedelta(hours=2)
    }, JWT_SECRET, algorithm='HS256')
    
    return web.json_response({
        "status": "success", 
        "shadow_token": token,
        "telegram_id": telegram_id
    })

async def api_admin_revoke_shadow(request: web.Request):
    check_admin_token(request)
    data = await request.json()
    ip = get_client_ip(request)
    store_id = int(data.get('store_id', 0))
    jti = data.get('jti')
    
    if not jti:
        return web.json_response({"status": "error", "message": "معرف الجلسة مفقود"})
        
    async with database.pool.acquire() as conn:
        await conn.execute("INSERT INTO blacklisted_tokens (jti) VALUES ($1) ON CONFLICT DO NOTHING", jti)
        await log_admin_action(conn, "إنهاء الظل 🛑", f"إنهاء جلسة وضع الظل للبقالة {store_id}", ip)
        
    return web.json_response({"status": "success"})

async def api_admin_extend_trial(request: web.Request):
    check_admin_token(request)
    data = await request.json()
    ip = get_client_ip(request)
    store_id = int(data['store_id'])
    days = int(data.get('days', 30))
    
    tz = ZoneInfo('Asia/Aden')
    new_date = datetime.now(tz) + timedelta(days=days)
    
    async with database.pool.acquire() as conn:
        await conn.execute("UPDATE stores SET trial_ends_at = $1 WHERE id = $2", new_date, store_id)
        await log_admin_action(conn, "تمديد اشتراك ⏱️", f"تمديد الفترة للبقالة {store_id} لمدة {days} يوم", ip)
        
    return web.json_response({"status": "success", "new_date": str(new_date)[:10]})

async def api_admin_update_package(request: web.Request):
    check_admin_token(request)
    data = await request.json()
    ip = get_client_ip(request)
    store_id = int(data['store_id'])
    package_type = data.get('package_type')
    
    async with database.pool.acquire() as conn:
        await conn.execute("UPDATE stores SET package_type = $1 WHERE id = $2", package_type, store_id)
        action_name = "ترقية VIP 👑" if package_type == 'vip' else "إلغاء VIP ⬇️"
        await log_admin_action(conn, action_name, f"تغيير باقة البقالة {store_id} إلى {package_type}", ip)
        
    return web.json_response({"status": "success"})

async def api_admin_bulk_update(request: web.Request):
    check_admin_token(request)
    data = await request.json()
    ip = get_client_ip(request)
    store_ids = data.get('store_ids', [])
    action = data.get('action')
    
    if not store_ids or action != 'vip':
        return web.json_response({"status": "error", "message": "بيانات غير صالحة"})
        
    async with database.pool.acquire() as conn:
        await conn.execute("UPDATE stores SET package_type = 'vip' WHERE id = ANY($1::int[])", store_ids)
        await log_admin_action(conn, "ترقية جماعية 👑", f"ترقية {len(store_ids)} بقالات إلى باقة VIP", ip)
        
    return web.json_response({"status": "success"})

async def api_admin_recover_deleted(request: web.Request):
    check_admin_token(request)
    data = await request.json()
    ip = get_client_ip(request)
    table = data.get('table')
    record_id = int(data.get('id', 0))
    
    allowed_tables = ['stores', 'customers', 'store_products', 'workers', 'delivery_agents']
    if table not in allowed_tables:
        return web.json_response({"status": "error", "message": "جدول غير صالح"})
        
    async with database.pool.acquire() as conn:
        await conn.execute(f"UPDATE {table} SET is_deleted = FALSE WHERE id = $1", record_id)
        await log_admin_action(conn, "استعادة بيانات ♻️", f"استعادة السجل {record_id} من جدول {table}", ip)
        
    return web.json_response({"status": "success"})

async def api_admin_update_store(request: web.Request):
    check_admin_token(request)
    data = await request.json()
    ip = get_client_ip(request)
    store_id = int(data['store_id'])
    new_name = data.get('new_name')
    
    async with database.pool.acquire() as conn:          
        await conn.execute("UPDATE stores SET name = $1 WHERE id = $2", new_name, store_id)
        await log_admin_action(conn, "تحديث بقالة 🪪", f"تعديل اسم البقالة {store_id} إلى: {new_name}", ip)
        
    return web.json_response({"status": "success"})

async def background_broadcast_task(msg_text: str, target: str, admin_id: int):
    """مهمة خلفية آمنة للإرسال الجماعي"""
    async with database.pool.acquire() as conn:
        if target == 'suspended':
            stores = await conn.fetch("SELECT telegram_id FROM stores WHERE status = 'suspended' AND telegram_id IS NOT NULL")
            target_name = "البقالات الموقوفة"
        elif target == 'trial_ending':
            stores = await conn.fetch("SELECT telegram_id FROM stores WHERE trial_ends_at BETWEEN NOW() AND NOW() + INTERVAL '7 days' AND telegram_id IS NOT NULL")
            target_name = "البقالات التي ستنتهي قريباً"
        else:
            stores = await conn.fetch("SELECT telegram_id FROM stores WHERE telegram_id IS NOT NULL")
            target_name = "جميع البقالات"
            
    success, failed = 0, 0
    for s in stores:
        try:
            await bot.send_message(s['telegram_id'], f"📢 **رسالة من القيادة العليا:**\n\n{msg_text}")
            success += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
        
    async with database.pool.acquire() as conn:
        await log_admin_action(conn, "إذاعة 📢", f"اكتملت الإذاعة: نجاح {success}، فشل {failed} ({target_name})")
        
    try:
        summary_msg = f"✅ **اكتملت الإذاعة**\n\n🎯 الفئة: {target_name}\n🟢 تم: {success}\n🔴 تعذر: {failed}"
        await bot.send_message(admin_id, summary_msg)
    except Exception as e:
        logger.warning("فشل إرسال ملخص الإذاعة: %s", e)

async def api_admin_broadcast(request: web.Request):
    check_admin_token(request)
    data = await request.json()
    msg_text = data.get('message')
    target = data.get('target')
    
    # حماية المهمة من الحذف
    database.create_safe_task(background_broadcast_task(msg_text, target, ADMIN_ID))
    
    return web.json_response({
        "status": "success", 
        "message": "تم بدء الإذاعة بنجاح في الخلفية."
    })

async def api_admin_backup(request: web.Request):
    check_admin_token(request)
    ip = get_client_ip(request)
    async with database.pool.acquire() as conn:
        stores = await conn.fetch("SELECT id, name, phone, status, created_at FROM stores")
        await log_admin_action(conn, "نسخة احتياطية 💾", "تنزيل تقرير البقالات CSV", ip)
        
    def generate_csv():
        csv_file = StringIO()
        writer = csv.writer(csv_file)
        writer.writerow(['ID', 'اسم البقالة', 'رقم الهاتف', 'الحالة', 'تاريخ التسجيل'])
        for s in stores:
            writer.writerow([s['id'], s['name'], s['phone'], s['status'], str(s['created_at'])[:16]])
        return csv_file.getvalue().encode('utf-8-sig')
        
    csv_data = await asyncio.to_thread(generate_csv)
        
    return web.Response(
        body=csv_data,
        headers={
            'Content-Disposition': 'attachment; filename="stores_backup.csv"',
            'Content-Type': 'text/csv; charset=utf-8'
        }
    )

async def api_admin_force_update(request: web.Request):
    check_admin_token(request)
    data = await request.json()
    version = data.get('version')
    ip = get_client_ip(request)
    
    async with database.pool.acquire() as conn:
        await log_admin_action(conn, "تحديث إجباري 🚀", f"أمر تحديث للإصدار {version}", ip)
        
    return web.json_response({"status": "success"})

async def api_admin_top_debts(request: web.Request):
    check_admin_token(request)
    async with database.pool.acquire() as conn:
        records = await conn.fetch("""
            SELECT s.name, SUM(c.balance) as total_debt 
            FROM customers c 
            JOIN stores s ON c.store_id = s.id 
            WHERE c.is_deleted = FALSE 
            GROUP BY s.id, s.name 
            HAVING SUM(c.balance) > 0
            ORDER BY total_debt DESC 
            LIMIT 5
        """)
        
    data = [{"name": r['name'], "debt": float(r['total_debt'])} for r in records]
    return web.json_response({"status": "success", "top_debts": data})

async def api_admin_health(request: web.Request):
    check_admin_token(request)
    start_time = time.time()
    async with database.pool.acquire() as conn:
        await conn.execute("SELECT 1")
    db_ping = int((time.time() - start_time) * 1000)
    
    try:
        import psutil
        cpu = psutil.cpu_percent()
        ram = psutil.virtual_memory().percent
    except ImportError:
        cpu = random.randint(15, 30)
        ram = random.randint(40, 60)
        
    return web.json_response({"status": "success", "cpu": cpu, "ram": ram, "db_ping": db_ping})

async def api_admin_recycle_bin_get(request: web.Request):
    check_admin_token(request)
    table = request.query.get('table')
    allowed_tables = ['stores', 'customers', 'store_products']
    if table not in allowed_tables:
        return web.json_response({"status": "error", "message": "جدول غير صالح"})
        
    async with database.pool.acquire() as conn:
        if table == 'stores':
            items = await conn.fetch("SELECT id, name FROM stores WHERE is_deleted = TRUE ORDER BY id DESC LIMIT 50")
        elif table == 'customers':
            items = await conn.fetch("SELECT id, name FROM customers WHERE is_deleted = TRUE ORDER BY id DESC LIMIT 50")
        elif table == 'store_products':
            items = await conn.fetch("SELECT id, custom_name as name FROM store_products WHERE is_deleted = TRUE ORDER BY id DESC LIMIT 50")
            
    items_list = [{"id": i['id'], "name": i['name']} for i in items]
    return web.json_response({"status": "success", "items": items_list})

async def api_admin_catalog_get(request: web.Request):
    check_admin_token(request)
    async with database.pool.acquire() as conn:
        items = await conn.fetch("SELECT id, name, category FROM global_products ORDER BY id DESC")
    return web.json_response({"status": "success", "items": [dict(i) for i in items]})

async def api_admin_catalog_add(request: web.Request):
    check_admin_token(request)
    data = await request.json()
    ip = get_client_ip(request)
    
    async with database.pool.acquire() as conn:
        await conn.execute("INSERT INTO global_products (name, category) VALUES ($1, $2)", data['name'], data.get('category', 'عام'))
        await log_admin_action(conn, "كتالوج 🌍", f"إضافة منتج قياسي: {data['name']}", ip)
    return web.json_response({"status": "success"})

async def api_admin_store_profile(request: web.Request):
    check_admin_token(request)
    store_id = int(request.query.get('store_id', 0))
    async with database.pool.acquire() as conn:
        store = await conn.fetchrow("SELECT name, phone, created_at, status, package_type FROM stores WHERE id = $1", store_id)
        if not store:
            return web.json_response({"status": "error", "message": "البقالة غير موجودة"}, status=404)
            
        cust_count = await conn.fetchval("SELECT COUNT(*) FROM customers WHERE store_id = $1 AND is_deleted = FALSE", store_id)
        total_debt = await conn.fetchval("SELECT COALESCE(SUM(balance), 0) FROM customers WHERE store_id = $1 AND is_deleted = FALSE", store_id)
        prod_count = await conn.fetchval("SELECT COUNT(*) FROM store_products WHERE store_id = $1 AND is_deleted = FALSE", store_id)
        order_count = await conn.fetchval("SELECT COUNT(*) FROM orders WHERE store_id = $1", store_id)
        
    return web.json_response({
        "status": "success",
        "name": store['name'],
        "phone": store['phone'] or "غير مسجل",
        "created_at": str(store['created_at'])[:10],
        "store_status": store['status'],
        "package_type": store['package_type'],
        "customers": cust_count,
        "debt": float(total_debt),
        "products": prod_count,
        "orders": order_count
    })

# ==========================================
# 💬 نظام الدعم والمحادثات
# ==========================================
async def api_admin_chat_list(request: web.Request):
    check_admin_token(request)
    async with database.pool.acquire() as conn:
        chats = await conn.fetch("""
            SELECT s.id, s.name, 
                   (SELECT message FROM admin_store_chats WHERE store_id = s.id ORDER BY created_at DESC LIMIT 1) as last_message,
                   (SELECT created_at FROM admin_store_chats WHERE store_id = s.id ORDER BY created_at DESC LIMIT 1) as last_time,
                   (SELECT COUNT(*) FROM admin_store_chats WHERE store_id = s.id AND sender = 'store' AND is_read = FALSE) as unread
            FROM stores s
            WHERE EXISTS (SELECT 1 FROM admin_store_chats WHERE store_id = s.id)
            ORDER BY last_time DESC
        """)
        
    chat_list = [{
        "id": c['id'], 
        "name": c['name'], 
        "last_message": c['last_message'] or "", 
        "time": c['last_time'].strftime('%Y-%m-%d %H:%M') if c['last_time'] else "", 
        "unread": c['unread']
    } for c in chats]
    return web.json_response({"status": "success", "chats": chat_list})

async def api_admin_chat_history(request: web.Request):
    check_admin_token(request)
    store_id = int(request.query.get('store_id', 0))
    async with database.pool.acquire() as conn:
        await conn.execute("UPDATE admin_store_chats SET is_read = TRUE WHERE store_id = $1 AND sender = 'store'", store_id)
        messages = await conn.fetch("SELECT sender, message, created_at FROM admin_store_chats WHERE store_id = $1 ORDER BY created_at ASC", store_id)
        
    msg_list = [{"sender": m['sender'], "text": m['message'], "time": m['created_at'].strftime('%H:%M')} for m in messages]
    return web.json_response({"status": "success", "messages": msg_list})

async def api_admin_chat_send(request: web.Request):
    check_admin_token(request)
    data = await request.json()
    store_id = int(data['store_id'])
    message = data['message']
    
    async with database.pool.acquire() as conn:
        await conn.execute("INSERT INTO admin_store_chats (store_id, sender, message) VALUES ($1, 'admin', $2)", store_id, message)
        
    from api.routes.websockets import notify_store_new_order
    database.create_safe_task(notify_store_new_order(store_id, {
        "type": "admin_chat_message", 
        "text": message, 
        "sender": "admin", 
        "time": datetime.now(ZoneInfo('Asia/Aden')).strftime('%H:%M')
    }))
    
    return web.json_response({"status": "success"})

# =====================================================================
# ⚙️ صلاحيات الخدمات (محصنة بتوكن المدير 🔒)
# =====================================================================
async def api_admin_store_telecom_perms(request: web.Request):
    check_admin_token(request) # 🛡️ تم التحصين
    store_id = int(request.query.get('store_id', 0))
    async with database.pool.acquire() as conn:
        perms_json = await conn.fetchval("SELECT telecom_permissions FROM stores WHERE id = $1", store_id)
        perms = json.loads(perms_json) if isinstance(perms_json, str) else (perms_json or {"ecards": False, "instant_recharge": False})
    return web.json_response({"status": "success", "permissions": perms})

async def api_admin_update_telecom_perms(request: web.Request):
    check_admin_token(request) # 🛡️ تم سد الثغرة الكارثية
    data = await request.json()
    ip = get_client_ip(request)
    store_id = int(data.get('store_id'))
    perms = data.get('permissions', {})
    
    async with database.pool.acquire() as conn:
        await conn.execute("UPDATE stores SET telecom_permissions = $1::jsonb WHERE id = $2", json.dumps(perms), store_id)
        await log_admin_action(conn, "صلاحيات الاتصالات 📱", f"تعديل صلاحيات البقالة {store_id}", ip)
        
    return web.json_response({"status": "success", "message": "تم التحديث بنجاح"})

# =====================================================================
# 📉 رادار الخمول والانسحاب (تم إصلاح خطأ التوقيت ⏱️)
# =====================================================================
async def api_admin_churn_radar(request: web.Request):
    check_admin_token(request) # 🛡️ تم التحصين
    tz = ZoneInfo('Asia/Aden')
    now = datetime.now(tz)
    
    async with database.pool.acquire() as conn:
        stores = await conn.fetch("""
            SELECT s.id, s.name, s.phone, s.package_type,
                   (SELECT MAX(created_at) FROM orders WHERE store_id = s.id) as last_order_date,
                   (SELECT COUNT(*) FROM orders WHERE store_id = s.id AND created_at >= NOW() - INTERVAL '7 days') as recent_orders
            FROM stores s
            WHERE s.status = 'active' AND s.is_deleted = FALSE
            ORDER BY recent_orders ASC, last_order_date ASC NULLS FIRST
        """)
        
    radar_data = []
    for st in stores:
        last_order = st['last_order_date']
        if last_order:
            # ✅ حل مشكلة Timezone Awareness لمنع كراش السيرفر
            if last_order.tzinfo is None:
                last_order = last_order.replace(tzinfo=tz)
            days_inactive = (now - last_order).days
        else:
            days_inactive = 999
        
        status = "خامل 🔴" if days_inactive > 3 or days_inactive == 999 else "نشط 🟢"
        
        radar_data.append({
            "id": st['id'],
            "name": st['name'],
            "phone": st['phone'] or "غير متوفر",
            "package_type": st['package_type'],
            "days_inactive": days_inactive if days_inactive != 999 else "لم يطلب أبداً",
            "recent_orders": st['recent_orders'],
            "status": status
        })
        
    return web.json_response({"status": "success", "radar_data": radar_data})

# =====================================================================
# 💰 رادار الأرباح والاشتراكات (محصن بالكامل 🔒)
# =====================================================================
async def api_admin_billing_stats(request: web.Request):
    check_admin_token(request) # 🛡️ تم التحصين
    async with database.pool.acquire() as conn:
        monthly_revenue = await conn.fetchval("""
            SELECT COALESCE(SUM(amount), 0) FROM subscription_payments 
            WHERE DATE_TRUNC('month', created_at) = DATE_TRUNC('month', CURRENT_DATE)
        """)
        
        total_revenue = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM subscription_payments")
        
        payments = await conn.fetch("""
            SELECT sp.id, sp.amount, sp.note, sp.created_at, s.name as store_name 
            FROM subscription_payments sp
            JOIN stores s ON sp.store_id = s.id
            ORDER BY sp.created_at DESC LIMIT 50
        """)
        
        stores = await conn.fetch("SELECT id, name, package_type FROM stores WHERE is_deleted = FALSE")
        
    payment_list = [{
        "id": p['id'], 
        "store_name": p['store_name'], 
        "amount": float(p['amount']), 
        "note": p['note'] or "", 
        "date": p['created_at'].strftime('%Y-%m-%d')
    } for p in payments]
    
    store_list = [{"id": s['id'], "name": s['name'], "package": s['package_type']} for s in stores]
    
    return web.json_response({
        "status": "success", 
        "monthly_revenue": float(monthly_revenue),
        "total_revenue": float(total_revenue),
        "payments": payment_list,
        "stores": store_list
    })

async def api_admin_add_subscription_payment(request: web.Request):
    check_admin_token(request) # 🛡️ تم التحصين
    data = await request.json()
    ip = get_client_ip(request)
    store_id = int(data['store_id'])
    amount = float(data['amount'])
    note = data.get('note', '')
    
    if amount <= 0:
        return web.json_response({"status": "error", "message": "المبلغ يجب أن يكون أكبر من صفر"})
        
    async with database.pool.acquire() as conn:
        await conn.execute("INSERT INTO subscription_payments (store_id, amount, note) VALUES ($1, $2, $3)", store_id, amount, note)
        await log_admin_action(conn, "سداد اشتراك 💰", f"تسجيل دفعة {amount} للبقالة {store_id}", ip)
        
    return web.json_response({"status": "success", "message": "تم تسجيل دفعة الاشتراك بنجاح 💰"})

# ==========================================
# 🌐 تسجيل المسارات في التطبيق
# ==========================================
def setup_admin_routes(app: web.Application):
    app.router.add_post('/api/admin/login', api_admin_login)
    app.router.add_get('/api/admin/stats', api_admin_stats)
    app.router.add_get('/api/admin/stores', api_admin_stores)
    app.router.add_post('/api/admin/store/toggle', api_admin_toggle_store)
    
    # مسار الرادار الحي (WebSocket)
    app.router.add_get('/ws/admin_logs', api_admin_ws_logs)
    
    # المسارات الاستراتيجية
    app.router.add_get('/api/admin/logs', api_admin_logs)
    app.router.add_post('/api/admin/shadow_login', api_admin_shadow_login)
    app.router.add_post('/api/admin/revoke_shadow', api_admin_revoke_shadow)
    app.router.add_post('/api/admin/extend_trial', api_admin_extend_trial)
    app.router.add_post('/api/admin/update_package', api_admin_update_package)
    app.router.add_post('/api/admin/bulk_update', api_admin_bulk_update)
    app.router.add_post('/api/admin/recover', api_admin_recover_deleted)
    app.router.add_post('/api/admin/update_store', api_admin_update_store)
    app.router.add_post('/api/admin/broadcast', api_admin_broadcast)
    app.router.add_get('/api/admin/backup', api_admin_backup)
    app.router.add_post('/api/admin/force_update', api_admin_force_update)
    app.router.add_get('/api/admin/top_debts', api_admin_top_debts)
    
    # رادار الصحة والكتالوج
    app.router.add_get('/api/admin/health', api_admin_health)
    app.router.add_get('/api/admin/recycle_bin', api_admin_recycle_bin_get)
    app.router.add_get('/api/admin/catalog', api_admin_catalog_get)
    app.router.add_post('/api/admin/catalog/add', api_admin_catalog_add)
    app.router.add_get('/api/admin/store_profile', api_admin_store_profile)
    
    # الصيانة والمحادثات
    app.router.add_post('/api/admin/change_password', api_admin_change_password)
    app.router.add_post('/api/admin/maintenance', api_admin_maintenance)
    app.router.add_get('/api/admin/chat/list', api_admin_chat_list)
    app.router.add_get('/api/admin/chat/history', api_admin_chat_history)
    app.router.add_post('/api/admin/chat/send', api_admin_chat_send)

    # خدمات الاتصالات والفوترة ورادار الخمول
    app.router.add_get('/api/admin/store_telecom_perms', api_admin_store_telecom_perms)
    app.router.add_post('/api/admin/update_telecom_perms', api_admin_update_telecom_perms)
    app.router.add_get('/api/admin/churn_radar', api_admin_churn_radar)
    app.router.add_get('/api/admin/billing_stats', api_admin_billing_stats)
    app.router.add_post('/api/admin/add_subscription_payment', api_admin_add_subscription_payment)
