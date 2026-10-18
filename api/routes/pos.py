# مسار الملف: api/routes/pos.py
from aiohttp import web
import json
import os
import logging
import asyncio
import time 
import database
from services.notifications import send_push_notification
from bot.setup import bot
from config import ADMIN_ID
from api.routes.manage import get_real_store_id # 👈 نقل الاستيراد للأعلى لترتيب الكود
from decimal import Decimal

# =================================================================
# 🛡️ نظام المهام الآمنة في الخلفية (لمنع قتل المهام بواسطة الذاكرة)
# =================================================================
background_tasks = set()

def create_safe_task(coro):
    """
    إنشاء مهمة آمنة في الخلفية تضمن عدم قيام Garbage Collector
    بحذفها قبل أن تكتمل (مفيدة جداً للإشعارات والعمليات البطيئة).
    """
    task = asyncio.create_task(coro)  # ✅ هذا هو التصحيح السليم
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return task

# 👇 قاموس لحفظ وقت آخر طلب مع آلية تنظيف تلقائية لمنع تسرب الذاكرة 👇
POS_ORDER_COOLDOWN = {}

def clean_cooldown():
    """تنظيف الذاكرة من الطلبات القديمة (مُحسنة لتعمل فقط عند الحاجة لتخفيف الضغط)"""
    if len(POS_ORDER_COOLDOWN) > 100: 
        current_time = time.time()
        keys_to_delete = [k for k, v in POS_ORDER_COOLDOWN.items() if current_time - v > 10]
        for k in keys_to_delete:
            del POS_ORDER_COOLDOWN[k]

async def api_pos_data(request: web.Request):
    """جلب بيانات الكاشير (المنتجات، الزبائن، إعدادات البقالة)"""
    store_tid = request.query.get('store_id')
    if not store_tid or store_tid == 'NaN' or store_tid == 'undefined' or not str(store_tid).isdigit(): 
        return web.json_response({"error": "Missing or invalid store_id"}, status=400)

    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, int(store_tid))
        if not store_id: 
            return web.json_response({"error": "Store not found"}, status=404)
        
        # 🚀 جلب كل البيانات في "اتصال واحد" لتوفير موارد السيرفر
        products = await conn.fetch("""
            SELECT sp.id, sp.custom_name as name, sp.price, sp.category, sp.barcode, 
                   sp.carton_qty, sp.carton_price, sp.carton_barcode,
                   COALESCE(sp.telegram_file_id, gp.image_url) as telegram_file_id 
            FROM store_products sp 
            LEFT JOIN global_products gp ON sp.product_id = gp.id 
            WHERE sp.store_id = $1 AND sp.is_available=TRUE AND sp.is_deleted = FALSE
        """, store_id)
        
        customers = await conn.fetch("SELECT id, name, balance FROM customers WHERE store_id = $1 AND is_deleted = FALSE AND parent_id IS NULL", store_id)
        store_info = await conn.fetchrow("SELECT theme_color, logo_url FROM stores WHERE id = $1", store_id)
        
    # 🛡️ الحماية من أخطاء الـ NULL عند تحويل الأرقام (TypeError)
    prod_list = []
    for p in products:
        prod_list.append({
            "id": p['id'], 
            "name": p['name'], 
            "price": float(p['price'] or 0.0), 
            "category": p['category'] or 'عام', 
            "barcode": p['barcode'], 
            "image": p['telegram_file_id'], 
            "carton_qty": p['carton_qty'], 
            "carton_price": float(p['carton_price'] or 0.0), 
            "carton_barcode": p['carton_barcode']
        })
        
    cust_list = [{"id": c['id'], "name": c['name'], "balance": float(c['balance'] or 0.0)} for c in customers]
        
    return web.json_response({
        "theme_color": store_info['theme_color'] if store_info else '#0ba360',
        "products": prod_list,
        "customers": cust_list
    })

