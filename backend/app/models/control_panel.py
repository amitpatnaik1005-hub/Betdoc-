"""Control Panel: the global settings singleton (UI, APIs, bots, risk)."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models import Base

SETTINGS_SINGLETON_ID = 1


class SystemSettingsModel(Base):
    __tablename__ = "system_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_singleton"),
        CheckConstraint("default_kelly_fraction >= 0.0 AND default_kelly_fraction <= 1.0", name="ck_kelly"),
        CheckConstraint("global_stop_loss >= 0.0", name="ck_stop_loss"),
        CheckConstraint("max_bet_size >= 0.0", name="ck_max_bet"),
        CheckConstraint("max_daily_exposure >= 0.0", name="ck_max_exposure"),
        CheckConstraint("max_stake_pct >= 1 AND max_stake_pct <= 10", name="ck_max_stake_pct"),
        CheckConstraint("research_frequency_minutes >= 1", name="ck_research_frequency"),
        CheckConstraint("theme IN ('light', 'dark', 'auto')", name="ck_theme"),
        CheckConstraint("length(accent_color) BETWEEN 4 AND 16", name="ck_accent_color_length"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=SETTINGS_SINGLETON_ID, autoincrement=False)

    # Profile
    developer_name: Mapped[str] = mapped_column(String(128), default="Amit Ashok Kumar Patnaik")
    app_version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    build_info: Mapped[str] = mapped_column(String(64), default="OMEGA-LEVIATHAN")

    # Appearance
    theme: Mapped[str] = mapped_column(String(16), default="auto")
    accent_color: Mapped[str] = mapped_column(String(16), default="#3b82f6")
    reduce_motion: Mapped[bool] = mapped_column(Boolean, default=False)

    # API integrations
    odds_api_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    news_api_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    omniroute_url: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Bots
    bots_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    research_frequency_minutes: Mapped[int] = mapped_column(Integer, default=60)

    # Risk
    default_kelly_fraction: Mapped[float] = mapped_column(Float, default=0.25)
    global_stop_loss: Mapped[float] = mapped_column(Float, default=100.0)
    max_bet_size: Mapped[float] = mapped_column(Float, default=50.0)
    max_daily_exposure: Mapped[float] = mapped_column(Float, default=500.0)
    # Aryabhata never recommends a stake above this share of the live bankroll (Control Panel slider)
    max_stake_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("5.00"), server_default="5.00")

    # Telemetry
    last_emergency_stop_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
