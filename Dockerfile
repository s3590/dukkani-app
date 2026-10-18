# استخدام نسخة بايثون خفيفة وسريعة مخصصة للسيرفرات
FROM python:3.11-slim

# إعداد مجلد العمل داخل الحاوية
WORKDIR /app

# تثبيت أدوات النظام الأساسية المطلوبة لقواعد البيانات
RUN apt-get update && apt-get install -y gcc libpq-dev curl && rm -rf /var/lib/apt/lists/*

# نسخ ملف المتطلبات وتثبيت المكتبات
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# نسخ باقي ملفات المشروع إلى الحاوية
COPY . .

# فتح المنفذ الذي سيعمل عليه السيرفر
EXPOSE 8080

# أمر تشغيل السيرفر (محرك الإقلاع الذي راجعناه)
CMD ["python", "run.py"]
