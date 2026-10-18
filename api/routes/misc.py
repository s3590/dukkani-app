import os
import time
import json
import html
import asyncio
import logging
from collections import OrderedDict

import aiohttp
from aiohttp import web
import jwt

import database
from bot.setup import bot
from config import ADMIN_ID, BOT_TOKEN, JWT_SECRET
from api.middlewares import get_user_from_token

# =================================================================
# 🛡️ 1. نظام المهام الآمنة في الخلفية (يمنع السيرفر من حذف المهام )
# =================================================================
background_tasks = set()
def create_safe_task(coro):
    task = asyncio.create_task(coro)
    background_tasks.add(task)
    
    def handle_task_result(t):
        background_tasks.discard(t)
        try:
            # 🛡️ تسجيل الخطأ إذا فشلت المهمة في الخلفية بدلاً من الصمت
            exc = t.exception()
            if exc:
                logging.error(f"⚠️ خطأ في مهمة الخلفية: {exc}")
        except asyncio.CancelledError:
            pass
            
    task.add_done_callback(handle_task_result)
    return task

# =================================================================
# 🛡️ 2. دورة حياة التطبيق (إدارة اتصالات الشبكة والملفات بذكاء)
# =================================================================
async def on_startup(app: web.Application):
    """فتح جلسة شبكة موحدة وقراءة الملفات الثابتة في الخلفية"""
    app['http_session'] = aiohttp.ClientSession( )
    
    # قراءة ملف Service Worker عند بدء التشغيل باستخدام Thread لمنع توقف السيرفر
    global FIREBASE_SW_CACHE
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sw_path = os.path.join(base_dir, 'firebase-messaging-sw.js')
    
    def load_sw():
        if os.path.exists(sw_path):
            with open(sw_path, 'r', encoding='utf-8') as f:
                return f.read()
        return None
        
    try:
        FIREBASE_SW_CACHE = await asyncio.to_thread(load_sw)
    except Exception as e:
        logging.error(f"⚠️ خطأ في تحميل ملف Service Worker: {e}")

async def on_cleanup(app: web.Application):
    """إغلاق الجلسة بنظافة وانتظار المهام المتبقية لتكتمل بأمان"""
    # 1. إعطاء المهام في الخلفية فرصة لتكتمل قبل إغلاق السيرفر (لمدة أقصاها 3 ثوانٍ)
    if background_tasks:
        logging.info(f"⏳ جاري انتظار اكتمال {len(background_tasks)} مهام في الخلفية...")
        await asyncio.wait(background_tasks, timeout=3.0)
        
    # 2. إغلاق جلسة الشبكة بعد اكتمال المهام
    if 'http_session' in app:
        await app['http_session'].close( )

# =================================================================
# 🛡️ 3. الذاكرة المؤقتة الذكية
# =================================================================
# كاش للصور (LRU) منظم يمنع تسريب الرام تماماً
IMAGE_CACHE = OrderedDict()
MAX_CACHE_SIZE = 50  # تقليل العدد لحماية الرام في سيرفر Render
CACHE_TTL = 3600  # 60 minutes

# كاش لملف السيرفيس وركر لمنع حظر السيرفر
FIREBASE_SW_CACHE = None

