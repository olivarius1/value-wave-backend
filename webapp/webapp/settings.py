"""
Django 设置 —— 本地单用户工具：SQLite + 无登录 + runserver 直跑。
scripts/ 目录加入 sys.path，使 Web 层可直接复用既有数据脚本模块
（kline_store / fin_store / scan_watchlist / score_factors ...）。
"""
import os
import sys
from pathlib import Path

# backend/ 目录（webapp 的上一级，即数据脚本所在项目根）
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = BACKEND_DIR / 'scripts'

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

SECRET_KEY = os.environ.get(
    'DJANGO_SECRET_KEY',
    'local-only-0xvalve-wave-web-console-key')
DEBUG = True
ALLOWED_HOSTS = ['127.0.0.1', 'localhost']

INSTALLED_APPS = [
    'django.contrib.staticfiles',
    'dashboard',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'webapp.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
            ],
        },
    },
]

WSGI_APPLICATION = 'webapp.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BACKEND_DIR / 'webapp' / 'db.sqlite3',
    }
}

AUTH_PASSWORD_VALIDATORS = []
LANGUAGE_CODE = 'zh-hans'
TIME_ZONE = 'Asia/Shanghai'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATICFILES_DIRS = [BACKEND_DIR / 'webapp' / 'dashboard' / 'static']

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
X_FRAME_OPTIONS = 'SAMEORIGIN'
CSRF_COOKIE_HTTPONLY = False

# 数据脚本产品物目录（报告 HTML / 估值 JSON），views 直接读
REPORTS_DIR = BACKEND_DIR / 'artifacts' / 'reports'
JSON_DATA_DIR = BACKEND_DIR / 'artifacts' / 'json_data'

# 报告缓存有效期（小时）：报告页打开时若产物过期则自动后台重建（另有"数据落后"硬判据）
REPORT_TTL_HOURS = int(os.environ.get('VALVE_REPORT_TTL_HOURS', '8'))
