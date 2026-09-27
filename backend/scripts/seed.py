#!/usr/bin/env python3
"""BetDoc secure database seeder.

Creates the master ADMIN account and its exchange account and risk mandate. If
they already exist, it updates them instead.

Secrets are never accepted as CLI flags, because flags leak into shell history
and `ps` output. They are read from hidden prompts, or from environment
variables for non-interactive runs (CI / containers):
    BETDOC_ADMIN_PASSWORD
    BETDOC_EXCHANGE_API_SECRET

Usage:
    python scripts/seed.py --username admin --api-key KEY \
        --max-stake 100 --max-exposure 1000
"""

import argparse
import asyncio
import getpass
import os
import pathlib
import sys

# Path resolution MUST happen before any `app.*` import.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation  # noqa: E402

from sqlalchemy import select  # noqa: E402
from sqlalchemy.exc import IntegrityError, SQLAlchemyError  # noqa: E402

from app.core.database import AsyncSessionLocal, engine  # noqa: E402
from app.core.security import encrypt_api_key, get_password_hash  # noqa: E402
from app.models import ExchangeAccount, RiskMandate, User  # noqa: E402

ADMIN_ROLE = "ADMIN"
SCALE = Decimal("0.0001")
NUMERIC_16_4_MAX = Decimal("999999999999.9999")
MIN_PASSWORD_LENGTH = 12

ENV_PASSWORD = "BETDOC_ADMIN_PASSWORD"
ENV_API_SECRET = "BETDOC_EXCHANGE_API_SECRET"


# --------------------------------------------------------------------------- #
# Input parsing / validation
# --------------------------------------------------------------------------- #
def money(raw: str) -> Decimal:
    """argparse type: parse straight to Decimal (never via float), quantize to 4dp."""
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"invalid decimal value: {raw!r}")
    if not value.is_finite():
        raise argparse.ArgumentTypeError(f"value must be finite: {raw!r}")
    value = value.quantize(SCALE, rounding=ROUND_HALF_EVEN)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"value must be > 0 at 4dp scale: {raw!r}")
    if value > NUMERIC_16_4_MAX:
        raise argparse.ArgumentTypeError(f"value exceeds Numeric(16,4) bounds: {raw!r}")
    return value


def non_blank(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise argparse.ArgumentTypeError("value must not be blank")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Seed or update the BetDoc master ADMIN account.",
    )
    parser.add_argument("--username", type=non_blank, required=True)
    parser.add_argument("--exchange-name", type=non_blank, default="mock")
    parser.add_argument("--api-key", type=non_blank, required=True)
    parser.add_argument("--max-stake", type=money, required=True)
    parser.add_argument("--max-exposure", type=money, required=True)
    parser.add_argument("--kill-threshold", type=money, default=money("100.0"))
    return parser


def read_secret(prompt: str, env_var: str, *, confirm: bool = False, min_length: int = 1) -> str:
    """Read a secret from an env var or a hidden TTY prompt. Never echoes it."""
    value = os.environ.get(env_var)
    if value is not None:
        source = f"env {env_var}"
    else:
        if not sys.stdin.isatty():
            # getpass falls back to echoing stdin without a TTY. Refuse instead.
            sys.exit(f"error: no TTY available; set {env_var} for non-interactive runs")
        value = getpass.getpass(prompt)
        source = "prompt"
        if confirm and getpass.getpass("Confirm (hidden): ") != value:
            sys.exit("error: values do not match")

    if len(value) < min_length:
        sys.exit(f"error: secret from {source} must be at least {min_length} characters")
    return value


# --------------------------------------------------------------------------- #
# Seeder
# --------------------------------------------------------------------------- #
async def async_main(args: argparse.Namespace, password: str, api_secret: str) -> int:
    try:
        stake: Decimal = args.max_stake
        exposure: Decimal = args.max_exposure
        kill_threshold: Decimal = args.kill_threshold

        async with AsyncSessionLocal() as db:
            try:
                # 1. Upsert user (row-locked if it already exists)
                user = (
                    await db.execute(
                        select(User).where(User.username == args.username).with_for_update()
                    )
                ).scalar_one_or_none()

                hashed = get_password_hash(password)
                if user is not None:
                    user.hashed_password = hashed
                    user.role = ADMIN_ROLE
                    action_user = "updated"
                else:
                    user = User(
                        username=args.username,
                        hashed_password=hashed,
                        role=ADMIN_ROLE,
                    )
                    db.add(user)
                    action_user = "created"
                await db.flush()

                # 2. Upsert exchange account
                account = (
                    await db.execute(
                        select(ExchangeAccount)
                        .where(
                            ExchangeAccount.user_id == user.id,
                            ExchangeAccount.exchange_name == args.exchange_name,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()

                enc_key = encrypt_api_key(args.api_key)
                enc_secret = encrypt_api_key(api_secret)
                if account is not None:
                    account.api_key_encrypted = enc_key
                    account.api_secret_encrypted = enc_secret
                    action_account = "updated"
                else:
                    account = ExchangeAccount(
                        user_id=user.id,
                        exchange_name=args.exchange_name,
                        api_key_encrypted=enc_key,
                        api_secret_encrypted=enc_secret,
                    )
                    db.add(account)
                    action_account = "created"
                await db.flush()

                # 3. Upsert risk mandate
                mandate = (
                    await db.execute(
                        select(RiskMandate)
                        .where(RiskMandate.user_id == user.id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()

                if mandate is not None:
                    mandate.max_stake_per_bet = stake
                    mandate.max_daily_exposure = exposure
                    mandate.kill_threshold_pct = kill_threshold
                    action_mandate = "updated"
                else:
                    mandate = RiskMandate(
                        user_id=user.id,
                        max_stake_per_bet=stake,
                        max_daily_exposure=exposure,
                        kill_threshold_pct=kill_threshold,
                    )
                    db.add(mandate)
                    action_mandate = "created"

                # 4. Commit atomically: all three records or none
                user_id = user.id
                await db.commit()

            except IntegrityError as exc:
                await db.rollback()
                # Typically a concurrent seeder inserted the same username first.
                print(f"error: integrity violation, re-run to apply as update ({exc.orig})",
                      file=sys.stderr)
                return 1
            except SQLAlchemyError as exc:
                await db.rollback()
                print(f"error: database failure: {exc.__class__.__name__}: {exc}",
                      file=sys.stderr)
                return 1

        print("BetDoc seed complete")
        print(f"  User ID        : {user_id} ({action_user}, role={ADMIN_ROLE})")
        print(f"  Username       : {args.username}")
        print(f"  Exchange       : {args.exchange_name} ({action_account}, keys encrypted)")
        print(f"  Mandate        : {action_mandate}")
        print(f"    Max stake    : {stake}")
        print(f"    Max exposure : {exposure}")
        print(f"    Kill thresh. : {kill_threshold}")
        return 0

    finally:
        await engine.dispose()


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.max_stake > args.max_exposure:
        parser.error("--max-stake cannot exceed --max-exposure")

    try:
        password = read_secret(
            "Enter Superuser Password (hidden): ",
            ENV_PASSWORD,
            confirm=True,
            min_length=MIN_PASSWORD_LENGTH,
        )
        api_secret = read_secret(
            "Enter Exchange API Secret (hidden): ",
            ENV_API_SECRET,
        )
    except (KeyboardInterrupt, EOFError):
        sys.exit("\naborted")

    try:
        exit_code = asyncio.run(async_main(args, password, api_secret))
    except KeyboardInterrupt:
        sys.exit("\naborted")
    finally:
        # Drop references to plaintext secrets as early as possible.
        del password, api_secret

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
