"""Settings, read from environment variables (see .env.example)."""

import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).lower() in {"1", "true", "yes"}


def env_int(key: str, default: int) -> int:
    return int(os.getenv(key, default))


DEBUG = env_bool("DEBUG")
SECRET_KEY = os.getenv("SECRET_KEY") or ("dev-only-insecure-key" if DEBUG or os.getenv("PYTEST_VERSION") else None)
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY must be set when DEBUG is off")

ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
CSRF_TRUSTED_ORIGINS = [o for o in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if o]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "uploads",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "uploads.middleware.MethodOverrideMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "frontend_dist"],
        "APP_DIRS": False,
    }
]

DATABASES = {
    "default": dj_database_url.config(
        default="postgres://postgres:postgres@localhost:5440/uploads",
        conn_max_age=60,
        conn_health_checks=True,
    )
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

USE_TZ = True
TIME_ZONE = "UTC"

# The React build is served by WhiteNoise from the same origin, so tus needs no CORS setup
STATIC_URL = "/assets/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [d for d in [BASE_DIR / "frontend_dist" / "assets"] if d.exists()]
# Vite already puts content hashes in file names, so no manifest storage is needed;
# those files are served with a one-year immutable cache
STORAGES = {"staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"}}
WHITENOISE_IMMUTABLE_FILE_TEST = r"^.+-[0-9A-Za-z_-]{8}\.(js|css)$"

# Behind Render's proxy
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

# ---- Uploads ----

# "local" (files under MEDIA_ROOT) or "s3"
UPLOAD_STORAGE = os.getenv("UPLOAD_STORAGE", "local")
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", BASE_DIR / "media"))
S3_BUCKET = os.getenv("S3_BUCKET", "")
S3_PREFIX = os.getenv("S3_PREFIX", "uploads/")

MAX_UPLOAD_SIZE = env_int("MAX_UPLOAD_SIZE", 50 * 1024 * 1024)  # per file
MAX_CHUNK_SIZE = env_int("MAX_CHUNK_SIZE", 16 * 1024 * 1024)  # per PATCH request
UPLOAD_TTL_HOURS = env_int("UPLOAD_TTL_HOURS", 24)  # uploads and files are deleted after this
# Abuse limits for the public demo: they cap what the S3 bucket can ever hold
DAILY_BYTE_QUOTA = env_int("DAILY_BYTE_QUOTA", 1024 * 1024 * 1024)  # all clients, rolling 24 h
UPLOADS_PER_IP_PER_HOUR = env_int("UPLOADS_PER_IP_PER_HOUR", 20)

# Request bodies are streamed by the tus view; this only limits ordinary form/JSON bodies
DATA_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": os.getenv("LOG_LEVEL", "INFO")},
}