# =================================================================
# 🚀 4. مسارات الـ API
# =================================================================
async def serve_telegram_image(request: web.Request):
    file_id = request.match_info.get('file_id')
    if not file_id or file_id == 'null':
        return web.HTTPFound('/static/logo_customer.png')
        
    current_time = time.time()
    
    # 1. التحقق من الذاكرة المؤقتة
    if file_id in IMAGE_CACHE:
        cached_data, content_type, timestamp = IMAGE_CACHE[file_id]
        if current_time - timestamp < CACHE_TTL:
            # 🛡️ تحديث موقع العنصر (LRU) قبل أي شيء حتى للصور المكسورة لمنع حذفها مبكراً
            IMAGE_CACHE.move_to_end(file_id)
            
            # 🛡️ كاش الأخطاء: إذا كانت البيانات None يعني أن الصورة مكسورة
            if cached_data is None:
                return web.HTTPFound('/static/logo_customer.png')
                
            headers = {'Cache-Control': 'public, max-age=31536000, immutable'}
            return web.Response(body=cached_data, content_type=content_type, headers=headers)
        else:
            del IMAGE_CACHE[file_id]
            
    try:
        file = await bot.get_file(file_id)
        if not file or not getattr(file, 'file_path', None):
            raise ValueError("File path is missing or file is too large.")

        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
        session = request.app.get('http_session')
        if not session or session.closed:
            session = aiohttp.ClientSession()
        
        async with session.get(file_url) as resp:
            if resp.status == 200:
                data = await resp.read()
                content_type = resp.headers.get('Content-Type', 'image/jpeg')
                
                headers = {'Cache-Control': 'public, max-age=31536000, immutable'}
                
                # 🛡️ حماية الرام: لا تحفظ الصور التي يزيد حجمها عن 2 ميجابايت في الذاكرة
                if len(data) > 2 * 1024 * 1024:
                    return web.Response(body=data, content_type=content_type, headers=headers)
                
                if len(IMAGE_CACHE) >= MAX_CACHE_SIZE:
                    IMAGE_CACHE.popitem(last=False)
                    
                IMAGE_CACHE[file_id] = (data, content_type, current_time)
                return web.Response(body=data, content_type=content_type, headers=headers)
            else:
                raise ValueError(f"Telegram returned status {resp.status}")
                
    except Exception as e:
        logging.error(f"Image fetch error for {file_id}: {e}")
        # 🛡️ كاش الأخطاء السلبي
        if len(IMAGE_CACHE) >= MAX_CACHE_SIZE:
            IMAGE_CACHE.popitem(last=False)
        IMAGE_CACHE[file_id] = (None, None, current_time) # حفظ وقت الفشل الحالي بدلاً من الماضي
        
    return web.HTTPFound('/static/logo_customer.png')

# قاموس لمنع إغراق البوت برسائل الأخطاء (Rate Limiting)
ERROR_RATE_LIMIT = {}

async def api_log_error(request: web.Request):
    # 🛡️ قراءة الـ IP الحقيقي سواء كان الاتصال مباشراً أو عبر Nginx / Cloudflare
    client_ip = request.headers.get('CF-Connecting-IP') or \
                request.headers.get('X-Forwarded-For') or \
                request.headers.get('X-Real-IP') or \
                request.remote or "unknown_ip"
                
    # إذا كان هناك عدة عناوين (سلسلة من الوكلاء)، نأخذ الأول (الأصلي)
    if ',' in client_ip:
        client_ip = client_ip.split(',')[0].strip()
        
    current_time = time.time()
    
    # 🛡️ حماية ضد الإغراق: السماح بخطأ واحد كل 10 ثوانٍ لكل IP
    if client_ip in ERROR_RATE_LIMIT and current_time - ERROR_RATE_LIMIT[client_ip] < 10:
        return web.json_response({"status": "rate_limited"})
    ERROR_RATE_LIMIT[client_ip] = current_time

    # تنظيف العناوين القديمة من القاموس لحماية الرام (كل 1000 طلب)
    if len(ERROR_RATE_LIMIT) > 1000:
        ERROR_RATE_LIMIT.clear()

    try:
        data = await request.json()
        err_msg = data.get('error', 'Unknown error')
        user_info = f"مجهول / زائر (IP: {client_ip})"
        
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            parts = auth_header.split(' ')
            if len(parts) == 2: 
                token = parts[1]
                try:
                    # 🛡️ تفعيل التحقق من التوقيع لمنع اختراق الهوية
                    user = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
                    if 'customer_id' in user:
                        user_info = f"🛒 زبون رقم: {user['customer_id']} | بقالة: {user.get('store_id', 'غير معروف')}"
                    elif 'worker_id' in user:
                        user_info = f"👷‍♂️ عامل رقم: {user['worker_id']} | بقالة: {user.get('store_id', 'غير معروف')}"
                    elif 'store_id' in user:
                        user_info = f"🏪 صاحب بقالة رقم: {user['store_id']}"
                except jwt.ExpiredSignatureError:
                    user_info = "مستخدم بتوكن منتهي الصلاحية"
                except jwt.InvalidTokenError:
                    user_info = "مستخدم بتوكن مزيف أو غير صالح!"
        
        safe_err_msg = html.escape(str(err_msg)[:1500])
        msg = (
            f"📱 <b>خطأ في واجهة التطبيق (Frontend):</b>\n"
            f"👤 <b>المستخدم:</b> {user_info}\n\n"
            f"<pre>{safe_err_msg}</pre>"
        )
        create_safe_task(bot.send_message(ADMIN_ID, msg, parse_mode="HTML"))
        
        async with database.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO admin_logs (action, details) VALUES ($1, $2)", 
                "خطأ واجهة 📱", 
                f"المستخدم: {user_info}\n{str(err_msg)[:150]}"
            )
            
    except Exception as e: 
        logging.error(f"Log error handler failed: {e}")
        
    return web.json_response({"status": "logged"})

