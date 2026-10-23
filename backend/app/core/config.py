from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal, List

from cryptography.fernet import Fernet
from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Async drivers the engine accepts. SQLite is for the in-memory test databases only.
_ASYNC_DB_PREFIXES = ("postgresql+asyncpg://", "sqlite+aiosqlite://")


def _env(name: str) -> AliasChoices:
    """Accept both UPPER_CASE (deployment convention) and the lowercase field name."""
    return AliasChoices(name.upper(), name)


PROJECT_ROOT = Path(__file__).resolve().parents[3]  # backend/app/core/config.py -> the repository root
DEFAULT_VAULT_IMPORT_DIRS: tuple[str, ...] = (r"D:\confidential", str(PROJECT_ROOT))


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
    # Group 112: the name The Odds API's own dashboard uses; when both are set it is read first (Fleet Command's vault key
    # still wins over either)
    THE_ODDS_API_KEY: SecretStr | None = None
    ODDS_POLLING_INTERVAL_SEC: int = 60
    ODDS_API_BASE_URL: str = "https://api.the-odds-api.com/v4"
    # Group 112, point b.13: international and franchise cricket, and Europe's five big football competitions. Keys out of
    # season are skipped for free (ODDS_API_SKIP_INACTIVE_SPORTS), so listing them costs nothing until they start.
    ODDS_SPORT_KEYS: str = (
        "cricket_international_t20,cricket_odi,cricket_test_match,cricket_ipl,cricket_t20_blast,"
        "soccer_epl,soccer_uefa_champs_league,soccer_spain_la_liga,soccer_italy_serie_a,soccer_germany_bundesliga"
    )
    # Before buying a board, read The Odds API's /sports list (not metered) and leave out sports that are not in season;
    # the list is kept ODDS_API_ACTIVE_SPORTS_TTL_SECONDS per process. An unreadable list leaves every sport in.
    ODDS_API_SKIP_INACTIVE_SPORTS: bool = True
    ODDS_API_ACTIVE_SPORTS_TTL_SECONDS: float = Field(default=6 * 3600.0, gt=0)
    ODDS_API_REGIONS: str = "uk,eu"
    # Markets per /odds call. Each costs regions x markets credits: "h2h" protects the quota; add
    # "totals,spreads" for Ashoka's Over/Under and Asian handicap legs (BTTS is per event only).
    ODDS_API_MARKETS: str = "h2h"
    # Per sport, overriding ODDS_API_MARKETS ({"soccer_epl": "h2h,totals,spreads"}); the Control Panel's
    # Vault & Fleet tab sets the same per sport at runtime, and its quiet hours drop every sport to h2h.
    ODDS_MARKETS_BY_SPORT: dict[str, str] = {}
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
    ARYABHATA_LINE_MAX_AGE_SECONDS: float = Field(default=90.0, gt=0)  # a price older than this is never bet (a local .env may lengthen it)
    ARYABHATA_BOOK_MAX_AGE_SECONDS: float = Field(default=300.0, gt=0)  # oldest book still in the consensus (a local .env may lengthen it)
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

    # ---- Hedging, live portfolio (Group 64; arbitrage retired in Group 96) ----
    PORTFOLIO_STREAM_ENABLED: bool = True  # one API worker at a time (Redis leader lock) publishes every watched portfolio
    PORTFOLIO_CHANNEL_PREFIX: str = "betdoc:live_portfolio"  # pub/sub <prefix>:<user_id>
    PORTFOLIO_TICK_SECONDS: float = Field(default=0.2, ge=0.05, le=5)  # 5 updates a second
    PORTFOLIO_POSITIONS_REFRESH_SECONDS: float = Field(default=15.0, gt=0)  # open positions re-read from the DB at most this often (or on change)
    PORTFOLIO_WATCH_TTL_SECONDS: int = Field(default=30, ge=5)  # a socket heartbeats its user into the watch set
    PORTFOLIO_PRICE_MAX_AGE_SECONDS: float = Field(default=120.0, gt=0)  # an older book price can't hedge
    PORTFOLIO_BOARD_REFRESH_SECONDS: float = Field(default=1.0, ge=0.2)  # the board's fixture names and kickoffs re-read this often
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
    # Only selections priced at 10% or more (decimal odds <= 10.0) can trip it: a longshot's relative swing is noise
    HIVE_FLASH_MIN_PROBABILITY: float = Field(default=0.10, gt=0, lt=1)
    HIVE_SCAN_INTERVAL_SECONDS: float = Field(default=5.0, ge=1)  # flash-crash scan, breakers, shadow grading
    HIVE_SLICE_DELAY_MIN_SECONDS: int = Field(default=60, ge=1)
    HIVE_SLICE_DELAY_MAX_SECONDS: int = Field(default=120, ge=1)
    HIVE_SLICE_JITTER_PCT: float = Field(default=15.0, ge=0, lt=50)  # each slice within this of an equal split
    HIVE_BOT_CACHE_SECONDS: float = Field(default=5.0, gt=0)
    HIVE_EVENT_DEDUPE_SECONDS: int = Field(default=60, ge=1)  # one "skipped for the same reason" row per minute
    HIVE_HISTORY_POINTS: int = Field(default=60, ge=5)  # probability history the time-series models read

    # The Lab (Group 66): quantitative backtests on the lab_hist_* market history
    LAB_BACKTEST_EXECUTOR: Literal["inline", "celery"] = "inline"  # inline: a worker thread in the API process
    LAB_MAX_CONCURRENT_RUNS: int = Field(default=2, ge=1, le=10)  # per user
    LAB_RUN_STALE_SECONDS: int = Field(default=900, ge=60)  # a RUNNING run silent this long is marked FAILED
    LAB_RISK_FREE_RATE: float = Field(default=0.04, ge=0, le=0.5)  # a year: the excess return Sharpe and Sortino measure (Group 77)
    LAB_FX_MAX_AGE_HOURS: float = Field(default=96.0, gt=0)  # an older historical fixing hands over to the static map
    # Simulation-only reference rates (INR per unit) for instants no historical fixing covers. Never
    # used for a live order: live pricing fails closed without a current rate (app.services.fx_rates).
    LAB_STATIC_FX_RATES: dict[str, str] = {
        "GBP": "107.50", "EUR": "92.00", "USD": "84.00", "AUD": "55.50", "CAD": "61.50", "SEK": "8.40", "DKK": "12.30", "NOK": "8.10",
    }

    # Nalanda (Group 67): the tick lake and the hash-chained settlement warehouse
    NALANDA_ENABLED: bool = True
    NALANDA_PREFIX: str = "nalanda"  # Redis keys: <prefix>:firehose (stream), :stats, :leader, :verify
    NALANDA_STREAM_MAXLEN: int = Field(default=2_000_000, ge=10_000)  # approximate: a firehose outage this long loses the oldest
    NALANDA_BATCH: int = Field(default=2_000, ge=10, le=50_000)  # stream entries per bulk insert
    NALANDA_WEEKS_AHEAD: int = Field(default=8, ge=2, le=52)  # weekly partitions kept pre-allocated
    NALANDA_ROLLUP_AFTER_DAYS: int = Field(default=30, ge=1)  # ticks older than this are rolled into 1-minute candles
    NALANDA_COLD_AFTER_DAYS: int = Field(default=90, ge=7)  # tick partitions older than this go to Parquet and leave PostgreSQL
    NALANDA_CANDLE_RETENTION_DAYS: int = Field(default=730, ge=30)  # candles older than this go to Parquet too
    NALANDA_ARCHIVE_DIR: str = "data/archive"  # the Parquet cold tier and the chain anchors (relative to the backend)
    NALANDA_S3_BUCKET: str | None = None  # set to mirror the cold tier (scripts/s3_mirror.py); unset: local only
    NALANDA_S3_PREFIX: str = "nalanda"
    NALANDA_REQUIRE_MIRROR_BEFORE_DROP: bool = False  # True: a partition leaves PostgreSQL only once its file is mirrored
    NALANDA_VACUUM_MIN_DEAD_TUPLES: int = Field(default=1_000, ge=0)
    NALANDA_READ_WORK_MEM: str = Field(default="32MB", pattern=r"^\d{1,6}(kB|MB|GB)$")  # per-sort memory for a forensic read
    NALANDA_READ_STATEMENT_TIMEOUT_MS: int = Field(default=30_000, ge=1_000)
    NALANDA_READ_POOL_SIZE: int = Field(default=4, ge=1, le=50)  # reads never borrow the writers' connections
    NALANDA_READ_DATABASE_URL: SecretStr | None = None  # a replica for reads; unset: the primary, on its own pool
    NALANDA_ANOMALY_WINDOW: int = Field(default=20, ge=5, le=500)  # recent prices a tick is scored against
    NALANDA_ANOMALY_Z: float = Field(default=4.0, gt=1)  # |z| of log-odds that makes a tick a suspect
    NALANDA_ANOMALY_MIN_JUMP: float = Field(default=0.25, gt=0)  # and a log-odds jump at least this big (~28%)
    NALANDA_ANOMALY_HOLD_SECONDS: float = Field(default=3.0, gt=0)  # a suspect that reverts within this is a ghost spike
    NALANDA_MIRROR_INTERVAL_SECONDS: float = Field(default=60.0, ge=5)  # the ledger mirror sweep
    NALANDA_MIRROR_OVERLAP_SECONDS: int = Field(default=300, ge=0)  # re-read behind the watermark: late commits are never missed
    NALANDA_MIRROR_BATCH: int = Field(default=5_000, ge=100)

    # The Sentinel (Group 68): alert bus, dependency health, dead man's switch, outbound dispatchers.
    # Channel credentials live in the database, encrypted with MASTER_VAULT_KEY (Control Panel, Sentinel tab).
    SENTINEL_ENABLED: bool = True
    SENTINEL_STREAM: str = "sentinel_alerts"  # the Redis stream every alert lands on; pub/sub fan-out on <stream>:live
    SENTINEL_PREFIX: str = "sentinel"  # every other Sentinel key
    SENTINEL_STREAM_MAXLEN: int = Field(default=20_000, ge=100)
    SENTINEL_DEBOUNCE_SECONDS: float = Field(default=30.0, ge=1)  # at most one CRITICAL message per channel per window
    SENTINEL_HEALTH_INTERVAL_SECONDS: float = Field(default=30.0, ge=5)  # Postgres, Redis and bookmaker APIs
    SENTINEL_HEALTH_TIMEOUT_SECONDS: float = Field(default=3.0, gt=0)
    SENTINEL_HEALTH_REMIND_SECONDS: float = Field(default=900.0, ge=60)  # a dependency still down is re-raised this often
    SENTINEL_HEARTBEAT_SECONDS: float = Field(default=5.0, gt=0)  # Garuda (the ingestion fleet) beats this often
    SENTINEL_LIVENESS_TIMEOUT_SECONDS: float = Field(default=60.0, ge=10)  # silent this long = FATAL
    SENTINEL_SOURCE_STALE_INTERVALS: float = Field(default=10.0, ge=2)  # a source with no success in this many intervals is down
    SENTINEL_WHALE_STAKE_INR: Decimal = Field(default=Decimal("50000"), gt=0)  # one order this big is a whale
    SENTINEL_MARGIN_UTILISATION: Decimal = Field(default=Decimal("0.90"), gt=0, le=1)  # exposure / equity that is a margin call
    SENTINEL_HYPE_HOUR: int = Field(default=8, ge=0, le=23)  # the daily market forecast, local time
    SENTINEL_HYPE_MINUTE: int = Field(default=0, ge=0, le=59)
    SENTINEL_TIMEZONE: str = "Asia/Kolkata"
    SENTINEL_HYPE_MIN_FIXTURES: int = Field(default=6, ge=1)  # a day worth hyping: this many fixtures today ...
    SENTINEL_HYPE_MIN_TOTAL_EV_PCT: Decimal = Field(default=Decimal("8"), gt=0)  # ... and live edges worth this much EV, summed
    SENTINEL_HYPE_MIN_STEAM_MOVES: int = Field(default=3, ge=1)  # or sharp money moving this many lines
    SENTINEL_HTTP_TIMEOUT_SECONDS: float = Field(default=8.0, gt=0)
    SENTINEL_RESUME_CONFIRM_SECONDS: int = Field(default=120, ge=15)  # a Telegram /resume needs its code back within this

    # ASHOKA, the Oracle (Group 69): vetted slips, odds shopping, the cashout advisor, the user's own bet ledger.
    ASHOKA_MC_PATHS: int = Field(default=10_000, ge=1_000, le=200_000)  # Monte Carlo paths per candidate slip
    ASHOKA_MIN_JOINT_EV: float = Field(default=0.075, gt=0)  # the "1000%" gate: joint EV at least +7.5% ...
    ASHOKA_MIN_JOINT_PROBABILITY: float = Field(default=0.55, gt=0, lt=1)  # ... and >= 55% true joint probability for multi-leg slips
    ASHOKA_MAX_QUOTE_AGE_SECONDS: float = Field(default=180.0, ge=10)  # an older price is "re-check" material, never vetted (a local .env may lengthen it)
    ASHOKA_MIN_BOOKS: int = Field(default=2, ge=1)  # books a market needs for a consensus probability
    ASHOKA_KELLY_FRACTION: float = Field(default=0.25, gt=0, le=1)
    ASHOKA_MAX_STAKE_PCT: float = Field(default=0.02, gt=0, le=0.25)  # of bankroll, a vetted slip
    ASHOKA_VALUE_MAX_STAKE_PCT: float = Field(default=0.005, gt=0, le=0.25)  # strictly bounded: EV clears, probability does not
    ASHOKA_MAX_CANDIDATE_LEGS: int = Field(default=12, ge=3, le=40)  # the best legs the generator combines
    ASHOKA_MAX_SLIPS: int = Field(default=12, ge=1, le=100)
    ASHOKA_BOOKMAKER_PRIORITY: str = "parimatch,1xbet,stake,pinnacle,betfair"
    ASHOKA_CASHOUT_HOLD_RATIO: float = Field(default=0.85, gt=0, le=1)  # an offer under 85% of fair value is penalised: HOLD
    ORACLE_TIMEZONE: str = "Asia/Kolkata"  # today / this week / this month for the P&L scorecard
    ORACLE_TDS_RATE: float = Field(default=0.30, ge=0, lt=1)  # the estimate the tax toggle applies to net winnings
    ORACLE_SCORES_POLL_ENABLED: bool = True  # The Odds API /scores, only for fixtures a pending user bet waits on
    ORACLE_SCORES_POLL_MINUTES: float = Field(default=30.0, ge=5)  # at most once per sport per this many minutes (2 credits)
    ORACLE_SETTLE_INTERVAL_SECONDS: float = Field(default=300.0, ge=30)

    # ---- The True Digital Betting Twin: the 14-pillar fortress and the in-play watch (Group 72) ----
    TWIN_PREFIX: str = "twin"  # Redis: <prefix>:intel:<fixture>, <prefix>:model_weights, <prefix>:inplay:lock
    TWIN_RETAIL_BOOKS: str = "parimatch,1xbet"  # where the twin's slips are placed, in priority order
    TWIN_SHARP_BOOKS: str = "pinnacle,betfair"  # the de-vigged reference price, first complete market wins
    TWIN_ADVISORY_PILLARS: str = ""  # pillar numbers that report but never veto ("9,11"); empty: all 14 enforced
    TWIN_MIN_MODELS: int = Field(default=3, ge=1)  # pillar 1: models that must price every leg
    TWIN_MIN_CONSENSUS_EV: float = Field(default=0.045, gt=0)  # pillar 1: weighted model EV per leg, and the slip's joint EV
    TWIN_MAX_WIND_KMH: float = Field(default=25.0, gt=0)  # pillar 2
    TWIN_MAX_RAIN_MMH: float = Field(default=2.5, gt=0)
    TWIN_MAX_FLIGHT_DELAY_HOURS: float = Field(default=3.0, gt=0)  # pillar 3: the backed side's travel
    TWIN_MIN_REST_HOURS: float = Field(default=72.0, gt=0)
    TWIN_CIRCADIAN_TIMEZONES: int = Field(default=2, ge=1)  # this many time zones inside the rest window ...
    TWIN_CIRCADIAN_PENALTY: float = Field(default=0.045, ge=0, lt=1)  # ... cut the side's win probability by this (points)
    TWIN_KEY_PLAYER_IMPACT: float = Field(default=0.85, gt=0, le=1)  # pillar 4: a tier-1 absence
    TWIN_MANAGER_CHANGE_DAYS: float = Field(default=7.0, ge=0)
    TWIN_RLM_PUBLIC_SHARE: float = Field(default=0.75, gt=0, le=1)  # pillar 6: the public's ticket share that makes a drift RLM
    TWIN_MIN_SHARP_EDGE: float = Field(default=0.05, gt=0)  # pillar 7: retail odds over the de-vigged sharp price
    TWIN_SHARP_MAX_AGE_SECONDS: float = Field(default=300.0, ge=10)
    TWIN_REFEREE_SPORTS: str = "soccer"  # pillar 9 applies to sport keys with these prefixes
    TWIN_REFEREE_MAX_PENALTIES_PER_90: float = Field(default=0.45, gt=0)
    TWIN_REFEREE_STRICT_CARDS: float = Field(default=4.8, gt=0)  # flagged in the audit, never a veto
    TWIN_MOTIVATION_MAX_GAP: float = Field(default=0.30, gt=0, le=1)  # pillar 10: the opponent wanting it this much more vetoes
    TWIN_DERBY_EV_MULTIPLIER: float = Field(default=1.5, ge=1)  # a derby needs this times the consensus EV bar
    # pillar 13 sizes with the CFO's policy (CFO_* below, Group 76): one Kelly fraction, ceiling and drawdown damper
    TWIN_STAKE_STEP_INR: Decimal = Field(default=Decimal("50"), gt=0)  # stakes round down to this
    TWIN_MAX_QUOTE_AGE_SECONDS: float = Field(default=60.0, ge=1)  # pillar 14: an older retail price is not executable
    TWIN_MAX_ODDS_DRIFT_PCT: float = Field(default=0.01, ge=0, lt=1)  # pillar 14 re-check: the price may fall this much, no more
    # pillars 2-6, 9-11: how old each kind of evidence may be (minutes)
    TWIN_INTEL_MAX_AGE_MINUTES: dict[str, float] = Field(default_factory=lambda: {
        "weather": 360.0, "travel": 2880.0, "injuries": 720.0, "lineups": 180.0, "referee": 4320.0,
        "motivation": 4320.0, "public_splits": 60.0, "liquidity": 1440.0,
    })
    TWIN_INPLAY_ENABLED: bool = True
    TWIN_INPLAY_POLL_SECONDS: float = Field(default=5.0, ge=1)  # Pathway B: every watched bet re-priced this often
    TWIN_PULLOUT_PROB_DROP: float = Field(default=0.35, gt=0, lt=1)  # win probability down this much (points) from the start
    TWIN_PULLOUT_TARGET_PROFIT_PCT: float = Field(default=0.50, gt=0)  # fair value (or the offer) this far over the stake
    # Group 77: the in-play stop-loss shield (checked first on every tick of the watch)
    TWIN_STOP_LOSS_PCT: float = Field(default=0.25, gt=0, lt=1)  # cash out once the value is at or under (1 - this) x stake
    TWIN_STOP_LOSS_MIN_PCT: float = Field(default=0.15, gt=0, lt=1)  # the range a bet's own stop-loss may take
    TWIN_STOP_LOSS_MAX_PCT: float = Field(default=0.40, gt=0, lt=1)
    TWIN_PULLOUT_PROB_RATIO: float = Field(default=0.35, gt=0, lt=1)  # live win probability under this x the entry one: collapse

    # ---- Post-execution feedback loop: CLV, model attribution, root causes, weight recalibration (Group 73) ----
    FEEDBACK_ENABLED: bool = True
    FEEDBACK_SWEEP_INTERVAL_SECONDS: float = Field(default=300.0, ge=30)  # settle, then attribute what settled
    FEEDBACK_BATCH_SIZE: int = Field(default=200, ge=1, le=5000)  # settled bets attributed per sweep (locked, SKIP LOCKED)
    FEEDBACK_CLOSING_LOOKBACK_HOURS: float = Field(default=24.0, gt=0)  # the sharp closing price is the last tick in this window before kickoff
    FEEDBACK_WINDOW_DAYS: float = Field(default=90.0, gt=0)  # the Brier window the weights are computed over
    FEEDBACK_LOG_LOSS_EPSILON: float = Field(default=1e-6, gt=0, lt=0.5)  # probabilities clipped to [eps, 1 - eps]
    FEEDBACK_CALIBRATION_BINS: int = Field(default=10, ge=2, le=50)
    FEEDBACK_RCA_CONFIDENT_PROB: float = Field(default=0.65, gt=0.5, lt=1)  # the models were this sure and the bet lost
    FEEDBACK_RCA_STEAM_CLV_PCT: float = Field(default=-4.0, lt=0)  # closing line this far against the bet: adverse steam
    FEEDBACK_ALERT_MAX_AGE_HOURS: float = Field(default=24.0, gt=0)  # a bet settled longer ago is attributed silently (no page)

    # ---- Model recalibration engine: lifecycle states and pillar 1's weights (Group 74; the only weight publisher) ----
    TWIN_RECALIBRATION_BENCHMARK_MODEL: str = "closing_sharp"  # Brier skill is measured against it, on the legs both priced
    TWIN_RECALIBRATION_WINDOW_DAYS: float = Field(default=90.0, gt=0)
    TWIN_RECALIBRATION_SHORT_WINDOW_DAYS: float = Field(default=30.0, gt=0)  # the auto-bench Brier is this recent
    TWIN_RECALIBRATION_HALF_LIFE_DAYS: float = Field(default=30.0, gt=0)  # w_i = exp(-ln 2 / T_half x age_i)
    TWIN_RECALIBRATION_TEMPERATURE: float = Field(default=0.10, gt=0)  # softmax: S_m = -BS_m / tau + beta x CLV_m
    TWIN_RECALIBRATION_CLV_WEIGHT: float = Field(default=2.0, ge=0)  # beta, per CLV percentage point
    TWIN_RECALIBRATION_MIN_SAMPLES: int = Field(default=25, ge=1)  # a verdict other than ACTIVE needs this many predictions
    TWIN_RECALIBRATION_PRIOR_STRENGTH: float = Field(default=20.0, gt=0)  # N0: under MIN_SAMPLES, w = N/(N+N0) w~ + N0/(N+N0)
    TWIN_RECALIBRATION_AUTO_BENCH_BRIER: float = Field(default=0.25, gt=0)  # short-window Brier at or above: benched
    TWIN_RECALIBRATION_BENCH_CLV: float = Field(default=-3.0)  # or CLV under this ...
    TWIN_RECALIBRATION_BENCH_CLV_MIN_SAMPLES: int = Field(default=40, ge=1)  # ... over at least this many predictions
    TWIN_RECALIBRATION_PROMOTION_BSS: float = Field(default=0.05)  # alpha boost: BSS and CLV at or above both
    TWIN_RECALIBRATION_PROMOTION_CLV: float = Field(default=2.5)
    TWIN_RECALIBRATION_PROBATION_BSS: float = Field(default=-0.03)  # probation: BSS or CLV under either
    TWIN_RECALIBRATION_PROBATION_CLV: float = Field(default=-1.5)
    # the weight each lifecycle state may carry (the softmax weight is clamped into its band)
    TWIN_RECALIBRATION_WEIGHT_BANDS: dict[str, tuple[float, float]] = Field(default_factory=lambda: {
        "ALPHA_BOOSTED": (1.2, 2.5), "ACTIVE": (0.8, 1.2), "PROBATION": (0.2, 0.5),
    })
    TWIN_RECALIBRATION_DAY_OF_WEEK: str = "sun"  # the scheduled run, in ORACLE_TIMEZONE
    TWIN_RECALIBRATION_HOUR: int = Field(default=0, ge=0, le=23)
    TWIN_RECALIBRATION_MINUTE: int = Field(default=0, ge=0, le=59)
    TWIN_RECALIBRATION_LOSS_TRIGGER_COUNT: int = Field(default=3, ge=1)  # this many model-blamed losses ...
    TWIN_RECALIBRATION_LOSS_TRIGGER_HOURS: float = Field(default=24.0, gt=0)  # ... inside this window trigger a run
    TWIN_RECALIBRATION_LOSS_COOLDOWN_HOURS: float = Field(default=6.0, gt=0)  # at most one triggered run per this
    TWIN_RECALIBRATION_LOCK_SECONDS: int = Field(default=120, ge=10)  # one run at a time

    # ---- The Never-Forget shield: pillar 15, lessons from lost legs (Group 75) ----
    NEVER_FORGET_ENABLED: bool = True  # off: pillar 15 passes with nothing checked, and no loss is memorised
    NEVER_FORGET_SIMILARITY_THRESHOLD: float = Field(default=0.82, gt=0.5, le=1.0)  # exp(-gamma D_w^2) at or above: the same situation
    NEVER_FORGET_GAMMA: float = Field(default=4.0, gt=0)
    # w_k per feature (normalised over the features both vectors carry); a feature with no weight is recorded, never compared
    NEVER_FORGET_FEATURE_WEIGHTS: dict[str, float] = Field(default_factory=lambda: {
        "rain": 0.25, "wind": 0.15, "fatigue": 0.25, "cards": 0.15, "penalties": 0.10, "steam": 0.05, "odds": 0.05,
    })
    # x~ = min(1, raw / scale); odds: (odds - 1) / scale
    NEVER_FORGET_FEATURE_SCALES: dict[str, float] = Field(default_factory=lambda: {
        "rain": 10.0, "wind": 50.0, "cards": 8.0, "penalties": 1.0, "odds": 10.0, "model_ev": 0.20, "sharp_edge": 0.20,
    })
    NEVER_FORGET_FULL_REST_HOURS: float = Field(default=72.0, gt=0)  # fatigue x~ = clamp((full - rest) / span, 0, 1)
    NEVER_FORGET_FATIGUE_SPAN_HOURS: float = Field(default=48.0, gt=0)
    NEVER_FORGET_MIN_COVERAGE: float = Field(default=0.6, gt=0, le=1)  # the share of a lesson's feature weight a comparison needs
    NEVER_FORGET_MATCH_SCOPE: Literal["shape", "any"] = "shape"  # shape: a lesson guards the same market kind and side of it only
    NEVER_FORGET_SHADOW_CAUSES: tuple[str, ...] = ("VARIANCE_BAD_LUCK",)  # a loss with this root cause is kept as an EXPERIMENTAL lesson
    NEVER_FORGET_MAX_MATCH_RATE: float = Field(default=0.10, gt=0, le=1)  # a lesson matching more of the legs seen recently is too broad ...
    NEVER_FORGET_SPECIFICITY_MIN_LEGS: int = Field(default=20, ge=1)  # ... judged once this many comparable legs were seen ...
    NEVER_FORGET_SPECIFICITY_WINDOW_DAYS: float = Field(default=30.0, gt=0)  # ... over this window (EXPERIMENTAL until promoted)
    NEVER_FORGET_SPECIFICITY_MAX_AUDITS: int = Field(default=2000, ge=10)  # the most recent fortress runs read for it

    # ---- KUMBHA's capital growth: the one sizing policy (pillar 13 and the CFO), forecasts, rebalancing (Group 76) ----
    # f = min(f* x kappa x psi(BSS) x lambda(odds), f_max) x phi(D)
    CFO_KELLY_FRACTION_DEFAULT: float = Field(default=0.25, gt=0.0, le=1.0)  # kappa: quarter Kelly
    CFO_MAX_SINGLE_STAKE_PCT: float = Field(default=0.025, gt=0.0, le=0.10)  # f_max, a fraction of bankroll: 2.5%
    CFO_DRAWDOWN_WINDOW_DAYS: float = Field(default=7.0, gt=0)  # the rolling drawdown D, from the user's settled bets
    CFO_DRAWDOWN_CAUTIOUS_THRESHOLD_PCT: float = Field(default=10.0, gt=0.0, lt=100.0)  # phi(D): from here x the cautious multiplier ...
    CFO_DRAWDOWN_CAUTIOUS_MULTIPLIER: float = Field(default=0.50, gt=0.0, le=1.0)
    CFO_DRAWDOWN_DEFENSIVE_THRESHOLD_PCT: float = Field(default=15.0, gt=0.0, lt=100.0)  # ... from here x the defensive one ...
    CFO_DRAWDOWN_DEFENSIVE_MULTIPLIER: float = Field(default=0.25, gt=0.0, le=1.0)
    CFO_DRAWDOWN_HALT_THRESHOLD_PCT: float = Field(default=20.0, gt=0.0, lt=100.0)  # ... from here 0, latched until a supervisor signs off
    CFO_SKILL_SLOPE: float = Field(default=2.0, ge=0.0)  # psi(BSS) = min(cap, max(floor, 1 + slope x BSS)); no measured skill: 1
    CFO_SKILL_FLOOR: float = Field(default=0.6, gt=0.0, le=1.0)
    CFO_SKILL_CAP: float = Field(default=1.5, ge=1.0)
    CFO_LONGSHOT_PIVOT_ODDS: float = Field(default=5.0, gt=1.0)  # lambda(o) = max(floor, (pivot / o)^exponent) above the pivot
    CFO_LONGSHOT_EXPONENT: float = Field(default=0.75, ge=0.0)
    CFO_LONGSHOT_FLOOR: float = Field(default=0.20, gt=0.0, le=1.0)
    CFO_HISTORY_DAYS: float = Field(default=180.0, gt=0)  # forecasts bootstrap the user's settled bets of this window ...
    CFO_MIN_HISTORY_BETS: int = Field(default=30, ge=5)  # ... and refuse with fewer
    CFO_MONTE_CARLO_PATHS: int = Field(default=10000, ge=1000, le=25000)
    CFO_SIMULATION_HORIZONS: tuple[int, ...] = (30, 90, 180, 365)  # days
    CFO_COMPARISON_PATHS: int = Field(default=2000, ge=200)  # the strategy comparison runs every strategy at this many paths ...
    CFO_COMPARISON_HORIZON_DAYS: int = Field(default=90, ge=7)  # ... over this horizon
    CFO_CURVE_POINTS: int = Field(default=15, ge=2)  # checkpoints on a forecast's percentile curve
    CFO_RISK_FREE_RATE_ANNUAL: float = Field(default=0.0, ge=0.0)  # R_f in the Sharpe and Sortino ratios
    CFO_RUIN_LEVEL: float = Field(default=0.50, gt=0.0, lt=1.0)  # ruin: the bankroll under this share of its start
    CFO_REBALANCE_MIN_TRANSFER_INR: Decimal = Field(default=Decimal("5000.00"), gt=0)
    CFO_REBALANCE_GAMMA: float = Field(default=0.8, gt=0.0)  # V_j* = W E_j^gamma / sum E_k^gamma
    CFO_REBALANCE_LOOKBACK_DAYS: float = Field(default=28.0, gt=0)  # E_j: the EV each venue's bets captured over this window
    CFO_ADVISORY_CONFIRM_HOURS: float = Field(default=24.0, gt=0)  # a steady regime is re-confirmed at most this often
    CFO_ADVISORY_SCAN_MINUTES: int = Field(default=15, ge=1, le=59)  # the beat scans every user's regime this often

    # ---- The manual parlay workbench: the cognitive rater over the 15 pillars (Group 77) ----
    MANUAL_PARLAY_PILLAR_CREDIT: dict[str, float] = Field(default_factory=lambda: {"PASS": 1.0, "ADVISORY": 0.5, "UNVERIFIED": 0.25, "FAIL": 0.0})
    MANUAL_PARLAY_FAIL_CAP: float = Field(default=54.0, ge=0, le=100)  # one failed pillar: the score goes no higher (BRONZE at most)
    # The BetDoc Slip Score's tiers (Group 104, point b.5): DIAMOND FORTRESS, GOLD GRADE, SILVER CAUTION, BRONZE / DANGER TRAP
    MANUAL_PARLAY_TIERS: dict[str, float] = Field(default_factory=lambda: {"DIAMOND": 90.0, "GOLD": 75.0, "SILVER": 55.0, "BRONZE": 0.0})
    # Badges over the fortress score (Group 104): points each adds or takes, earned from what the inspection measured
    MANUAL_PARLAY_BADGE_POINTS: dict[str, float] = Field(default_factory=lambda: {
        "VIG_CRUSHER": 5.0, "ZERO_CORRELATION": 8.0, "SCORE_SYNCED": 10.0, "EV_KING": 10.0, "SAME_MATCH_TAX": -18.0, "HEAVY_JUICE": -12.0,
    })
    MANUAL_PARLAY_VIG_CRUSHER_MAX: float = Field(default=0.04, gt=0, lt=1)  # the legs' mean margin at the account's book: at most this
    MANUAL_PARLAY_HEAVY_JUICE_MIN: float = Field(default=0.075, gt=0, lt=1)  # ... at least this
    MANUAL_PARLAY_EV_KING_MIN: float = Field(default=0.07, gt=0, lt=10)  # the slip's Monte Carlo joint EV: at least this
    MANUAL_PARLAY_AUTOFIX_CANDIDATES: int = Field(default=4, ge=1, le=12)  # replacement legs the auto-fix tries (one fortress run each)
    # The Chef's Table (Group 104, 5(c)): Ashoka's best vetted slips re-rated through the fortress, served three ways
    CHEF_TABLE_CANDIDATES: int = Field(default=6, ge=3, le=20)
    CHEF_TABLE_CACHE_SECONDS: int = Field(default=180, ge=0, le=3600)
    # The BetDoc Consultant (Group 105, ASHVATTHAMA, point b.6): the Doctor, the Lawyer and the Consultant on one slip.
    # A book's rules are the user's to read: BetDoc has no copy of any bookmaker's terms. Each book's maximum payout per
    # slip (INR) and the clause references below are what the user read on the book's own rules page; nothing is assumed.
    CONSULTANT_MAX_PAYOUT_INR: dict[str, Decimal] = Field(default_factory=dict)  # canonical book -> maximum payout per slip
    CONSULTANT_RULE_REFERENCES: dict[str, dict[str, str]] = Field(default_factory=dict)  # canonical book -> rule code -> clause reference
    # Competitions that can hold a knockout tie (matched in the sport key or league name): extra time and penalties
    # can decide them, and standard football markets settle on 90 minutes plus stoppage time
    CONSULTANT_KNOCKOUT_KEYWORDS: list[str] = Field(default_factory=lambda: [
        "cup", "champs_league", "champions league", "europa", "conference", "copa", "libertadores", "sudamericana", "pokal", "coppa", "playoff",
    ])
    # Limited-overs cricket: format -> sport-key fragments, and the ICC playing conditions' minimum overs a side for a result
    CONSULTANT_CRICKET_FORMATS: dict[str, list[str]] = Field(default_factory=lambda: {
        "T20": ["t20", "ipl", "big_bash", "psl", "caribbean", "blast"], "ODI": ["odi", "one_day", "icc_world_cup"],
    })
    CONSULTANT_CRICKET_MIN_OVERS: dict[str, int] = Field(default_factory=lambda: {"T20": 5, "ODI": 20})
    CONSULTANT_PALPABLE_EDGE: float = Field(default=0.25, gt=0, lt=10)  # a leg this far over BetDoc's fair value risks the obvious-error rule
    CONSULTANT_HISTORY_LIMIT: int = Field(default=20, ge=1, le=200)  # signed verdicts listed per request
    # The Bet-Slip ornament (Group 106, points b.7 and b.12): the printable ticket of an approved, sealed verdict.
    # Its prices are the ones signed; past this age the ticket says to re-check them at the book before placing.
    BET_SLIP_FRESH_MINUTES: int = Field(default=30, ge=1, le=1440)
    # Live smallcases (Group 107, point b.8): six thematic baskets of independent positive-EV singles, rebuilt from the
    # live market on every scan and sized leg by leg by KUMBHA's policy, the basket as a whole under its own ceiling
    SMALLCASE_MAX_LEGS: int = Field(default=5, ge=2, le=8)
    SMALLCASE_MIN_EV: float = Field(default=0.02, ge=0, lt=1)  # a leg's EV at the best retail book, to enter any basket
    SMALLCASE_SAFE_MIN_PROBABILITY: float = Field(default=0.55, gt=0, lt=1)  # the safe compounder: BetDoc's fair chance at least this
    SMALLCASE_UNDERDOG_MIN_ODDS: float = Field(default=2.5, gt=1)  # the high-yield underdog basket: priced at least this
    SMALLCASE_EUROPEAN_LEAGUES: list[str] = Field(default_factory=lambda: [
        "soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga", "soccer_italy_serie_a", "soccer_france_ligue_one",
        "soccer_uefa_champs_league", "soccer_uefa_europa_league",
    ])
    SMALLCASE_MAX_BASKET_FRACTION: float = Field(default=0.06, gt=0, le=0.5)  # one basket's singles together, of bankroll
    SMALLCASE_SCAN_SECONDS: int = Field(default=30, ge=5, le=3600)  # the market scan is shared by every viewer this long
    # VIDUR's intelligence board (Group 108, point b.14b): the Wire's own records (stored articles, catalysts, absences,
    # forecasts, steam) as one board, each item against its fixture's measured market move and Ashoka's live edges
    WIRE_BOARD_HOURS: float = Field(default=48.0, gt=0)  # articles published within this
    WIRE_BOARD_LIMIT: int = Field(default=80, ge=1, le=500)  # items on the board after the filters
    WIRE_BOARD_CACHE_SECONDS: int = Field(default=20, ge=1, le=600)  # the unfiltered board is shared by every viewer this long
    WIRE_BOARD_SPORTS: dict[str, list[str]] = Field(default_factory=lambda: {  # the filter's sports -> the feed's sport families
        "CRICKET": ["cricket"], "FOOTBALL": ["soccer"], "TENNIS": ["tennis"], "MMA": ["mma"],
    })
    # tier 1: governing bodies, leagues and clubs (host suffixes); tier 2: the press at or over this credibility; tier 3: the rest
    WIRE_OFFICIAL_SOURCE_HOSTS: list[str] = Field(default_factory=lambda: [
        "icc-cricket.com", "bcci.tv", "iplt20.com", "cricket.com.au", "ecb.co.uk", "premierleague.com", "uefa.com", "fifa.com", "thefa.com",
        "laliga.com", "bundesliga.com", "legaseriea.it", "ligue1.com", "atptour.com", "wtatennis.com", "ufc.com",
    ])
    WIRE_PRESS_MIN_CREDIBILITY: float = Field(default=0.75, gt=0, le=1)
    WIRE_BOARD_STEAM_WINDOW_MINUTES: int = Field(default=120, ge=1, le=1440)  # steam moves measured over the odds ticks this far back
    WIRE_BOARD_STEAM_MIN_PROB_DELTA: float = Field(default=2.0, gt=0, le=100)  # percentage points of implied probability
    WIRE_BOARD_STEAM_MIN_LINE_SHIFT: float = Field(default=0.25, ge=0)
    WIRE_BOARD_STEAM_MIN_SECONDS: int = Field(default=60, ge=1)
    WIRE_BOARD_WEATHER_MIN_EFFECT: float = Field(default=0.05, gt=0, lt=1)  # a forecast is news when the friction factor moves this far from 1
    # the neural event bus (Group 108, point b.9): a Wire item run through the Score Predictor, the Oracle, the Consultant and
    # the Vault's smallcases, every step a real engine call; frames on Redis pub/sub "<prefix>:pipeline", in process without Redis
    NEURAL_BUS_PREFIX: str = "betdoc:neural"
    NEURAL_BUS_HISTORY: int = Field(default=30, ge=1, le=500)  # chains kept per audience (the system's, each user's)
    NEURAL_BUS_HISTORY_HOURS: float = Field(default=24.0, gt=0)
    NEURAL_BUS_TOP_EDGES: int = Field(default=3, ge=1, le=20)  # the Oracle step lists this many of the fixture's best legs
    NEURAL_BUS_AUTO_IMPACTS: List[str] = Field(default_factory=lambda: ["CRITICAL"])  # news at these impacts on a tracked fixture starts a chain
    NEURAL_BUS_AUTO_CATALYSTS: bool = True  # a news-to-steam catalyst starts one too
    NEURAL_BUS_AUTO_MAX_PER_SCAN: int = Field(default=3, ge=0, le=50)
    NEURAL_BUS_AUTO_COOLDOWN_SECONDS: int = Field(default=600, ge=0)  # one automatic chain per fixture this often
    NEURAL_BUS_USER_LOCK_SECONDS: int = Field(default=120, ge=1, le=900)  # one chain at a time per user (released when it ends)
    # The Pocket Twin (Group 109, point b.10): the installable PWA and the two-way Telegram desk. The bot is the Sentinel's
    # Telegram channel (its token and webhook secret stay in the vault); these only shape what it answers
    PUBLIC_APP_URL: str | None = None  # the PWA's https origin: Telegram's buttons open it (unset, or not https: no link buttons)
    POCKET_TWIN_LINK_CODE_SECONDS: int = Field(default=600, ge=60, le=3600)  # a one-time code that links a Telegram chat to an account
    POCKET_TWIN_MATCHES: int = Field(default=5, ge=1, le=10)  # /matches lists this many fixtures
    POCKET_TWIN_DIAMOND_ALERTS: bool = True  # a DIAMOND plate on the Chef's Table before kickoff pages the phone channels
    # the sports filter: label -> the feed's sport_key prefixes
    MANUAL_PARLAY_SPORTS: dict[str, tuple[str, ...]] = Field(default_factory=lambda: {
        "Football": ("soccer",), "Basketball": ("basketball",), "Tennis": ("tennis",), "Cricket": ("cricket",), "Ice Hockey": ("icehockey",),
        "Baseball": ("baseball",), "American Football": ("americanfootball",), "MMA/UFC": ("mma", "boxing_ufc"), "Esports": ("esports",),
    })
    MANUAL_PARLAY_MAX_LEGS: int = Field(default=8, ge=2, le=20)
    # The Oracle's match board (Group 103): the workbench board by country and time window, these sports first
    MATCH_BOARD_PRIORITY_SPORTS: list[str] = Field(default_factory=lambda: ["Cricket", "Football"])
    MATCH_BOARD_MAX_FIXTURES: int = Field(default=200, ge=10, le=2000)

    # ---- Experience (FA-2): XP for discipline, wins, lessons and shielded losses (Group 75) ----
    XP_AWARD_SLIP_VETTED: int = Field(default=25, ge=1)  # once per slip that clears every enforced pillar
    XP_AWARD_BET_WON: int = Field(default=50, ge=1)
    XP_AWARD_LOSS_PREVENTED: int = Field(default=150, ge=1)  # once the leg pillar 15 vetoed has lost
    XP_AWARD_MISTAKE_MEMORIZED: int = Field(default=200, ge=1)
    XP_AWARD_STREAK_BONUS: int = Field(default=500, ge=1)
    XP_AWARD_DISCIPLINED_BET: int = Field(default=40, ge=1)  # Group 79: once per bet the discipline read clears (FA-2)
    XP_STREAK_DAYS: int = Field(default=7, ge=2)  # consecutive days (ORACLE_TIMEZONE) of fortress runs and only vetted bets placed
    # rank -> the XP that reaches it, ascending from 0
    XP_TIERS: dict[str, int] = Field(default_factory=lambda: {
        "ROOKIE": 0, "QUANT_APPRENTICE": 1001, "HIGH_ROLLER": 5001, "SYNDICATE_MASTER": 15001, "THE_ORACLE": 50001,
    })

    # ---- The Vault: fleet credentials and accounts (Group 70) ---------------
    # Directories a server-side path import may read from (the Control Panel's "load from path" and the CLI's
    # --file go through the same check). Default: the owner's D:\confidential folder and the project root, so the
    # credentials file imports out of the box; set it to [] to refuse path imports (uploads and pasted text still work).
    VAULT_IMPORT_ALLOWED_DIRS: List[str] = Field(default_factory=lambda: list(DEFAULT_VAULT_IMPORT_DIRS))
    VAULT_IMPORT_MAX_BYTES: int = Field(default=1_000_000, ge=1_000, le=10_000_000)
    VAULT_PROBE_ENABLED: bool = True  # the credential health prober (sanctioned APIs only)
    VAULT_PROBE_INTERVAL_MINUTES: float = Field(default=360.0, ge=30)
    VAULT_PROBE_SPACING_SECONDS: tuple[float, float] = (3.0, 7.0)  # random pause between two probes: a batch never bursts
    VAULT_PROBE_MIN_INTERVAL_SECONDS: int = Field(default=60, ge=60)  # at most one probe per bookmaker per minute
    VAULT_RESERVATION_TTL_MINUTES: float = Field(default=30.0, ge=5)  # an order's stake hold with no ledger row by then is released
    VAULT_BACKUP_MIN_PASSPHRASE: int = Field(default=12, ge=12)
    # ---- Smart Order Router & multi-venue slicer (Group 71) ------------------
    ROUTER_MAX_QUOTE_AGE_SECONDS: float = Field(default=30.0, ge=1)  # an older Garuda price cannot clear the pre-dispatch guard
    ROUTER_MIN_SLICE_STAKE: Decimal = Field(default=Decimal("100"), gt=0)  # the smallest slice worth its own bet (order currency)
    ROUTER_STAKE_QUANTUM: Decimal = Field(default=Decimal("1"), gt=0)  # slices are whole rupees; the remainder goes to the best price
    # Per venue {"parimatch": {"min": "10", "max": "50000"}}: the book's own stake rules as the user knows them (none assumed)
    ROUTER_VENUE_STAKE_LIMITS: dict[str, dict[str, Decimal]] = Field(default_factory=dict)
    ROUTER_MIN_SLICE_EV: Decimal = Field(default=Decimal("0"), ge=-1, le=1)  # with a true probability: each slice's net EV floor
    ROUTER_SLICE_TIMEOUT_SECONDS: float = Field(default=20.0, gt=0)  # no answer by then: the slice is UNKNOWN (held, never released)
    ROUTER_ORPHAN_SECONDS: float = Field(default=120.0, ge=10)  # reserved, never dispatched: an orphan the operator may release
    ROUTER_BREAKER_FAILURES: int = Field(default=2, ge=1)  # consecutive rejects / timeouts at one venue ...
    ROUTER_BREAKER_WINDOW_SECONDS: float = Field(default=60.0, gt=0)  # ... inside this window trip its breaker ...
    ROUTER_BREAKER_PAUSE_SECONDS: float = Field(default=300.0, gt=0)  # ... and pause it this long
    ROUTER_SWEEP_INTERVAL_SECONDS: float = Field(default=60.0, ge=10)
    PINNACLE_API_BASE_URL: str = "https://api.pinnacle.com"
    BETFAIR_IDENTITY_URL: str = "https://identitysso.betfair.com/api"
    # Parimatch direct injection: odds posted by the user or an authorised feed (POST /parimatch/odds)
    PARIMATCH_FEED_TOKEN: SecretStr | None = None  # X-Parimatch-Feed-Token for a feed without a user login
    PARIMATCH_FEED_MAX_EVENTS: int = Field(default=200, ge=1, le=2000)

    # The Wire: public sports RSS feeds (no key needed)
    WIRE_NEWS_FEEDS: List[str] = [
        "https://feeds.bbci.co.uk/sport/rss.xml",
        "https://feeds.bbci.co.uk/sport/football/rss.xml",
        "https://feeds.bbci.co.uk/sport/cricket/rss.xml",
    ]
    # ---- The Wire: VIDUR's feeds become the fortress's evidence (Group 78) ----
    WIRE_SCAN_ENABLED: bool = True  # the beat's news, ESPN and weather scans
    # Group 112: the dashboard's scores come from ESPN (free, synced by the beat). The Odds API scores call costs credits
    # per sport on every dashboard poll, so it is off unless switched on here (then cached per sport across requests).
    WIRE_ODDS_API_SCORES_ENABLED: bool = False
    WIRE_PREFIX: str = "betdoc:wire"  # Redis: the live channel, recent frames, ESPN links
    WIRE_RECENT_FRAMES: int = Field(default=50, ge=1, le=500)  # what a new /ws/the-wire socket is sent first
    WIRE_HTTP_TIMEOUT_SECONDS: float = Field(default=8.0, gt=0)
    WIRE_USER_AGENT: str = "BetDoc-Wire/2.0"
    WIRE_HORIZON_HOURS: float = Field(default=72.0, gt=0)  # fixtures kicking off within this are scanned
    # news: RSS (above) and newsapi.org when a key is set
    NEWSAPI_ORG_API_KEY: SecretStr | None = None
    WIRE_NEWSAPI_URL: str = "https://newsapi.org/v2/top-headlines"
    WIRE_NEWSAPI_PARAMS: dict[str, str] = Field(default_factory=lambda: {"category": "sports", "language": "en", "pageSize": "50"})
    WIRE_NEWS_SCAN_MINUTES: int = Field(default=3, ge=1, le=59)
    WIRE_NEWS_MAX_AGE_HOURS: float = Field(default=48.0, gt=0)  # older articles are not ingested
    WIRE_NEWS_LIMIT: int = Field(default=60, ge=1, le=500)  # the dashboard and ticker show this many
    # host suffix -> credibility (tier 1 1.0, tier 2 0.9, tier 3 0.75, tabloids 0.4); unknown hosts get the default
    WIRE_SOURCE_CREDIBILITY: dict[str, float] = Field(default_factory=lambda: {
        "bbc.co.uk": 1.0, "bbc.com": 1.0, "reuters.com": 1.0,
        "skysports.com": 0.9, "espn.com": 0.9, "espn.co.uk": 0.9, "espncricinfo.com": 0.9, "theathletic.com": 0.9, "nytimes.com": 0.9,
        "theguardian.com": 0.85,
        "marca.com": 0.75, "as.com": 0.75, "gazzetta.it": 0.75, "lequipe.fr": 0.75, "kicker.de": 0.75,
        "thesun.co.uk": 0.4, "dailymail.co.uk": 0.4, "mirror.co.uk": 0.4, "dailystar.co.uk": 0.4,
    })
    WIRE_SOURCE_DEFAULT_CREDIBILITY: float = Field(default=0.4, ge=0, le=1)  # tier 4: an unverified aggregator
    WIRE_SENTIMENT_MEDIUM: float = Field(default=0.35, gt=0, lt=1)  # |S| over this lifts an unclassified item to MEDIUM
    WIRE_CRITICAL_WINDOW_HOURS: float = Field(default=2.0, gt=0)  # an absence or sacking this close to a mentioned kickoff is CRITICAL
    WIRE_ALERT_IMPACTS: List[str] = Field(default_factory=lambda: ["CRITICAL"])  # news on a tracked fixture at these impacts pages
    # news-to-steam catalysts: the mentioned fixture's de-vigged consensus moves this much, the news's way, inside the window
    WIRE_CATALYST_WINDOW_SECONDS: float = Field(default=900.0, gt=0)
    WIRE_CATALYST_MIN_PROB_SHIFT: float = Field(default=0.035, gt=0, lt=1)
    WIRE_CATALYST_IMPACTS: List[str] = Field(default_factory=lambda: ["CRITICAL", "HIGH"])
    # venue weather (Open-Meteo: free, no key) and the scoring friction factor
    WIRE_OPEN_METEO_FORECAST_URL: str = "https://api.open-meteo.com/v1/forecast"
    WIRE_OPEN_METEO_GEOCODING_URL: str = "https://geocoding-api.open-meteo.com/v1/search"
    WIRE_WEATHER_SCAN_MINUTES: int = Field(default=30, ge=5, le=59)
    WIRE_MATCH_HOURS: dict[str, float] = Field(default_factory=lambda: {
        "soccer": 2.0, "americanfootball": 3.5, "baseball": 3.0, "cricket": 8.0, "tennis": 3.0, "rugby": 2.0, "aussierules": 2.5,
    })  # the forecast window after kickoff, by sport key prefix
    WIRE_MATCH_HOURS_DEFAULT: float = Field(default=2.5, gt=0)
    WIRE_INDOOR_SPORT_PREFIXES: List[str] = Field(default_factory=lambda: ["basketball", "icehockey", "mma", "boxing", "esports"])
    WIRE_GEOCODER_FUZZY_CUTOFF: float = Field(default=0.85, gt=0, le=1)  # 0.70 maps "Manchester City" to United's ground
    WIRE_TEMP_COLD_BANDS: List[tuple[float, float]] = Field(default_factory=lambda: [(0.0, 0.92), (10.0, 0.96)])  # under each bound: the factor
    WIRE_TEMP_HOT_ABOVE_C: float = 32.0
    WIRE_TEMP_HOT_FACTOR: float = Field(default=0.95, gt=0)
    WIRE_WIND_FREE_KMH: float = Field(default=15.0, ge=0)
    WIRE_WIND_SLOPE: float = Field(default=0.008, ge=0)  # per km/h over the free speed
    WIRE_WIND_FLOOR: float = Field(default=0.70, gt=0, le=1)
    WIRE_RAIN_LIGHT_FACTOR: float = Field(default=0.90, gt=0)
    WIRE_RAIN_HEAVY_MMH: float = Field(default=5.0, gt=0)
    WIRE_RAIN_HEAVY_FACTOR: float = Field(default=0.78, gt=0)  # also any snow
    WIRE_ALTITUDE_SLOPE: float = Field(default=0.00006, ge=0)  # per metre
    WIRE_ALTITUDE_CAP_M: float = Field(default=2500.0, ge=0)
    WIRE_DEW_SPORT_PREFIXES: List[str] = Field(default_factory=lambda: ["cricket"])
    WIRE_DEW_FACTOR: float = Field(default=1.15, gt=0)
    WIRE_DEW_HUMIDITY_PCT: float = Field(default=75.0, ge=0, le=100)
    WIRE_DEW_LOCAL_FROM: str = Field(default="19:30", pattern=r"^\d{2}:\d{2}$")  # local time at the venue
    # ESPN's public scoreboard and match summaries (scores with the clock, venues, team sheets, injuries, officials, cards)
    WIRE_ESPN_ENABLED: bool = True
    WIRE_ESPN_BASE_URL: str = "https://site.api.espn.com/apis/site/v2/sports"
    WIRE_ESPN_LEAGUES: dict[str, str] = Field(default_factory=lambda: {  # The Odds API sport key -> ESPN league path
        "soccer_epl": "soccer/eng.1", "soccer_efl_champ": "soccer/eng.2", "soccer_spain_la_liga": "soccer/esp.1", "soccer_italy_serie_a": "soccer/ita.1",
        "soccer_germany_bundesliga": "soccer/ger.1", "soccer_france_ligue_one": "soccer/fra.1", "soccer_netherlands_eredivisie": "soccer/ned.1",
        "soccer_portugal_primeira_liga": "soccer/por.1", "soccer_spl": "soccer/sco.1", "soccer_usa_mls": "soccer/usa.1", "soccer_mexico_ligamx": "soccer/mex.1",
        "soccer_brazil_campeonato": "soccer/bra.1", "soccer_argentina_primera_division": "soccer/arg.1", "soccer_uefa_champs_league": "soccer/uefa.champions",
        "soccer_uefa_europa_league": "soccer/uefa.europa", "soccer_uefa_europa_conference_league": "soccer/uefa.europa.conf",
        "americanfootball_nfl": "football/nfl", "americanfootball_ncaaf": "football/college-football", "basketball_nba": "basketball/nba",
        "basketball_wnba": "basketball/wnba", "basketball_ncaab": "basketball/mens-college-basketball", "baseball_mlb": "baseball/mlb",
        "icehockey_nhl": "hockey/nhl", "mma_mixed_martial_arts": "mma/ufc",
    })
    WIRE_ESPN_SYNC_MINUTES: int = Field(default=2, ge=1, le=59)
    WIRE_ESPN_DAYS_BACK: int = Field(default=1, ge=0, le=7)  # yesterday's finished matches feed the officiating records
    WIRE_ESPN_SUMMARY_REFRESH_MINUTES: float = Field(default=30.0, gt=0)  # a fixture's summary (sheets, injuries) is re-read this often ...
    WIRE_ESPN_SUMMARY_NEAR_MINUTES: float = Field(default=5.0, gt=0)  # ... and this often inside the critical window
    WIRE_TEAM_MATCH_CUTOFF: float = Field(default=0.80, gt=0, le=1)  # ESPN team name vs the odds feed's
    WIRE_KICKOFF_TOLERANCE_MINUTES: float = Field(default=90.0, gt=0)
    # absences: delta = -min(cap, sum rating/scale x lambda(position) x (1 - replacement) x status weight)
    WIRE_POSITION_WEIGHTS: dict[str, dict[str, float]] = Field(default_factory=lambda: {
        "soccer": {"G": 1.4, "GK": 1.4, "F": 1.3, "ST": 1.3, "CF": 1.3, "CD": 1.2, "CB": 1.2, "D": 1.2, "CM": 1.1, "M": 1.1, "DM": 1.1, "AM": 1.1,
                   "LB": 0.8, "RB": 0.8, "LWB": 0.8, "RWB": 0.8, "LW": 0.9, "RW": 0.9, "LM": 0.9, "RM": 0.9},
        "basketball": {"PG": 1.5, "C": 1.4, "SG": 1.2, "SF": 1.2},
        "americanfootball": {"QB": 3.5, "OT": 1.3, "LT": 1.3, "DE": 1.2, "EDGE": 1.2, "CB": 1.0},
        "cricket": {"BOWLER_PACE": 1.6, "BATTER_TOP": 1.5, "WK": 1.1},
    })
    WIRE_POSITION_DEFAULT_WEIGHT: float = Field(default=1.0, gt=0)
    WIRE_ABSENCE_STATUS_WEIGHTS: dict[str, float] = Field(default_factory=lambda: {"OUT": 1.0, "SUSPENDED": 1.0, "DOUBTFUL": 0.5, "QUESTIONABLE": 0.25})
    WIRE_RATING_SCALE: float = Field(default=10.0, gt=0)  # player ratings run 0 .. this
    WIRE_RATING_FULL_COST: float = Field(default=0.10, gt=0, lt=1)  # a top-rated player at weight 1: this much of the side's win probability
    WIRE_LINEUP_MAX_DELTA: float = Field(default=0.25, gt=0, lt=1)  # one side's win probability moves at most this much
    WIRE_REPLACEMENT_QUALITY_DEFAULT: float = Field(default=0.0, ge=0, lt=1)
    # referees: tendencies from officiating records, shrunk towards the league's mean
    WIRE_REFEREE_PRIOR_MATCHES: float = Field(default=10.0, ge=0)
    WIRE_REFEREE_MIN_MATCHES: int = Field(default=5, ge=1)  # fewer: the profile is shown but not written to the fortress
    WIRE_REFEREE_TOTALS_LINE: float = Field(default=2.5, gt=0)

    # ---- Live competitive market intelligence: KARNA & PANINI (Group 85) ----
    # Parimatch and 1xBet against the sharp zero-vig consensus, on the live board's Match Odds; public ticket and
    # money shares only as measured (the public splits evidence, else BetDoc's own recorded bets): never seeded.
    COMPETITIVE_LIVE_BOOKS: dict[str, str] = Field(default_factory=lambda: {"parimatch": "Parimatch", "1xbet": "1xBet"})  # canonical book -> display
    COMPETITIVE_LIVE_SPORT_PRIORITY: List[str] = Field(default_factory=lambda: ["Cricket", "Football", "Tennis", "Basketball", "MMA/UFC"])
    COMPETITIVE_LIVE_SYNC_MINUTES: int = Field(default=10, ge=1, le=59)
    COMPETITIVE_LIVE_REFERENCE_BANKROLL_INR: Decimal = Field(default=Decimal("100000"), gt=0)  # stakes shown at this bankroll (KUMBHA's sizing policy)
    # reverse line movement: the crowd heavy on a side whose price drifts, or light on one the money steams in
    COMPETITIVE_RLM_PUBLIC_HEAVY_PCT: float = Field(default=65.0, gt=0, le=100)
    COMPETITIVE_RLM_PUBLIC_LIGHT_PCT: float = Field(default=38.0, gt=0, le=100)
    COMPETITIVE_RLM_MONEY_HEAVY_PCT: float = Field(default=58.0, gt=0, le=100)
    COMPETITIVE_RLM_MIN_ODDS_MOVE: float = Field(default=0.04, gt=0)
    # a public trap: the crowd heavy on a side that is worth less than its price, or that the money avoids
    COMPETITIVE_TRAP_PUBLIC_PCT: float = Field(default=65.0, gt=0, le=100)
    COMPETITIVE_TRAP_MAX_EV_PCT: float = -1.5
    COMPETITIVE_TRAP_MONEY_MAX_PCT: float = Field(default=40.0, ge=0, le=100)
    # verdicts: a sharp contrarian edge, a sharp consensus edge
    COMPETITIVE_CONTRARIAN_MIN_EV_PCT: float = 3.0
    COMPETITIVE_CONTRARIAN_MIN_DIVERGENCE_PCT: float = 12.0
    COMPETITIVE_CONSENSUS_MIN_EV_PCT: float = 2.0
    COMPETITIVE_CONSENSUS_MIN_MONEY_PCT: float = Field(default=55.0, gt=0, le=100)
    COMPETITIVE_HIGH_VIG_PCT: float = Field(default=7.0, gt=0)  # a crowd pick whose cheapest account book charges more: CAUTION_HIGH_VIG
    # ---- Emergency fortress: kill switch, auto-drawdown and streak breaker, per-book locks (Group 86, KAUTILYA, CHANAKYA & SHIVAJI) ----
    EMERGENCY_AUTO_KILL_DRAWDOWN_PCT: float = Field(default=15.0, ge=1, le=95)  # rolling loss / reference bankroll that trips the breaker
    EMERGENCY_AUTO_KILL_WINDOW_HOURS: int = Field(default=24, ge=1, le=168)
    EMERGENCY_MAX_CONSECUTIVE_LOSSES: int = Field(default=5, ge=1, le=25)
    EMERGENCY_DRAWDOWN_COOLDOWN_MINUTES: int = Field(default=240, ge=1)
    EMERGENCY_STREAK_COOLDOWN_MINUTES: int = Field(default=120, ge=1)
    EMERGENCY_MANUAL_COOLDOWN_MINUTES: int = Field(default=240, ge=1)
    EMERGENCY_RESUME_DAILY_EXPOSURE_INR: float = Field(default=500.0, gt=0)  # the resume form's default
    EMERGENCY_BOOK_LOCK_PREFIX: str = "betdoc:kill_switch:book"  # <prefix>:<parimatch|1xbet> = "1" while that book is locked
    # ---- Encrypted backup & integrity restore vault (Group 87, SHIVAJI & TODAR MAL) ----
    BACKUP_VAULT_DIR: str = "data/backups"  # encrypted snapshots; relative to PROJECT_ROOT unless absolute (gitignored)
    BACKUP_RETENTION_COUNT: int = Field(default=30, ge=1, le=365)  # newest snapshots kept; pinned ones are never pruned
    BACKUP_SCHEDULE_ENABLED: bool = True  # the beat's automated snapshot (and retention pass)
    BACKUP_SCHEDULE_HOURS: int = Field(default=6, ge=1, le=168)
    BACKUP_MAX_ROWS_PER_TABLE: int = Field(default=50_000, ge=100, le=500_000)  # a table over this is cut and the snapshot marked incomplete
    # ---- Group 92: API rate-limit intelligence & quota guard (BHEESHMA) -----
    # A metered fleet source at or under this quota fraction is shielded (AUTO mode): the beat polls it every
    # interval x QUOTA_GUARD_SHIELD_INTERVAL_MULTIPLIER and the desk serves the stored odds in between. Under
    # OMNI_FLEET_QUOTA_RESERVE the existing reserve takes over (no polling, failover) whatever the shield mode.
    QUOTA_GUARD_WARN_FRACTION: float = Field(default=0.20, gt=0.0, lt=1.0)
    QUOTA_GUARD_SHIELD_INTERVAL_MULTIPLIER: float = Field(default=4.0, ge=1.0, le=100.0)
    QUOTA_GUARD_SHIELD_MODE_KEY: str = "betdoc:quota_guard:shield_mode"  # Redis hash: mode (AUTO | FORCE_CACHE_SHIELD | LIVE_UNRESTRICTED), set_by, set_at, reason
    QUOTA_GUARD_EXCLUDED_SOURCES: list[str] = Field(default_factory=lambda: ["phantom", "garuda", "arjuna", "coingecko"])  # retired feeds, hidden from the guard (listed as excluded)
    # the contrarian edge score (0-100): 50 + clamp(divergence x w) + clamp(EV x w) + RLM bonus - trap penalty + tight-margin bonus
    COMPETITIVE_EDGE_WEIGHTS: dict[str, float] = Field(default_factory=lambda: {
        "base": 50.0, "divergence_weight": 0.75, "divergence_min": -25.0, "divergence_max": 25.0, "ev_weight": 1.8, "ev_min": -20.0, "ev_max": 25.0,
        "rlm_bonus": 12.0, "trap_penalty": 22.0, "tight_margin_pct": 4.5, "tight_margin_bonus": 4.0,
    })

    # ---- The multi-sport engine: nine sport adapters, omni-provider line shopping (Group 79) ----
    SPORTS_SCAN_ENABLED: bool = True  # the beat's consensus poll and value-line screen
    SPORTS_PREFIX: str = "betdoc:sports"  # Redis: boards per feed key, the edge list, the live channels
    SPORTS_ODDS_POLL_SECONDS: float = Field(default=60.0, ge=10)  # poll_multi_sport_consensus_odds
    SPORTS_ARB_SCREEN_SECONDS: float = Field(default=30.0, ge=5)  # screen_cross_provider_arbitrage (historical name): the value-line screen
    SPORTS_FEED_MIN_INTERVAL_SECONDS: float = Field(default=900.0, ge=30)  # one feed key is fetched at most this often (quota)
    SPORTS_ODDS_REGIONS: str = "uk,eu,us,au"
    # Group 112: "store" builds the consensus boards from the boards the ingestion fleet already bought
    # (pre_match_odds_snapshots), spending no credit; "direct" buys each feed key itself (regions x 1 credits a call)
    SPORTS_ODDS_SOURCE: Literal["store", "direct"] = "store"
    SPORTS_VALUE_LINE_MAX_AGE_SECONDS: float = Field(default=900.0, gt=0)  # a board older than this lists no value line
    SPORTS_BOARD_TTL_SECONDS: int = Field(default=3600, ge=60)
    SPORTS_MIN_EDGE: float = Field(default=0.02, gt=0, lt=1)  # a value line: EV over the fair (model or sharp) price
    SPORTS_NORMALIZATION_BUDGET_MS: float = Field(default=12.0, gt=0)  # FA-3: one event's odds normalised within this
    SPORTS_MAX_EDGES: int = Field(default=200, ge=1, le=5000)  # the edge list kept in Redis

    # ---- Pre-match rate persistence (Group 101, point b.13a; Group 112's quota-safe cadence) ----
    # Every Odds API board the ingestion fleet fetches is also kept as one row per fixture (pre_match_odds_snapshots), and
    # the fleet fetches a sport only when its nearest fixture's tier says the stored board is due: tier 1 (kickoff over
    # TIER1_KICKOFF away) every TIER1_MAX_AGE, tier 2 (over TIER2_KICKOFF away) every TIER2_MAX_AGE, tier 3 (closer, or
    # in play) every TIER3_MAX_AGE, never faster than the fleet's own ODDS_POLLING_INTERVAL_SEC. One /odds call prices
    # every fixture of a sport, so the sport is the unit a credit buys. A sport with no fixture stored is on tier 1.
    # Group 112: 6h beyond 24h, 2h from 6h to 24h, 30 min inside 6h and in play (Group 101 ran 6h / 60 min / 60 s, which
    # a 500-credit monthly plan cannot carry: one sport in play at 60 s spends 360 credits in three hours).
    PRE_MATCH_TIERED_POLLING_ENABLED: bool = True  # False: the fleet polls every sport at its own interval (pre-Group 101)
    PRE_MATCH_HORIZON_HOURS: float = Field(default=72.0, gt=0, le=24 * 14)  # the "all" window served to the desk
    PRE_MATCH_INPLAY_WINDOW_HOURS: float = Field(default=3.0, gt=0, le=24 * 6)  # a fixture this long past kickoff is still live (tier 3)
    PRE_MATCH_RETENTION_HOURS: float = Field(default=48.0, gt=0)  # rows this long past kickoff are pruned on the next write
    PRE_MATCH_TIER1_KICKOFF_SECONDS: float = Field(default=24 * 3600.0, gt=0)
    PRE_MATCH_TIER1_MAX_AGE_SECONDS: float = Field(default=6 * 3600.0, gt=0)
    PRE_MATCH_TIER2_KICKOFF_SECONDS: float = Field(default=6 * 3600.0, gt=0)
    PRE_MATCH_TIER2_MAX_AGE_SECONDS: float = Field(default=2 * 3600.0, gt=0)
    PRE_MATCH_TIER3_MAX_AGE_SECONDS: float = Field(default=30 * 60.0, gt=0)
    PRE_MATCH_FORCE_REFRESH_COOLDOWN_SECONDS: float = Field(default=60.0, ge=0)  # a sport fetched this recently is served, not re-bought
    PRE_MATCH_REDIS_PREFIX: str = "betdoc:pre_match"  # <prefix>:sport_polled hash: sport key -> last fetch (epoch seconds)

    # ---- Live sports feeds beyond the odds (Group 112, point b.13) ----
    # Football-Data.org (schedules, kickoff times, league tables, match status and score) and Sportmonks (fixtures, status
    # and scores of the leagues its plan covers) are paired to the stored Odds API fixtures by team names and kickoff and
    # kept as each fixture's context in Redis; neither prices anything, and neither settles a bet.
    FOOTBALL_DATA_ORG_API_KEY: SecretStr | None = None
    FOOTBALL_DATA_BASE_URL: str = "https://api.football-data.org/v4"
    # Football-Data.org competition code -> the Odds API sport key it is paired against (the free tier's big five here)
    FOOTBALL_DATA_COMPETITIONS: dict[str, str] = Field(default_factory=lambda: {
        "PL": "soccer_epl", "CL": "soccer_uefa_champs_league", "PD": "soccer_spain_la_liga", "SA": "soccer_italy_serie_a", "BL1": "soccer_germany_bundesliga",
    })
    # Group 79 spelled it SPORTMONKS_API_KEY; both names are read
    SPORTSMONKS_API_KEY: SecretStr | None = Field(default=None, validation_alias=AliasChoices("SPORTSMONKS_API_KEY", "SPORTMONKS_API_KEY"))
    SPORTSMONKS_BASE_URL: str = "https://api.sportmonks.com/v3"
    # Sportmonks league id -> Odds API sport key (its free plan's two leagues; a paid plan adds rows here)
    SPORTSMONKS_LEAGUE_SPORT_KEYS: dict[int, str] = Field(default_factory=lambda: {271: "soccer_denmark_superliga", 501: "soccer_spl"})
    LIVE_CONTEXT_ENABLED: bool = True
    LIVE_CONTEXT_REFRESH_SECONDS: float = Field(default=900.0, ge=60.0)  # the beat task; one schedule call per provider a run
    LIVE_CONTEXT_LOOKBACK_DAYS: int = Field(default=1, ge=0, le=3)  # yesterday's matches, for their final score
    LIVE_CONTEXT_LOOKAHEAD_DAYS: int = Field(default=7, ge=1, le=9)  # Football-Data.org's free tier allows a 10-day range
    LIVE_CONTEXT_NAME_CUTOFF: float = Field(default=0.85, gt=0.5, le=1.0)  # both sides' name similarity (the Wire's rule)
    LIVE_CONTEXT_PAIR_WINDOW_HOURS: float = Field(default=72.0, gt=0, le=240)  # a provider kickoff this far off still pairs (and is MOVED)
    LIVE_CONTEXT_KICKOFF_TOLERANCE_MINUTES: float = Field(default=5.0, ge=0, le=120)  # within this the kickoff is CONFIRMED
    LIVE_CONTEXT_STANDINGS_MAX_AGE_SECONDS: float = Field(default=6 * 3600.0, ge=600)  # one table call per competition this often
    LIVE_CONTEXT_TTL_SECONDS: int = Field(default=2 * 86400, ge=3600)
    LIVE_CONTEXT_REDIS_PREFIX: str = "betdoc:live_context"
    LIVE_CONTEXT_HTTP_TIMEOUT_SECONDS: float = Field(default=15.0, gt=0, le=120)
    # The match board lists stored Odds API fixtures the live market is not pricing right now, with their stored prices,
    # how old those are and no EV (a stale price is never vetted, staked or added to the slip from the board)
    MATCH_BOARD_STORED_PRICES: bool = True

    # ---- BETDOC SCORE PREDICTION (Group 102, point b.16): presentation only; every number comes from the sport engines ----
    PREDICTION_HEATMAP_GOALS: int = Field(default=5, ge=3, le=9)  # the football heatmap's 0..N goals a side
    PREDICTION_TOP_SCORELINES: int = Field(default=3, ge=1, le=10)
    PREDICTION_INTERVAL_COVERAGE: float = Field(default=0.80, gt=0.5, lt=1.0)  # central interval of a projected total (0.80: p10-p90)
    # FA-4: the CFO's fractional Kelly per sport (model parameter uncertainty); overrides app.domain.sports.twin_bridge's table
    SPORTS_KELLY_MULTIPLIERS: dict[str, float] = Field(default_factory=lambda: {
        "soccer": 0.35, "cricket": 0.25, "basketball": 0.40, "tennis": 0.30, "baseball": 0.35, "american_football": 0.50,
        "ice_hockey": 0.30, "combat": 0.20, "esports": 0.25,
    })
    # The Wire's absence impact (0..1) as wins above replacement, per sport (a 1.0-impact player is this many WAR)
    SPORTS_WAR_PER_IMPACT: dict[str, float] = Field(default_factory=lambda: {
        "soccer": 6.0, "basketball": 12.0, "american_football": 4.0, "baseball": 6.0, "ice_hockey": 4.0,
    })
    # Omni-provider catalogue (Categories A-E): keys come from the Vault first, these settings second, never from Git
    PINNACLE_API_KEY: SecretStr | None = None
    BETFAIR_APP_KEY: SecretStr | None = None
    PARIMATCH_TARGET_URL: str = "https://parimatchs1.com/en/all-live"
    ONE_XBET_TARGET_URL: str = "https://indian.1xbet.com/en"
    STAKE_TARGET_URL: str = "https://stake.com/sports/home"
    RAPIDAPI_KEY: SecretStr | None = None
    ODDSPAPI_API_KEY: SecretStr | None = None
    SHARPAPI_API_KEY: SecretStr | None = None
    SPORTSDATA_IO_KEY: SecretStr | None = None
    THE_RUNDOWN_API_KEY: SecretStr | None = None
    KALSHI_BASE_URL: str = "https://api.elections.kalshi.com/trade-api/v2"
    MANIFOLD_BASE_URL: str = "https://api.manifold.markets/v0"

    # ---- TODAR MAL's two-currency engine and the Section 115BBJ ledger (Group 98: INR canonical, USD for display only) ----
    # Every stored amount, stake, payout and TDS figure is INR; USD is a display conversion at the ECB reference rate.
    # No fresh rate means no USD figure: nothing falls back to a guessed constant.
    CURRENCY_SUPPORTED: List[str] = Field(default_factory=lambda: ["INR", "USD"])
    CURRENCY_CACHE_KEY: str = "fx:rates:latest"  # Redis hash: "<BASE>_<QUOTE>" -> {"rate", "as_of", "provider"}
    CURRENCY_MAX_AGE_SECONDS: int = Field(default=6 * 3600, ge=60)  # an older rate is refused, never shown as current
    CURRENCY_REFRESH_SECONDS: float = Field(default=900.0, ge=60)
    CURRENCY_FIAT_URL: str = "https://api.frankfurter.app"  # ECB reference rate (USD vs INR)
    CURRENCY_HTTP_TIMEOUT_SECONDS: float = Field(default=6.0, gt=0)

    # ---- Real-Money Live Desk, Parimatch execution gateway, SHIVAJI circuit breaker (Group 95) ----
    REAL_MAX_STAKE_PCT: float = Field(default=0.05, gt=0, le=0.5)  # 5% max single wager ceiling
    REAL_MIN_EDGE: float = Field(default=0.015, ge=0, le=0.5)  # 1.5% minimum edge floor
    REAL_DAILY_DRAWDOWN_PCT: float = Field(default=0.15, gt=0, le=1.0)  # 15% daily drawdown stop-loss limit
    REAL_LOSS_STREAK_LIMIT: int = Field(default=3, ge=1, le=20)  # 3 consecutive losses
    REAL_COOLDOWN_MINUTES: int = Field(default=30, ge=1, le=1440)  # 30-minute cool-down
    REAL_SETTLE_INTERVAL_SECONDS: float = Field(default=60.0, ge=10)

    @property
    def odds_sport_keys(self) -> List[str]:
        """ODDS_SPORT_KEYS, then the sports the Vault activated (``app.core.fleet_overlay``)."""
        from app.core.fleet_overlay import current  # noqa: PLC0415 - state only, no import cycle

        keys = [s.strip() for s in self.ODDS_SPORT_KEYS.split(",") if s.strip()]
        return keys + [s for s in current().sports if s not in keys]

    def odds_api_env_key(self) -> SecretStr | None:
        """The environment's Odds API key (Group 112): THE_ODDS_API_KEY, else ODDS_API_KEY."""
        for key in (self.THE_ODDS_API_KEY, self.ODDS_API_KEY):
            if key is not None and key.get_secret_value():
                return key
        return None

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
    # ---- Group 100: Scout Oracle's local language model (Ollama) ---------------
    # Off, unreachable or slow: the Scout answers from its rule engine on the live book instead (never an error).
    SCOUT_LLM_ENABLED: bool = True
    SCOUT_LLM_BASE_URL: str = "http://localhost:11434"  # http://ollama:11434 inside docker compose
    SCOUT_LLM_MODEL: str = "qwen2.5:7b-instruct"  # qwen2.5:3b-instruct on a smaller machine
    SCOUT_LLM_TIMEOUT_SECONDS: float = Field(default=20.0, gt=0, le=120)  # one answer, including a cold model load
    SCOUT_LLM_PROBE_TIMEOUT_SECONDS: float = Field(default=1.5, gt=0, le=10)
    SCOUT_LLM_RETRY_AFTER_SECONDS: float = Field(default=30.0, ge=0)  # after a failed probe, skip the model this long
    SCOUT_LLM_TEMPERATURE: float = Field(default=0.2, ge=0, le=1)
    SCOUT_LLM_MAX_TOKENS: int = Field(default=600, ge=64, le=4096)
    HIVE_SUPERVISOR_INTERVAL_SECONDS: float = Field(default=30.0, ge=0)  # 0 disables commander heartbeats
    # ---- Group 90: commander telemetry -------------------------------------
    HIVE_SUPERVISOR_PROBE_TIMEOUT_SECONDS: float = Field(default=5.0, gt=0, le=30.0)  # one subsystem probe; over it the commander is FATAL
    # A beat older than interval x multiplier is NO SIGNAL (3.2 x 30s = 96s, the frontend's STALE_AFTER_MS);
    # the floor applies while the background supervisor is off and only on-demand probes write beats
    HIVE_SUPERVISOR_STALE_MULTIPLIER: float = Field(default=3.2, gt=1.0, le=20.0)
    HIVE_SUPERVISOR_STALE_FLOOR_SECONDS: float = Field(default=96.0, gt=0)
    HIVE_SUPERVISOR_LATENCY_WARN_MS: float = Field(default=250.0, gt=0)  # probe latency at or over this is flagged

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

    # ---- Group 93: historical match, score & CLV warehouse (TODAR MAL) -----
    # Edge tier from the mean CLV of settled slips: SHARP_ALPHA needs both the mean and the beat-close rate;
    # under CLV_WAREHOUSE_MIN_SAMPLE measured slips the tier is shown but flagged provisional
    CLV_WAREHOUSE_SHARP_ALPHA_MIN_PCT: float = Field(default=1.5, gt=0.0)
    CLV_WAREHOUSE_SHARP_BEAT_RATE_MIN: float = Field(default=0.55, gt=0.0, lt=1.0)
    CLV_WAREHOUSE_POSITIVE_MIN_PCT: float = Field(default=0.15, ge=0.0)  # mean CLV above this: POSITIVE_CLV
    CLV_WAREHOUSE_ADVERSE_BELOW_PCT: float = Field(default=-0.25, le=0.0)  # mean CLV below this: ADVERSE_DRIFT
    CLV_WAREHOUSE_MIN_SAMPLE: int = Field(default=30, ge=1)
    CLV_WAREHOUSE_DEFAULT_LIMIT: int = Field(default=50, ge=1, le=500)  # settled slips listed
    CLV_WAREHOUSE_RECENT_SCORES: int = Field(default=25, ge=1, le=200)  # final scores listed

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

    @field_validator("CURRENCY_SUPPORTED")
    @classmethod
    def validate_two_currencies(cls, v: list[str]) -> list[str]:
        codes = [c.strip().upper() for c in v]
        if "INR" not in codes or set(codes) - {"INR", "USD"}:
            raise ValueError("CURRENCY_SUPPORTED: BetDoc runs on INR (canonical) and USD (display) only")
        return list(dict.fromkeys(codes))

    @field_validator("PRE_MATCH_TIER3_MAX_AGE_SECONDS")
    @classmethod
    def validate_pre_match_tiers(cls, v: float, info) -> float:  # noqa: ANN001 - pydantic ValidationInfo
        d = info.data
        t1, t2 = d.get("PRE_MATCH_TIER1_KICKOFF_SECONDS"), d.get("PRE_MATCH_TIER2_KICKOFF_SECONDS")
        a1, a2 = d.get("PRE_MATCH_TIER1_MAX_AGE_SECONDS"), d.get("PRE_MATCH_TIER2_MAX_AGE_SECONDS")
        if None not in (t1, t2) and not t2 < t1:
            raise ValueError("PRE_MATCH_TIER2_KICKOFF_SECONDS must be below PRE_MATCH_TIER1_KICKOFF_SECONDS")
        if None not in (a1, a2) and not v <= a2 <= a1:
            raise ValueError("pre-match max ages must not grow as kickoff nears: TIER3 <= TIER2 <= TIER1")
        return v

    @field_validator("QUOTA_GUARD_WARN_FRACTION")
    @classmethod
    def validate_quota_warn_above_reserve(cls, v: float, info) -> float:  # noqa: ANN001 - pydantic ValidationInfo
        reserve = info.data.get("OMNI_FLEET_QUOTA_RESERVE")
        if reserve is not None and v <= reserve:
            raise ValueError("QUOTA_GUARD_WARN_FRACTION must be above OMNI_FLEET_QUOTA_RESERVE, or the shield could never engage before the reserve")
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

    @field_validator("NEVER_FORGET_FEATURE_WEIGHTS", "NEVER_FORGET_FEATURE_SCALES")
    @classmethod
    def validate_never_forget_features(cls, v: dict[str, float], info: object) -> dict[str, float]:
        field = getattr(info, "field_name", "")
        if any(x < 0 for x in v.values()) or (field.endswith("SCALES") and any(x <= 0 for x in v.values())):
            raise ValueError(f"{field}: weights are not negative and scales are positive")
        if field.endswith("WEIGHTS") and sum(v.values()) <= 0:
            raise ValueError(f"{field}: at least one feature needs a weight")
        return v

    @field_validator("XP_TIERS")
    @classmethod
    def validate_xp_tiers(cls, v: dict[str, int]) -> dict[str, int]:
        floors = list(v.values())
        if not floors or floors[0] != 0 or any(b <= a for a, b in zip(floors, floors[1:])):
            raise ValueError("XP_TIERS: ranks in ascending order, the first reached at 0 XP")
        return v


settings = Settings()  # type: ignore[call-arg]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings for code that resolves config lazily (workers, Omni, health)."""
    return settings
