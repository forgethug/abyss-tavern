import os
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(os.getenv('PLATFORM_ENV_FILE', str(BASE_DIR.parent / '.env')))


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "")
    JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "")
    JWT_EXPIRES_HOURS = int(os.getenv("JWT_EXPIRES_HOURS", "24"))
    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL",
        f"sqlite:///{(BASE_DIR / 'data' / 'platform_v2.db').as_posix()}",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    USERNAME_MIN_LENGTH = 3
    USERNAME_MAX_LENGTH = 32
    USERNAME_PATTERN = r"^[a-zA-Z0-9_]+$"
    PASSWORD_MIN_LENGTH = 6
    PASSWORD_MAX_LENGTH = 64
    DISPLAY_NAME_MAX_LENGTH = 64
    TAVERN_NAME_MAX_LENGTH = 80
    TAVERN_SLUG_MAX_LENGTH = 48
    TAVERN_SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
    DEFAULT_GAME_PACK = "s1_fantasy"
    DEFAULT_GAME_PACK_RELEASE = "s1-migration-preview"
    DEFAULT_RUNTIME_MODE = "dedicated_process"
    RUNTIME_AUTO_START = os.getenv(
        "RUNTIME_AUTO_START",
        os.getenv("S1_RUNTIME_AUTO_START", "true"),
    ).lower() == "true"
    RUNTIME_HOST = os.getenv(
        "RUNTIME_HOST",
        os.getenv("S1_RUNTIME_HOST", "127.0.0.1"),
    )
    RUNTIME_BASE_PORT = int(
        os.getenv(
            "RUNTIME_BASE_PORT",
            os.getenv("S1_RUNTIME_BASE_PORT", "5200"),
        )
    )
    RUNTIME_START_TIMEOUT_SECONDS = float(
        os.getenv(
            "RUNTIME_START_TIMEOUT_SECONDS",
            os.getenv("S1_RUNTIME_START_TIMEOUT_SECONDS", "60"),
        )
    )
    RUNTIME_HEALTH_CACHE_SECONDS = float(os.getenv("RUNTIME_HEALTH_CACHE_SECONDS", "5"))
    RUNTIME_IDENTITY_CACHE_MAX_USERS = int(os.getenv("RUNTIME_IDENTITY_CACHE_MAX_USERS", "1024"))
    RUNTIME_PROXY_TIMEOUT_SECONDS = float(os.getenv("RUNTIME_PROXY_TIMEOUT_SECONDS", "35"))
    RUNTIME_PROXY_ACTION_TIMEOUT_SECONDS = float(os.getenv("RUNTIME_PROXY_ACTION_TIMEOUT_SECONDS", "65"))
    RUNTIME_PROXY_ADMIN_TIMEOUT_SECONDS = float(os.getenv("RUNTIME_PROXY_ADMIN_TIMEOUT_SECONDS", "125"))
    RUNTIME_PROXY_EXPORT_TIMEOUT_SECONDS = float(os.getenv("RUNTIME_PROXY_EXPORT_TIMEOUT_SECONDS", "605"))
    RUNTIME_BOOTSTRAP_SECRET = os.getenv(
        "RUNTIME_BOOTSTRAP_SECRET",
        os.getenv(
            "S1_RUNTIME_BOOTSTRAP_SECRET",
            "",
        ),
    )
    RUNTIME_DATA_DIR = os.getenv(
        "RUNTIME_DATA_DIR",
        os.getenv(
            "S1_RUNTIME_DATA_DIR",
            str(BASE_DIR / "data" / "runtimes"),
        ),
    )
    RUNTIME_DATABASE_URL_TEMPLATE = os.getenv(
        "RUNTIME_DATABASE_URL_TEMPLATE",
        os.getenv("S1_RUNTIME_DATABASE_URL_TEMPLATE", ""),
    )
    RUNTIME_SEED_ON_START = os.getenv(
        "RUNTIME_SEED_ON_START",
        os.getenv("S1_RUNTIME_SEED_ON_START", "true"),
    ).lower() == "true"
    RUNTIME_SCHEDULER_ENABLED = os.getenv(
        "RUNTIME_SCHEDULER_ENABLED",
        os.getenv("S1_RUNTIME_SCHEDULER_ENABLED", "true"),
    ).lower() == "true"
    RUNTIME_MODEL_PROFILES_JSON = os.getenv(
        "RUNTIME_MODEL_PROFILES_JSON",
        os.getenv("ADVENTURER_S1_AI_PROFILES_JSON", "[]"),
    )
    RUNTIME_MODEL_ASSIGNMENTS_JSON = os.getenv(
        "RUNTIME_MODEL_ASSIGNMENTS_JSON",
        os.getenv("ADVENTURER_S1_AI_FEATURE_ASSIGNMENTS_JSON", "{}"),
    )
    RUNTIME_MODEL_REASONING_JSON = os.getenv(
        "RUNTIME_MODEL_REASONING_JSON",
        os.getenv("ADVENTURER_S1_AI_FEATURE_REASONING_JSON", "{}"),
    )


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SECRET_KEY = "test-flask-secret-not-for-deployment-32"
    JWT_SECRET_KEY = "test-jwt-secret-not-for-deployment-32"
    RUNTIME_BOOTSTRAP_SECRET = "test-bootstrap-secret-not-for-deployment-32"
    RUNTIME_AUTO_START = False
