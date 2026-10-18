# مسار الملف: api/middlewares.py
from aiohttp import web
import jwt
import time
import traceback
import logging
import urllib.parse
import hashlib
import hmac
import json
import os

# 🚀 محاولة استيراد مكتبة رديس للاتصال السحابي
try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    try:
        import aioredis as redis # دعم للإصدارات القديمة
        REDIS_AVAILABLE = True
    except ImportError:
        REDIS_AVAILABLE = False

from config import JWT_SECRET, ADMIN_ID, BOT_TOKEN, REDIS_URL
from bot.setup import bot
import database

# 🚀 تهيئة عميل رديس المركزي (Distributed Cache )
redis_client = redis.from_url(REDIS_URL, decode_responses=True) if REDIS_AVAILABLE and REDIS_URL else None

# ذاكرة احتياطية (Fallback) في حال كان رديس غير متاح
MAINTENANCE_CACHE = {"status": False, "last_checked": 0}
RATE_LIMIT_CACHE = {}
CACHE_TTL = 15 
MAX_ATTEMPTS = 5
BLOCK_TIME = 900 # 15 دقيقة

def get_user_from_token(request: web.Request):
    """دالة مساعدة لاستخراج بيانات الزبون (بأمان تام يمنع إزعاج الإدارة بأخطاء وهمية)"""
    if 'user' in request:
        return request['user']
    auth_header = request.headers.get('Authorization')
    if not auth_header: 
        raise web.HTTPUnauthorized(text='{"status": "error", "message": "غير مصرح"}', content_type='application/json')
    
    parts = auth_header.split(' ')
    if len(parts) != 2 or parts[0].lower() != 'bearer':
        raise web.HTTPUnauthorized(text='{"status": "error", "message": "صيغة التوكن غير صحيحة"}', content_type='application/json')
        
    token = parts[1]
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
    except jwt.ExpiredSignatureError:
        # 🛡️ إرجاع 401 صامت للواجهة الأمامية لتقوم بتسجيل الخروج دون إزعاج السيرفر
        raise web.HTTPUnauthorized(text='{"status": "error", "message": "انتهت الجلسة"}', content_type='application/json')
    except Exception:
        raise web.HTTPUnauthorized(text='{"status": "error", "message": "توكن غير صالح"}', content_type='application/json')

def validate_telegram_data(init_data: str) -> dict:
    """دالة للتحقق من صحة بيانات تيليجرام (initData) القادمة من الواجهة الأمامية"""
    try:
        parsed_data = dict(urllib.parse.parse_qsl(init_data))
        if 'hash' not in parsed_data: return None
        
        # 🛡️ حماية Replay Attack: التحقق من أن البيانات ليست قديمة (أكثر من 24 ساعة)
        auth_date = int(parsed_data.get('auth_date', 0))
        if time.time() - auth_date > 86400:
            logging.warning("⚠️ محاولة استخدام بيانات تيليجرام منتهية الصلاحية!")
            return None
            
        received_hash = parsed_data.pop('hash')
        data_check_string = '\n'.join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if calculated_hash == received_hash:
            return json.loads(parsed_data.get('user', '{}'))
        return None
    except Exception as e:
        logging.error(f"Telegram Auth Error: {e}")
        return None

@web.middleware
async def maintenance_middleware(request: web.Request, handler):
    """التحقق من وضع الصيانة (بأمان تام ضد انقطاع رديس)"""
    if not request.path.startswith('/api/') or request.path.startswith('/api/admin'):
        return await handler(request)
        
    is_maintenance = False
    redis_failed = False
    
    if redis_client:
        try:
            cached_status = await redis_client.get("system:maintenance")
            if cached_status is not None:
                is_maintenance = (cached_status == "1")
            else:
                async with database.pool.acquire() as conn:
                    db_status = await conn.fetchval("SELECT maintenance_mode FROM system_settings WHERE id = 1")
                    is_maintenance = bool(db_status)
                    await redis_client.setex("system:maintenance", CACHE_TTL, "1" if is_maintenance else "0")
        except Exception as e:
            # 🛡️ إذا انقطع رديس، نتجاهل الخطأ ونستخدم الذاكرة المحلية
            logging.warning(f"⚠️ انقطاع اتصال Redis في الصيانة: {e}")
            redis_failed = True

    if not redis_client or redis_failed:
        # 🛡️ نظام احتياطي (الذاكرة المحلية) يعمل فوراً إذا تعطل رديس
        current_time = time.time()
        if current_time - MAINTENANCE_CACHE["last_checked"] > CACHE_TTL:           
                try:
                    if database.pool:
                        async with database.pool.acquire() as conn:
                            MAINTENANCE_CACHE["status"] = await conn.fetchval("SELECT maintenance_mode FROM system_settings WHERE id = 1")
                        MAINTENANCE_CACHE["last_checked"] = current_time
                except: pass
        is_maintenance = MAINTENANCE_CACHE.get("status", False)
            
    if is_maintenance:
        return web.json_response({
            "status": "error", 
            "message": "النظام تحت الصيانة والتحديث حالياً 🛠️. يرجى المحاولة بعد قليل."
        }, status=503)
        
    return await handler(request)