async def api_pos_order(request: web.Request):
    """استقبال طلبات الكاشير السريع (طلبات جديدة أو مرتجعات) - نسخة الحماية القصوى"""
    try:
        data = await request.json()
        
        # 1. حماية من البيانات الناقصة
        if 'store_id' not in data or 'customer_id' not in data or 'items' not in data:
            return web.json_response({"status": "error", "message": "بيانات الطلب غير مكتملة."})
            
        try:
            store_tid = int(data['store_id'])
            cid = int(data['customer_id'])
            worker_id = int(data['worker_id']) if data.get('worker_id') else None
            # 🛡️ استخدام Decimal لضمان الدقة المحاسبية المطلقة وتجنب كسور بايثون
            discount = Decimal(str(data.get('discount', '0.0')))
        except ValueError:
            return web.json_response({"status": "error", "message": "صيغة البيانات غير صحيحة (يجب أن تكون أرقاماً)."})
            
        if discount < Decimal('0'):
            return web.json_response({"status": "error", "message": "قيمة الخصم لا يمكن أن تكون بالسالب."})
            
        is_return = data.get('is_return') in [True, 'true', 'True', 1]
        items = data['items']
        
        # 🛡️ 2. حماية من هجمات الإغراق (DoS) والحد الأقصى للسلة
        if not items or not isinstance(items, dict):
            return web.json_response({"status": "error", "message": "السلة فارغة أو البيانات تالفة."})
        if len(items) > 200:
            return web.json_response({"status": "error", "message": "السلة تتجاوز الحد الأقصى المسموح به (200 صنف)."})

        async with database.pool.acquire() as conn:
            store_id = await get_real_store_id(conn, request, store_tid)
            if not store_id:
                return web.json_response({"status": "error", "message": "لم يتم العثور على البقالة."})

            clean_cooldown()
            current_time = time.time()
            cooldown_key = f"{store_id}_{worker_id}"
            if cooldown_key in POS_ORDER_COOLDOWN:
                if current_time - POS_ORDER_COOLDOWN[cooldown_key] < 3:
                    return web.json_response({"status": "error", "message": "الرجاء الانتظار لحظة قبل إرسال طلب جديد."})
            POS_ORDER_COOLDOWN[cooldown_key] = current_time

            # 🛡️ تهيئة الإجمالي باستخدام Decimal
            real_total = Decimal('0.0')
            try:
                product_ids = [int(pid) for pid in items.keys()]
            except ValueError:
                return web.json_response({"status": "error", "message": "معرفات المنتجات غير صالحة."})

            db_products = await conn.fetch("""
                SELECT id, custom_name, price 
                FROM store_products 
                WHERE id = ANY($1) AND store_id = $2
            """, product_ids, store_id)
            
            # تخزين الأسعار كـ Decimal
            db_items_dict = {str(p['id']): {'price': Decimal(str(p['price'])), 'name': p['custom_name']} for p in db_products}

            # جلب اسم الكاشير لتوثيقه في الفاتورة (مهم جداً للجرد والمحاسبة)
            worker_name = "صاحب البقالة"
            if worker_id:
                worker_name = await conn.fetchval("SELECT name FROM workers WHERE id = $1 AND store_id = $2", worker_id, store_id)
                if not worker_name:
                    worker_name = "عامل مجهول"

            safe_details_list = []
            
            for pid_str, item in items.items():
                try:
                    qty = int(item.get('qty', 1))
                except ValueError:
                    return web.json_response({"status": "error", "message": "كمية المنتج غير صالحة."})
                    
                # 🛡️ 3. منع الكميات الصفرية، السالبة، أو الفلكية
                if qty <= 0 or qty > 10000:
                    return web.json_response({"status": "error", "message": "الكمية غير منطقية (يجب أن تكون بين 1 و 10,000)."})
                    
                if pid_str not in db_items_dict:
                    return web.json_response({"status": "error", "message": "أحد المنتجات غير موجود أو تم حذفه."})
                    
                trusted_item = db_items_dict[pid_str]
                # حساب السعر الموثوق بالـ Decimal
                real_total += trusted_item['price'] * Decimal(str(qty))
                safe_details_list.append(f"- {qty}x {trusted_item['name'] or 'منتج غير مسمى'}")
                
            # 🛡️ 4. إصلاح ثغرة المرتجع المزدوج (الخصم لا يتجاوز الإجمالي أبداً)
            if discount > real_total:
                return web.json_response({"status": "error", "message": "الخصم لا يمكن أن يكون أكبر من إجمالي الفاتورة!"})
                
            final_total = real_total - discount
            notification_data = None 

            try:
                async with conn.transaction():
                    if cid != 0:
                        # 🛡️ قفل صف الزبون مع شرط المتجر store_id
                        cust = await conn.fetchrow("SELECT name, balance, credit_limit FROM customers WHERE id = $1 AND store_id = $2 FOR UPDATE", cid, store_id)
                        if not cust:
                            raise ValueError("الزبون غير موجود في هذه البقالة.")
                            
                        current_balance = Decimal(str(cust['balance']))
                        credit_limit = Decimal(str(cust['credit_limit']))
                        
                        if not is_return and (current_balance + final_total) > credit_limit:
                            raise ValueError(f"الطلب يتجاوز سقف الدين المسموح للزبون ({cust['name']}).")

                    # ⚡ الترتيب الذهبي للمنتجات (تصاعدياً حسب الـ ID) لمنع الـ Deadlock نهائياً
                    sorted_items = sorted(items.items(), key=lambda x: int(x[0]))

                    for pid_str, item in sorted_items:
                        qty = int(item.get('qty', 1))
                        pid = int(pid_str)
                        if is_return:
                            await conn.execute("UPDATE store_products SET stock = stock + $1 WHERE id = $2 AND store_id = $3 AND stock IS NOT NULL", qty, pid, store_id)
                        else:
                            res = await conn.execute("UPDATE store_products SET stock = stock - $1 WHERE id = $2 AND store_id = $3 AND (stock IS NULL OR stock >= $1)", qty, pid, store_id)
                            if res == "UPDATE 0":
                                raise ValueError(f"الكمية المطلوبة من أحد المنتجات نفدت من المخزون للتو!")

                    details = "\n".join(safe_details_list)
                    if discount > Decimal('0'):
                        details += f"\n🎁 الخصم: {float(discount)} ريال"
                        
                    trans_type = 'pos_return' if is_return else 'pos_order'
                    action_name = "مرتجع" if is_return else "طلب"
                    
                    # 💡 إضافة توقيع الكاشير في تفاصيل الفاتورة
                    cashier_signature = f"\n👨‍💼 الكاشير: {worker_name}"
                    
                    if cid != 0:
                        details_full = f"{action_name} كاشير (آجل)\n{details}\nالإجمالي النهائي: {float(final_total)}{cashier_signature}"
                        
                        if is_return:
                            await conn.execute("UPDATE customers SET balance = balance - $1 WHERE id = $2", float(final_total), cid)
                            await conn.execute("INSERT INTO transactions (store_id, customer_id, trans_type, amount, details) VALUES ($1, $2, $3, $4, $5)", store_id, cid, trans_type, float(-final_total), details_full)
                            new_balance = current_balance - final_total
                        else:
                            await conn.execute("UPDATE customers SET balance = balance + $1 WHERE id = $2", float(final_total), cid)
                            await conn.execute("INSERT INTO transactions (store_id, customer_id, trans_type, amount, details) VALUES ($1, $2, $3, $4, $5)", store_id, cid, trans_type, float(final_total), details_full)
                            new_balance = current_balance + final_total
                            
                        notification_data = {
                            "cid": cid,
                            "title": f"🔄 فاتورة مرتجع" if is_return else f"🛒 فاتورة جديدة (آجل)",
                            "body": f"تم تسجيل {action_name} بقيمة {float(final_total)} ريال بواسطة ({worker_name}). رصيد دينك الحالي: {float(new_balance)} ريال."
                        }
                    else:
                        details_full = f"{action_name} كاشير (نقدي)\n{details}\nالإجمالي النهائي: {float(final_total)}{cashier_signature}"
                        await conn.execute("INSERT INTO transactions (store_id, customer_id, trans_type, amount, details) VALUES ($1, NULL, $2, $3, $4)", store_id, trans_type, float(final_total) if not is_return else float(-final_total), details_full)
            
            except ValueError as ve:
                return web.json_response({"status": "error", "message": str(ve)})

            if notification_data:
                create_safe_task(send_push_notification(
                    notification_data["cid"], 
                    notification_data["title"], 
                    notification_data["body"]
                ))

            if is_return:
                returns_count = await conn.fetchval("""
                    SELECT COUNT(*) FROM transactions 
                    WHERE store_id = $1 AND trans_type = 'pos_return' 
                    AND created_at >= NOW() - INTERVAL '1 day'
                """, store_id)
                
                if returns_count >= 3:
                    try:
                        await conn.execute("""
                            INSERT INTO fraud_logs (store_id, worker_name, action) 
                            VALUES ($1, $2, 'تم رصد 3 عمليات مرتجع/إلغاء خلال 24 ساعة!')
                        """, store_id, worker_name)
                        
                        store_tid_real = await conn.fetchval("SELECT telegram_id FROM stores WHERE id = $1", store_id)
                        
                        async def send_fraud_alerts():
                            if store_tid_real:
                                try: await bot.send_message(store_tid_real, "🚨 **تنبيه أمني خطير!** 🚨\n\nتم رصد 3 عمليات (مرتجع/إلغاء فاتورة) في بقالتك خلال 24 ساعة.\nيرجى مراجعة الكاميرات فوراً!")
                                except: pass
                            try: await bot.send_message(ADMIN_ID, f"⚠️ **نظام كشف التلاعب:**\nنشاط مشبوه في بقالة رقم {store_id} (كثرة المرتجعات).")
                            except: pass
                            
                        create_safe_task(send_fraud_alerts())
                    except Exception as e:
                        logging.error(f"Fraud detection error: {e}")

        return web.json_response({"status": "success", "message": f"تم تسجيل الـ {action_name} بنجاح!"})

    except Exception as e:
        logging.error(f"POS API Error: {e}")
        return web.json_response({"status": "error", "message": "حدث خطأ غير متوقع أثناء معالجة الطلب."})

