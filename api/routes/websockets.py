import json
import logging
import asyncio
from aiohttp import web
import redis.asyncio as redis
import jwt
from config import REDIS_URL, JWT_SECRET
import database
from api.middlewares import validate_telegram_data

# قواميس لحفظ الاتصالات الحية
connected_stores = {}    # {store_id: [ws1, ws2]}
connected_customers = {} # {customer_id: [ws1, ws2]}
redis_client = None

async def start_redis_listener(app ):
    """تهيئة رديس وتشغيل مستمع اللاسلكي كمهمة خلفية (محصن ضد الانقطاع)"""
    global redis_client
    if not REDIS_URL:
        logging.warning("⚠️ لم يتم العثور على REDIS_URL، الإشعارات الحية ستعمل على الذاكرة المحلية فقط.")
        return

    redis_client = redis.from_url(REDIS_URL)
    
    async def listen_loop():
        # 🛡️ حلقة لا نهائية تضمن عودة المستمع للحياة إذا انقطع الاتصال
        while True:
            pubsub = None
            try:
                # 🛡️ إنشاء الـ pubsub والاشتراك داخل الحلقة لضمان تجديدهما بعد أي انقطاع
                pubsub = redis_client.pubsub()
                await pubsub.subscribe("dukkani_events")
                logging.info("📡 تم الاتصال بـ Redis بنجاح وبدء الاستماع للإشعارات...")
                
                async for message in pubsub.listen():
                    if message["type"] == "message":
                        try:
                            data = json.loads(message["data"])
                            event_type = data.get("type")
                            
                            if event_type in ["new_order", "new_chat_message"]:
                                store_id_val = data.get("store_id")
                                if store_id_val is not None:
                                    store_id = int(store_id_val)
                                    if store_id in connected_stores:
                                        msg_str = json.dumps({"type": event_type, "data": data.get("data")})
                                        # 🛡️ استخدام list لمنع خطأ تغيير حجم المصفوفة أثناء التكرار
                                        for ws in list(connected_stores[store_id]):
                                            try: 
                                                await ws.send_str(msg_str)
                                            except Exception: 
                                                pass
                                            
                            elif event_type == "customer_update":
                                cust_id_val = data.get("customer_id")
                                if cust_id_val is not None:
                                    customer_id = int(cust_id_val)
                                    if customer_id in connected_customers:
                                        msg_str = json.dumps({"type": "order_update"})
                                        # 🛡️ استخدام list لحماية الاتصالات الحية
                                        for ws in list(connected_customers[customer_id]):
                                            try: 
                                                await ws.send_str(msg_str)
                                            except Exception: 
                                                pass

                            elif event_type == "admin_log":
                                # بث الحدث لشاشات الإدارة المتصلة بالرادار
                                try:
                                    from api.routes.admin import ADMIN_WEBSOCKETS
                                except ImportError:
                                    from admin import ADMIN_WEBSOCKETS

                                msg_str = json.dumps({"type": "new_log", "data": data.get("data")})
                                for ws in list(ADMIN_WEBSOCKETS):
                                    try:
                                        await ws.send_str(msg_str)
                                    except Exception:
                                        pass

                        except Exception as e:
                            logging.error(f"Redis Data Parse Error: {e}")

            except asyncio.CancelledError:
                if pubsub:
                    try:
                        await pubsub.unsubscribe("dukkani_events")
                        await pubsub.close()
                    except Exception:
                        pass
                await redis_client.close()
                logging.info("🛑 تم إغلاق اتصال Redis بأمان.")
                break # خروج آمن فقط عند إغلاق السيرفر

            except Exception as e:
                logging.error(f"⚠️ انقطع اتصال Redis، سيتم إعادة المحاولة بعد 3 ثوانٍ... الخطأ: {e}")
                if pubsub:
                    try:
                        await pubsub.close()
                    except Exception:
                        pass
                await asyncio.sleep(3)

    app['redis_listener_task'] = asyncio.create_task(listen_loop())