@web.middleware
async def auth_and_shadow_middleware(request: web.Request, handler):
    """نظام المصادقة المزدوج (JWT + Telegram Web App) مع حماية وضع الظل"""
    if not request.path.startswith('/api/'):
        return await handler(request)

    auth_header = request.headers.get('Authorization', '')
    is_manage_route = request.path.startswith('/api/manage/')
    auth_success = False

    if auth_header.startswith('Bearer '):
        token = auth_header[7:]
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
            request['user'] = payload
            
            role = payload.get('role', 'admin')
            # 🛡️ في وضع الظل (store_owner)، نمنح الطلب صلاحية المدير admin ليعرف السيرفر أنه وضع ظل
            if role == 'store_owner':
                request['auth_role'] = 'admin'
                request['safe_telegram_id'] = payload.get('store_id')
            else:
                request['auth_role'] = role
                request['safe_telegram_id'] = payload.get('admin_id') or payload.get('agent_id') or payload.get('store_id')
            
            # التحقق من القائمة السوداء لتوكنات الظل
            if role == 'store_owner' and 'jti' in payload:
                async with database.pool.acquire() as conn:
                    is_blacklisted = await conn.fetchval("SELECT 1 FROM blacklisted_tokens WHERE jti = $1", payload['jti'])
                    if is_blacklisted:
                        return web.json_response({"status": "error", "message": "تم إنهاء جلسة الدعم الفني (الظل) 🛑"}, status=401)
            auth_success = True
        except Exception:
            pass 

    elif auth_header.startswith('tma '):
        init_data = auth_header[4:]
        user_data = validate_telegram_data(init_data)
        if user_data and 'id' in user_data:
            request['safe_telegram_id'] = user_data['id']
            request['auth_role'] = 'store_user'
            auth_success = True

    if is_manage_route and not auth_success:
        return web.json_response({"status": "error", "message": "غير مصرح: بيانات المصادقة مفقودة أو غير صالحة"}, status=401)
        
    return await handler(request)

