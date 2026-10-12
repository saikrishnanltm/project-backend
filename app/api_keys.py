"""
API keys for the Developer API. Every key is minted for an api_customer
row, which is auto-created transparently the first time a LOGGED-IN
CodeSage user (any of: password, GitHub, Google) touches any
/developers/* endpoint -- see get_or_create_api_customer_for_user below
and main.py's get_or_create_current_api_customer dependency. There's no
separate "developer signup" step for someone who already has a CodeSage
account. Anyone wanting API access signs up for a normal CodeSage
account first, then mints a key from the Developer API page.

Key format: `cbi_<32 random url-safe chars>`. The `cbi_` prefix is never
secret (it's shown back in list_api_keys for the customer to tell keys
apart) and lets us cheaply reject obviously-malformed keys before even
hitting the database. Only a sha256 hash of the full key is ever stored --
see app/db.py's api_keys table docstring for why sha256 (not PBKDF2) is
the right choice here.
"""
from __future__ import annotations

import hashlib
import secrets
import time
import uuid

import psycopg2

from app.db import get_cursor

KEY_PREFIX = "cbi_"
KEY_RANDOM_CHARS = 32


class ApiKeyError(Exception):
    """Raised for any api-customer signup/key-management failure; main.py
    maps this to a 400."""


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """Returns (raw_key, key_hash, key_prefix). raw_key is returned to
    the caller exactly once -- neither this function nor anything that
    stores its output ever needs to see it again."""
    raw_key = KEY_PREFIX + secrets.token_urlsafe(KEY_RANDOM_CHARS)
    return raw_key, _hash_key(raw_key), raw_key[:len(KEY_PREFIX) + 6]


# ---------------------------------------------------------------------------
# API customers (orgs)
# ---------------------------------------------------------------------------

