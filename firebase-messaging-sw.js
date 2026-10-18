// مسار الملف: firebase-messaging-sw.js

// ================= 1. إعدادات Firebase للإشعارات =================
importScripts('https://www.gstatic.com/firebasejs/8.10.1/firebase-app.js');
importScripts('https://www.gstatic.com/firebasejs/8.10.1/firebase-messaging.js');

firebase.initializeApp({
  apiKey: "AIzaSyDvCRGzkVUgZodqd63QZCkwjfCjOhc6nYo",
  authDomain: "dukkani-app-30b43.firebaseapp.com",
  projectId: "dukkani-app-30b43",
  storageBucket: "dukkani-app-30b43.firebasestorage.app",
  messagingSenderId: "757873553883",
  appId: "1:757873553883:web:44c531dfc7dbe1f807f7fe"
});

const messaging = firebase.messaging();

// 🔔 استقبال الإشعارات في الخلفية (محصن ضد الانهيار والإشعارات المزدوجة)
messaging.onBackgroundMessage(function(payload) {
  console.log('[firebase-messaging-sw.js] استلام إشعار في الخلفية:', payload);

  // 🛡️ حماية: استخراج العنوان والنص بأمان سواء كان إشعار Notification أو Data
  const title = (payload.notification && payload.notification.title) || 
                (payload.data && payload.data.title) || 
                '🏪 دُكّاني';

  const body = (payload.notification && payload.notification.body) || 
               (payload.data && payload.data.body) || 
               'لديك تحديث جديد في المتجر';

  const notificationData = payload.data || {};

  const notificationOptions = {
    body: body,
    icon: '/static/logo_customer.png',
    badge: '/static/logo_customer.png',
    // 🛡️ دمج الإشعارات المتطابقة ومنع تكرار الإشعار مرتين
    tag: notificationData.tag || notificationData.order_id || 'dukkani_notification',
    renotify: true,
    vibrate: [200, 100, 200, 100, 200], // اهتزاز قوي لإيقاظ الشاشة
    requireInteraction: true,           // إبقاء الإشعار ظاهراً حتى ينتبه له المستخدم
    data: notificationData
  };

  return self.registration.showNotification(title, notificationOptions);
});

// ================= 2. إعدادات الكاش وتطبيق الويب (PWA) =================
const CACHE_NAME = 'dukkani-saas-v8'; // 🚀 ترقية الكاش لإجبار الأجهزة على التحديث الفوري

const CORE_ASSETS = [
    '/',
    '/static/manifest.json',
    '/static/logo_customer.png',
    'https://fonts.googleapis.com/css2?family=Cairo:wght@400;700;900&display=swap'
];

self.addEventListener('install', (event) => {
    self.skipWaiting();
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => cache.addAll(CORE_ASSETS))
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((cacheNames) => {
            return Promise.all(
                cacheNames.map((cache) => {
                    if (cache !== CACHE_NAME) {
                        console.log('🧹 [Service Worker] تم مسح الكاش القديم وتفعيل الإصدار الجديد');
                        return caches.delete(cache);
                    }
                })
            );
        })
    );
    self.clients.claim();
});

self.addEventListener('fetch', (event) => {
    const url = event.request.url;

    // 1. استثناء طلبات الخادم الحية والـ WebSockets من الكاش
    if (url.includes('/api/') || url.includes('/ws/')) {
        return; 
    }

    // 2. 🛡️ استراتيجية (Network-First) للتنقل بين الصفحات
    if (event.request.mode === 'navigate') {
        event.respondWith(
            fetch(event.request).then((networkResponse) => {
                if (networkResponse && networkResponse.status === 200) {
                    const responseToCache = networkResponse.clone();
                    caches.open(CACHE_NAME).then((cache) => {
                        cache.put(event.request, responseToCache);
                    });
                }
                return networkResponse;
            }).catch(() => {
                // عند انقطاع الإنترنت: فتح النسخة المحفوظة أو الصفحة الرئيسية
                return caches.match(event.request, {ignoreSearch: true})
                    .then((cachedResp) => cachedResp || caches.match('/'));
            })
        );
        return;
    }

    // 3. استراتيجية (Stale-While-Revalidate) للملفات الثابتة (أيقونات، خطوط)
    event.respondWith(
        caches.match(event.request, {ignoreSearch: true}).then((cachedResponse) => {
            const fetchPromise = fetch(event.request).then((networkResponse) => {
                if (networkResponse && (networkResponse.status === 200 || networkResponse.type === 'opaque')) {
                    const responseToCache = networkResponse.clone();
                    caches.open(CACHE_NAME).then((cache) => {
                        cache.put(event.request, responseToCache);
                    });
                }
                return networkResponse;
            }).catch(() => {});
            return cachedResponse || fetchPromise;
        })
    );
});

// ================= 3. التفاعل والتوجيه عند النقر على الإشعار =================
self.addEventListener('notificationclick', function(event) {
    event.notification.close();

    const data = event.notification.data || {};
    let targetUrl = '/';

    // تحديد الوجهة المناسبة بناءً على نوع الحدث
    if (data.action === 'open_inbox') {
        targetUrl = '/?action=inbox';
    } else if (data.action === 'open_chat' || data.action === 'open_orders') {
        targetUrl = '/manage';
    } else if (data.url) {
        targetUrl = data.url;
    }

    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(clientList) {
            // إذا كان التطبيق مفتوحاً بالفعل في تبويب سابق
            for (let i = 0; i < clientList.length; i++) {
                let client = clientList[i];
                if ('focus' in client) {
                    client.postMessage({ action: data.action, data: data });
                    // توجيه التبويب النشط إلى الرابط إذا كان مختلفاً
                    if ('navigate' in client && client.url !== targetUrl) {
                        client.navigate(targetUrl);
                    }
                    return client.focus();
                }
            }
            // إذا كان التطبيق مغلقاً تماماً، نقوم بفتحه في نافذة جديدة
            if (clients.openWindow) {
                return clients.openWindow(targetUrl);
            }
        })
    );
});