async def serve_firebase_sw(request: web.Request):
    """إرجاع ملف Service Worker بسرعة فائقة مع آلية إنقاذ من الـ 404"""
    global FIREBASE_SW_CACHE
    if not FIREBASE_SW_CACHE:
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        sw_path = os.path.join(base_dir, 'firebase-messaging-sw.js')
        if os.path.exists(sw_path):
            try:
                with open(sw_path, 'r', encoding='utf-8') as f:
                    FIREBASE_SW_CACHE = f.read()
            except Exception:
                pass
                
    if not FIREBASE_SW_CACHE:
        return web.Response(text="// Service Worker Ready", content_type='application/javascript', status=200)
        
    return web.Response(text=FIREBASE_SW_CACHE, content_type='application/javascript')

async def api_save_subscription(request: web.Request):
    user = get_user_from_token(request)
    if not user:
        return web.json_response({"error": "Unauthorized"}, status=401)
        
    # 🛡️ حماية السيرفر من الانهيار إذا كانت البيانات المرسلة غير صالحة
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
        
    token = data.get("subscription")
    
    if isinstance(token, dict):
        token = json.dumps(token)
    
    # 🛡️ التحقق من المستخدم والتوكن
    if token and user:
        user_id = None
        user_type = 'customer'
        
        if 'customer_id' in user:
            user_id = user['customer_id']
            user_type = 'customer'
        elif 'agent_id' in user:
            user_id = user['agent_id']
            user_type = 'agent'
        elif 'store_id' in user:
            user_id = user.get('worker_id') or user['store_id']
            user_type = 'worker' if 'worker_id' in user else 'store'
            
        if user_id:
            try:
                async with database.pool.acquire() as conn:
                    # 🛡️ الحذف والتحديث الذري لمنع خطأ UniqueViolationError نهائياً
                    async with conn.transaction():
                        await conn.execute("DELETE FROM push_subscriptions WHERE subscription_json = $1", token)
                        
                        await conn.execute("""
                            INSERT INTO push_subscriptions (user_id, user_type, subscription_json)
                            VALUES ($1, $2, $3)
                            ON CONFLICT (user_id) 
                            DO UPDATE SET user_type = EXCLUDED.user_type, subscription_json = EXCLUDED.subscription_json
                        """, user_id, user_type, token)
            except Exception as e:
                logging.error(f"⚠️ خطأ في حفظ اشتراك الإشعارات: {e}")
                return web.json_response({"error": "Database error", "details": str(e)}, status=500)

    return web.json_response({"status": "success"})