def get_api_customer(customer_id: str) -> dict | None:
    with get_cursor() as cur:
        cur.execute(
            "SELECT id, org_name, email, credit_balance, created_at "
            "FROM api_customers WHERE id = %s",
            (customer_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def get_or_create_api_customer_for_user(user_id: str, email: str) -> dict:
    """Auto-creates (or fetches) the api_customer record backing a
    logged-in CodeSage web user's Developer API access. Deliberately
    reuses the SAME id as the user's own users.id rather than minting a
    separate uuid -- exactly one api_customer per web account, created
    lazily and transparently the first time they touch any /developers/*
    endpoint. This is what main.py's get_or_create_current_api_customer
    dependency calls; see that dependency's docstring for the full
    "why no separate developer signup" reasoning."""
    existing = get_api_customer(user_id)
    if existing:
        return existing

    now = time.time()
    org_name = email  # no separate "org name" concept for an auto-created personal account
    try:
        with get_cursor() as cur:
            cur.execute(
                "INSERT INTO api_customers (id, org_name, email, credit_balance, created_at) "
                "VALUES (%s, %s, %s, 0, %s)",
                (user_id, org_name, email, now),
            )
    except psycopg2.IntegrityError:
        # Race: two concurrent first-touch requests (e.g. two tabs both
        # loading the Developer API page at once) both saw "missing" and
        # both tried to insert -- the loser's insert conflicts on the
        # primary key (id = user_id). Letting the exception propagate out
        # of the `with get_cursor()` block above (rather than catching it
        # INSIDE that block) matters: it's what makes get_conn() roll the
        # aborted transaction back correctly before we touch the
        # connection again below -- catching it inside the block instead
        # would leave that transaction aborted, and the implicit commit()
        # on a clean exit would itself then raise InFailedSqlTransaction.
        pass

    return get_api_customer(user_id) or {
        "id": user_id, "org_name": org_name, "email": email, "credit_balance": 0,
    }


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

def create_api_key(
    api_customer_id: str,
    label: str | None = None,
    rate_limit_per_min: int | None = None,
    monthly_credit_cap: int | None = None,
) -> str:
    """Mints an additional key for an existing api_customer (e.g. one
    per environment/service on their end) -- callers authenticate this
    with any one of their existing valid keys (see main.py's
    POST /developers/api-keys), so losing one key doesn't lock them out
    of minting a replacement. Returns the raw key -- shown once."""
    raw_key, key_hash, key_prefix = generate_api_key()
    key_id = uuid.uuid4().hex
    now = time.time()
    with get_cursor() as cur:
        cur.execute(
            "INSERT INTO api_keys (id, api_customer_id, key_hash, key_prefix, label, created_at, "
            "rate_limit_per_min, monthly_credit_cap) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (key_id, api_customer_id, key_hash, key_prefix, label, now,
             rate_limit_per_min, monthly_credit_cap),
        )
    return raw_key


def authenticate_api_key(raw_key: str) -> dict | None:
    """Looks up the api_customer owning a raw key, or None if it's
    unknown, revoked, or malformed. The returned dict is the customer row
    plus api_key_id / rate_limit_per_min / monthly_credit_cap for the key
    that was presented. last_used_at is bumped at most once a minute per
    key, so "last used" stays accurate to the minute without turning
    every request into a write."""
    if not raw_key or not raw_key.startswith(KEY_PREFIX):
        return None

    key_hash = _hash_key(raw_key)
    now = time.time()
    with get_cursor() as cur:
        cur.execute(
            "SELECT id, api_customer_id, rate_limit_per_min, monthly_credit_cap "
            "FROM api_keys WHERE key_hash = %s AND revoked_at IS NULL",
            (key_hash,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute(
            "UPDATE api_keys SET last_used_at = %s "
            "WHERE id = %s AND (last_used_at IS NULL OR last_used_at < %s)",
            (now, row["id"], now - 60),
        )

    customer = get_api_customer(row["api_customer_id"])
    if customer is None:
        return None
    return {
        **customer,
        "api_key_id": row["id"],
        "rate_limit_per_min": row["rate_limit_per_min"],
        "monthly_credit_cap": row["monthly_credit_cap"],
    }


def current_period() -> str:
    """Calendar month in UTC, e.g. '2026-10' -- the unit monthly caps reset on."""
    return time.strftime("%Y-%m", time.gmtime())


def list_api_keys(api_customer_id: str) -> list[dict]:
    """Masked key list -- key_prefix only, never the full key or its
    hash -- plus per-key limits and usage (this month and all time)."""
    period = current_period()
    with get_cursor() as cur:
        cur.execute(
            "SELECT k.id, k.key_prefix, k.label, k.created_at, k.last_used_at, k.revoked_at, "
            "       k.rate_limit_per_min, k.monthly_credit_cap, "
            "       COALESCE(m.requests, 0) AS requests_this_month, "
            "       COALESCE(m.credits, 0)  AS credits_this_month, "
            "       COALESCE(t.requests, 0) AS requests_total, "
            "       COALESCE(t.credits, 0)  AS credits_total "
            "FROM api_keys k "
            "LEFT JOIN api_key_usage m ON m.api_key_id = k.id AND m.period = %s "
            "LEFT JOIN (SELECT api_key_id, SUM(requests) AS requests, SUM(credits) AS credits "
            "           FROM api_key_usage GROUP BY api_key_id) t ON t.api_key_id = k.id "
            "WHERE k.api_customer_id = %s ORDER BY k.created_at DESC",
            (period, api_customer_id),
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for f in ("requests_this_month", "credits_this_month", "requests_total", "credits_total"):
            d[f] = int(d[f])
        out.append(d)
    return out


def update_api_key_limits(
    api_customer_id: str, key_id: str, fields: dict
) -> bool:
    """Owner-scoped update of rate_limit_per_min / monthly_credit_cap.
    `fields` holds only the keys the caller actually sent, so an explicit
    None clears a limit back to the server default while an omitted key
    is left alone. False if the key isn't yours / doesn't exist / is revoked."""
    allowed = {"rate_limit_per_min", "monthly_credit_cap", "label"}
    cols = [c for c in fields if c in allowed]
    if not cols:
        return False
    sets = ", ".join(f"{c} = %s" for c in cols)
    with get_cursor(dict_rows=False) as cur:
        cur.execute(
            f"UPDATE api_keys SET {sets} "
            "WHERE id = %s AND api_customer_id = %s AND revoked_at IS NULL",
            [fields[c] for c in cols] + [key_id, api_customer_id],
        )
        return cur.rowcount > 0


class MonthlyCapExceeded(Exception):
    """This key's monthly credit cap would be exceeded by the call."""


def effective_monthly_cap(key_cap: int | None) -> int:
    """0 means no cap."""
    from app.config import settings
    return key_cap if key_cap is not None else settings.default_key_monthly_credit_cap


def reserve_usage(api_key_id: str, key_cap: int | None, credits: int) -> None:
    """Atomically counts one metered call and `credits` against this
    key's current month, refusing (MonthlyCapExceeded) if that would
    push the month over the cap. One upsert statement, so two concurrent
    requests can't both slip under the cap."""
    cap = effective_monthly_cap(key_cap)
    if cap > 0 and credits > cap:
        raise MonthlyCapExceeded(
            f"This call costs {credits} credits, more than this key's monthly cap of {cap}."
        )
    with get_cursor(dict_rows=False) as cur:
        cur.execute(
            "INSERT INTO api_key_usage (api_key_id, period, requests, credits) "
            "VALUES (%s, %s, 1, %s) "
            "ON CONFLICT (api_key_id, period) DO UPDATE SET "
            "  requests = api_key_usage.requests + 1, "
            "  credits = api_key_usage.credits + EXCLUDED.credits "
            "WHERE %s <= 0 OR api_key_usage.credits + EXCLUDED.credits <= %s "
            "RETURNING credits",
            (api_key_id, current_period(), credits, cap, cap),
        )
        if cur.fetchone() is None:
            raise MonthlyCapExceeded(
                f"Monthly credit cap of {cap} reached for this key. "
                "Raise it on the Developer page, or wait for the month to reset (UTC)."
            )


def release_usage(api_key_id: str, credits: int) -> None:
    """Undo a reserve_usage() when the call then failed to start (e.g.
    the account turned out to have insufficient credits)."""
    with get_cursor(dict_rows=False) as cur:
        cur.execute(
            "UPDATE api_key_usage SET requests = GREATEST(requests - 1, 0), "
            "credits = GREATEST(credits - %s, 0) WHERE api_key_id = %s AND period = %s",
            (credits, api_key_id, current_period()),
        )


def adjust_usage_credits(api_key_id: str, delta: int) -> None:
    """Change this month's credit count by `delta` without touching the
    request count (settling an ingest hold: the call was already counted)."""
    with get_cursor(dict_rows=False) as cur:
        cur.execute(
            "UPDATE api_key_usage SET credits = GREATEST(credits + %s, 0) "
            "WHERE api_key_id = %s AND period = %s",
            (delta, api_key_id, current_period()),
        )


def record_usage(api_key_id: str, credits: int) -> None:
    """Counts usage that already happened (an /ingest whose size-based
    cost was only known after the job ran). Never refuses -- the work is
    done, and the ledger should say so."""
    with get_cursor(dict_rows=False) as cur:
        cur.execute(
            "INSERT INTO api_key_usage (api_key_id, period, requests, credits) "
            "VALUES (%s, %s, 1, %s) "
            "ON CONFLICT (api_key_id, period) DO UPDATE SET "
            "  requests = api_key_usage.requests + 1, "
            "  credits = api_key_usage.credits + EXCLUDED.credits",
            (api_key_id, current_period(), credits),
        )


def month_credits_used(api_key_id: str) -> int:
    with get_cursor(dict_rows=False) as cur:
        cur.execute(
            "SELECT credits FROM api_key_usage WHERE api_key_id = %s AND period = %s",
            (api_key_id, current_period()),
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0


def revoke_api_key(api_customer_id: str, key_id: str) -> bool:
    """Owner-scoped revocation. Returns False for an id that doesn't
    exist, is already revoked, or belongs to a different api_customer --
    same non-distinguishing posture as the rest of this codebase's
    ownership checks (see app/shares.py's delete_share).

    No longer refuses to revoke your last active key. That guard existed
    back when the ONLY way to manage your keys at all was presenting a
    valid CODEBASE12-Key -- revoking your one remaining key would have meant
    no way back in. Now that key management is authenticated with your
    normal CodeSage login instead (see main.py's
    get_or_create_current_api_customer), that lockout is impossible: you
    can always log back in and mint a fresh key, so there's nothing left
    for this guard to protect against, only friction it would add."""
    with get_cursor(dict_rows=False) as cur:
        cur.execute(
            "UPDATE api_keys SET revoked_at = %s "
            "WHERE id = %s AND api_customer_id = %s AND revoked_at IS NULL",
            (time.time(), key_id, api_customer_id),
        )
        return cur.rowcount > 0