async def api_pos_close_shift(request: web.Request):
    data = await request.json()
    store_tid = data.get('store_id')
    if not store_tid:
        return web.json_response({"status": "error", "message": "معرف البقالة مفقود"})
        
    worker_id = data.get('worker_id')
    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, int(store_tid))
        if not store_id:
            return web.json_response({"status": "error", "message": "لم يتم العثور على البقالة"})
            
        worker_name = "صاحب البقالة"
        if worker_id:
            worker_name = await conn.fetchval("SELECT name FROM workers WHERE id = $1 AND store_id = $2", int(worker_id), store_id) or "كاشير"
        else:
            worker_name = await conn.fetchval("SELECT name FROM stores WHERE id = $1", store_id) or "صاحب البقالة"
        
    from services.accounting import generate_and_send_z_report
    
    # 🚀 تشغيل عملية التقرير في الخلفية لكي لا يتجمد تطبيق الكاشير
    create_safe_task(generate_and_send_z_report(store_id, worker_name, is_auto=False))
    
    # إرجاع الرد فوراً في أجزاء من الثانية
    return web.json_response({"status": "success", "message": "تم البدء في إغلاق الوردية وإرسال التقرير."})

# =====================================================================
# 🌉 الجسر السري: سحب وإرسال الكروت إلكترونياً للزبائن بصمت
# =====================================================================
ALSHEHAB_API_URL = os.getenv("ALSHEHAB_API_URL", "https://my-bot-ehio.onrender.com" )
B2B_SECRET_KEY = os.getenv("B2B_SECRET_KEY", "SuperSecretDukkaniBridge2026")

