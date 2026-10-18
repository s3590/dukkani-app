# مسار الملف: api/app.py
import os
import asyncio
import socket
from aiohttp import web
import database
from bot.setup import bot, dp

# استيراد المسارات والـ Middlewares
from api.middlewares import error_middleware, maintenance_middleware, auth_and_shadow_middleware, rate_limit_middleware, redis_client
from api.views import setup_views
from api.routes.customer import setup_customer_routes
from api.routes.pos import setup_pos_routes
from api.routes.manage import setup_manage_routes
from api.routes.misc import setup_misc_routes
from api.routes.websockets import setup_websocket_routes
from api.routes.admin import setup_admin_routes

async def run_leader_tasks(app ):
    """نظام (انتخاب القائد) لمنع تكرار المهام وتضارب البوت في سيرفرات Render 👑"""
    instance_id = socket.gethostname()  # معرف فريد للنسخة الحالية من السيرفر
    lock_key = "system:leader:lock"
    app['is_leader'] = False

    print(f"🔄 بدء نظام انتخاب القائد للنسخة: {instance_id}")

    while True:
        try:
            if redis_client:
                # محاولة حجز القفل لمدة 30 ثانية
                acquired = await redis_client.set(lock_key, instance_id, nx=True, ex=30)
                
                if not acquired:
                    # 🛡️ فك تشفير بايتات Redis إلى نص عادي لضمان نجاح المقارنة 100%
                    raw_leader = await redis_client.get(lock_key)
                    current_leader = raw_leader.decode('utf-8') if isinstance(raw_leader, bytes) else str(raw_leader or '')
                    
                    if current_leader == instance_id:
                        await redis_client.expire(lock_key, 30)
                        acquired = True
            else:
                # إذا لم يكن هناك رديس، نفترض أن هذه هي النسخة الوحيدة (للتشغيل المحلي)
                acquired = True

            # 🟢 إذا أصبحت هذه النسخة هي القائد
            if acquired and not app['is_leader']:
                app['is_leader'] = True
                print(f"👑 النسخة ({instance_id}) أصبحت القائد! جاري تشغيل البوت والمهام...")
                
                # 1. تشغيل بوت التيليجرام (هنا فقط لمنع تضارب الـ Polling)
                await bot.delete_webhook(drop_pending_updates=True)
                app['bot_task'] = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
                
                # 2. تشغيل المهام المجدولة (المنظف والحارس الليلي) 🧹
                from database import run_auto_cleaner, run_daily_tasks
                app['cleaner_task'] = asyncio.create_task(run_auto_cleaner())
                app['daily_task'] = asyncio.create_task(run_daily_tasks())

            # 🔴 إذا فقدت هذه النسخة القيادة (بسبب إعادة التشغيل أو سقوط رديس)
            elif not acquired and app['is_leader']:
                app['is_leader'] = False
                print(f"🛑 النسخة ({instance_id}) فقدت القيادة! جاري إيقاف المهام المكررة...")
                if 'bot_task' in app: app['bot_task'].cancel()
                if 'cleaner_task' in app: app['cleaner_task'].cancel()
                if 'daily_task' in app: app['daily_task'].cancel()

        except Exception as e:
            print(f"⚠️ خطأ في نظام القائد: {e}")

        await asyncio.sleep(10)  # تجديد القفل والفحص كل 10 ثواني

async def background_init(app):
    """مهمة خلفية لتشغيل الإعدادات دون تأخير إقلاع السيرفر"""
    try:
        await database.init_db()
        print("✅ تم تجهيز قاعدة البيانات.")
        
        # 🚀 تشغيل نظام انتخاب القائد (بدلاً من تشغيل البوت مباشرة)
        app['leader_task'] = asyncio.create_task(run_leader_tasks(app))
        
    except Exception as e:
        print(f"❌ خطأ أثناء التهيئة الخلفية: {e}")

async def on_startup(app):
    # تهيئة قائمة لحفظ اتصالات الـ WebSockets النشطة لإغلاقها بأمان لاحقاً
    app['websockets'] = set()
    
    # إرسال مهام قاعدة البيانات للخلفية لكي يفتح المنفذ فوراً!
    asyncio.create_task(background_init(app))

async def on_cleanup(app):
    """دالة الإغلاق الآمن (Graceful Shutdown)"""
    print("⚠️ جاري إغلاق السيرفر بأمان...")

    # 1. إيقاف نظام القائد، البوت، والمهام اليومية بنظافة
    for task_name in ['leader_task', 'bot_task', 'cleaner_task', 'daily_task', 'redis_listener_task']:
        task = app.get(task_name)
        if task:
            task.cancel()

    # 2. إغلاق جميع اتصالات الـ WebSockets المفتوحة بهدوء
    for ws in set(app['websockets']):
        if not ws.closed:
            await ws.close(code=1001, message=b'Server is restarting')
    app['websockets'].clear()
    
    # 3. إغلاق اتصال البوت (Telegram Session)
    if bot.session:
        await bot.session.close()

    # 4. إغلاق بركة اتصالات قاعدة البيانات
    if database.pool:
        await database.pool.close()
        
    # 5. إغلاق مستمع رديس (Redis Listener) بأمان
    if 'redis_listener_task' in app:
        app['redis_listener_task'].cancel()
            
    print("🛑 تم إغلاق جميع الاتصالات بنجاح.")

def create_app():
    # ترتيب الـ Middlewares مهم جداً هنا
    app = web.Application(middlewares=[
        error_middleware,           # 1. صائد الأخطاء الشامل
        maintenance_middleware,     # 2. جدار الصيانة
        rate_limit_middleware,      # 3. 🛡️ جدار حماية التخمين
        auth_and_shadow_middleware  # 4. المصادقة وفك التشفير
    ])
    
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup) # 👈 تسجيل دالة الإغلاق الآمن

    # 🛡️ تحديد المسار المطلق لمجلد static لضمان ظهور الصور في كل السيرفرات
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) if 'api' in os.path.dirname(os.path.abspath(__file__)) else os.path.dirname(os.path.abspath(__file__))
    static_dir = os.path.join(base_dir, 'static')
    os.makedirs(static_dir, exist_ok=True)
    app.router.add_static('/static/', path=static_dir, name='static')
    
    setup_views(app)
    setup_customer_routes(app)
    setup_pos_routes(app)
    setup_manage_routes(app)
    setup_misc_routes(app)
    setup_websocket_routes(app)
    setup_admin_routes(app)
    
    return app
