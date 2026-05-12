from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus


@dataclass(frozen=True)
class Settings:
    database_host: str
    database_port: int
    database_name: str
    database_user: str
    database_password: str
    database_schema: str
    database_echo: bool
    database_use_null_pool: bool
    database_pool_size: int
    database_max_overflow: int
    database_pool_timeout: int
    database_pool_recycle: int
    workflow_v2_enabled: bool
    workflow_v2_execute_m2: bool
    workflow_v2_max_steps: int
    celery_enabled: bool
    celery_broker_url: str
    celery_result_backend: str | None
    celery_dispatch_interval_seconds: int
    celery_dispatch_batch_size: int
    celery_dispatch_queue: str
    celery_execute_queue: str
    celery_heartbeat_queue: str
    celery_beat_heartbeat_enabled: bool
    celery_beat_dispatch_enabled: bool
    celery_dispatch_workspace_uuid: str | None
    celery_task_always_eager: bool
    celery_health_heartbeat_key: str
    celery_health_heartbeat_ttl_seconds: int
    celery_generate_file_enabled: bool
    celery_generate_file_scan_enabled: bool
    celery_generate_file_scan_interval_seconds: int
    celery_generate_file_run_queue: str
    celery_generate_file_scan_queue: str
    celery_generate_file_scan_batch_size: int
    celery_generate_file_stale_processing_minutes: int
    celery_generate_file_workspace_uuid: str | None
    celery_fileapp_ingest_enabled: bool
    celery_s3_files_ingest_queue: str
    celery_source_list_ingest_queue: str
    celery_fileapp_mailing_assoc_queue: str
    celery_fileapp_mailing_assoc_delay_seconds: int
    orch_lab_workspace_uuid: str | None
    orch_default_workspace_uuid: str | None
    sync_ws_client_id: str | None
    sync_ws_client_secret: str | None
    sync_webhook_base_url: str | None
    sync_ws_timeout_seconds: float
    target_core_api_bearer_token: str | None
    otima_llm_api_base_url: str | None
    otima_llm_api_gateway: str | None
    otima_llm_api_key: str | None
    otima_llm_api_timeout_seconds: float
    orch_queue_profile: str

    @property
    def psycopg_dsn(self) -> str:
        user = quote_plus(self.database_user)
        password = quote_plus(self.database_password)
        host = quote_plus(self.database_host)
        db_name = quote_plus(self.database_name)
        return f"postgresql://{user}:{password}@{host}:{self.database_port}/{db_name}?application_name=orch"

    @property
    def sqlalchemy_async_dsn(self) -> str:
        user = quote_plus(self.database_user)
        password = quote_plus(self.database_password)
        host = quote_plus(self.database_host)
        db_name = quote_plus(self.database_name)

        return f"postgresql+asyncpg://{user}:{password}@{host}:{self.database_port}/{db_name}"


def _read_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        raise ValueError(f"Variável obrigatória não definida: {name}")
    return value