# 👇 الدالة الجديدة: جلب قائمة الفئات المتوفرة من الشهاب برو 👇
async def api_pos_get_ecards_list(request: web.Request):
    try:
        store_tid = int(request.query.get('store_id', 0))
    except ValueError:
        return web.json_response({"status": "error", "message": "معرف البقالة غير صالح"})

    async with database.pool.acquire() as conn:
        store_id = await get_real_store_id(conn, request, store_tid)
        if not store_id:
            return web.json_response({"status": "error", "message": "البقالة غير موجودة"})
            
        alshehab_phone = await conn.fetchval("SELECT alshehab_phone FROM stores WHERE id = $1", store_id)
        if not alshehab_phone:
            return web.json_response({"status": "error", "message": "يجب ربط حساب الشهاب أولاً من الإعدادات"})

    try:
        async with aiohttp.ClientSession( ) as session:
            headers = {"Authorization": f"Bearer {B2B_SECRET_KEY}", "Content-Type": "application/json"}
            async with session.post(f"{ALSHEHAB_API_URL}/api/b2b/available_cards", headers=headers, json={"store_telegram_id": alshehab_phone}) as resp:
                result = await resp.json()
                return web.json_response(result)
    except Exception as e:
        logging.error(f"POS Get Cards Error: {e}")
        return web.json_response({"status": "error", "message": "فشل الاتصال بسيرفر الشهاب برو"})

