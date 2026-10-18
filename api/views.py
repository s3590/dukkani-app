import os
from aiohttp import web

# 👇 ذاكرة مؤقتة لحفظ صفحات HTML لتسريع السيرفر مليون مرة 👇
TEMPLATE_CACHE = {}

# 🛡️ تحديد مسار المجلد الرئيسي للمشروع بدقة مطلقة
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) if 'routes' in os.path.dirname(os.path.abspath(__file__)) else os.path.dirname(os.path.abspath(__file__))

def get_template(filename: str) -> str:
    """دالة ذكية تقرأ الملف مرة واحدة وتحفظه في الذاكرة مع تحديد دقيق للمسار"""
    if filename not in TEMPLATE_CACHE:
        # البحث عن مجلد templates في مسار المشروع أو المسار الحالي
        filepath = os.path.join(BASE_DIR, 'templates', filename)
        if not os.path.exists(filepath):
            filepath = os.path.join('templates', filename)

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                TEMPLATE_CACHE[filename] = f.read()
        except FileNotFoundError:
            return f"<h1>عفواً، ملف {filename} غير موجود في السيرفر!</h1>"
            
    return TEMPLATE_CACHE[filename]

# 🚀 ترويسات تضمن عدم تجميد الكود القديم في هواتف الزبائن عند عمل أي تحديث
HTML_HEADERS = {
    'Cache-Control': 'no-cache, no-store, must-revalidate',
    'Pragma': 'no-cache',
    'Expires': '0'
}

async def serve_index(request: web.Request):
    return web.Response(text=get_template('index.html'), content_type='text/html', charset='utf-8', headers=HTML_HEADERS)

async def serve_pos(request: web.Request):
    return web.Response(text=get_template('pos.html'), content_type='text/html', charset='utf-8', headers=HTML_HEADERS)

async def serve_manage(request: web.Request):
    return web.Response(text=get_template('manage.html'), content_type='text/html', charset='utf-8', headers=HTML_HEADERS)

async def serve_admin(request: web.Request):
    return web.Response(text=get_template('admin.html'), content_type='text/html', charset='utf-8', headers=HTML_HEADERS)

def setup_views(app: web.Application):
    """تسجيل مسارات الصفحات في التطبيق"""
    app.router.add_get('/', serve_index)
    app.router.add_get('/pos', serve_pos)
    app.router.add_get('/manage', serve_manage)
    app.router.add_get('/admin', serve_admin)
    