def _read_env_optional(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None:
        return default
    stripped = value.strip()
    if not stripped:
        return default
    return stripped


def _read_env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _read_env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Valor inválido para {name}: {raw}")


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, raw_value = line.split("=", 1)
        key = key.strip()
        value = raw_value.strip().strip("'").strip('"')

        if key and key not in os.environ:
            os.environ[key] = value


def _resolve_queue_profile() -> str:
    raw_profile = (_read_env_optional("ORCH_QUEUE_PROFILE", "auto") or "auto").strip().lower()
    if raw_profile in {"prod", "production"}:
        return "prod"
    if raw_profile in {"launchd_local", "local_launchd", "dev_launchd"}:
        return "launchd_local"
    if raw_profile in {"f5_local", "dev_f5_local"}:
        return "f5_local"
    if raw_profile in {"local", "dev"}:
        return "launchd_local"
    if raw_profile != "auto":
        raise ValueError(f"Valor inválido para ORCH_QUEUE_PROFILE: {raw_profile}")

    is_darwin = platform.system().strip().lower() == "darwin"
    return "launchd_local" if is_darwin else "prod"


def _default_queue_by_profile(profile: str, queue_key: str) -> str:
    base = {
        "dispatch": "orch_dispatch",
        "execute": "orch_execute",
        "heartbeat": "orch_heartbeat",
        "fileapp_ingest": "orch_fileapp_ingest_events",
        "fileapp_process": "orch_fileapp_source_list_ingest",
        "fileapp_mailing_assoc": "orch_fileapp_mailing_assoc",
        "generate_file_run": "orch_component_generate_file_run",
        "generate_file_scan": "orch_component_generate_file_scan",
    }
    if queue_key not in base:
        raise ValueError(f"Queue key desconhecida: {queue_key}")
    if profile == "prod":
        return base[queue_key]
    if profile == "launchd_local":
        mapping = {
            "dispatch": "orch_dispatch_launchd_local",
            "execute": "orch_execute_launchd_local",
            "heartbeat": "orch_heartbeat_launchd_local",
            "fileapp_ingest": "orch_fileapp_ingest_launchd_local",
            "fileapp_process": "orch_fileapp_source_list_launchd_local",
            "fileapp_mailing_assoc": "orch_fileapp_mailing_assoc_launchd_local",
            "generate_file_run": "orch_component_generate_file_run_launchd_local",
            "generate_file_scan": "orch_component_generate_file_scan_launchd_local",
        }
        return mapping[queue_key]
    if profile == "f5_local":
        mapping = {
            "dispatch": "orch_dispatch_f5_local",
            "execute": "orch_execute_f5_local",
            "heartbeat": "orch_heartbeat_f5_local",
            "fileapp_ingest": "orch_fileapp_ingest_f5_local",
            "fileapp_process": "orch_fileapp_source_list_f5_local",
            "fileapp_mailing_assoc": "orch_fileapp_mailing_assoc_f5_local",
            "generate_file_run": "orch_component_generate_file_run_f5_local",
            "generate_file_scan": "orch_component_generate_file_scan_f5_local",
        }
        return mapping[queue_key]
    raise ValueError(f"Perfil de fila não suportado: {profile}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_dotenv(Path(".env"))
    queue_profile = _resolve_queue_profile()
    rabbitmq_host = _read_env_optional("RABBITMQ_HOST")
    rabbitmq_port = _read_env_optional("RABBITMQ_PORT", "5672")
    rabbitmq_user = _read_env_optional("RABBITMQ_USER")
    rabbitmq_password = _read_env_optional("RABBITMQ_PASSWORD")

    broker_url = _read_env_optional("CELERY_BROKER_URL")
    if broker_url is None and rabbitmq_host and rabbitmq_user and rabbitmq_password:
        rabbit_user = quote_plus(rabbitmq_user)
        rabbit_password = quote_plus(rabbitmq_password)
        rabbit_host = quote_plus(rabbitmq_host)
        broker_url = f"amqp://{rabbit_user}:{rabbit_password}@{rabbit_host}:{rabbitmq_port}//"
    if broker_url is None:
        broker_url = "memory://"

    result_backend = _read_env_optional("CELERY_RESULT_BACKEND", _read_env_optional("REDIS_URL"))

    return Settings(
        database_host=_read_env("DATABASE_HOST"),
        database_port=int(_read_env("DATABASE_PORT")),
        database_name=_read_env("DATABASE_NAME"),
        database_user=_read_env("DATABASE_USER"),
        database_password=_read_env("DATABASE_PASSWORD"),
        database_schema=_read_env("DATABASE_SCHEMA"),
        database_echo=_read_env_bool("DATABASE_ECHO", False),
        database_use_null_pool=_read_env_bool("DATABASE_USE_NULL_POOL", True),
        database_pool_size=_read_env_int("DATABASE_POOL_SIZE", 20),
        database_max_overflow=_read_env_int("DATABASE_MAX_OVERFLOW", 40),
        database_pool_timeout=_read_env_int("DATABASE_POOL_TIMEOUT", 30),
        database_pool_recycle=_read_env_int("DATABASE_POOL_RECYCLE", 1800),
        workflow_v2_enabled=_read_env_bool("WORKFLOW_V2_ENABLED", False),
        workflow_v2_execute_m2=_read_env_bool("WORKFLOW_V2_EXECUTE_M2", False),
        workflow_v2_max_steps=_read_env_int("WORKFLOW_V2_MAX_STEPS", 25),
        celery_enabled=_read_env_bool("CELERY_ENABLED", False),
        celery_broker_url=broker_url,
        celery_result_backend=result_backend,
        celery_dispatch_interval_seconds=_read_env_int("CELERY_DISPATCH_INTERVAL_SECONDS", 2),
        celery_dispatch_batch_size=_read_env_int("CELERY_DISPATCH_BATCH_SIZE", 100),
        celery_dispatch_queue=(
            _read_env_optional("CELERY_DISPATCH_QUEUE", _default_queue_by_profile(queue_profile, "dispatch"))
            or _default_queue_by_profile(queue_profile, "dispatch")
        ),
        celery_execute_queue=(
            _read_env_optional("CELERY_EXECUTE_QUEUE", _default_queue_by_profile(queue_profile, "execute"))
            or _default_queue_by_profile(queue_profile, "execute")
        ),
        celery_heartbeat_queue=(
            _read_env_optional("CELERY_HEARTBEAT_QUEUE", _default_queue_by_profile(queue_profile, "heartbeat"))
            or _default_queue_by_profile(queue_profile, "heartbeat")
        ),
        celery_beat_heartbeat_enabled=_read_env_bool("CELERY_BEAT_HEARTBEAT_ENABLED", True),
        celery_beat_dispatch_enabled=_read_env_bool("CELERY_BEAT_DISPATCH_ENABLED", True),
        celery_dispatch_workspace_uuid=_read_env_optional("CELERY_DISPATCH_WORKSPACE_UUID"),
        celery_task_always_eager=_read_env_bool("CELERY_TASK_ALWAYS_EAGER", False),
        celery_health_heartbeat_key=_read_env_optional("CELERY_HEARTBEAT_KEY", "orch:beat:heartbeat") or "orch:beat:heartbeat",
        celery_health_heartbeat_ttl_seconds=_read_env_int("CELERY_HEARTBEAT_TTL_SECONDS", 30),
        celery_generate_file_enabled=_read_env_bool("CELERY_GENERATE_FILE_ENABLED", True),
        celery_generate_file_scan_enabled=_read_env_bool("CELERY_GENERATE_FILE_SCAN_ENABLED", True),
        celery_generate_file_scan_interval_seconds=_read_env_int("CELERY_GENERATE_FILE_SCAN_INTERVAL_SECONDS", 10),
        celery_generate_file_run_queue=(
            _read_env_optional("CELERY_GENERATE_FILE_RUN_QUEUE", _default_queue_by_profile(queue_profile, "generate_file_run"))
            or _default_queue_by_profile(queue_profile, "generate_file_run")
        ),
        celery_generate_file_scan_queue=(
            _read_env_optional("CELERY_GENERATE_FILE_SCAN_QUEUE", _default_queue_by_profile(queue_profile, "generate_file_scan"))
            or _default_queue_by_profile(queue_profile, "generate_file_scan")
        ),
        celery_generate_file_scan_batch_size=_read_env_int("CELERY_GENERATE_FILE_SCAN_BATCH_SIZE", 200),
        celery_generate_file_stale_processing_minutes=_read_env_int("CELERY_GENERATE_FILE_STALE_PROCESSING_MINUTES", 5),
        celery_generate_file_workspace_uuid=_read_env_optional("CELERY_GENERATE_FILE_WORKSPACE_UUID"),
        celery_fileapp_ingest_enabled=_read_env_bool("CELERY_FILEAPP_INGEST_ENABLED", True),
        celery_s3_files_ingest_queue=(
            _read_env_optional("CELERY_S3_FILES_INGEST_QUEUE", _default_queue_by_profile(queue_profile, "fileapp_ingest"))
            or _default_queue_by_profile(queue_profile, "fileapp_ingest")
        ),
        celery_source_list_ingest_queue=(
            _read_env_optional("CELERY_SOURCE_LIST_INGEST_QUEUE", _default_queue_by_profile(queue_profile, "fileapp_process"))
            or _default_queue_by_profile(queue_profile, "fileapp_process")
        ),
        celery_fileapp_mailing_assoc_queue=(
            _read_env_optional("CELERY_FILEAPP_MAILING_ASSOC_QUEUE", _default_queue_by_profile(queue_profile, "fileapp_mailing_assoc"))
            or _default_queue_by_profile(queue_profile, "fileapp_mailing_assoc")
        ),
        celery_fileapp_mailing_assoc_delay_seconds=_read_env_int("CELERY_FILEAPP_MAILING_ASSOC_DELAY_SECONDS", 20),
        orch_lab_workspace_uuid=_read_env_optional("ORCH_LAB_WORKSPACE_UUID"),
        orch_default_workspace_uuid=_read_env_optional(
            "ORCH_DEFAULT_WORKSPACE_UUID",
            _read_env_optional("ORCH_LAB_WORKSPACE_UUID"),
        ),
        sync_ws_client_id=_read_env_optional("SYNC_WS_CLIENT_ID"),
        sync_ws_client_secret=_read_env_optional("SYNC_WS_CLIENT_SECRET"),
        sync_webhook_base_url=_read_env_optional("SYNC_WEBHOOK_BASE_URL"),
        sync_ws_timeout_seconds=float(_read_env_optional("SYNC_WS_TIMEOUT_SECONDS", "5") or "5"),
        target_core_api_bearer_token=(
            _read_env_optional("TARGET_CORE_API_BEARER_TOKEN")
            or _read_env_optional("SYNC_WEBHOOK_BEARER_TOKEN")
        ),
        otima_llm_api_base_url=_read_env_optional("OTIMA_LLM_API_BASE_URL"),
        otima_llm_api_gateway=_read_env_optional("OTIMA_LLM_API_GATEWAY"),
        otima_llm_api_key=_read_env_optional("OTIMA_LLM_API_KEY"),
        otima_llm_api_timeout_seconds=float(_read_env_optional("OTIMA_LLM_API_TIMEOUT_SECONDS", "10.0") or "10.0"),
        orch_queue_profile=queue_profile,
    )
