"""
Per-API-key rate limiting: a fixed one-minute window counted in Redis.

Fixed windows allow a short burst of up to 2x the limit across a window
boundary; that's an acceptable trade for a counter that is one INCR per
request and needs no cleanup beyond a TTL. If Redis is unreachable we
fail OPEN (log and allow) -- a Redis blip shouldn't take the whole API
down, and credits/monthly caps (Postgres) still apply.
"""
from __future__ import annotations

import time

from fastapi import HTTPException

from app.config import settings


def effective_rate_limit(key_limit: int | None) -> int:
    """A key's own limit wins; NULL falls back to the server default.
    0 (either place) means unlimited."""
    return key_limit if key_limit is not None else settings.default_key_rate_limit_per_min


def check_rate_limit(api_key_id: str, key_limit: int | None) -> None:
    limit = effective_rate_limit(key_limit)
    if limit <= 0:
        return
    now = time.time()
    window = int(now // 60)
    try:
        from app.jobs import get_redis
        r = get_redis()
        k = f"rl:{api_key_id}:{window}"
        count = r.incr(k)
        if count == 1:
            r.expire(k, 120)
    except Exception as e:  # fail open
        print(f"[ratelimit] Redis unavailable, allowing request: {e}")
        return
    if count > limit:
        retry_after = max(1, int(60 - (now % 60)))
        raise HTTPException(
            429,
            f"Rate limit exceeded for this API key ({limit} requests/minute). "
            f"Retry in {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )


# ---------------------------------------------------------------------------
# Auth endpoint limits (login / signup / resend-verification)
# ---------------------------------------------------------------------------
import hashlib


def client_ip(request) -> str:
    """Real client IP. Behind N trusted proxies each one appends the address
    it saw to X-Forwarded-For, so the entry N from the right is the client;
    anything further left was supplied by the client and can be spoofed."""
    hops = settings.trusted_proxy_hops
    peer = request.client.host if request.client else "unknown"
    if hops <= 0:
        return peer
    xff = request.headers.get("x-forwarded-for", "")
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    if len(parts) < hops:
        return peer
    ip = parts[-hops]
    if ip.count(":") == 1:  # "1.2.3.4:5678" (Azure appends the port)
        ip = ip.split(":")[0]
    return ip.strip("[]") or peer


def email_id(email: str) -> str:
    """Hashed so raw addresses never sit in Redis keys."""
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:20]


def _wkey(bucket: str, identity: str, window: int) -> str:
    return f"rl:{bucket}:{identity}:{int(time.time() // window)}"


def _reject(message: str, window: int) -> None:
    retry_after = max(1, int(window - (time.time() % window)))
    raise HTTPException(
        429, f"{message} Try again in {retry_after}s.",
        headers={"Retry-After": str(retry_after)},
    )


def hit(bucket: str, identity: str, limit: int, window: int, message: str) -> None:
    """Count one event; 429 once more than `limit` happen in the window."""
    if limit <= 0:
        return
    try:
        from app.jobs import get_redis
        r = get_redis()
        k = _wkey(bucket, identity, window)
        count = r.incr(k)
        if count == 1:
            r.expire(k, window * 2)
    except Exception as e:  # fail open, same policy as check_rate_limit
        print(f"[ratelimit] Redis unavailable, allowing request: {e}")
        return
    if count > limit:
        _reject(message, window)


def check_blocked(bucket: str, identity: str, limit: int, window: int, message: str) -> None:
    """429 if `limit` events were already recorded; does not count anything."""
    if limit <= 0:
        return
    try:
        from app.jobs import get_redis
        v = get_redis().get(_wkey(bucket, identity, window))
        count = int(v) if v else 0
    except Exception as e:
        print(f"[ratelimit] Redis unavailable, allowing request: {e}")
        return
    if count >= limit:
        _reject(message, window)


def record_failure(bucket: str, identity: str, window: int) -> None:
    try:
        from app.jobs import get_redis
        r = get_redis()
        k = _wkey(bucket, identity, window)
        if r.incr(k) == 1:
            r.expire(k, window * 2)
    except Exception as e:
        print(f"[ratelimit] Redis unavailable, failure not recorded: {e}")


def clear(bucket: str, identity: str, window: int) -> None:
    try:
        from app.jobs import get_redis
        get_redis().delete(_wkey(bucket, identity, window))
    except Exception:
        pass
