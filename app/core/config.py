from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus
from uuid import UUID


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
    workflow_contextual_member_routing_enabled: bool
    workflow_v2_max_steps: int
    workflow_m2_loop_guard_repeat_threshold: int
    workflow_dialer_event_correlation_window_hours: int
    celery_enabled: bool
    celery_broker_url: str
    celery_result_backend: str | None
    celery_dispatch_interval_seconds: int
    celery_dispatch_batch_size: int
    celery_dispatch_queue: str
    celery_execute_queue: str
    celery_switch_bot_flow_queue: str
    celery_dialer_supplier_v2_queue: str
    celery_channel_supplier_v2_queue: str
    celery_heartbeat_queue: str
    celery_beat_heartbeat_enabled: bool
    celery_beat_dispatch_enabled: bool
    celery_beat_reconcile_pending_events_enabled: bool
    celery_beat_dialer_supplier_v2_reconcile_enabled: bool
    celery_beat_channel_supplier_v2_reconcile_enabled: bool
    celery_dispatch_workspace_uuid: str | None
    celery_reconcile_pending_events_workspace_uuid: str | None
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
    celery_beat_fileapp_post_process_reconcile_enabled: bool
    celery_fileapp_post_process_reconcile_interval_seconds: int
    celery_fileapp_post_process_reconcile_batch_size: int
    celery_fileapp_post_process_reconcile_workspace_uuid: str | None
    celery_fileapp_post_process_reconcile_cooldown_seconds: int
    celery_beat_fileapp_entrada_rescue_enabled: bool
    celery_fileapp_entrada_rescue_interval_seconds: int
    celery_fileapp_entrada_rescue_workspace_uuid: str | None
    celery_fileapp_entrada_rescue_batch_size: int
    celery_fileapp_entrada_rescue_grace_seconds: int
    celery_fileapp_entrada_rescue_fail_after_seconds: int
    celery_fileapp_entrada_rescue_max_retries: int
    celery_fileapp_entrada_rescue_lock_seconds: int
    celery_beat_fileapp_entrada_hygiene_enabled: bool
    celery_fileapp_entrada_hygiene_interval_seconds: int
    celery_fileapp_entrada_hygiene_workspace_uuid: str | None
    celery_fileapp_entrada_hygiene_batch_size: int
    celery_fileapp_entrada_hygiene_lock_seconds: int
    celery_fileapp_entrada_hygiene_no_source_list_sla_seconds: int
    celery_fileapp_entrada_hygiene_ready_to_ingest_sla_seconds: int
    celery_fileapp_entrada_hygiene_pending_field_mapping_sla_seconds: int
    celery_fileapp_entrada_hygiene_root_max_age_seconds: int
    celery_fileapp_entrada_hygiene_resubmit_cooldown_seconds: int
    celery_fileapp_entrada_hygiene_no_source_list_max_resubmits: int
    celery_reconcile_pending_events_interval_seconds: int
    celery_reconcile_pending_events_batch_size: int
    celery_reconcile_pending_events_stale_seconds: int
    celery_reconcile_pending_events_cooldown_seconds: int
    orch_billing_snapshot_enabled: bool
    orch_billing_rabbitmq_url: str | None
    orch_billing_exchange: str
    orch_billing_routing_key: str
    orch_billing_application_code: str
    orch_billing_service_code: str
    orch_billing_metric_code: str
    orch_billing_publish_interval_seconds: int
    orch_billing_publish_batch_size: int
    orch_billing_publish_max_attempts: int
    orch_billing_publish_timeout_seconds: float
    orch_billing_enabled: bool
    billing_batch_size: int
    billing_flush_interval_seconds: int
    billing_retry_scan_interval_seconds: int
    billing_processing_lease_seconds: int
    billing_retry_initial_seconds: int
    billing_retry_max_seconds: int
    billing_retry_jitter_seconds: int
    billing_reconcile_interval_seconds: int
    billing_reconcile_lookback_hours: int
    billing_reprocess_lease_seconds: int
    billing_reprocess_scan_interval_seconds: int
    billing_reprocess_chunk_size: int
    billing_publish_confirm_timeout_seconds: float
    billing_publish_claim_batch_size: int
    billing_rabbitmq_url: str | None
    billing_exchange: str
    billing_routing_key: str
    billing_application_code: str
    billing_service_code: str
    billing_metric_code: str
    billing_admin_client_id: str | None
    billing_admin_client_secret: str | None
    orch_observability_client_id: str | None
    orch_observability_client_secret: str | None
    orch_observability_max_window_hours: int
    orch_observability_statement_timeout_ms: int
    orch_observability_max_trace_steps: int
    celery_billing_queue: str
    orch_lab_workspace_uuid: str | None
    orch_default_workspace_uuid: str | None
    sync_ws_client_id: str | None
    sync_ws_client_secret: str | None
    arquivos_client_id: str | None
    arquivos_client_secret: str | None
    arquivos_base_url: str | None
    sync_webhook_base_url: str | None
    sync_ws_timeout_seconds: float
    target_core_api_base_url: str | None
    target_core_api_bearer_token: str | None
    target_core_supplier_api_base_url: str | None
    dialer_supplier_v2_enabled: bool
    dialer_supplier_v2_workspace_allowlist: tuple[str, ...]
    dialer_supplier_v2_flow_allowlist: tuple[str, ...]
    dialer_supplier_v2_http_timeout_seconds: float
    dialer_supplier_v2_max_attempts: int
    dialer_supplier_v2_retry_backoff_seconds: float
    dialer_supplier_v2_reconcile_interval_seconds: int
    dialer_supplier_v2_reconcile_batch_size: int
    dialer_supplier_v2_registration_lease_seconds: int
    channel_supplier_v2_enabled: bool
    channel_supplier_v2_workspace_allowlist: tuple[str, ...]
    channel_supplier_v2_flow_allowlist: tuple[str, ...]
    channel_supplier_v2_encryption_key: str | None
    channel_supplier_v2_encryption_key_id: str
    channel_supplier_v2_http_timeout_seconds: float
    channel_supplier_v2_max_attempts: int
    channel_supplier_v2_retry_backoff_seconds: float
    channel_supplier_v2_reconcile_interval_seconds: int
    channel_supplier_v2_reconcile_batch_size: int
    channel_supplier_v2_registration_lease_seconds: int
    orch_dialer_multilane_v2_enabled: bool
    orch_dialer_multilane_v2_flow_uuids: tuple[str, ...]
    orch_dialer_multilane_v2_max_lanes_per_flow: int
    orch_dialer_multilane_v2_max_execution_groups_per_flow: int
    restriction_list_check_http_timeout_seconds: float
    restriction_list_check_max_attempts: int
    restriction_list_check_retry_backoff_seconds: float
    switch_bot_flow_enabled: bool
    switch_bot_flow_http_timeout_seconds: float
    switch_bot_flow_max_attempts: int
    switch_bot_flow_retry_backoff_seconds: float
    otima_llm_api_base_url: str | None
    otima_llm_api_gateway: str | None
    otima_llm_api_key: str | None
    otima_llm_api_timeout_seconds: float
    orch_queue_profile: str
    docs_access_control_enabled: bool
    docs_internal_cidrs: tuple[str, ...]
    docs_trusted_proxy_cidrs: tuple[str, ...]
    docs_blocked_hosts: tuple[str, ...]

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


