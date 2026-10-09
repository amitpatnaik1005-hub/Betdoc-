from functools import lru_cache
from typing import Literal, List

from cryptography.fernet import Fernet
from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Async drivers the engine accepts. SQLite is for the in-memory test databases only.
_ASYNC_DB_PREFIXES = ("postgresql+asyncpg://", "sqlite+aiosqlite://")


def _env(name: str) -> AliasChoices:
    """Accept both UPPER_CASE (deployment convention) and the lowercase field name."""
    return AliasChoices(name.upper(), name)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    PROJECT_NAME: str = "BetDoc"

    # SecretStr keeps the DB password out of repr(), logs and tracebacks
    DATABASE_URL: SecretStr
    SECRET_KEY: SecretStr
    ENCRYPTION_KEY: SecretStr
    INGESTION_API_KEY: SecretStr

    # FIX: Moved CORS origins to settings to prevent hardcoding in production
    BACKEND_CORS_ORIGINS: List[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    ODDS_API_KEY: SecretStr | None = None
    ODDS_POLLING_INTERVAL_SEC: int = 60
    ODDS_API_BASE_URL: str = "https://api.the-odds-api.com/v4"
    ODDS_SPORT_KEYS: str = "soccer_epl"
    ODDS_API_REGIONS: str = "uk,eu"
    ODDS_QUOTA_FLOOR: int = 10

    # Polymarket public Gamma API (no key). Leagues are Polymarket sport codes from GET /sports.
    POLYMARKET_GAMMA_BASE_URL: str = "https://gamma-api.polymarket.com"
    POLYMARKET_LEAGUES: str = "epl,nfl,nba"
    POLYMARKET_POLL_INTERVAL_SEC: int = Field(default=30, ge=5)

    # ---- /ws/live-odds: Redis pub/sub fan-out across API workers -------------
    LIVE_ODDS_CHANNEL: str = "betdoc:live_odds"
    LIVE_ODDS_SNAPSHOT_TTL_SECONDS: int = Field(default=900, gt=0)  # board cells idle longer are dropped

    # ---- Omni ingestion fleet (Celery) --------------------------------------
    # Run due ingestors inside the API process while no Celery worker is heartbeating (dev without a
    # worker). Production compose turns this off so API workers only serve requests.
    OMNI_FLEET_INPROCESS_FALLBACK: bool = True
    OMNI_FLEET_FAILURE_THRESHOLD: int = Field(default=3, ge=1)  # consecutive failures -> FATAL + paused
    OMNI_FLEET_SUCCESS_WINDOW: int = Field(default=50, ge=1)  # runs behind the success-rate figure
    OMNI_FLEET_HEARTBEAT_SECONDS: float = Field(default=45.0, gt=0)  # beat ticks every 5s; this long without one = no worker
    OMNI_FLEET_FALLBACK_TICK_SECONDS: float = Field(default=5.0, gt=0)
    OMNI_FLEET_LOCK_TIMEOUT_SECONDS: float = Field(default=120.0, gt=0)  # lock auto-expires if a worker dies
    OMNI_FLEET_MAX_ATTEMPTS: int = Field(default=4, ge=1)  # per request, on 429 / 5xx / transport errors
    OMNI_FLEET_BACKOFF_BASE_SECONDS: float = Field(default=1.0, gt=0)
    OMNI_FLEET_BACKOFF_MAX_SECONDS: float = Field(default=30.0, gt=0)
    OMNI_FLEET_DEADLETTER_MAX: int = Field(default=200, ge=1)
    # Circuit breaker: each failure opens it for base x 2^(n-1) seconds (capped); the next run after
    # the cooldown is the half-open trial. Dead-lettering (above) still applies at the threshold.
    OMNI_FLEET_BREAKER_BASE_SECONDS: float = Field(default=30.0, gt=0)
    OMNI_FLEET_BREAKER_MAX_SECONDS: float = Field(default=900.0, gt=0)
    # Quota-aware failover: a metered source under this share of its quota stops being scheduled and
    # its market groups move to the next source; a free quota probe rechecks it on this cadence.
    OMNI_FLEET_QUOTA_RESERVE: float = Field(default=0.05, ge=0, lt=1)
    OMNI_FLEET_QUOTA_RECHECK_SECONDS: float = Field(default=1800.0, gt=0)
    OMNI_FLEET_REDUNDANCY: int = Field(default=1, ge=1, le=10)  # metered sources fetching each market group
    OMNI_FLEET_THROTTLE_MAX_WAIT_SECONDS: float = Field(default=20.0, gt=0)  # longer = defer the run

    # ---- Aryabhata quant engine: market frames stream -> edges -> /ws/signals ----
    ARYABHATA_ENABLED: bool = True  # every API worker joins the consumer group (frames are shared, not duplicated)
    ARYABHATA_PREFIX: str = "aryabhata"
    ARYABHATA_STREAM_MAXLEN: int = Field(default=10_000, ge=100)  # approximate cap: frames are minutes-lived
    ARYABHATA_LINE_MAX_AGE_SECONDS: float = Field(default=90.0, gt=0)  # a price older than this is never bet
    ARYABHATA_BOOK_MAX_AGE_SECONDS: float = Field(default=300.0, gt=0)  # oldest book still in the consensus
    ARYABHATA_RISK_CACHE_SECONDS: int = Field(default=300, gt=0)  # Redis mirror of the Control Panel limits
    ARYABHATA_STEAM_PERIOD_SECONDS: int = Field(default=5, ge=1)  # EMA period: 12 of these = the 60s window
    ARYABHATA_STEAM_PERIODS: int = Field(default=12, ge=2)

    # ---- CFO ledger, risk guards, two-phase execution (Group 62) -------------
    # paper: fills every order without leaving the process. live: the Omni-Sniper routes each order
    # to the execution venue for its bookmaker (Group 63)
    CFO_EXECUTION_MODE: Literal["paper", "live"] = "paper"
    CFO_BOOKMAKER_TIMEOUT_SECONDS: float = Field(default=8.0, gt=0, le=30)  # the bankroll row stays locked this long at most
    CFO_IDEMPOTENCY_TTL_SECONDS: int = Field(default=60, ge=1)
    CFO_IDEMPOTENCY_KEY_PREFIX: str = "betdoc:idempotency"
    CFO_KILL_SWITCH_KEY: str = "betdoc:kill_switch"
    CFO_STREAK_KEY_PREFIX: str = "betdoc:risk:streak"
    CFO_VELOCITY_WINDOW_SECONDS: float = Field(default=60.0, gt=0)
    CFO_TICK_HISTORY_SECONDS: int = Field(default=180, ge=60)  # per-selection price history kept in Redis
    CFO_SETTLE_INTERVAL_SECONDS: float = Field(default=60.0, gt=0)

    # ---- Omni-Sniper: venue sessions, outbound limits, order resolution (Group 63) ----
    SNIPER_PREFIX: str = "sniper"
    SNIPER_SESSION_REFRESH_MARGIN_SECONDS: int = Field(default=300, ge=30)  # refresh tokens this long before expiry
    SNIPER_AUTH_TIMEOUT_SECONDS: float = Field(default=5.0, gt=0, le=30)
    SNIPER_RATE_MAX_WAIT_SECONDS: float = Field(default=3.0, ge=0, le=10)  # queue a shot this long for an outbound token
    SNIPER_RESOLVE_INTERVAL_SECONDS: float = Field(default=30.0, gt=0)
    SNIPER_RESOLVE_BACKOFF_BASE_SECONDS: float = Field(default=30.0, gt=0)
    SNIPER_RESOLVE_BACKOFF_MAX_SECONDS: float = Field(default=3600.0, gt=0)
    SNIPER_RESOLVE_MAX_FAILURES: int = Field(default=10, ge=1)  # then the order is dead-lettered
    SNIPER_OPEN_POLL_SECONDS: float = Field(default=300.0, gt=0)  # an accepted, unsettled bet is re-checked this often
    SNIPER_DLQ_AFTER_HOURS: float = Field(default=24.0, gt=0)  # pending this long past kick-off -> manual intervention
    SNIPER_FEED_LENGTH: int = Field(default=200, ge=10)  # terminal lines kept per user
    # The in-process sandbox bookmaker (simulated venue for development); never mounted in production
    SNIPER_SANDBOX_ENABLED: bool = True
    SNIPER_SANDBOX_TOKEN_TTL_SECONDS: int = Field(default=600, ge=60)
    SNIPER_SANDBOX_BETS_PER_SECOND: float = Field(default=2.0, gt=0)

    # Liquidity per order: a stake above this is only partly matched (0 = fill everything)
    SNIPER_SANDBOX_MAX_MATCH_INR: float = Field(default=0.0, ge=0)

    @property
    def sniper_sandbox_active(self) -> bool:
        return self.SNIPER_SANDBOX_ENABLED and self.ENVIRONMENT != "production"

    # ---- Hedging, arbitrage, live portfolio (Group 64) -----------------------
    PORTFOLIO_STREAM_ENABLED: bool = True  # one API worker at a time (Redis leader lock) publishes every watched portfolio
    PORTFOLIO_CHANNEL_PREFIX: str = "betdoc:live_portfolio"  # pub/sub <prefix>:<user_id>
    PORTFOLIO_TICK_SECONDS: float = Field(default=0.2, ge=0.05, le=5)  # 5 updates a second
    PORTFOLIO_POSITIONS_REFRESH_SECONDS: float = Field(default=15.0, gt=0)  # open positions re-read from the DB at most this often (or on change)
    PORTFOLIO_WATCH_TTL_SECONDS: int = Field(default=30, ge=5)  # a socket heartbeats its user into the watch set
    PORTFOLIO_PRICE_MAX_AGE_SECONDS: float = Field(default=120.0, gt=0)  # an older book price can't hedge or arb
    ARB_SCAN_INTERVAL_SECONDS: float = Field(default=1.0, ge=0.2)
    ARB_MIN_MARGIN_PCT: float = Field(default=0.1, ge=0, le=50)  # thinner arbs are noise (stale books, rounding)
    ARB_REFERENCE_STAKE_INR: float = Field(default=10_000.0, gt=0)  # the scanner sizes every arb at this total
    ARB_MAX_RESULTS: int = Field(default=25, ge=1, le=200)
    # Exchange commission on net winnings, by bookmaker id. A venue row's own rate overrides it.
    EXCHANGE_COMMISSION_RATES: dict[str, float] = {
        "betfair": 0.05,
        "betfair_ex_uk": 0.05,
        "betfair_ex_eu": 0.05,
        "betfair_ex_au": 0.05,
        "matchbook": 0.02,
        "smarkets": 0.02,
        "betdaq": 0.02,
        "pinnacle": 0.0,
    }
    # Account currency by bookmaker id. Unset: the book's own (see app.services.venue_costs). A venue row overrides both.
    BOOKMAKER_CURRENCIES: dict[str, str] = {}
    FX_RATES_KEY: str = "betdoc:fx:rates"  # hash: currency -> {"inr_per_unit", "as_of", "source"}
    FX_MAX_AGE_SECONDS: int = Field(default=3600, ge=60)  # an older rate is refused: legs in that currency can't be priced
    FX_HAIRCUT_PCT: float = Field(default=0.5, ge=0, lt=20)  # taken off every foreign payout coming home

    # ---- The Hive: autonomous trading bots (Group 65) ------------------------
    HIVE_ENABLED: bool = True  # every API worker joins the bots' signal consumer group
    HIVE_PREFIX: str = "hive"  # <p>:signals stream, <p>:halt flag, <p>:lock:*, <p>:fires:*, <p>:seen:*
    HIVE_STREAM_MAXLEN: int = Field(default=10_000, ge=100)
    HIVE_MERGE_LOCK_MS: int = Field(default=30_000, ge=1_000)  # one order per owner and market at a time
    HIVE_VELOCITY_WINDOW_SECONDS: int = Field(default=60, ge=10)  # the velocity breaker's window
    HIVE_FLASH_WINDOW_SECONDS: float = Field(default=120.0, ge=10)
    HIVE_FLASH_THRESHOLD_PCT: float = Field(default=15.0, gt=0, le=100)  # consensus swing that halts every bot
    HIVE_FLASH_MIN_POINTS: int = Field(default=3, ge=2)
    HIVE_SCAN_INTERVAL_SECONDS: float = Field(default=5.0, ge=1)  # flash-crash scan, breakers, shadow grading
    HIVE_SLICE_DELAY_MIN_SECONDS: int = Field(default=60, ge=1)
    HIVE_SLICE_DELAY_MAX_SECONDS: int = Field(default=120, ge=1)
    HIVE_SLICE_JITTER_PCT: float = Field(default=15.0, ge=0, lt=50)  # each slice within this of an equal split
    HIVE_BOT_CACHE_SECONDS: float = Field(default=5.0, gt=0)
    HIVE_EVENT_DEDUPE_SECONDS: int = Field(default=60, ge=1)  # one "skipped for the same reason" row per minute
    HIVE_HISTORY_POINTS: int = Field(default=60, ge=5)  # probability history the time-series models read

    # The Wire: public sports RSS feeds (no key needed)
    WIRE_NEWS_FEEDS: List[str] = [
        "https://feeds.bbci.co.uk/sport/rss.xml",
        "https://feeds.bbci.co.uk/sport/football/rss.xml",
        "https://feeds.bbci.co.uk/sport/cricket/rss.xml",
    ]

    @property
    def odds_sport_keys(self) -> List[str]:
        return [s.strip() for s in self.ODDS_SPORT_KEYS.split(",") if s.strip()]

    @property
    def polymarket_leagues(self) -> List[str]:
        return [s.strip().lower() for s in self.POLYMARKET_LEAGUES.split(",") if s.strip()]

    # Literal type: an env var can't downgrade the algorithm (e.g. to "none")
    ALGORITHM: Literal["HS256"] = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440

    # ---- Group 58: production hardening -------------------------------------
    ENVIRONMENT: Literal["development", "test", "staging", "production"] = "development"
    # SecretStr: production URLs carry the Redis password
    REDIS_URL: SecretStr = SecretStr("redis://localhost:6379/0")
    CELERY_BROKER_URL: SecretStr | None = None  # defaults to REDIS_URL
    RATE_LIMIT_GLOBAL_RPM: int = Field(default=600, gt=0)
    ARCHIVE_BACKUP_DIR: str | None = None  # where scripts/backup.sh writes *.sql.gz
    HIVE_SUPERVISOR_INTERVAL_SECONDS: float = Field(default=30.0, ge=0)  # 0 disables commander heartbeats

    # ---- Group 57: cross-section integration (orchestrator + risk) ----------
    mask_visible_chars: int = Field(default=4, ge=0)
    telemetry_timeout_seconds: float = Field(default=2.0, gt=0)
    starting_bankroll: float = Field(default=10_000.0, gt=0)
    kelly_fraction: float = Field(default=0.25, gt=0, le=1)
    max_stake_fraction: float = Field(default=0.05, gt=0, le=1)
    max_open_exposure_fraction: float = Field(default=0.25, gt=0, le=1)
    min_edge: float = Field(default=0.02, ge=0)

    # ---- Omni-Ingestion engine: vault + admin -------------------------------
    # Optional so the API still boots without Omni; VaultCrypto / admin routes fail closed when unset.
    master_vault_key: SecretStr | None = Field(default=None, validation_alias=_env("master_vault_key"))
    master_vault_previous_keys: list[SecretStr] = Field(
        default_factory=list, validation_alias=_env("master_vault_previous_keys")
    )
    omni_admin_token: SecretStr | None = Field(default=None, validation_alias=_env("omni_admin_token"))
    omni_admin_header: str = "X-Omni-Admin-Token"

    # ---- Omni-Ingestion engine: Redis, Celery, dispatch ---------------------
    omni_redis_prefix: str = "omni"
    omni_live_channel: str = "omni_live_stream"
    omni_queues: tuple[str, ...] = ("omni_default",)
    omni_default_queue: str = "omni_default"
    omni_category_queue_map: dict[str, str] = Field(default_factory=dict)  # category code (A-H) -> queue
    omni_broker_visibility_timeout_seconds: int = Field(default=3_600, gt=0)
    omni_task_time_limit_seconds: int = Field(default=120, gt=0)
    omni_task_soft_time_limit_seconds: int = Field(default=90, gt=0)
    omni_task_min_expiry_seconds: float = Field(default=5.0, gt=0)
    omni_dispatch_refresh_seconds: float = Field(default=30.0, gt=0)
    omni_dispatch_tick_seconds: float = Field(default=1.0, gt=0)

    # ---- Omni-Ingestion engine: outbound HTTP + circuit breaker -------------
    omni_http_max_connections: int = Field(default=20, gt=0)
    omni_http_timeout_seconds: float = Field(default=10.0, gt=0)
    omni_http_max_response_bytes: int = Field(default=5_000_000, gt=0)
    omni_publish_max_bytes: int = Field(default=256_000, gt=0)
    omni_breaker_window_seconds: int = Field(default=60, gt=0)
    omni_breaker_failure_threshold: int = Field(default=5, gt=0)
    omni_breaker_cooldown_seconds: int = Field(default=120, gt=0)
    omni_breaker_status_floor: int = Field(default=500, ge=400, le=599)
    omni_allow_insecure_http: bool = False  # SSRF guard: https/wss only unless explicitly allowed
    omni_allow_private_networks: bool = False  # SSRF guard: block private/loopback targets
    omni_user_agent: str = "BetDoc-Omni/1.0"

    # ---- Omni-Ingestion engine: quorum consensus ----------------------------
    # Ages are sized for REST sources polled every 30-60s: an Odds API price is still a valid vote
    # when the next Polymarket poll lands, at half weight after one half-life.
    omni_quorum_variance_threshold: float = Field(default=0.05, ge=0)
    omni_quorum_half_life_seconds: float = Field(default=60.0, gt=0)
    omni_quorum_max_age_seconds: float = Field(default=300.0, gt=0)
    omni_quorum_zero_tolerance: float = Field(default=1e-9, gt=0)
    omni_quorum_interval_seconds: float = Field(default=15.0, gt=0)  # beat cadence of omni.run_scheduled_quorum
    omni_quorum_min_providers: int = Field(default=2, ge=2)  # a single source is not a quorum

    # ---- Omni-Ingestion engine: provider WebSocket client -------------------
    omni_ws_queue_max: int = Field(default=10_000, gt=0)
    omni_ws_refresh_seconds: float = Field(default=30.0, gt=0)
    omni_ws_key_placeholder: str = "{{API_KEY}}"
    omni_ws_open_timeout_seconds: float = Field(default=10.0, gt=0)
    omni_ws_ping_interval_seconds: float = Field(default=20.0, gt=0)
    omni_ws_max_message_bytes: int = Field(default=1_048_576, gt=0)
    omni_ws_reconnect_base_seconds: float = Field(default=1.0, gt=0)
    omni_ws_reconnect_max_seconds: float = Field(default=60.0, gt=0)
    omni_ws_flush_interval_seconds: float = Field(default=1.0, gt=0)
    omni_ws_batch_size: int = Field(default=500, gt=0)

    @property
    def celery_broker_url(self) -> SecretStr:
        return self.CELERY_BROKER_URL or self.REDIS_URL

    @property
    def database_url(self) -> str:
        return self.DATABASE_URL.get_secret_value()

    @property
    def encryption_key(self) -> SecretStr:
        return self.ENCRYPTION_KEY

    @field_validator("DATABASE_URL")
    @classmethod
    def validate_async_driver(cls, v: SecretStr) -> SecretStr:
        if not v.get_secret_value().startswith(_ASYNC_DB_PREFIXES):
            raise ValueError("DATABASE_URL must use an async driver: postgresql+asyncpg:// (or sqlite+aiosqlite:// in tests)")
        return v

    @field_validator("SECRET_KEY")
    @classmethod
    def validate_secret_key(cls, v: SecretStr) -> SecretStr:
        if len(v.get_secret_value()) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters for HS256")
        return v

    @field_validator("ENCRYPTION_KEY")
    @classmethod
    def validate_fernet_key(cls, v: SecretStr) -> SecretStr:
        # Fail at startup, not at the first encrypt/decrypt call
        try:
            Fernet(v.get_secret_value())
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "ENCRYPTION_KEY must be a 32-byte url-safe base64 key "
                "(generate with Fernet.generate_key())"
            ) from exc
        return v

    @field_validator("INGESTION_API_KEY")
    @classmethod
    def validate_ingestion_key(cls, v: SecretStr) -> SecretStr:
        if len(v.get_secret_value()) < 32:
            raise ValueError("INGESTION_API_KEY must be at least 32 characters")
        return v

    @field_validator("ACCESS_TOKEN_EXPIRE_MINUTES")
    @classmethod
    def validate_expiry(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("ACCESS_TOKEN_EXPIRE_MINUTES must be positive")
        return v


settings = Settings()  # type: ignore[call-arg]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings for code that resolves config lazily (workers, Omni, health)."""
    return settings
