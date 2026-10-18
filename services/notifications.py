# مسار الملف: services/notifications.py
import logging
import asyncio
from datetime import timedelta
from firebase_admin import messaging as fcm_messaging
import database

async def send_push_notification(user_id: int, title: str, body: str, extra_data: dict = None, user_type: str = 'customer'):
    try:
        async with database.pool.acquire() as conn:
            sub = await conn.fetchrow("SELECT subscription_json FROM push_subscriptions WHERE user_id = $1 AND user_type = $2", user_id, user_type)

        if not sub or not sub['subscription_json']:
            return 
            
        fcm_token = sub['subscription_json']
        
        import os
        import time

        # 🛡️ نسخة آمنة لمنع تعديل القاموس الأصلي وحقن البيانات
        payload_data = dict(extra_data) if extra_data else {}
        payload_data['title'] = title
        payload_data['body'] = body
        payload_data = {str(k): str(v) for k, v in payload_data.items()}
        
        BASE_URL = os.getenv("BASE_URL", "https://dukkani-app.onrender.com")
        raw_action = payload_data.get('action', '')
        
        # التوجيه الذكي
        if raw_action == 'open_inbox': click_url = f"{BASE_URL}/?action=inbox"
        elif raw_action == 'open_chat': click_url = f"{BASE_URL}/?action=chat"
        elif raw_action == 'open_orders': click_url = f"{BASE_URL}/manage"
        else: click_url = f"{BASE_URL}/"
            
        message = fcm_messaging.Message(
            notification=fcm_messaging.Notification(title=title, body=body),
            data=payload_data,
            android=fcm_messaging.AndroidConfig(
                priority="high",
                direct_boot_ok=True,
                ttl=timedelta(seconds=86400),
                notification=fcm_messaging.AndroidNotification(
                    sound="bell", # 🔔 يطابق صوت القناة
                    channel_id="dukkani_alerts_vip", # 🟢 تم تصحيح القناة لتطابق كود أندرويد
                    priority="max",
                    visibility="public",
                    click_action="FCM_PLUGIN_ACTIVITY"
                )
            ),

            webpush=fcm_messaging.WebpushConfig(
                headers={"Urgency": "high"},
                fcm_options=fcm_messaging.WebpushFCMOptions(link=click_url),
                notification=fcm_messaging.WebpushNotification(
                    icon="/static/logo_customer.png",
                    badge="/static/logo_customer.png",
                    vibrate=[500, 200, 500, 200, 500],
                    require_interaction=True,
                    renotify=True,
                    tag=str(time.time()) # 🛡️ بصمة وقت آمنة
                )
            ),
            token=fcm_token,
        )

        # الإرسال في مسار خلفي لكي لا يجمد السيرفر
        await asyncio.to_thread(fcm_messaging.send, message)
        
    except Exception as e:
        err_msg = str(e).lower()
        # التنظيف الذاتي للتوكنات الميتة أو المحذوفة
        if "registration token" in err_msg or "unregistered" in err_msg or "not found" in err_msg:
            try:
                async with database.pool.acquire() as conn:
                    await conn.execute("DELETE FROM push_subscriptions WHERE user_id = $1 AND user_type = $2", user_id, user_type)
            except: pass
