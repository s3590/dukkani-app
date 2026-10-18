# مسار الملف: database.py
import asyncio
import asyncpg
import logging
import csv
import json
import gzip
from io import StringIO, BytesIO
from datetime import datetime
from config import DATABASE_URL

logger = logging.getLogger(__name__)

# 🛡️ لحماية المهام الخلفية الطويلة من الحذف التلقائي
background_tasks = set()

def create_safe_task(coro):
    task = asyncio.create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return task

pool = None

async def init_db():
    global pool
    if pool is not None:
        return 
        
    try:
        pool = await asyncpg.create_pool(
            DATABASE_URL, 
            min_size=2,
            max_size=20,
            server_settings={'timezone': 'Asia/Aden'}
        )
        
        async with pool.acquire() as conn:
            schema_query = """          
            -- 1. جدول البقالات
            CREATE TABLE IF NOT EXISTS stores (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT UNIQUE, 
                name VARCHAR(100) NOT NULL,
                phone VARCHAR(50) UNIQUE,
                status VARCHAR(20) DEFAULT 'active', 
                package_type VARCHAR(20) DEFAULT 'free',
                subscription_end TIMESTAMP,
                trial_ends_at TIMESTAMP,
                suspend_reason TEXT,
                archive_group_id BIGINT,
                theme_color VARCHAR(20) DEFAULT '#0ba360',
                logo_url TEXT DEFAULT '/static/logo_customer.png',
                alshehab_phone VARCHAR(50),
                alshehab_password VARCHAR(255),
                telecom_permissions JSONB DEFAULT '{"paper_cards": false, "ecards": false, "instant_recharge": false}'::jsonb,
                is_deleted BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 2. جدول الزبائن
            CREATE TABLE IF NOT EXISTS customers (
                id SERIAL PRIMARY KEY,
                store_id INT REFERENCES stores(id) ON DELETE CASCADE,
                parent_id INT REFERENCES customers(id) ON DELETE SET NULL,
                phone VARCHAR(50),
                name VARCHAR(100) NOT NULL,
                password VARCHAR(255) NOT NULL,
                balance NUMERIC DEFAULT 0.0,
                credit_limit NUMERIC DEFAULT 50000,
                address TEXT,
                status VARCHAR(20) DEFAULT 'active',
                can_pull_cards BOOLEAN DEFAULT FALSE,
                allowed_ecard_amounts JSONB DEFAULT '[]'::jsonb,
                family_direct_order BOOLEAN DEFAULT FALSE,
                daily_allowance NUMERIC DEFAULT 0.0, -- 🌟 سقف المصروف التلقائي للطفل
                spent_today NUMERIC DEFAULT 0.0,      -- 🌟 ما تم صرفه اليوم
                is_deleted BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 3. جدول الكتالوج المركزي
            CREATE TABLE IF NOT EXISTS global_products (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                category VARCHAR(50) NOT NULL,
                image_url TEXT DEFAULT '/static/logo_customer.png'
            );

            -- 4. جدول منتجات البقالات
            CREATE TABLE IF NOT EXISTS store_products (
                id SERIAL PRIMARY KEY,
                store_id INT REFERENCES stores(id) ON DELETE CASCADE,
                product_id INT REFERENCES global_products(id) ON DELETE SET NULL,
                custom_name VARCHAR(100), 
                category VARCHAR(50) DEFAULT 'عام',
                cost_price NUMERIC DEFAULT 0.0,
                price NUMERIC NOT NULL,
                min_price NUMERIC DEFAULT 0.0, -- 🌟 الحد الأدنى المسموح للبيع
                carton_qty INT DEFAULT 0,
                carton_price NUMERIC DEFAULT 0.0,
                carton_barcode VARCHAR(50),
                unit_name VARCHAR(20) DEFAULT 'حبة',
                bulk_name VARCHAR(20) DEFAULT 'كرتون',
                is_fresh_item BOOLEAN DEFAULT FALSE,
                offer_text TEXT,
                telegram_file_id TEXT,
                stock INT DEFAULT NULL, 
                low_stock_alert INT DEFAULT 5,
                is_available BOOLEAN DEFAULT TRUE,
                barcode VARCHAR(50),
                expires_at TIMESTAMP DEFAULT NULL,
                is_deleted BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 5. جدول المناديب (عمال التوصيل)
            CREATE TABLE IF NOT EXISTS delivery_agents (
                id SERIAL PRIMARY KEY,
                store_id INT REFERENCES stores(id) ON DELETE CASCADE,
                name VARCHAR(100) NOT NULL,
                phone VARCHAR(20),
                passcode VARCHAR(20),
                is_active BOOLEAN DEFAULT TRUE,
                is_deleted BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 6. جدول الطلبات
            CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                store_id INT REFERENCES stores(id) ON DELETE CASCADE,
                customer_id INT REFERENCES customers(id) ON DELETE CASCADE,
                agent_id INT REFERENCES delivery_agents(id) ON DELETE SET NULL,
                status VARCHAR(20) DEFAULT 'pending',
                payment_method VARCHAR(20) DEFAULT 'credit',
                total_amount NUMERIC NOT NULL,
                delivery_fee NUMERIC DEFAULT 0.0,
                delivery_address TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 7. جدول تفاصيل الطلب
            CREATE TABLE IF NOT EXISTS order_items (
                id SERIAL PRIMARY KEY,
                order_id INT REFERENCES orders(id) ON DELETE CASCADE,
                product_id INT REFERENCES store_products(id) ON DELETE CASCADE,
                quantity INT NOT NULL,
                price_at_time NUMERIC NOT NULL
            );

            -- 8. جدول المعاملات المالية
            CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY,
                store_id INT REFERENCES stores(id) ON DELETE CASCADE,
                customer_id INT REFERENCES customers(id) ON DELETE CASCADE,
                order_id INT REFERENCES orders(id) ON DELETE SET NULL,
                trans_type VARCHAR(50) NOT NULL,
                amount NUMERIC NOT NULL, 
                details TEXT, 
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 9. جدول العمال والصلاحيات (🔒 تم إصلاح قيد passcode ليكون على مستوى المتجر)
            CREATE TABLE IF NOT EXISTS workers (
                id SERIAL PRIMARY KEY,
                store_id INT REFERENCES stores(id) ON DELETE CASCADE,
                telegram_id BIGINT UNIQUE,
                name VARCHAR(100) NOT NULL,
                passcode VARCHAR(50) NOT NULL,
                permissions TEXT DEFAULT '["pos"]',
                is_deleted BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(store_id, passcode) -- ✅ التعديل الأمني الحاسم
            );

            -- 10. جدول اشتراكات الإشعارات (يدعم آيديات التيليجرام الكبيرة BIGINT وقيد ON CONFLICT)
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT UNIQUE NOT NULL,
                user_type VARCHAR(20) NOT NULL,
                subscription_json TEXT
            );

            -- 11. جدول سجل العمليات
            CREATE TABLE IF NOT EXISTS admin_logs (
                id SERIAL PRIMARY KEY,
                action VARCHAR(255) NOT NULL,
                ip_address VARCHAR(50),
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 12. تذاكر الدعم الفني
            CREATE TABLE IF NOT EXISTS support_tickets (
                id SERIAL PRIMARY KEY,
                store_id INT REFERENCES stores(id) ON DELETE CASCADE,
                subject VARCHAR(255) NOT NULL,
                message TEXT NOT NULL,
                status VARCHAR(20) DEFAULT 'open',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 13. الفواتير وصندوق الوارد
            CREATE TABLE IF NOT EXISTS invoices (
                id SERIAL PRIMARY KEY,
                store_id INT REFERENCES stores(id) ON DELETE CASCADE,
                customer_id INT REFERENCES customers(id) ON DELETE CASCADE,
                title VARCHAR(255) NOT NULL,
                pdf_url TEXT NOT NULL,
                is_read BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 14. إعدادات النظام
            CREATE TABLE IF NOT EXISTS system_settings (
                id SERIAL PRIMARY KEY,
                maintenance_mode BOOLEAN DEFAULT FALSE
            );

            -- 15. القائمة السوداء للتوكنات
            CREATE TABLE IF NOT EXISTS blacklisted_tokens (
                jti VARCHAR(50) PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 16. محادثات الدعم
            CREATE TABLE IF NOT EXISTS admin_store_chats (
                id SERIAL PRIMARY KEY,
                store_id INT REFERENCES stores(id) ON DELETE CASCADE,
                sender VARCHAR(20),
                message TEXT,
                is_read BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 17. الموردون
            CREATE TABLE IF NOT EXISTS suppliers (
                id SERIAL PRIMARY KEY,
                store_id INT REFERENCES stores(id) ON DELETE CASCADE,
                name VARCHAR(100) NOT NULL,
                phone VARCHAR(20),
                balance NUMERIC DEFAULT 0.0,
                is_deleted BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 18. المصروفات والمشتريات
            CREATE TABLE IF NOT EXISTS expenses (
                id SERIAL PRIMARY KEY,
                store_id INT REFERENCES stores(id) ON DELETE CASCADE,
                supplier_id INT REFERENCES suppliers(id) ON DELETE SET NULL,
                amount NUMERIC NOT NULL,
                category VARCHAR(50) NOT NULL,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 19. الجرد والتسويات
            CREATE TABLE IF NOT EXISTS inventory_checks (
                id SERIAL PRIMARY KEY,
                store_id INT REFERENCES stores(id) ON DELETE CASCADE,
                product_id INT REFERENCES store_products(id) ON DELETE CASCADE,
                old_stock INT,
                new_stock INT NOT NULL,
                difference INT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 20. مدفوعات الاشتراكات
            CREATE TABLE IF NOT EXISTS subscription_payments (
                id SERIAL PRIMARY KEY,
                store_id INT REFERENCES stores(id) ON DELETE CASCADE,
                amount NUMERIC NOT NULL,
                note TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 21. سجل الاحتيال والتلاعب
            CREATE TABLE IF NOT EXISTS fraud_logs (
                id SERIAL PRIMARY KEY,
                store_id INT REFERENCES stores(id) ON DELETE CASCADE,
                worker_name TEXT,
                action TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 22. المحادثات الفورية
            CREATE TABLE IF NOT EXISTS chat_messages (
                id SERIAL PRIMARY KEY,
                store_id INT REFERENCES stores(id) ON DELETE CASCADE,
                customer_id INT REFERENCES customers(id) ON DELETE CASCADE,
                sender_type VARCHAR(20) NOT NULL,
                message_text TEXT NOT NULL,
                is_read BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 23. المحادثات العائلية
            CREATE TABLE IF NOT EXISTS family_chat_messages (
                id SERIAL PRIMARY KEY,
                main_customer_id INT REFERENCES customers(id) ON DELETE CASCADE,
                sub_customer_id INT REFERENCES customers(id) ON DELETE CASCADE,
                sender_id INT REFERENCES customers(id) ON DELETE CASCADE,
                message_text TEXT NOT NULL,
                is_read BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
            await conn.execute(schema_query)
            await conn.execute("INSERT INTO system_settings (id, maintenance_mode) VALUES (1, FALSE) ON CONFLICT (id) DO NOTHING;")

            # =================================================================
            # 🚀 فهارس السرعة القصوى وحماية البيانات (Optimized Indexes)
            # =================================================================
            indexes_query = """
            -- فهارس العلاقات الأساسية
            CREATE INDEX IF NOT EXISTS idx_orders_store ON orders(store_id);
            CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id); -- ⚡ تسريع جلب عناصر الفاتورة
            CREATE INDEX IF NOT EXISTS idx_trans_store_date ON transactions(store_id, created_at DESC); -- ⚡ تسريع تقرير Z
            CREATE INDEX IF NOT EXISTS idx_products_store ON store_products(store_id) WHERE is_deleted = FALSE;
            CREATE INDEX IF NOT EXISTS idx_admin_logs_time ON admin_logs(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_invoices_customer ON invoices(customer_id);
            CREATE INDEX IF NOT EXISTS idx_expenses_store ON expenses(store_id);
            CREATE INDEX IF NOT EXISTS idx_inventory_store ON inventory_checks(store_id);
            CREATE INDEX IF NOT EXISTS idx_chat_store_cust ON chat_messages(store_id, customer_id);

            -- فهارس جزئية للسرعة ومنع التضارب
            CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_real_phone ON customers(store_id, phone) WHERE phone IS NOT NULL AND phone != '' AND is_deleted = FALSE;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_store_barcode ON store_products(store_id, barcode) WHERE barcode IS NOT NULL AND barcode != '' AND is_deleted = FALSE;
            CREATE INDEX IF NOT EXISTS idx_orders_pending ON orders(store_id, status) WHERE status = 'pending';
            """
            await conn.execute(indexes_query)

# ترقيعات الأعمدة الإضافية وتنظيف تكرارات الإشعارات
            migration_query = """
            ALTER TABLE customers ADD COLUMN IF NOT EXISTS daily_allowance NUMERIC DEFAULT 0.0;
            ALTER TABLE customers ADD COLUMN IF NOT EXISTS spent_today NUMERIC DEFAULT 0.0;
            ALTER TABLE store_products ADD COLUMN IF NOT EXISTS min_price NUMERIC DEFAULT 0.0;
            
            -- 1. تنظيف السجلات المكررة القديمة لكي تقبل قاعدة البيانات إنشاء القيد الفريد
            DELETE FROM push_subscriptions a 
            USING push_subscriptions b 
            WHERE a.ctid < b.ctid AND a.user_id = b.user_id;

            -- 2. تحويل نوع user_id إلى BIGINT لدعم آيديات التيليجرام الكبيرة
            ALTER TABLE push_subscriptions ALTER COLUMN user_id TYPE BIGINT;

            -- 3. إنشاء الفهارس الفريدة لكلا الحالتين (user_id, user_type) و (user_id) لحل ON CONFLICT نهائياً
            CREATE UNIQUE INDEX IF NOT EXISTS idx_push_user_type_unique ON push_subscriptions (user_id, user_type);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_push_user_id_unique ON push_subscriptions (user_id);
            """
            try:
                await conn.execute(migration_query)
                logger.info("✅ تم تنظيف تكرار الإشعارات وتفعيل الفهارس الفريدة بنجاح.")
            except Exception as mig_err:
                logger.warning("ملاحظة أثناء الترقيع: %s", mig_err)

            logger.info("✅ تم إعداد وتحديث قاعدة البيانات بكفاءة وفهارس متقدمة.")
            
    except Exception as e:
        logger.error("❌ فشل الاتصال بقاعدة البيانات: %s", e)

async def close_db():
    global pool
    if pool:
        await pool.close()
        logger.info("تم إغلاق اتصالات قاعدة البيانات بأمان.")

# ==========================================
# 🧹 المنظف التلقائي والأرشفة الآمنة
# ==========================================
async def run_auto_cleaner():
    """أرشفة السجلات القديمة تلقائياً كل 24 ساعة"""
    await asyncio.sleep(60)
    
    while True:
        try:
            if pool:
                async with pool.acquire() as conn:
                    await conn.execute("DELETE FROM blacklisted_tokens WHERE created_at < NOW() - INTERVAL '1 day'")
                    
                    old_logs = await conn.fetch("DELETE FROM admin_logs WHERE id IN (SELECT id FROM admin_logs WHERE created_at < NOW() - INTERVAL '30 days' LIMIT 5000) RETURNING *")
                    old_frauds = await conn.fetch("DELETE FROM fraud_logs WHERE id IN (SELECT id FROM fraud_logs WHERE created_at < NOW() - INTERVAL '30 days' LIMIT 5000) RETURNING *")
                    old_chats = await conn.fetch("DELETE FROM chat_messages WHERE id IN (SELECT id FROM chat_messages WHERE created_at < NOW() - INTERVAL '60 days' LIMIT 5000) RETURNING *")
                    
                total_records = len(old_logs) + len(old_frauds) + len(old_chats)
                
                if total_records > 0:
                    try:
                        csv_file = StringIO()
                        writer = csv.writer(csv_file)
                        writer.writerow(["الجدول", "البيانات"])
                        
                        for row in old_logs: writer.writerow(["admin_logs", str(dict(row))])
                        for row in old_frauds: writer.writerow(["fraud_logs", str(dict(row))])
                        for row in old_chats: writer.writerow(["chat_messages", str(dict(row))])
                        
                        from bot.setup import bot
                        from config import ADMIN_ID, ARCHIVE_GROUP_ID
                        from aiogram.types import BufferedInputFile
                        from zoneinfo import ZoneInfo
                        
                        target_chat = ARCHIVE_GROUP_ID if ARCHIVE_GROUP_ID else ADMIN_ID
                        file_data = csv_file.getvalue().encode('utf-8-sig')
                        date_str = datetime.now(ZoneInfo('Asia/Aden')).strftime('%Y-%m-%d')
                        
                        document = BufferedInputFile(file_data, filename=f"Archive_Cleanup_{date_str}.csv")
                        await bot.send_document(
                            chat_id=target_chat,
                            document=document,
                            caption=f"🧹 **تقرير المنظف التلقائي**\nتم أرشفة وحذف `{total_records}` سجل قديم بنجاح."
                        )
                    except Exception as telegram_error:
                        logger.error("⚠️ فشل إرسال ملف الأرشيف للتيليجرام: %s", telegram_error)
                        
        except Exception as e:
            logger.error("❌ خطأ أثناء تشغيل المنظف التلقائي: %s", e)
            
        await asyncio.sleep(86400)

async def trigger_z_reports_background():
    """إرسال تقارير نهاية اليوم لجميع البقالات النشطة"""
    if not pool: return
    try:
        async with pool.acquire() as conn:
            stores = await conn.fetch("SELECT id FROM stores WHERE status = 'active'")
            
        from services.accounting import generate_and_send_z_report
        for s in stores:
            create_safe_task(generate_and_send_z_report(s['id'], "تقفيل آلي", is_auto=True))
            await asyncio.sleep(0.2)
    except Exception as e:
        logger.error("❌ خطأ في مهمة التقارير الجماعية: %s", e)

async def trigger_backups_background():
    """نسخ احتياطي شامل يتضمن الزبائن، المنتجات، والديون والمعاملات المالية"""
    if not pool: return
    from zoneinfo import ZoneInfo
    tz = ZoneInfo('Asia/Aden')
        
    def json_serial(obj):
        from datetime import datetime, date
        from decimal import Decimal
        if isinstance(obj, (datetime, date)): return obj.isoformat()
        if isinstance(obj, Decimal): return float(obj)
        raise TypeError(f"Type {type(obj)} not serializable")

    try:
        async with pool.acquire() as conn:
            stores = await conn.fetch("SELECT id, name, telegram_id, archive_group_id FROM stores WHERE status = 'active'")
                                
        for store in stores:
            try:
                backup_data = {}
                async with pool.acquire() as conn:
                    backup_data['store_info'] = dict(store)
                    backup_data['customers'] = [dict(r) for r in await conn.fetch("SELECT * FROM customers WHERE store_id = $1", store['id'])]
                    backup_data['products'] = [dict(r) for r in await conn.fetch("SELECT * FROM store_products WHERE store_id = $1", store['id'])]
                    # 🛡️ تم إضافة المعاملات والطلبات لحماية الحسابات المالية
                    backup_data['recent_orders'] = [dict(r) for r in await conn.fetch("SELECT * FROM orders WHERE store_id = $1 ORDER BY id DESC LIMIT 500", store['id'])]
                    backup_data['recent_transactions'] = [dict(r) for r in await conn.fetch("SELECT * FROM transactions WHERE store_id = $1 ORDER BY id DESC LIMIT 1000", store['id'])]
                
                from aiogram.types import BufferedInputFile
                from bot.setup import bot
                
                # ضغط البيانات بصيغة gzip لتوفير الحجم
                json_bytes = (await asyncio.to_thread(json.dumps, backup_data, default=json_serial, ensure_ascii=False)).encode('utf-8')
                compressed_io = BytesIO()
                with gzip.GzipFile(fileobj=compressed_io, mode='wb') as gz:
                    gz.write(json_bytes)
                
                date_str = datetime.now(tz).strftime('%Y-%m-%d')
                document = BufferedInputFile(compressed_io.getvalue(), filename=f"Backup_{store['name']}_{date_str}.json.gz")
                
                target_chat = store['archive_group_id'] if store['archive_group_id'] else store['telegram_id']
                if target_chat:
                    try:
                        await bot.send_document(
                            chat_id=target_chat, 
                            document=document, 
                            caption=f"💾 **النسخة الاحتياطية الشاملة المضغوطة**\nلبقالة: {store['name']}\n📅 التاريخ: {date_str}"
                        )
                    except Exception as e:
                        logger.warning("فشل إرسال النسخة الاحتياطية لتيليجرام: %s", e)
                        
                await asyncio.sleep(1)
            except Exception as store_err:
                logger.error("❌ خطأ أثناء نسخ بيانات بقالة %s: %s", store['name'], store_err)
    except Exception as e:
        logger.error("❌ خطأ في مهمة النسخ الاحتياطي الجماعية: %s", e)

async def run_daily_tasks():
    """الحارس الليلي لإدارة المهام التلقائية وضبط استهلاك الأطفال اليومي"""
    await asyncio.sleep(60)
    from zoneinfo import ZoneInfo
    tz = ZoneInfo('Asia/Aden')
    last_executed_minute = -1

    while True:
        try:
            now = datetime.now(tz)
            if now.minute != last_executed_minute:
                last_executed_minute = now.minute
                
                # 1. التقفيل التلقائي
                if now.hour == 23 and now.minute == 55:
                    create_safe_task(trigger_z_reports_background())

                # 2. النسخ الاحتياطي الشامل
                if now.hour == 23 and now.minute == 59:
                    create_safe_task(trigger_backups_background())

                # 3. 🌟 تصفير استهلاك الأطفال اليومي عند منتصف الليل
                if now.hour == 0 and now.minute == 0:
                    if pool:
                        async with pool.acquire() as conn:
                            await conn.execute("UPDATE customers SET spent_today = 0.0 WHERE spent_today > 0")

                # 4. تنبيهات وإيقاف الاشتراكات
                if now.hour == 10 and now.minute == 0:
                    if pool:
                        async with pool.acquire() as conn:
                            from bot.setup import bot
                            expired = await conn.fetch("SELECT id, name, telegram_id FROM stores WHERE status = 'active' AND trial_ends_at < NOW()")
                            for st in expired:
                                await conn.execute("UPDATE stores SET status = 'suspended', suspend_reason = 'انتهاء فترة الاشتراك' WHERE id = $1", st['id'])
                                if st['telegram_id']:
                                    try: await bot.send_message(st['telegram_id'], f"🛑 **إشعار إيقاف النظام**\n\nانتهت فترة اشتراك بقالة ({st['name']}).")
                                    except: pass
                                await asyncio.sleep(0.1)
                
        except Exception as e:
            logger.error("Daily Tasks Error: %s", e)
            
        now_after = datetime.now(tz)
        sleep_seconds = 60 - now_after.second - (now_after.microsecond / 1_000_000.0)
        await asyncio.sleep(max(1.0, sleep_seconds))