async def websocket_handler(request: web.Request):
    """مسار شاشة الطلبات الحية للبقالة (محصن ضد الانهيار وتجمد الذاكرة)"""
    ws = web.WebSocketResponse(heartbeat=15.0)    
    await ws.prepare(request)
    
    store_id = request.query.get('store_id')
    token = request.query.get('token') or request.cookies.get('store_token')
    auth_type = request.query.get('auth_type', 'jwt')
    
    if not store_id or not str(store_id).isdigit():
        await ws.close()
        return ws
        
    store_id = int(store_id)
    is_authorized = False
    
    try:
        async with database.pool.acquire() as conn:        
            # 🛡️ تحويل ذكي: إذا كان الرقم أكبر من 2 مليار، فهو آيدي تيليجرام ويجب جلب id المتجر الحقيقي
            if store_id > 2147483647:
                real_store = await conn.fetchrow("SELECT id FROM stores WHERE telegram_id = $1 AND is_deleted = FALSE", store_id)
            else:
                real_store = await conn.fetchrow("SELECT id FROM stores WHERE (id = $1 OR telegram_id = $1) AND is_deleted = FALSE", store_id)

            if real_store:
                store_id = real_store['id']  # توحيد المتجر بالـ ID الصغير ليعمل الرادار اللحظي 100%

            # 🛡️ التحقق من الصلاحيات والتوكن
            if not token:
                if real_store:
                    is_authorized = True
            elif auth_type == 'tma':
                user_data = validate_telegram_data(token)
                if user_data and 'id' in user_data:
                    u_store_id = await conn.fetchval("SELECT id FROM stores WHERE telegram_id = $1", user_data['id'])
                    if not u_store_id:
                        u_store_id = await conn.fetchval("SELECT store_id FROM workers WHERE telegram_id = $1", user_data['id'])
                    if u_store_id == store_id:
                        is_authorized = True
            else:
                payload = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
                jti = payload.get('jti')
                is_blacklisted = False
                if jti:
                    is_blacklisted = await conn.fetchval("SELECT 1 FROM blacklisted_tokens WHERE jti = $1", jti)
                if not is_blacklisted and (payload.get('role') == 'super_admin' or payload.get('store_id') == store_id):
                    is_authorized = True
    except Exception as e:
        logging.error(f"Websocket Auth DB Error: {e}")
        await ws.close()
        return ws

    if not is_authorized:
        await ws.close()
        return ws
    
    # تأمين وجود القائمة في التطبيق
    if 'websockets' not in request.app:
        request.app['websockets'] = set()
        
    request.app['websockets'].add(ws)
    if store_id not in connected_stores: 
        connected_stores[store_id] = []
    connected_stores[store_id].append(ws)
    
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                if 'ping' in msg.data:
                    await ws.send_str('{"type": "pong"}')
    finally:
        # 🛡️ حذف آمن يمنع خطأ ValueError الذي يعلق السيرفر
        if store_id in connected_stores and ws in connected_stores[store_id]:
            connected_stores[store_id].remove(ws)
            if not connected_stores[store_id]: 
                del connected_stores[store_id]
        if 'websockets' in request.app:
            request.app['websockets'].discard(ws)
        
    return ws

async def customer_websocket_handler(request: web.Request):
    """مسار الرادار الحي للزبون (محصن)"""
    ws = web.WebSocketResponse(heartbeat=15.0) # 🛡️ تم تقليلها لـ 15 لتطابق الكاشير وتناسب Render
    await ws.prepare(request)
    
    token = request.query.get('token')
    if not token:
        await ws.close()
        return ws
        
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
        customer_id = payload.get('customer_id')
        if not customer_id: 
            raise ValueError()
        customer_id = int(customer_id) # 🛡️ تحويل إجباري لـ Integer لضمان وصول الإشعارات
    except Exception:
        await ws.close()
        return ws
        
    if 'websockets' not in request.app:
        request.app['websockets'] = set()
        
    request.app['websockets'].add(ws)
    if customer_id not in connected_customers: 
        connected_customers[customer_id] = []
    connected_customers[customer_id].append(ws)
    
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                if 'ping' in msg.data:
                    await ws.send_str('{"type": "pong"}')
    finally:
        # 🛡️ حذف آمن
        if customer_id in connected_customers and ws in connected_customers[customer_id]:
            connected_customers[customer_id].remove(ws)
            if not connected_customers[customer_id]: 
                del connected_customers[customer_id]
        if 'websockets' in request.app:
            request.app['websockets'].discard(ws)
        
    return ws

# --- دوال النشر (Publishers) ---
async def notify_store_new_order(store_id: int, order_data: dict):
    if redis_client:
        await redis_client.publish("dukkani_events", json.dumps({"type": "new_order", "store_id": store_id, "data": order_data}))
    elif store_id in connected_stores:
        msg_str = json.dumps({"type": "new_order", "data": order_data})
        for ws in list(connected_stores[store_id]):
            try: 
                await ws.send_str(msg_str)
            except Exception: 
                pass

async def notify_store_new_chat_message(store_id: int, chat_data: dict):
    if redis_client:
        await redis_client.publish("dukkani_events", json.dumps({"type": "new_chat_message", "store_id": store_id, "data": chat_data}))
    elif store_id in connected_stores:
        msg_str = json.dumps({"type": "new_chat_message", "data": chat_data})
        for ws in list(connected_stores[store_id]):
            try: 
                await ws.send_str(msg_str)
            except Exception: 
                pass

async def notify_customer_order_update(customer_id: int):
    """إشعار الزبون بتحديث حالة طلبه"""
    if redis_client:
        await redis_client.publish("dukkani_events", json.dumps({"type": "customer_update", "customer_id": customer_id}))
    elif customer_id in connected_customers:
        msg_str = json.dumps({"type": "order_update"})
        for ws in list(connected_customers[customer_id]):
            try: 
                await ws.send_str(msg_str)
            except Exception: 
                pass

async def notify_admin_new_log(log_data: dict):
    """بث أحداث النظام الحية للوحة القيادة العليا (Super Admin)"""
    if redis_client:
        try:
            await redis_client.publish("dukkani_events", json.dumps({"type": "admin_log", "data": log_data}))
        except Exception as e:
            logging.error(f"Redis Admin Log Publish Error: {e}")

async def stop_redis_listener(app: web.Application):
    """إلغاء مهمة استماع Redis بأمان عند إغلاق التطبيق"""
    task = app.get('redis_listener_task')
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

def setup_websocket_routes(app: web.Application):
    # 🛡️ تهيئة قائمة الويب سوكيت مبكراً
    app.setdefault('websockets', set())
    
    app.router.add_get('/ws/live_orders', websocket_handler)
    app.router.add_get('/ws/customer_updates', customer_websocket_handler)
    app.on_startup.append(start_redis_listener)
    app.on_cleanup.append(stop_redis_listener)