@web.middleware
async def rate_limit_middleware(request: web.Request, handler):
    """نظام حماية التخمين (Rate Limiting) الاحترافي المدعوم بـ Redis"""
    if request.path.endswith('/login'):
        ip = request.headers.get('X-Forwarded-For', request.remote)
        if ip: ip = ip.split(',')[0].strip()
        
        if not ip: return await handler(request)
        
        now = time.time()
        redis_worked = False
        
        # 🟢 المسار الأول: إذا كان رديس متصلاً (وهو الأفضل)
        if redis_client:
            try:
                block_key = f"block:ip:{ip}"
                attempts_key = f"attempts:ip:{ip}"
                
                # 1. التحقق من الحظر
                ttl = await redis_client.ttl(block_key)
                if ttl > 0:
                    return web.json_response({
                        "status": "error", 
                        "message": f"تم حظر عنوان IP الخاص بك. يرجى المحاولة بعد {(ttl // 60) or 1} دقيقة 🛑."
                    }, status=429)
                    
                # 2. تنفيذ الطلب
                response = await handler(request)
                
                # 3. تحليل النتيجة
                is_failed = response.status in [401, 403]
                if response.status == 200 and response.content_type == 'application/json':
                    try:
                        body = json.loads(response.text)
                        if body.get('status') == 'error': is_failed = True
                    except: pass

                if is_failed:
                    attempts = await redis_client.incr(attempts_key)
                    if attempts == 1:
                        await redis_client.expire(attempts_key, 600) # صلاحية العداد 10 دقائق
                    
                    if attempts >= MAX_ATTEMPTS:
                        await redis_client.setex(block_key, BLOCK_TIME, "1")
                        await redis_client.delete(attempts_key)
                        logging.warning(f"🚨 [أمان مركزي]: تم حظر الـ IP ({ip}) لمدة 15 دقيقة.")
                elif response.status == 200:
                    await redis_client.delete(attempts_key)
                    
                redis_worked = True
                return response
            except Exception as e:
                logging.warning(f"⚠️ انقطاع اتصال Redis في حماية التخمين: {e}")
                redis_worked = False

        # 🟡 المسار الثاني: احتياطي عبر الذاكرة (إذا تعطل رديس لسبب ما)
        if not redis_client or not redis_worked:
            if ip in RATE_LIMIT_CACHE:
                attempts, block_until = RATE_LIMIT_CACHE[ip]
                if now < block_until:
                    remaining_mins = int((block_until - now) / 60) or 1
                    return web.json_response({
                        "status": "error", 
                        "message": f"تم حظر عنوان IP الخاص بك. يرجى المحاولة بعد {remaining_mins} دقيقة 🛑."
                    }, status=429)
                elif now > block_until and block_until > 0:
                    RATE_LIMIT_CACHE.pop(ip, None)
                    
            response = await handler(request)
            
            is_failed = response.status in [401, 403]
            if response.status == 200 and response.content_type == 'application/json':
                try:
                    body = json.loads(response.text)
                    if body.get('status') == 'error': is_failed = True
                except: pass

            if is_failed:
                attempts, _ = RATE_LIMIT_CACHE.get(ip, (0, 0))
                attempts += 1
                if attempts >= MAX_ATTEMPTS:
                    RATE_LIMIT_CACHE[ip] = (attempts, now + BLOCK_TIME)
                    logging.warning(f"🚨 [أمان محلي]: تم حظر الـ IP ({ip}) لمدة 15 دقيقة.")
                else:
                    RATE_LIMIT_CACHE[ip] = (attempts, 0)
            elif response.status == 200:
                RATE_LIMIT_CACHE.pop(ip, None)
                    
            return response
            
    return await handler(request)

@web.middleware
async def error_middleware(request: web.Request, handler):
    """صائد الأخطاء الشامل لسيرفر الويب"""
    try:
        return await handler(request)
    except web.HTTPException as ex:
        raise ex
    except Exception as e:
        import traceback
        import asyncio
        from bot.setup import bot
        from config import ADMIN_ID
        
        err_str = traceback.format_exc()
        user_info = "مجهول / غير مسجل دخول"
        
        try:
            auth_header = request.headers.get('Authorization')
            if auth_header and auth_header.startswith('Bearer '):
                token = auth_header.split(' ')[1]
                # 💡 نتخطى التحقق من التوقيع هنا فقط لكي نعرف (من هو المستخدم) الذي واجه الخطأ حتى لو انتهت جلسته
                user = jwt.decode(token, JWT_SECRET, algorithms=['HS256'], options={"verify_signature": False})
                
                if 'customer_id' in user: user_info = f"🛒 زبون: {user['customer_id']} | بقالة: {user.get('store_id')}"
                elif 'worker_id' in user: user_info = f"👷‍♂️ عامل: {user['worker_id']} | بقالة: {user.get('store_id')}"
                elif 'agent_id' in user: user_info = f"🛵 مندوب: {user['agent_id']} | بقالة: {user.get('store_id')}"
                elif 'store_id' in user: user_info = f"🏪 صاحب بقالة: {user['store_id']}"
        except: pass

        import html
        safe_trace = html.escape(err_str[:3000])
        safe_path = html.escape(request.path)
        safe_user = html.escape(str(user_info))
        
        msg = (
            f"🚨 <b>خطأ برمجي في السيرفر (Backend):</b>\n"
            f"👤 <b>المستخدم:</b> {safe_user}\n"
            f"🔗 <b>المسار:</b> <code>{safe_path}</code>\n\n"
            f"<pre>{safe_trace}</pre>"
        )
        
        try:
            asyncio.create_task(bot.send_message(ADMIN_ID, msg, parse_mode="HTML"))
        except: pass
        return web.json_response({"status": "error", "message": "حدث خطأ داخلي وتم إبلاغ الإدارة فوراً لإصلاحه 🛠️."}, status=500)