async def api_pos_pull_and_send_card(request: web.Request):
    """يسحب الكرت من الشهاب، يقيده كدين في دكاني، ويرسله لصندوق الزبون"""
    try:
        data = await request.json()
        
        # 🛡️ حماية ضد الأخطاء النوعية (Type Errors)
        try:
            store_tid = int(data.get('store_id', 0))
            customer_id = int(data.get('customer_id', 0))
            amount = float(data.get('amount', 0))
        except ValueError:
            return web.json_response({"status": "error", "message": "بيانات غير صالحة، يجب أن تكون أرقاماً."})
            
        category = data.get('category')
        
        # 🛡️ حماية صارمة ضد حقن المبالغ السالبة أو الصفرية
        if amount <= 0:
            return web.json_response({"status": "error", "message": "المبلغ يجب أن يكون أكبر من الصفر."})
            
        if not customer_id or not category:
            return web.json_response({"status": "error", "message": "بيانات غير مكتملة"})

        async with database.pool.acquire() as conn:
            store_id = await get_real_store_id(conn, request, store_tid)
            if not store_id: return web.json_response({"status": "error", "message": "البقالة غير موجودة"})

            alshehab_phone = await conn.fetchval("SELECT alshehab_phone FROM stores WHERE id = $1", store_id)
            if not alshehab_phone: return web.json_response({"status": "error", "message": "يجب ربط حساب الشهاب أولاً من الإعدادات"})

        # 🚨 1. التحقق من رصيد الزبون وتطابق المتجر لمنع التلاعب
        async with database.pool.acquire() as conn:
            cust = await conn.fetchrow("SELECT name, balance, credit_limit FROM customers WHERE id = $1 AND store_id = $2", customer_id, store_id)
            if not cust: return web.json_response({"status": "error", "message": "الزبون غير موجود في هذه البقالة"})
            
            if (Decimal(str(cust['balance'])) + Decimal(str(amount))) > Decimal(str(cust['credit_limit'])):
                return web.json_response({"status": "error", "message": f"لا يمكن السحب! يتجاوز سقف دين الزبون ({cust['name']})."})

        # 🌐 2. سحب الكرت من الشهاب أولاً
        try:
            async with aiohttp.ClientSession( ) as session:
                headers = {"Authorization": f"Bearer {B2B_SECRET_KEY}", "Content-Type": "application/json"}
                payload = {"store_telegram_id": alshehab_phone, "category": category, "amount": int(amount)}
                
                async with session.post(f"{ALSHEHAB_API_URL}/api/b2b/pull_card", headers=headers, json=payload) as resp:
                    result = await resp.json()
        except Exception as e:
            logging.error(f"API Connection Error: {e}")
            return web.json_response({"status": "error", "message": "فشل الاتصال بسيرفر الشهاب، لم يتم خصم أي رصيد."})

        if result.get("status") != "success":
            return web.json_response({"status": "error", "message": result.get("message", "فشل سحب الكرت من المورد.")})

        # ✅ 3. نجح السحب من المورد -> الآن نفتح Transaction واحد فقط لخصم الرصيد وتسجيل الفواتير بأمان
        card_data = result['card']
        sell_price = Decimal(str(card_data['sell_price']))
        
        async with database.pool.acquire() as conn:
            async with conn.transaction():
                # قفل السجل FOR UPDATE أثناء التحديث النهائي
                await conn.execute("SELECT id FROM customers WHERE id = $1 FOR UPDATE", customer_id)
                await conn.execute("UPDATE customers SET balance = balance + $1 WHERE id = $2", float(sell_price), customer_id)
                    
                await conn.execute("""
                    INSERT INTO transactions (store_id, customer_id, trans_type, amount, details) 
                    VALUES ($1, $2, 'شراء_كرت', $3, $4)
                """, store_id, customer_id, float(sell_price), f"شراء كرت {category} فئة {amount}")
                
                card_details = f"الشبكة: {category}\nالفئة: {amount}\nرقم التعبئة (الرمز السري):\n{card_data['pin_code']}"
                await conn.execute("""
                    INSERT INTO invoices (store_id, customer_id, title, pdf_url)
                    VALUES ($1, $2, $3, $4)
                """, store_id, customer_id, f"💳 كرت {category} فئة {amount}", card_details)

        create_safe_task(send_push_notification(
            customer_id=customer_id,
            title=f"💳 تم إرسال كرت {category} لك",
            body=f"تم إرسال الكرت بنجاح إلى صندوق الوارد، وتم قيد {float(sell_price)} ريال على حسابك.",
            extra_data={"action": "open_inbox"}
        ))
        
        return web.json_response({"status": "success", "message": "تم سحب الكرت وإرساله للزبون بنجاح!"})
        
    except Exception as e:
        logging.error(f"POS Pull Card Error: {e}")
        return web.json_response({"status": "error", "message": "حدث خطأ داخلي أثناء معالجة الكرت"})

def setup_pos_routes(app: web.Application):
    app.router.add_get('/api/pos/data', api_pos_data)
    app.router.add_post('/api/pos/order', api_pos_order)
    app.router.add_post('/api/pos/close_shift', api_pos_close_shift)
    
    # 🌉 مسارات إرسال الكروت السرية من الكاشير
    app.router.add_get('/api/pos/available_ecards', api_pos_get_ecards_list)
    app.router.add_post('/api/pos/pull_and_send_card', api_pos_pull_and_send_card)