def _read_env_int_range(name: str, default: int, *, minimum: int, maximum: int) -> int:
    value = _read_env_int(name, default)
    if value < minimum or value > maximum:
        raise ValueError(f"Valor inválido para {name}: esperado entre {minimum} e {maximum}.")
    return value


def _read_env_float_range(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = _read_env_optional(name)
    value = default if raw is None else float(raw)
    if value < minimum or value > maximum:
        raise ValueError(f"Valor inválido para {name}: esperado entre {minimum} e {maximum}.")
    return value


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


def _read_env_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    return values or default


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
        "switch_bot_flow": "orch_switch_bot_flow",
        "dialer_supplier_v2": "orch_dialer_supplier_v2",
        "channel_supplier_v2": "orch_channel_supplier_v2",
        "heartbeat": "orch_heartbeat",
        "fileapp_ingest": "orch_fileapp_ingest_events",
        "fileapp_process": "orch_fileapp_source_list_ingest",
        "fileapp_mailing_assoc": "orch_fileapp_mailing_assoc",
        "generate_file_run": "orch_component_generate_file_run",
        "generate_file_scan": "orch_component_generate_file_scan",
        "billing": "orch.billing.outbox",
    }
    if queue_key not in base:
        raise ValueError(f"Queue key desconhecida: {queue_key}")
    if profile == "prod":
        return base[queue_key]
    if profile == "launchd_local":
        mapping = {
            "dispatch": "orch_dispatch_launchd_local",
            "execute": "orch_execute_launchd_local",
            "switch_bot_flow": "orch_switch_bot_flow_launchd_local",
            "dialer_supplier_v2": "orch_dialer_supplier_v2_launchd_local",
            "channel_supplier_v2": "orch_channel_supplier_v2_launchd_local",
            "heartbeat": "orch_heartbeat_launchd_local",
            "fileapp_ingest": "orch_fileapp_ingest_launchd_local",
            "fileapp_process": "orch_fileapp_source_list_launchd_local",
            "fileapp_mailing_assoc": "orch_fileapp_mailing_assoc_launchd_local",
            "generate_file_run": "orch_component_generate_file_run_launchd_local",
            "generate_file_scan": "orch_component_generate_file_scan_launchd_local",
            "billing": "orch.billing.outbox_launchd_local",
        }
        return mapping[queue_key]
    if profile == "f5_local":
        mapping = {
            "dispatch": "orch_dispatch_f5_local",
            "execute": "orch_execute_f5_local",
            "switch_bot_flow": "orch_switch_bot_flow_f5_local",
            "dialer_supplier_v2": "orch_dialer_supplier_v2_f5_local",
            "channel_supplier_v2": "orch_channel_supplier_v2_f5_local",
            "heartbeat": "orch_heartbeat_f5_local",
            "fileapp_ingest": "orch_fileapp_ingest_f5_local",
            "fileapp_process": "orch_fileapp_source_list_f5_local",
            "fileapp_mailing_assoc": "orch_fileapp_mailing_assoc_f5_local",
            "generate_file_run": "orch_component_generate_file_run_f5_local",
            "generate_file_scan": "orch_component_generate_file_scan_f5_local",
            "billing": "orch.billing.outbox_f5_local",
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
    legacy_billing_enabled = _read_env_bool("ORCH_BILLING_SNAPSHOT_ENABLED", False)
    billing_enabled = _read_env_bool("ORCH_BILLING_ENABLED", False)
    if legacy_billing_enabled and billing_enabled:
        raise ValueError(
            "ORCH_BILLING_SNAPSHOT_ENABLED e ORCH_BILLING_ENABLED não podem estar ativos ao mesmo tempo."
        )

    settings = Settings(
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
        workflow_contextual_member_routing_enabled=_read_env_bool(
            "WORKFLOW_CONTEXTUAL_MEMBER_ROUTING_ENABLED",
            False,
        ),
        workflow_v2_max_steps=_read_env_int("WORKFLOW_V2_MAX_STEPS", 25),
        workflow_m2_loop_guard_repeat_threshold=_read_env_int("WORKFLOW_M2_LOOP_GUARD_REPEAT_THRESHOLD", 300),
        workflow_dialer_event_correlation_window_hours=_read_env_int("WORKFLOW_DIALER_EVENT_CORRELATION_WINDOW_HOURS", 24),
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
        celery_switch_bot_flow_queue=(
            _read_env_optional(
                "CELERY_SWITCH_BOT_FLOW_QUEUE",
                _default_queue_by_profile(queue_profile, "switch_bot_flow"),
            )
            or _default_queue_by_profile(queue_profile, "switch_bot_flow")
        ),
        celery_dialer_supplier_v2_queue=(
            _read_env_optional(
                "CELERY_DIALER_SUPPLIER_V2_QUEUE",
                _default_queue_by_profile(queue_profile, "dialer_supplier_v2"),
            )
            or _default_queue_by_profile(queue_profile, "dialer_supplier_v2")
        ),
        celery_channel_supplier_v2_queue=(
            _read_env_optional(
                "CELERY_CHANNEL_SUPPLIER_V2_QUEUE",
                _default_queue_by_profile(queue_profile, "channel_supplier_v2"),
            )
            or _default_queue_by_profile(queue_profile, "channel_supplier_v2")
        ),
        celery_heartbeat_queue=(
            _read_env_optional("CELERY_HEARTBEAT_QUEUE", _default_queue_by_profile(queue_profile, "heartbeat"))
            or _default_queue_by_profile(queue_profile, "heartbeat")
        ),
        celery_beat_heartbeat_enabled=_read_env_bool("CELERY_BEAT_HEARTBEAT_ENABLED", True),
        celery_beat_dispatch_enabled=_read_env_bool("CELERY_BEAT_DISPATCH_ENABLED", True),
        celery_beat_reconcile_pending_events_enabled=_read_env_bool("CELERY_BEAT_RECONCILE_PENDING_EVENTS_ENABLED", True),
        celery_beat_dialer_supplier_v2_reconcile_enabled=_read_env_bool(
            "CELERY_BEAT_DIALER_SUPPLIER_V2_RECONCILE_ENABLED", False
        ),
        celery_beat_channel_supplier_v2_reconcile_enabled=_read_env_bool(
            "CELERY_BEAT_CHANNEL_SUPPLIER_V2_RECONCILE_ENABLED", False
        ),
        celery_dispatch_workspace_uuid=_read_env_optional("CELERY_DISPATCH_WORKSPACE_UUID"),
        celery_reconcile_pending_events_workspace_uuid=_read_env_optional(
            "CELERY_RECONCILE_PENDING_EVENTS_WORKSPACE_UUID",
            _read_env_optional("CELERY_DISPATCH_WORKSPACE_UUID"),
        ),
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
        celery_beat_fileapp_post_process_reconcile_enabled=_read_env_bool(
            "CELERY_BEAT_FILEAPP_POST_PROCESS_RECONCILE_ENABLED",
            True,
        ),
        celery_fileapp_post_process_reconcile_interval_seconds=_read_env_int(
            "CELERY_FILEAPP_POST_PROCESS_RECONCILE_INTERVAL_SECONDS",
            60,
        ),
        celery_fileapp_post_process_reconcile_batch_size=_read_env_int(
            "CELERY_FILEAPP_POST_PROCESS_RECONCILE_BATCH_SIZE",
            100,
        ),
        celery_fileapp_post_process_reconcile_workspace_uuid=_read_env_optional(
            "CELERY_FILEAPP_POST_PROCESS_RECONCILE_WORKSPACE_UUID",
            _read_env_optional("CELERY_DISPATCH_WORKSPACE_UUID"),
        ),
        celery_fileapp_post_process_reconcile_cooldown_seconds=_read_env_int(
            "CELERY_FILEAPP_POST_PROCESS_RECONCILE_COOLDOWN_SECONDS",
            120,
        ),
        celery_beat_fileapp_entrada_rescue_enabled=_read_env_bool(
            "CELERY_BEAT_FILEAPP_ENTRADA_RESCUE_ENABLED",
            False,
        ),
        celery_fileapp_entrada_rescue_interval_seconds=_read_env_int(
            "CELERY_FILEAPP_ENTRADA_RESCUE_INTERVAL_SECONDS",
            60,
        ),
        celery_fileapp_entrada_rescue_workspace_uuid=_read_env_optional(
            "CELERY_FILEAPP_ENTRADA_RESCUE_WORKSPACE_UUID",
            _read_env_optional("CELERY_DISPATCH_WORKSPACE_UUID"),
        ),
        celery_fileapp_entrada_rescue_batch_size=_read_env_int(
            "CELERY_FILEAPP_ENTRADA_RESCUE_BATCH_SIZE",
            100,
        ),
        celery_fileapp_entrada_rescue_grace_seconds=_read_env_int(
            "CELERY_FILEAPP_ENTRADA_RESCUE_GRACE_SECONDS",
            600,
        ),
        celery_fileapp_entrada_rescue_fail_after_seconds=_read_env_int(
            "CELERY_FILEAPP_ENTRADA_RESCUE_FAIL_AFTER_SECONDS",
            3600,
        ),
        celery_fileapp_entrada_rescue_max_retries=_read_env_int(
            "CELERY_FILEAPP_ENTRADA_RESCUE_MAX_RETRIES",
            3,
        ),
        celery_fileapp_entrada_rescue_lock_seconds=_read_env_int(
            "CELERY_FILEAPP_ENTRADA_RESCUE_LOCK_SECONDS",
            120,
        ),
        celery_beat_fileapp_entrada_hygiene_enabled=_read_env_bool(
            "CELERY_BEAT_FILEAPP_ENTRADA_HYGIENE_ENABLED",
            False,
        ),
        celery_fileapp_entrada_hygiene_interval_seconds=_read_env_int(
            "CELERY_FILEAPP_ENTRADA_HYGIENE_INTERVAL_SECONDS",
            300,
        ),
        celery_fileapp_entrada_hygiene_workspace_uuid=_read_env_optional(
            "CELERY_FILEAPP_ENTRADA_HYGIENE_WORKSPACE_UUID",
            _read_env_optional("CELERY_FILEAPP_ENTRADA_RESCUE_WORKSPACE_UUID"),
        ),
        celery_fileapp_entrada_hygiene_batch_size=_read_env_int(
            "CELERY_FILEAPP_ENTRADA_HYGIENE_BATCH_SIZE",
            200,
        ),
        celery_fileapp_entrada_hygiene_lock_seconds=_read_env_int(
            "CELERY_FILEAPP_ENTRADA_HYGIENE_LOCK_SECONDS",
            180,
        ),
        celery_fileapp_entrada_hygiene_no_source_list_sla_seconds=_read_env_int(
            "CELERY_FILEAPP_ENTRADA_HYGIENE_NO_SOURCE_LIST_SLA_SECONDS",
            600,
        ),
        celery_fileapp_entrada_hygiene_ready_to_ingest_sla_seconds=_read_env_int(
            "CELERY_FILEAPP_ENTRADA_HYGIENE_READY_TO_INGEST_SLA_SECONDS",
            1200,
        ),
        celery_fileapp_entrada_hygiene_pending_field_mapping_sla_seconds=_read_env_int(
            "CELERY_FILEAPP_ENTRADA_HYGIENE_PENDING_FIELD_MAPPING_SLA_SECONDS",
            1800,
        ),
        celery_fileapp_entrada_hygiene_root_max_age_seconds=_read_env_int(
            "CELERY_FILEAPP_ENTRADA_HYGIENE_ROOT_MAX_AGE_SECONDS",
            1800,
        ),
        celery_fileapp_entrada_hygiene_resubmit_cooldown_seconds=_read_env_int(
            "CELERY_FILEAPP_ENTRADA_HYGIENE_RESUBMIT_COOLDOWN_SECONDS",
            300,
        ),
        celery_fileapp_entrada_hygiene_no_source_list_max_resubmits=_read_env_int(
            "CELERY_FILEAPP_ENTRADA_HYGIENE_NO_SOURCE_LIST_MAX_RESUBMITS",
            1,
        ),
        celery_reconcile_pending_events_interval_seconds=_read_env_int("CELERY_RECONCILE_PENDING_EVENTS_INTERVAL_SECONDS", 15),
        celery_reconcile_pending_events_batch_size=_read_env_int("CELERY_RECONCILE_PENDING_EVENTS_BATCH_SIZE", 200),
        celery_reconcile_pending_events_stale_seconds=_read_env_int("CELERY_RECONCILE_PENDING_EVENTS_STALE_SECONDS", 30),
        celery_reconcile_pending_events_cooldown_seconds=_read_env_int("CELERY_RECONCILE_PENDING_EVENTS_COOLDOWN_SECONDS", 30),
        orch_billing_snapshot_enabled=legacy_billing_enabled,
        orch_billing_rabbitmq_url=_read_env_optional("ORCH_BILLING_RABBITMQ_URL"),
        orch_billing_exchange=_read_env_optional("ORCH_BILLING_EXCHANGE", "domain.events") or "domain.events",
        orch_billing_routing_key=(
            _read_env_optional("ORCH_BILLING_ROUTING_KEY", "billing.usage.snapshot.v1.target")
            or "billing.usage.snapshot.v1.target"
        ),
        orch_billing_application_code=_read_env_optional("ORCH_BILLING_APPLICATION_CODE", "target") or "target",
        orch_billing_service_code=_read_env_optional("ORCH_BILLING_SERVICE_CODE", "service-orch") or "service-orch",
        orch_billing_metric_code=_read_env_optional("ORCH_BILLING_METRIC_CODE", "service-orch") or "service-orch",
        orch_billing_publish_interval_seconds=_read_env_int("ORCH_BILLING_PUBLISH_INTERVAL_SECONDS", 10),
        orch_billing_publish_batch_size=_read_env_int("ORCH_BILLING_PUBLISH_BATCH_SIZE", 100),
        orch_billing_publish_max_attempts=_read_env_int("ORCH_BILLING_PUBLISH_MAX_ATTEMPTS", 3),
        orch_billing_publish_timeout_seconds=float(
            _read_env_optional("ORCH_BILLING_PUBLISH_TIMEOUT_SECONDS", "3") or "3"
        ),
        orch_billing_enabled=billing_enabled,
        billing_batch_size=_read_env_int_range("BILLING_BATCH_SIZE", 200, minimum=1, maximum=1000),
        billing_flush_interval_seconds=_read_env_int_range(
            "BILLING_FLUSH_INTERVAL_SECONDS", 300, minimum=5, maximum=86400
        ),
        billing_retry_scan_interval_seconds=_read_env_int_range(
            "BILLING_RETRY_SCAN_INTERVAL_SECONDS", 15, minimum=5, maximum=3600
        ),
        billing_processing_lease_seconds=_read_env_int_range(
            "BILLING_PROCESSING_LEASE_SECONDS", 120, minimum=30, maximum=86400
        ),
        billing_retry_initial_seconds=_read_env_int_range(
            "BILLING_RETRY_INITIAL_SECONDS", 15, minimum=1, maximum=3600
        ),
        billing_retry_max_seconds=_read_env_int_range(
            "BILLING_RETRY_MAX_SECONDS", 3600, minimum=1, maximum=86400
        ),
        billing_retry_jitter_seconds=_read_env_int_range(
            "BILLING_RETRY_JITTER_SECONDS", 10, minimum=0, maximum=3600
        ),
        billing_reconcile_interval_seconds=_read_env_int_range(
            "BILLING_RECONCILE_INTERVAL_SECONDS", 300, minimum=30, maximum=86400
        ),
        billing_reconcile_lookback_hours=_read_env_int_range(
            "BILLING_RECONCILE_LOOKBACK_HOURS", 48, minimum=1, maximum=8760
        ),
        billing_reprocess_lease_seconds=_read_env_int_range(
            "BILLING_REPROCESS_LEASE_SECONDS", 3600, minimum=60, maximum=86400
        ),
        billing_reprocess_scan_interval_seconds=_read_env_int_range(
            "BILLING_REPROCESS_SCAN_INTERVAL_SECONDS", 60, minimum=5, maximum=3600
        ),
        billing_reprocess_chunk_size=_read_env_int_range(
            "BILLING_REPROCESS_CHUNK_SIZE", 1000, minimum=1, maximum=10000
        ),
        billing_publish_confirm_timeout_seconds=_read_env_float_range(
            "BILLING_PUBLISH_CONFIRM_TIMEOUT_SECONDS", 10.0, minimum=1.0, maximum=120.0
        ),
        billing_publish_claim_batch_size=_read_env_int_range(
            "BILLING_PUBLISH_CLAIM_BATCH_SIZE", 20, minimum=1, maximum=200
        ),
        billing_rabbitmq_url=_read_env_optional("BILLING_RABBITMQ_URL"),
        billing_exchange=_read_env_optional("BILLING_EXCHANGE", "domain.events") or "domain.events",
        billing_routing_key=(
            _read_env_optional("BILLING_ROUTING_KEY", "billing.usage.snapshot.v1.target")
            or "billing.usage.snapshot.v1.target"
        ),
        billing_application_code=_read_env_optional("BILLING_APPLICATION_CODE", "target") or "target",
        billing_service_code=_read_env_optional("BILLING_SERVICE_CODE", "service-orch") or "service-orch",
        billing_metric_code=_read_env_optional("BILLING_METRIC_CODE", "service-orch") or "service-orch",
        billing_admin_client_id=_read_env_optional("ORCH_BILLING_ADMIN_CLIENT_ID"),
        billing_admin_client_secret=_read_env_optional("ORCH_BILLING_ADMIN_CLIENT_SECRET"),
        orch_observability_client_id=_read_env_optional("ORCH_OBSERVABILITY_CLIENT_ID"),
        orch_observability_client_secret=_read_env_optional("ORCH_OBSERVABILITY_CLIENT_SECRET"),
        orch_observability_max_window_hours=_read_env_int_range(
            "ORCH_OBSERVABILITY_MAX_WINDOW_HOURS", 168, minimum=1, maximum=744
        ),
        orch_observability_statement_timeout_ms=_read_env_int_range(
            "ORCH_OBSERVABILITY_STATEMENT_TIMEOUT_MS", 5000, minimum=250, maximum=30000
        ),
        orch_observability_max_trace_steps=_read_env_int_range(
            "ORCH_OBSERVABILITY_MAX_TRACE_STEPS", 2000, minimum=100, maximum=10000
        ),
        celery_billing_queue=(
            _read_env_optional("CELERY_BILLING_QUEUE", _default_queue_by_profile(queue_profile, "billing"))
            or _default_queue_by_profile(queue_profile, "billing")
        ),
        orch_lab_workspace_uuid=_read_env_optional("ORCH_LAB_WORKSPACE_UUID"),
        orch_default_workspace_uuid=_read_env_optional(
            "ORCH_DEFAULT_WORKSPACE_UUID",
            _read_env_optional("ORCH_LAB_WORKSPACE_UUID"),
        ),
        sync_ws_client_id=_read_env_optional("SYNC_WS_CLIENT_ID"),
        sync_ws_client_secret=_read_env_optional("SYNC_WS_CLIENT_SECRET"),
        arquivos_client_id=_read_env_optional("ARQUIVOS_CLIENT_ID", _read_env_optional("SYNC_WS_CLIENT_ID")),
        arquivos_client_secret=_read_env_optional("ARQUIVOS_CLIENT_SECRET", _read_env_optional("SYNC_WS_CLIENT_SECRET")),
        arquivos_base_url=_read_env_optional("ARQUIVOS_BASE_URL"),
        sync_webhook_base_url=_read_env_optional("SYNC_WEBHOOK_BASE_URL"),
        sync_ws_timeout_seconds=float(_read_env_optional("SYNC_WS_TIMEOUT_SECONDS", "5") or "5"),
        target_core_api_base_url=(
            _read_env_optional("TARGET_CORE_API_BASE_URL")
            or _read_env_optional("SYNC_WEBHOOK_BASE_URL")
        ),
        target_core_api_bearer_token=(
            _read_env_optional("TARGET_CORE_API_BEARER_TOKEN")
            or _read_env_optional("SYNC_WEBHOOK_BEARER_TOKEN")
        ),
        target_core_supplier_api_base_url=_read_env_optional(
            "TARGET_CORE_SUPPLIER_API_BASE_URL"
        ),
        dialer_supplier_v2_enabled=_read_env_bool(
            "DIALER_SUPPLIER_V2_ENABLED", False
        ),
        dialer_supplier_v2_workspace_allowlist=_read_env_csv(
            "DIALER_SUPPLIER_V2_WORKSPACE_ALLOWLIST", ()
        ),
        dialer_supplier_v2_flow_allowlist=_read_env_csv(
            "DIALER_SUPPLIER_V2_FLOW_ALLOWLIST", ()
        ),
        dialer_supplier_v2_http_timeout_seconds=_read_env_float_range(
            "DIALER_SUPPLIER_V2_HTTP_TIMEOUT_SECONDS",
            5.0,
            minimum=1.0,
            maximum=60.0,
        ),
        dialer_supplier_v2_max_attempts=_read_env_int_range(
            "DIALER_SUPPLIER_V2_MAX_ATTEMPTS", 3, minimum=1, maximum=8
        ),
        dialer_supplier_v2_retry_backoff_seconds=_read_env_float_range(
            "DIALER_SUPPLIER_V2_RETRY_BACKOFF_SECONDS",
            2.0,
            minimum=0.0,
            maximum=300.0,
        ),
        dialer_supplier_v2_reconcile_interval_seconds=_read_env_int_range(
            "DIALER_SUPPLIER_V2_RECONCILE_INTERVAL_SECONDS",
            30,
            minimum=10,
            maximum=3600,
        ),
        dialer_supplier_v2_reconcile_batch_size=_read_env_int_range(
            "DIALER_SUPPLIER_V2_RECONCILE_BATCH_SIZE",
            100,
            minimum=1,
            maximum=500,
        ),
        dialer_supplier_v2_registration_lease_seconds=_read_env_int_range(
            "DIALER_SUPPLIER_V2_REGISTRATION_LEASE_SECONDS",
            120,
            minimum=30,
            maximum=3600,
        ),
        channel_supplier_v2_enabled=_read_env_bool(
            "CHANNEL_SUPPLIER_V2_ENABLED", False
        ),
        channel_supplier_v2_workspace_allowlist=_read_env_csv(
            "CHANNEL_SUPPLIER_V2_WORKSPACE_ALLOWLIST", ()
        ),
        channel_supplier_v2_flow_allowlist=_read_env_csv(
            "CHANNEL_SUPPLIER_V2_FLOW_ALLOWLIST", ()
        ),
        channel_supplier_v2_encryption_key=_read_env_optional(
            "CHANNEL_SUPPLIER_V2_ENCRYPTION_KEY"
        ),
        channel_supplier_v2_encryption_key_id=(
            _read_env_optional("CHANNEL_SUPPLIER_V2_ENCRYPTION_KEY_ID", "v1")
            or "v1"
        ),
        channel_supplier_v2_http_timeout_seconds=_read_env_float_range(
            "CHANNEL_SUPPLIER_V2_HTTP_TIMEOUT_SECONDS",
            5.0,
            minimum=1.0,
            maximum=60.0,
        ),
        channel_supplier_v2_max_attempts=_read_env_int_range(
            "CHANNEL_SUPPLIER_V2_MAX_ATTEMPTS", 3, minimum=1, maximum=8
        ),
        channel_supplier_v2_retry_backoff_seconds=_read_env_float_range(
            "CHANNEL_SUPPLIER_V2_RETRY_BACKOFF_SECONDS",
            2.0,
            minimum=0.0,
            maximum=300.0,
        ),
        channel_supplier_v2_reconcile_interval_seconds=_read_env_int_range(
            "CHANNEL_SUPPLIER_V2_RECONCILE_INTERVAL_SECONDS",
            30,
            minimum=10,
            maximum=3600,
        ),
        channel_supplier_v2_reconcile_batch_size=_read_env_int_range(
            "CHANNEL_SUPPLIER_V2_RECONCILE_BATCH_SIZE",
            100,
            minimum=1,
            maximum=500,
        ),
        channel_supplier_v2_registration_lease_seconds=_read_env_int_range(
            "CHANNEL_SUPPLIER_V2_REGISTRATION_LEASE_SECONDS",
            120,
            minimum=30,
            maximum=3600,
        ),
        orch_dialer_multilane_v2_enabled=_read_env_bool(
            "ORCH_DIALER_MULTILANE_V2_ENABLED", False
        ),
        orch_dialer_multilane_v2_flow_uuids=_read_env_csv(
            "ORCH_DIALER_MULTILANE_V2_FLOW_UUIDS", ()
        ),
        orch_dialer_multilane_v2_max_lanes_per_flow=_read_env_int_range(
            "ORCH_DIALER_MULTILANE_V2_MAX_LANES_PER_FLOW",
            1,
            minimum=1,
            maximum=16,
        ),
        orch_dialer_multilane_v2_max_execution_groups_per_flow=_read_env_int_range(
            "ORCH_DIALER_MULTILANE_V2_MAX_EXECUTION_GROUPS_PER_FLOW",
            1,
            minimum=1,
            maximum=16,
        ),
        restriction_list_check_http_timeout_seconds=max(
            1.0,
            float(
                _read_env_optional(
                    "RESTRICTION_LIST_CHECK_HTTP_TIMEOUT_SECONDS", "5"
                )
                or "5"
            ),
        ),
        restriction_list_check_max_attempts=max(
            1,
            min(_read_env_int("RESTRICTION_LIST_CHECK_MAX_ATTEMPTS", 2), 5),
        ),
        restriction_list_check_retry_backoff_seconds=max(
            0.0,
            float(
                _read_env_optional(
                    "RESTRICTION_LIST_CHECK_RETRY_BACKOFF_SECONDS", "0.25"
                )
                or "0.25"
            ),
        ),
        switch_bot_flow_enabled=_read_env_bool("SWITCH_BOT_FLOW_ENABLED", False),
        switch_bot_flow_http_timeout_seconds=float(
            _read_env_optional("SWITCH_BOT_FLOW_HTTP_TIMEOUT_SECONDS", "30") or "30"
        ),
        switch_bot_flow_max_attempts=max(
            1,
            min(_read_env_int("SWITCH_BOT_FLOW_MAX_ATTEMPTS", 3), 5),
        ),
        switch_bot_flow_retry_backoff_seconds=max(
            0.0,
            float(_read_env_optional("SWITCH_BOT_FLOW_RETRY_BACKOFF_SECONDS", "1") or "1"),
        ),
        otima_llm_api_base_url=_read_env_optional("OTIMA_LLM_API_BASE_URL"),
        otima_llm_api_gateway=_read_env_optional("OTIMA_LLM_API_GATEWAY"),
        otima_llm_api_key=_read_env_optional("OTIMA_LLM_API_KEY"),
        otima_llm_api_timeout_seconds=float(_read_env_optional("OTIMA_LLM_API_TIMEOUT_SECONDS", "10.0") or "10.0"),
        orch_queue_profile=queue_profile,
        docs_access_control_enabled=_read_env_bool("DOCS_ACCESS_CONTROL_ENABLED", True),
        docs_internal_cidrs=_read_env_csv(
            "DOCS_INTERNAL_CIDRS",
            ("10.1.20.0/24", "127.0.0.1/32", "::1/128"),
        ),
        docs_trusted_proxy_cidrs=_read_env_csv(
            "DOCS_TRUSTED_PROXY_CIDRS",
            ("10.1.20.0/24", "127.0.0.1/32", "::1/128"),
        ),
        docs_blocked_hosts=_read_env_csv(
            "DOCS_BLOCKED_HOSTS",
            ("orch.otima.digital",),
        ),
    )
    if settings.billing_retry_initial_seconds > settings.billing_retry_max_seconds:
        raise ValueError("BILLING_RETRY_INITIAL_SECONDS não pode exceder BILLING_RETRY_MAX_SECONDS.")
    if settings.orch_billing_enabled and not settings.billing_rabbitmq_url:
        raise ValueError("BILLING_RABBITMQ_URL é obrigatória quando ORCH_BILLING_ENABLED=true.")
    if settings.orch_billing_enabled and settings.celery_broker_url == "memory://":
        raise ValueError("CELERY_BROKER_URL real é obrigatória quando ORCH_BILLING_ENABLED=true.")
    if settings.dialer_supplier_v2_enabled:
        if not settings.celery_enabled:
            raise ValueError(
                "CELERY_ENABLED=true é obrigatório quando "
                "DIALER_SUPPLIER_V2_ENABLED=true, pois o registro ocorre "
                "somente após o commit em fila dedicada."
            )
        if not settings.dialer_supplier_v2_workspace_allowlist:
            raise ValueError(
                "DIALER_SUPPLIER_V2_WORKSPACE_ALLOWLIST é obrigatória quando "
                "DIALER_SUPPLIER_V2_ENABLED=true."
            )
        if not settings.dialer_supplier_v2_flow_allowlist:
            raise ValueError(
                "DIALER_SUPPLIER_V2_FLOW_ALLOWLIST é obrigatória quando "
                "DIALER_SUPPLIER_V2_ENABLED=true."
            )
        try:
            for workspace_uuid in settings.dialer_supplier_v2_workspace_allowlist:
                if UUID(workspace_uuid).int == 0:
                    raise ValueError
            for flow_uuid in settings.dialer_supplier_v2_flow_allowlist:
                if UUID(flow_uuid).int == 0:
                    raise ValueError
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError(
                "As allowlists do Supplier V2 contêm UUID inválido."
            ) from exc
        if not str(settings.target_core_supplier_api_base_url or "").strip():
            raise ValueError(
                "TARGET_CORE_SUPPLIER_API_BASE_URL é obrigatória quando "
                "DIALER_SUPPLIER_V2_ENABLED=true."
            )
        if not str(settings.target_core_api_bearer_token or "").strip():
            raise ValueError(
                "TARGET_CORE_API_BEARER_TOKEN é obrigatória quando "
                "DIALER_SUPPLIER_V2_ENABLED=true."
            )
    if settings.channel_supplier_v2_enabled:
        if not settings.celery_enabled:
            raise ValueError(
                "CELERY_ENABLED=true é obrigatório quando "
                "CHANNEL_SUPPLIER_V2_ENABLED=true."
            )
        if not settings.channel_supplier_v2_workspace_allowlist:
            raise ValueError(
                "CHANNEL_SUPPLIER_V2_WORKSPACE_ALLOWLIST é obrigatória quando "
                "CHANNEL_SUPPLIER_V2_ENABLED=true."
            )
        if not settings.channel_supplier_v2_flow_allowlist:
            raise ValueError(
                "CHANNEL_SUPPLIER_V2_FLOW_ALLOWLIST é obrigatória quando "
                "CHANNEL_SUPPLIER_V2_ENABLED=true."
            )
        try:
            for workspace_uuid in settings.channel_supplier_v2_workspace_allowlist:
                if UUID(workspace_uuid).int == 0:
                    raise ValueError
            for flow_uuid in settings.channel_supplier_v2_flow_allowlist:
                if UUID(flow_uuid).int == 0:
                    raise ValueError
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError(
                "As allowlists do channel dispatch Supplier V2 contêm UUID inválido."
            ) from exc
        if not str(settings.channel_supplier_v2_encryption_key or "").strip():
            raise ValueError(
                "CHANNEL_SUPPLIER_V2_ENCRYPTION_KEY é obrigatória quando "
                "CHANNEL_SUPPLIER_V2_ENABLED=true."
            )
        if not str(settings.target_core_supplier_api_base_url or "").strip():
            raise ValueError(
                "TARGET_CORE_SUPPLIER_API_BASE_URL é obrigatória quando "
                "CHANNEL_SUPPLIER_V2_ENABLED=true."
            )
        if not str(settings.target_core_api_bearer_token or "").strip():
            raise ValueError(
                "TARGET_CORE_API_BEARER_TOKEN é obrigatória quando "
                "CHANNEL_SUPPLIER_V2_ENABLED=true."
            )
    if settings.orch_dialer_multilane_v2_enabled:
        if not settings.dialer_supplier_v2_enabled:
            raise ValueError(
                "DIALER_SUPPLIER_V2_ENABLED=true é obrigatório quando "
                "ORCH_DIALER_MULTILANE_V2_ENABLED=true."
            )
        if not settings.orch_dialer_multilane_v2_flow_uuids:
            raise ValueError(
                "ORCH_DIALER_MULTILANE_V2_FLOW_UUIDS é obrigatória quando "
                "ORCH_DIALER_MULTILANE_V2_ENABLED=true."
            )
        try:
            multilane_flows = {
                str(UUID(flow_uuid))
                for flow_uuid in settings.orch_dialer_multilane_v2_flow_uuids
                if UUID(flow_uuid).int != 0
            }
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError(
                "ORCH_DIALER_MULTILANE_V2_FLOW_UUIDS contém UUID inválido."
            ) from exc
        if len(multilane_flows) != len(
            settings.orch_dialer_multilane_v2_flow_uuids
        ):
            raise ValueError(
                "ORCH_DIALER_MULTILANE_V2_FLOW_UUIDS contém UUID inválido ou duplicado."
            )
        supplier_flows = {
            str(UUID(flow_uuid))
            for flow_uuid in settings.dialer_supplier_v2_flow_allowlist
        }
        if not multilane_flows.issubset(supplier_flows):
            raise ValueError(
                "Todo flow multilane do ORCH deve também estar em "
                "DIALER_SUPPLIER_V2_FLOW_ALLOWLIST."
            )
        if settings.orch_dialer_multilane_v2_max_lanes_per_flow < 2:
            raise ValueError(
                "ORCH_DIALER_MULTILANE_V2_MAX_LANES_PER_FLOW deve ser pelo menos 2 "
                "quando o multilane estiver habilitado."
            )
        if (
            settings.orch_dialer_multilane_v2_max_execution_groups_per_flow
            > settings.orch_dialer_multilane_v2_max_lanes_per_flow
        ):
            raise ValueError(
                "ORCH_DIALER_MULTILANE_V2_MAX_EXECUTION_GROUPS_PER_FLOW não pode "
                "exceder ORCH_DIALER_MULTILANE_V2_MAX_LANES_PER_FLOW."
            )
    return settings
