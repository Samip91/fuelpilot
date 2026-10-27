"""Django settings for FuelPilot, read from environment variables (and `.env` if present)."""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

from fuelplan.http import require_http_base_url

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_seconds(name: str, default: int, *, low: int = 1, high: int = 30) -> int:
    """Read a timeout and clamp it, so a typo can never disable a deadline."""
    raw = os.environ.get(name, "").strip()
    # isascii + isdecimal: "²".isdigit() is True but int("²") raises.
    seconds = int(raw) if raw.isascii() and raw.isdecimal() else default
    return max(low, min(high, seconds))


def _env_positive_int(name: str, default: int) -> int:
    """Read a whole number of at least 1; anything else stops startup with a clear error."""
    raw = os.environ.get(name, "").strip()
    if raw == "":
        return default
    if not (raw.isascii() and raw.isdecimal()) or int(raw) < 1:
        raise ImproperlyConfigured(f"{name} must be a whole number of at least 1, got {raw!r}.")
    return int(raw)


def _env_base_url(name: str, default: str) -> str:
    """A routing base URL from the env, checked once at startup (FR-12); blank means default."""
    raw = os.environ.get(name, "").strip() or default
    try:
        return require_http_base_url(raw)
    except ValueError as error:
        raise ImproperlyConfigured(f"{name} {error}.") from None


DEBUG = _env_bool("FUELPILOT_DEBUG", default=False)

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "").strip()
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY is required when FUELPILOT_DEBUG is off (see .env.example)."
        )
    SECRET_KEY = "dev-only-insecure-key"

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("FUELPILOT_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if host.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "rest_framework",
    "fuelplan",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        # Wait at most 2 s for a lock (cache writes and the call slot share the file), so
        # the 2 s provider-slot wait stays a bound of about 4 s, not Python's 5 s default.
        "OPTIONS": {"timeout": 2},
    }
}

# Saved plans and routes live in the database cache, so they work across workers
# without Redis (spec 01 §6, D9).
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "fuelpilot_cache",
        "OPTIONS": {"MAX_ENTRIES": 10_000},
    }
}

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": [],
    # The throttle is set on the POST view only, so other pages never share its bucket.
    "DEFAULT_THROTTLE_RATES": {"anon": "30/min"},
    # 0 = use REMOTE_ADDR; None would trust a spoofable X-Forwarded-For header.
    "NUM_PROXIES": 0,
    # Every exception inside an API view becomes the JSON error envelope (§7a).
    "EXCEPTION_HANDLER": "fuelplan.errors.api_exception_handler",
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# FuelPilot settings (spec 01 §11).
FUELPILOT_ROUTING_BASE_URL = _env_base_url(
    "FUELPILOT_ROUTING_BASE_URL", default="https://router.project-osrm.org"
)
FUELPILOT_ROUTING_CONNECT_TIMEOUT_S = _env_seconds("FUELPILOT_ROUTING_CONNECT_TIMEOUT_S", 3)
FUELPILOT_ROUTING_READ_TIMEOUT_S = _env_seconds("FUELPILOT_ROUTING_READ_TIMEOUT_S", 10)
FUELPILOT_PLAN_TTL_S = _env_positive_int("FUELPILOT_PLAN_TTL_S", default=604_800)
# FR-44 seam: a dotted path, not an env var; v1 ships only the no-op.
FUELPILOT_DETOUR_VERIFIER = "fuelplan.verification.verify_detours_noop"
GEOAPIFY_API_KEY = os.environ.get("GEOAPIFY_API_KEY", "").strip()
# Owner-only batch geocoding (`geocode_stations`); its jobs are slower than one route call.
GEOAPIFY_BASE_URL = (
    os.environ.get("GEOAPIFY_BASE_URL", "").strip() or "https://api.geoapify.com"
).rstrip("/")
GEOAPIFY_CONNECT_TIMEOUT_S = _env_seconds("GEOAPIFY_CONNECT_TIMEOUT_S", 10, high=60)
GEOAPIFY_TOTAL_TIMEOUT_S = _env_seconds("GEOAPIFY_TOTAL_TIMEOUT_S", 30, high=60)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {"fuelplan": {"handlers": ["console"], "level": "INFO"}},
}