async def serve_assetlinks(request: web.Request):
    assetlinks_data = [{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {
            "namespace": "android_app",
            "package_name": "com.dukkani.app",
            "sha256_cert_fingerprints": ["9F:39:7F:09:D9:1F:13:EA:1E:53:65:EB:D5:C7:FF:BF:FF:D4:A4:0C:8A:F8:88:30:E3:58:CD:65:43:38:18:7D"]
        }
    }]
    # 🛡️ إخبار جوجل والمتصفحات بحفظ الملف لديهم لمدة 24 ساعة لتقليل الضغط
    headers = {'Cache-Control': 'public, max-age=86400'}
    return web.json_response(assetlinks_data, headers=headers)

# كاش لتخزين بيانات المتاجر لملف الـ Manifest (يمنع الضغط على قاعدة البيانات)
MANIFEST_CACHE = {}

async def serve_dynamic_manifest(request: web.Request):
    store_id = request.query.get('store_id')
    current_time = time.time()
    
    app_name = "دُكّاني"
    theme_color = "#0ba360"
    logo_url = "/static/logo_customer.png"
    start_url = "/"
    
    if store_id and store_id.isdigit():
        store_id_int = int(store_id)
        start_url = f"/?store={store_id}"
        
        # 1. التحقق من وجود بيانات المتجر في الكاش (البيانات صالحة لمدة ساعة)
        if store_id_int in MANIFEST_CACHE and current_time - MANIFEST_CACHE[store_id_int]['time'] < 3600:
            cached_store = MANIFEST_CACHE[store_id_int]
            app_name = cached_store['name']
            theme_color = cached_store['theme_color']
            logo_url = cached_store['logo_url']
        else:
            # 2. إذا لم تكن في الكاش، جلبها من الداتابيز وحفظها
            try:
                async with database.pool.acquire() as conn:
                    store = await conn.fetchrow("SELECT name, theme_color, logo_url FROM stores WHERE id = $1", store_id_int)
                    if store:
                        app_name = store['name'] or app_name
                        theme_color = store['theme_color'] or theme_color
                        
                        raw_logo = store['logo_url']
                        if raw_logo:
                            logo_url = raw_logo if "/" in raw_logo else f"/api/image/{raw_logo}"
                            
                        # حماية الرام: تنظيف الكاش إذا زاد عدد المتاجر عن 1000
                        if len(MANIFEST_CACHE) > 1000:
                            MANIFEST_CACHE.clear()
                            
                        # حفظ في الكاش
                        MANIFEST_CACHE[store_id_int] = {
                            'name': app_name,
                            'theme_color': theme_color,
                            'logo_url': logo_url,
                            'time': current_time
                        }
            except Exception as e:
                logging.error(f"Manifest DB Error: {e}")

    manifest = {
        "id": f"dukkani-store-{store_id if (store_id and store_id.isdigit()) else 'default'}",
        "name": app_name,
        "short_name": app_name,
        "description": f"اطلب مقاضيك بسهولة من {app_name}",
        "start_url": start_url,
        "scope": "/",
        "display": "standalone",
        "background_color": "#f8fafc",
        "theme_color": theme_color,
        "orientation": "portrait",
        "dir": "rtl",
        "lang": "ar",
        "categories": ["shopping", "finance", "lifestyle"],
        "icons": [
            {"src": logo_url, "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": logo_url, "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": logo_url, "sizes": "512x512", "type": "image/png", "purpose": "maskable"}
        ]
    }
    
    return web.json_response(manifest, content_type='application/manifest+json')

def setup_misc_routes(app: web.Application):
    # 🛡️ ربط أحداث التشغيل والإيقاف بالتطبيق
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    
    app.router.add_get('/api/image/{file_id}', serve_telegram_image)
    app.router.add_post('/api/log_error', api_log_error)
    app.router.add_get('/firebase-messaging-sw.js', serve_firebase_sw)
    app.router.add_post('/api/subscribe', api_save_subscription)
    app.router.add_get('/.well-known/assetlinks.json', serve_assetlinks)
    app.router.add_get('/api/manifest', serve_dynamic_manifest)
