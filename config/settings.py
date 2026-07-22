from pathlib import Path
import os

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent

LOCAL_ENV_FILE = BASE_DIR / ".env.local"
PRODUCTION_ENV_FILE = BASE_DIR / ".env"
ENV_FILE_OVERRIDE = os.getenv("DJANGO_ENV_FILE", "").strip()

if ENV_FILE_OVERRIDE:
    ENV_FILE = Path(ENV_FILE_OVERRIDE).expanduser()

    if not ENV_FILE.is_absolute():
        ENV_FILE = BASE_DIR / ENV_FILE
elif LOCAL_ENV_FILE.exists():
    ENV_FILE = LOCAL_ENV_FILE
else:
    ENV_FILE = PRODUCTION_ENV_FILE

if not ENV_FILE.exists():
    raise RuntimeError(
        f"No se encontró el archivo de entorno: {ENV_FILE}"
    )

load_dotenv(ENV_FILE, override=False)


def env_bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return int(default)


def env_list(name, default=""):
    return [
        value.strip()
        for value in os.getenv(name, default).split(",")
        if value.strip()
    ]


DJANGO_ENV = os.getenv(
    "DJANGO_ENV",
    "local" if ENV_FILE == LOCAL_ENV_FILE else "production",
).strip().lower()

if DJANGO_ENV not in {
    "local",
    "production",
}:
    raise RuntimeError(
        "DJANGO_ENV debe ser 'local' o 'production'"
    )

IS_PRODUCTION = DJANGO_ENV == "production"


SECRET_KEY = os.getenv("SECRET_KEY", "").strip()

if not SECRET_KEY:
    raise RuntimeError(
        f"SECRET_KEY no está configurada en {ENV_FILE.name}"
    )


DEBUG = env_bool(
    "DEBUG",
    not IS_PRODUCTION,
)

if IS_PRODUCTION and DEBUG:
    raise RuntimeError(
        "DEBUG no puede estar activado en producción"
    )


ALLOWED_HOSTS = env_list(
    "ALLOWED_HOSTS",
    "127.0.0.1,localhost" if not IS_PRODUCTION else "",
)

CSRF_TRUSTED_ORIGINS = env_list(
    "CSRF_TRUSTED_ORIGINS",
    "",
)

if IS_PRODUCTION and not ALLOWED_HOSTS:
    raise RuntimeError(
        "ALLOWED_HOSTS no está configurado en producción"
    )


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.core",
    "apps.accounts.apps.AccountsConfig",
    "apps.documents",
    "apps.analysis",
    "apps.reports",
    "apps.certificates",
]


AUTH_USER_MODEL = "accounts.User"


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


TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [
            BASE_DIR / "templates",
        ],
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


WSGI_APPLICATION = "config.wsgi.application"


DB_ENGINE = os.getenv(
    "DB_ENGINE",
    "django.db.backends.postgresql",
).strip()


if DB_ENGINE == "django.db.backends.sqlite3":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }
else:
    DB_NAME = os.getenv("DB_NAME", "").strip()
    DB_USER = os.getenv("DB_USER", "").strip()
    DB_PASSWORD = os.getenv("DB_PASSWORD", "").strip()
    DB_HOST = os.getenv("DB_HOST", "127.0.0.1").strip()
    DB_PORT = os.getenv("DB_PORT", "5432").strip()
    DB_SSLMODE = os.getenv("DB_SSLMODE", "").strip()

    missing_database_variables = [
        variable
        for variable, value in {
            "DB_NAME": DB_NAME,
            "DB_USER": DB_USER,
            "DB_PASSWORD": DB_PASSWORD,
            "DB_HOST": DB_HOST,
            "DB_PORT": DB_PORT,
        }.items()
        if not value
    ]

    if missing_database_variables:
        raise RuntimeError(
            "Faltan variables de PostgreSQL: "
            + ", ".join(missing_database_variables)
        )

    DB_OPTIONS = {
        "connect_timeout": env_int(
            "DB_CONNECT_TIMEOUT",
            10,
        ),
    }

    if DB_SSLMODE:
        DB_OPTIONS["sslmode"] = DB_SSLMODE

    DATABASES = {
        "default": {
            "ENGINE": DB_ENGINE,
            "NAME": DB_NAME,
            "USER": DB_USER,
            "PASSWORD": DB_PASSWORD,
            "HOST": DB_HOST,
            "PORT": DB_PORT,
            "CONN_MAX_AGE": env_int(
                "DB_CONN_MAX_AGE",
                60 if IS_PRODUCTION else 0,
            ),
            "CONN_HEALTH_CHECKS": True,
            "ATOMIC_REQUESTS": env_bool(
                "DB_ATOMIC_REQUESTS",
                False,
            ),
            "OPTIONS": DB_OPTIONS,
        }
    }


AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "UserAttributeSimilarityValidator"
        ),
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "MinimumLengthValidator"
        ),
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "CommonPasswordValidator"
        ),
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "NumericPasswordValidator"
        ),
    },
]


LANGUAGE_CODE = "es-pe"

TIME_ZONE = "America/Lima"

USE_I18N = True

USE_TZ = True


STATIC_URL = "/static/"

STATIC_ROOT = BASE_DIR / "staticfiles"

STATICFILES_DIRS = (
    [BASE_DIR / "static"]
    if (BASE_DIR / "static").exists()
    else []
)


MEDIA_URL = "/media/"

MEDIA_ROOT = BASE_DIR / "media"


if DEBUG:
    STORAGES = {
        "default": {
            "BACKEND": (
                "django.core.files.storage.FileSystemStorage"
            ),
        },
        "staticfiles": {
            "BACKEND": (
                "django.contrib.staticfiles.storage."
                "StaticFilesStorage"
            ),
        },
    }
else:
    STORAGES = {
        "default": {
            "BACKEND": (
                "django.core.files.storage.FileSystemStorage"
            ),
        },
        "staticfiles": {
            "BACKEND": (
                "django.contrib.staticfiles.storage."
                "ManifestStaticFilesStorage"
            ),
        },
    }


DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


LOGIN_URL = "/"

LOGIN_REDIRECT_URL = "/documentos/subir/"

LOGOUT_REDIRECT_URL = "/"


SECURE_PROXY_SSL_HEADER = (
    "HTTP_X_FORWARDED_PROTO",
    "https",
)

USE_X_FORWARDED_HOST = env_bool(
    "USE_X_FORWARDED_HOST",
    False,
)

SECURE_SSL_REDIRECT = env_bool(
    "SECURE_SSL_REDIRECT",
    IS_PRODUCTION,
)

SESSION_COOKIE_SECURE = env_bool(
    "SESSION_COOKIE_SECURE",
    IS_PRODUCTION,
)

CSRF_COOKIE_SECURE = env_bool(
    "CSRF_COOKIE_SECURE",
    IS_PRODUCTION,
)

SESSION_COOKIE_HTTPONLY = True

SESSION_COOKIE_SAMESITE = "Lax"

CSRF_COOKIE_SAMESITE = "Lax"

SECURE_CONTENT_TYPE_NOSNIFF = True

SECURE_REFERRER_POLICY = "same-origin"

SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"

X_FRAME_OPTIONS = "DENY"


SECURE_HSTS_SECONDS = env_int(
    "SECURE_HSTS_SECONDS",
    0,
)

SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool(
    "SECURE_HSTS_INCLUDE_SUBDOMAINS",
    False,
)

SECURE_HSTS_PRELOAD = env_bool(
    "SECURE_HSTS_PRELOAD",
    False,
)


DATA_UPLOAD_MAX_MEMORY_SIZE = env_int(
    "DATA_UPLOAD_MAX_MEMORY_SIZE",
    52428800,
)

FILE_UPLOAD_MAX_MEMORY_SIZE = env_int(
    "FILE_UPLOAD_MAX_MEMORY_SIZE",
    5242880,
)

FILE_UPLOAD_PERMISSIONS = 0o644

FILE_UPLOAD_DIRECTORY_PERMISSIONS = 0o755


BRAVE_SEARCH_API_KEY = os.getenv(
    "BRAVE_SEARCH_API_KEY",
    "",
).strip()

WEB_ANALYSIS_ENABLED = env_bool(
    "WEB_ANALYSIS_ENABLED",
    True,
)

BRAVE_SEARCH_COUNTRY = os.getenv(
    "BRAVE_SEARCH_COUNTRY",
    "PE",
).strip()

BRAVE_SEARCH_LANG = os.getenv(
    "BRAVE_SEARCH_LANG",
    "es",
).strip()

WEB_ANALYSIS_TIMEOUT_SECONDS = env_int(
    "WEB_ANALYSIS_TIMEOUT_SECONDS",
    10,
)

WEB_ANALYSIS_MAX_QUERY_PHRASES = env_int(
    "WEB_ANALYSIS_MAX_QUERY_PHRASES",
    8,
)

WEB_ANALYSIS_MAX_PAGES_TOTAL = env_int(
    "WEB_ANALYSIS_MAX_PAGES_TOTAL",
    16,
)


LOG_LEVEL = os.getenv(
    "LOG_LEVEL",
    "INFO",
).strip().upper()


LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": (
                "{levelname} {asctime} "
                "{name} {module} {message}"
            ),
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
        },
    },
    "root": {
        "handlers": [
            "console",
        ],
        "level": LOG_LEVEL,
    },
    "loggers": {
        "django": {
            "handlers": [
                "console",
            ],
            "level": LOG_LEVEL,
            "propagate": False,
        },
        "django.request": {
            "handlers": [
                "console",
            ],
            "level": "ERROR",
            "propagate": False,
        },
    },
}