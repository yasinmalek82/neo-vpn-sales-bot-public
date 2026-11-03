import json
import os
from functools import lru_cache
from typing import Any

from pydantic import Field, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


def _parse_int_list(value: object) -> list[int]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [int(v) for v in value]
    if isinstance(value, int):
        return [value]
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",") if p.strip()]
        return [int(p) for p in parts]
    raise TypeError("expected a comma-separated list of ints")


class _LenientListDecode:
    """Hand a list value that is not JSON to the field's own validator.

    pydantic-settings JSON-decodes a `list[int]` field before any
    `mode="before"` validator runs, so the documented `1,2` died with a
    `SettingsError` at startup while a single id (a valid JSON int) worked.
    2.3.4 has no `NoDecode`; this hook is what it offers.
    """

    def decode_complex_value(
        self, field_name: str, field: FieldInfo, value: Any  # noqa: ARG002
    ) -> Any:
        try:
            return json.loads(value)
        except ValueError:
            return value


class _EnvSource(_LenientListDecode, EnvSettingsSource):
    pass


class _DotEnvSource(_LenientListDecode, DotEnvSettingsSource):
    pass


class Settings(BaseSettings):
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,  # noqa: ARG003
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # `env_file` is read off the default source so `Settings(_env_file=...)`
        # keeps working.
        assert isinstance(dotenv_settings, DotEnvSettingsSource)
        return (
            init_settings,
            _EnvSource(settings_cls),
            _DotEnvSource(
                settings_cls,
                env_file=dotenv_settings.env_file,
                env_file_encoding=dotenv_settings.env_file_encoding,
            ),
            file_secret_settings,
        )

    model_config = SettingsConfigDict(
        # Offline operator commands disable implicit dotenv before importing
        # this module. Normal application startup retains its existing source.
        env_file=None if os.environ.get("NEO_DISABLE_DOTENV") == "1" else ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Bot
    bot_token: str = Field(..., alias="BOT_TOKEN")
    bot_username: str = Field(default="", alias="BOT_USERNAME")
    admin_telegram_ids: list[int] = Field(default_factory=list, alias="ADMIN_TELEGRAM_IDS")
    # Store catalog owners (explicit; never auto-first-admin). Empty → no store-owner privileges.
    store_owner_telegram_ids: list[int] = Field(
        default_factory=list, alias="STORE_OWNER_TELEGRAM_IDS"
    )
    # Brand contact points (public, no secrets). Empty → the copy that needs them
    # degrades to a shorter sentence rather than printing an empty «@».
    # BOT_USERNAME may stay empty: services.brand.resolve_bot_username() asks
    # Telegram via get_me() and caches the answer.
    support_handle: str = Field(default="", alias="SUPPORT_HANDLE")
    support_hours_fa: str = Field(default="", alias="SUPPORT_HOURS_FA")
    channel_handle: str = Field(default="", alias="CHANNEL_HANDLE")
    run_mode: str = Field(default="polling", alias="RUN_MODE")  # polling | webhook
    polling_drop_pending_updates: bool = Field(default=True, alias="POLLING_DROP_PENDING_UPDATES")
    webhook_host: str = Field(default="", alias="WEBHOOK_HOST")
    webhook_port: int = Field(default=8080, alias="WEBHOOK_PORT")
    webhook_path: str = Field(default="/webhook", alias="WEBHOOK_PATH")
    webhook_secret: str = Field(default="", alias="WEBHOOK_SECRET")

    @field_validator("admin_telegram_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, value: object) -> list[int]:
        return _parse_int_list(value)

    @field_validator("store_owner_telegram_ids", mode="before")
    @classmethod
    def _parse_store_owner_ids(cls, value: object) -> list[int]:
        return _parse_int_list(value)

    # Database
    database_url: str = Field(default="sqlite+aiosqlite:///./data/bot.db", alias="DATABASE_URL")

    # PasarGuard Panel
    pasar_guard_base_url: str = Field(
        default="https://panel.example.com", alias="PASARGUARD_BASE_URL"
    )
    # Required for panel connection: raw key starts with pg_key_ (panel source).
    # Empty at boot is allowed; connection / client construction fails without a key.
    pasar_guard_api_key: str = Field(default="", alias="PASARGUARD_API_KEY")
    pasar_guard_sub_path: str = Field(default="sub", alias="PASARGUARD_SUB_PATH")
    pasar_guard_timeout: float = Field(default=30.0, alias="PASARGUARD_TIMEOUT")
    # No longer read for group resolution or provisioning: a store group's own
    # panel group is the only source. Kept so an existing .env still parses.
    default_panel_group_ids: list[int] = Field(
        default_factory=list, alias="DEFAULT_PANEL_GROUP_IDS"
    )

    @field_validator("default_panel_group_ids", mode="before")
    @classmethod
    def _parse_group_ids(cls, value: object) -> list[int]:
        if value is None or value == "":
            return []
        return _parse_int_list(value)

    # Business Logic
    order_expiry_minutes: int = Field(default=30, alias="ORDER_EXPIRY_MINUTES")
    wallet_provision_fail_credit_max_n: int = Field(
        default=1, alias="WALLET_PROVISION_FAIL_CREDIT_MAX_N"
    )
    referral_discount_percent: int = Field(default=10, alias="REFERRAL_DISCOUNT_PERCENT")
    referral_referred_discount_percent: int = Field(
        default=5, alias="REFERRAL_REFERRED_DISCOUNT_PERCENT"
    )
    max_discount_percent: int = Field(default=50, alias="MAX_DISCOUNT_PERCENT")
    default_reset_strategy: str = Field(default="no_reset", alias="DEFAULT_RESET_STRATEGY")
    pricing_config_path: str = Field(default="config/pricing.json", alias="PRICING_CONFIG_PATH")

    # Free trial
    trial_enabled: bool = Field(default=True, alias="TRIAL_ENABLED")
    trial_data_limit_gb: int = Field(default=5, alias="TRIAL_DATA_LIMIT_GB")
    trial_duration_days: int = Field(default=1, alias="TRIAL_DURATION_DAYS")
    trial_hwid_limit: int = Field(default=1, alias="TRIAL_HWID_LIMIT")

    # Card-to-card payment display (no secrets — public card details for customers)
    payment_card_number: str = Field(default="", alias="PAYMENT_CARD_NUMBER")
    payment_card_holder_name: str = Field(default="", alias="PAYMENT_CARD_HOLDER_NAME")
    payment_bank_name: str = Field(default="", alias="PAYMENT_BANK_NAME")
    payment_instructions: str = Field(default="", alias="PAYMENT_INSTRUCTIONS")

    # Dedicated iPhone -> Blu SMS receiver. Off until explicitly configured.
    bank_sms_enabled: bool = Field(default=False, alias="BANK_SMS_ENABLED")
    bank_sms_token: str = Field(default="", alias="BANK_SMS_TOKEN", repr=False)
    bank_sms_sender: str = Field(default="", alias="BANK_SMS_SENDER")
    bank_sms_account_key: str = Field(default="", alias="BANK_SMS_ACCOUNT_KEY")
    bank_sms_url: str = Field(default="", alias="BANK_SMS_URL")

    # Scheduler
    scheduler_enabled: bool = Field(default=True, alias="SCHEDULER_ENABLED")
    expire_orders_interval_minutes: int = Field(default=5, alias="EXPIRE_ORDERS_INTERVAL_MINUTES")
    renewal_reminder_days: list[int] = Field(
        default_factory=lambda: [3, 1], alias="RENEWAL_REMINDER_DAYS"
    )
    # Detail/list UX: warn when expire within N days (badge + renew CTA).
    service_expire_warn_days: int = Field(default=7, alias="SERVICE_EXPIRE_WARN_DAYS")
    # Display timezone for Jalali expiry (UTC stored → convert for UX).
    display_timezone: str = Field(default="Asia/Tehran", alias="DISPLAY_TIMEZONE")
    cleanup_logs_days: int = Field(default=90, alias="CLEANUP_LOGS_DAYS")
    # Panel/node watcher (services/panel_watch.py). 0 switches the job off.
    panel_watch_interval_seconds: int = Field(default=120, alias="PANEL_WATCH_INTERVAL_SECONDS")

    @field_validator("renewal_reminder_days", mode="before")
    @classmethod
    def _parse_reminder_days(cls, value: object) -> list[int]:
        if value is None or value == "":
            return [3, 1]
        return _parse_int_list(value)

    # Logging
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    debug: bool = Field(default=False, alias="DEBUG")

    # Sentry (optional — empty DSN disables SDK; safe for local/CI)
    sentry_dsn: str = Field(default="", alias="SENTRY_DSN")
    sentry_environment: str = Field(default="", alias="SENTRY_ENVIRONMENT")
    sentry_traces_sample_rate: float = Field(default=0.0, alias="SENTRY_TRACES_SAMPLE_RATE")

    # Button colours (Bot API 9.4). Off switch for launch day: if a client
    # renders them badly, this turns them off without shipping new code.
    button_colors_enabled: bool = Field(default=True, alias="BUTTON_COLORS_ENABLED")

    # Telegram Mini App panel (`web/`). Ships off: with the switch off no route
    # is mounted and the admin home has no «پنل وب» button. The URL is the
    # public https address the Mini App opens (Telegram refuses plain http).
    web_panel_enabled: bool = Field(default=False, alias="WEB_PANEL_ENABLED")
    web_panel_url: str = Field(default="", alias="WEB_PANEL_URL")
    # A second switch, off by default: while off, the personal pages
    # (`/panel/me/...`) open for admins only, exactly like every other panel
    # path. On, any signed user who has a row and is not banned may open them.
    web_panel_public_enabled: bool = Field(default=False, alias="WEB_PANEL_PUBLIC_ENABLED")

    # Backup
    backup_enabled: bool = Field(default=True, alias="BACKUP_ENABLED")
    backup_cron: str = Field(default="0 3 * * *", alias="BACKUP_CRON")
    backup_retain_daily: int = Field(default=7, alias="BACKUP_RETAIN_DAILY")
    backup_retain_weekly: int = Field(default=4, alias="BACKUP_RETAIN_WEEKLY")
    backup_dir: str = Field(default="backups", alias="BACKUP_DIR")
    pasar_guard_db_url: str | None = Field(default=None, alias="PASARGUARD_DB_URL")

    @property
    def webhook_url(self) -> str:
        if not self.webhook_host:
            return ""
        return f"https://{self.webhook_host}{self.webhook_path}"

    @property
    def is_webhook_mode(self) -> bool:
        return self.run_mode.lower() == "webhook"

    @property
    def web_panel_link(self) -> str:
        """The URL the admin-home «پنل وب» button opens; empty while the panel is off."""
        return self.web_panel_url.strip() if self.web_panel_enabled else ""


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
