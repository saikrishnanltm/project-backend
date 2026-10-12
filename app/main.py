"""
CodeSage API.

Endpoints:
    POST /auth/signup            - create an account, returns a login token,
                                    and emails a verification link
    POST /auth/login             - log in, returns a login token
    POST /auth/resend-verification - resend the verification email
    GET  /auth/verify-email      - clicked from that email; marks the
                                    account verified, redirects to the frontend
    GET  /auth/github/login      - redirects to GitHub's OAuth consent screen
    GET  /auth/github/link/start - already logged in? redirects to GitHub
                                    to CONNECT it to your existing account
                                    instead of creating a separate one
    GET  /auth/github/callback   - GitHub redirects here after approval;
                                    redirects on to the frontend with a token
    GET  /auth/google/login      - redirects to Google's OAuth consent screen
    GET  /auth/google/link/start - same as github/link/start, for Google
    GET  /auth/google/callback   - Google redirects here after approval;
                                    redirects on to the frontend with a token
    POST /ingest                 - kick off ingestion as a background job
                                    (requires login), returns a job_id
                                    immediately (202 Accepted)
    GET  /ingest/status/{job_id} - poll for that job's progress/result
    POST /query                  - ask a question, get an answer grounded
                                    in your own indexed repos
    POST /query/stream           - same as /query, but streams the answer
                                    back token-by-token over SSE
    GET  /repos                  - list repos YOU have indexed
    DELETE /repos/{repo_name}    - remove one of YOUR indexed repos
    GET  /history                - your own past questions + answers
    POST /conversations          - start a new multi-turn conversation
    GET  /conversations          - list your own conversations
    GET  /conversations/{id}     - a conversation's turns, oldest first
    POST /answers/{history_id}/share  - turn a past answer into a public,
                                    unauthenticated permalink
    GET  /share/{share_id}       - view a shared answer (no login needed)
    DELETE /share/{share_id}     - revoke a permalink you created
    POST /webhooks/github/{indexed_repo_id} - GitHub calls this on every
                                    push to a repo we registered a
                                    webhook for; triggers incremental
                                    re-indexing (see app/github_webhooks.py)
    POST /upload                 - index uploaded documents (md/txt/html/pdf/...)
    POST/GET /collections        - create / list named collections
    GET/DELETE /collections/{name} - inspect / delete one
    GET  /health                 - liveness check for Railway/uptime monitors

    -- Developer API (usage-based billing for /ingest and /query) --
    -- If you're already logged in (password/GitHub/Google), just call
    -- POST /developers/api-keys with your normal Authorization: Bearer
    -- <token> -- your Developer API access is created automatically on
    -- first touch, no separate signup. --
    PATCH /developers/api-keys/{id} - change a key's label / rate limit / monthly cap
    POST /developers/api-keys       - mint a key (auth: your normal
                                    login; auto-creates on first call)
    GET  /developers/api-keys       - list your keys (masked)
    DELETE /developers/api-keys/{id} - revoke one of your keys
    GET  /developers/usage          - credit balance + recent transactions
    GET  /developers/billing/packs  - available prepaid credit packs (public)
    POST /developers/billing/checkout - create a Stripe Checkout session
                                    to buy a credit pack
    POST /webhooks/stripe           - Stripe calls this on
                                    checkout.session.completed; fulfills
                                    a credit purchase (see app/billing.py)

Incremental re-indexing on push:
    After a full ingest of a real github.com URL, app/jobs.py best-effort
    registers a push webhook on that repo (only possible if you connected
    GitHub and have admin rights there, and BACKEND_PUBLIC_URL is set).
    From then on, a push only re-chunks/re-embeds the files that actually
    changed (see app/indexer.py's incremental_index_repo) instead of
    redoing the whole repo -- much cheaper, and keeps the index fresh
    without you ever re-running /ingest by hand.

Multi-turn conversations:
    /query and /query/stream both accept an optional conversation_id.
    Pass one (from POST /conversations) to have earlier turns replayed to
    the LLM as context for follow-up questions ("what about its error
    handling?"); omit it for a stateless one-off question, unchanged from
    before conversations existed at all.

Auth model:
    Every endpoint except /health, /auth/signup, /auth/login,
    /auth/resend-verification, /auth/verify-email, and the OAuth routes
    requires a `Authorization: Bearer <token>` header, where <token> is
    what /auth/signup, /auth/login, or an OAuth callback returned. All
    ingested repos, queries, and history are scoped to the logged-in
    account — one user never sees another user's repos or history, even
    if they pick the same repo name. See app/auth.py for the
    account/token implementation, app/github_oauth.py and
    app/google_oauth.py for the two OAuth flows, and app/email_sender.py
    for verification email delivery.

    Note: email_verified is informational only -- a signup returns a
    working access_token immediately, unverified. Nothing currently
    blocks login or API usage on it; it's there for the frontend to
    optionally nudge an unverified user, not a hard gate. Add a
    dependency check in get_current_user (auth.py) if you want to
    actually enforce it later.

Private repos:
    Logging in with GitHub (rather than email/password) requests the
    `repo` scope and stores the resulting access token, encrypted, against
    your account. /ingest transparently uses it when cloning a repo you
    own or collaborate on, so private repos work exactly like public ones
    once you've connected GitHub — no separate "paste your token" step.

API keys (third-party developers):
    /ingest, /query, and /query/stream ALSO accept `CODEBASE12-Key: cbi_...`
    as an alternative to `Authorization: Bearer <token>` -- see
    get_principal() below. An API key belongs to an api_customer (an org,
    auto-created the first time a logged-in user mints a key), a completely separate principal
    from a logged-in user; its id is used as the `user_id` for repo/chunk
    scoping, so an API customer's ingested repos are just as isolated
    from everyone else's as a regular user's are. Unlike a logged-in
    user, API-key requests are metered: each /query costs a flat number
    of credits, each /ingest costs credits proportional to repo size,
    and a request is rejected with 402 once the balance is spent. See
    app/billing.py and app/api_keys.py.

Why /ingest is a background job:
    Cloning a repo, AST-chunking every file, and running CPU embeddings
    (sentence-transformers, no GPU on Railway) can take minutes for a
    real-sized repo. Doing that inline, inside a single HTTP request, means
    any proxy in front of the app (Railway's included) eventually gives up
    and returns 502 "Application failed to respond" — the request itself
    was still running fine, it just outlived the proxy's patience.

    So /ingest does the minimum possible work synchronously (validate the
    request, enqueue a job) and returns a job_id right away (202 Accepted).
    Poll GET /ingest/status/{job_id} to find out when it's done.

    As of the Redis/RQ migration, "hands the real work off to" means
    enqueuing onto Redis (see app/jobs.py) rather than this process's own
    threadpool — a separate worker process (worker.py) does the actual
    cloning/chunking/embedding, which is what lets ingestion scale
    independently of the web server and survive a web-process
    restart/redeploy without losing an in-flight job.
"""
from __future__ import annotations

import json
import os
import re
import threading

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from starlette.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel

from app import api_keys, auth, billing, collection_store, conversations, email_sender, github_oauth, github_webhooks, google_oauth, jobs, shares
from app import repos as repo_registry
from app import ratelimit
from app.sources import check_source
from app.api_keys import ApiKeyError, MonthlyCapExceeded
from app.auth import AuthError, get_current_user
from app.billing import BillingError, InsufficientCreditsError
from app.config import settings
from app.graph import ask, ask_stream
from app.indexer import RepoIndexer
from app.jobs import JobStatus, get_redis

app = FastAPI(
    title="CodeSage",
    description="RAG over GitHub repositories with AST chunking, hybrid retrieval, and a LangGraph agent.",
    version="0.3.0",
)

# Only the configured frontend origin(s) may call this from a browser:
# CORS_ALLOW_ORIGINS (comma-separated), defaulting to FRONTEND_URL. Auth is by
# header token, never cookies, so credentials stay disabled. Retry-After is
# exposed so the browser can read it on a 429.
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_allow_origins),
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Retry-After"],
)

# Creates the users/query_history tables on first run; safe to call on
# every startup (CREATE TABLE IF NOT EXISTS). Must happen before any
# request touches the users DB.
auth.init_db()


# ---------------------------------------------------------------------------
# Indexer singleton -- used directly (inline, not via a queued job) by the
# /repos endpoints below, since listing/deleting is fast (no embedding
# work). Ingestion itself now runs in a separate worker process instead —
# see app/jobs.py and worker.py — with its own indexer instance over there;
# this one exists only for this web process's own inline reads/deletes.
# RepoIndexer.__init__ loads the sentence-transformers embedding model,
# which is the slow part to construct, so it's built once and reused.
# ---------------------------------------------------------------------------

_indexer: RepoIndexer | None = None
_indexer_lock = threading.Lock()


def _get_indexer() -> RepoIndexer:
    global _indexer
    if _indexer is None:
        with _indexer_lock:
            if _indexer is None:  # re-check inside the lock (another thread may have built it first)
                _indexer = RepoIndexer()
    return _indexer


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class SignupRequest(BaseModel):
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    email: str
    email_verified: bool = False


@app.post("/auth/signup", response_model=TokenResponse, status_code=201)
def signup(req: SignupRequest, request: Request):
    ratelimit.hit(
        "signup_ip", ratelimit.client_ip(request),
        settings.auth_signup_ip_limit, settings.auth_signup_window_seconds,
        "Too many signups from this address.",
    )
    try:
        user = auth.create_user(req.email, req.password)
    except AuthError as e:
        raise HTTPException(400, str(e))

    # Best-effort -- a mail-sending hiccup must never fail signup itself
    # (see email_sender.py's module docstring). If it silently doesn't
    # arrive, POST /auth/resend-verification covers that.
    verify_token = auth.create_email_verification_token(user["id"])
    verify_url = f"{settings.backend_public_url or 'http://localhost:8000'}/auth/verify-email?token={verify_token}"
    email_sender.send_verification_email(user["email"], verify_url)

    token = auth.create_access_token(user["id"])
    return TokenResponse(
        access_token=token, user_id=user["id"], email=user["email"], email_verified=False
    )


@app.post("/auth/login", response_model=TokenResponse)
def login(req: LoginRequest, request: Request):
    w = settings.auth_login_window_seconds
    ratelimit.hit(
        "login_ip", ratelimit.client_ip(request), settings.auth_login_ip_limit, w,
        "Too many login attempts from this address.",
    )
    # Per-account guard counts only FAILED attempts, so a stranger hammering an
    # address can lock guessing out but a correct password still ends the streak.
    eid = ratelimit.email_id(req.email)
    ratelimit.check_blocked(
        "login_fail_email", eid, settings.auth_login_fail_email_limit, w,
        "Too many failed login attempts for this account.",
    )
    try:
        user = auth.authenticate_user(req.email, req.password)
    except AuthError as e:
        ratelimit.record_failure("login_fail_email", eid, w)
        raise HTTPException(401, str(e))
    ratelimit.clear("login_fail_email", eid, w)
    full_user = auth.get_user_by_id(user["id"])  # picks up email_verified
    token = auth.create_access_token(user["id"])
    return TokenResponse(
        access_token=token,
        user_id=user["id"],
        email=user["email"],
        email_verified=bool(full_user and full_user["email_verified"]),
    )


class ResendVerificationRequest(BaseModel):
    email: str


@app.post("/auth/resend-verification")
def resend_verification(req: ResendVerificationRequest, request: Request):
    # Limits apply whether or not the address exists, so the 429 reveals
    # nothing about which emails have accounts.
    w = settings.auth_resend_window_seconds
    ratelimit.hit(
        "resend_ip", ratelimit.client_ip(request), settings.auth_resend_ip_limit, w,
        "Too many verification requests from this address.",
    )
    ratelimit.hit(
        "resend_email", ratelimit.email_id(req.email), settings.auth_resend_email_limit, w,
        "A verification email was already requested for this address recently.",
    )
    # Deliberately returns the exact same response whether the email
    # exists, doesn't exist, or is already verified -- so this endpoint
    # can't be used to enumerate which addresses have a CodeSage account.
    user = auth.get_user_by_email(req.email)
    if user and not user["email_verified"]:
        verify_token = auth.create_email_verification_token(user["id"])
        verify_url = (
            f"{settings.backend_public_url or 'http://localhost:8000'}"
            f"/auth/verify-email?token={verify_token}"
        )
        email_sender.send_verification_email(user["email"], verify_url)
    return {"message": "If that email has an account, a verification link has been sent."}


@app.get("/auth/verify-email")
def verify_email(token: str):
    # Clicked from an email, not called via fetch() -- same full-page
    # redirect pattern as the OAuth callbacks below, not a JSON response.
    # Your frontend needs a small page at {FRONTEND_URL}/verify-email that
    # reads ?verified=true or ?error=... and shows the right message.
    try:
        auth.verify_email_token(token)
    except AuthError as e:
        return RedirectResponse(f"{settings.frontend_url}/verify-email?error={e}")
    return RedirectResponse(f"{settings.frontend_url}/verify-email?verified=true")


# ---------------------------------------------------------------------------
# GitHub OAuth ("Continue with GitHub") -- see app/github_oauth.py for the
# actual exchange/profile-fetch logic. All three routes here are full-page
# browser redirects, not JSON APIs.
#
# Two different entry points feed the same consent screen and the same
# callback, distinguished by "intent" stored alongside the CSRF state:
#   - /auth/github/login       -- no session yet; signs in (or signs up)
#                                  via GitHub. See auth.find_or_create_github_user.
#   - /auth/github/link/start  -- already logged in (password account);
#                                  attaches GitHub to THIS existing account
#                                  instead of creating/finding a separate
#                                  one. See auth.link_github_account.
# The callback reads back whichever intent was stored and branches on it.
# ---------------------------------------------------------------------------

OAUTH_STATE_TTL_SECONDS = 600  # 10 minutes -- plenty of time to approve on GitHub's consent screen


def _store_oauth_state(intent: str, user_id: str | None = None) -> str:
    # The CSRF state has to be checkable by whichever web replica happens
    # to receive the callback request, not necessarily the same one that
    # issued it -- an in-process dict wouldn't work here the moment there's
    # more than one replica. Redis is already a dependency (app/jobs.py),
    # so it's reused here rather than adding a second piece of shared infra.
    state = github_oauth.generate_state()
    payload = json.dumps({"intent": intent, "user_id": user_id})
    get_redis().setex(f"oauth_state:{state}", OAUTH_STATE_TTL_SECONDS, payload)
    return state


@app.get("/auth/github/login")
def github_login():
    if not settings.github_client_id:
        raise HTTPException(500, "GitHub OAuth isn't configured on this server.")
    state = _store_oauth_state(intent="login")
    return RedirectResponse(github_oauth.build_authorize_url(state))


@app.get("/auth/github/link/start")
def github_link_start(token: str):
    # This is a plain browser navigation (the frontend puts this URL
    # behind a "Connect GitHub" link/button), not a fetch() call -- a
    # redirect-driven GET can't carry a custom Authorization header, so
    # the frontend includes the user's current JWT as a query param here
    # specifically, as the one deliberate exception to "auth always goes
    # in the header" everywhere else in this API. _decode_token (not the
    # get_current_user Header-based dependency) is reused directly since
    # it's the exact same validation, just fed a param instead of a header.
    if not settings.github_client_id:
        raise HTTPException(500, "GitHub OAuth isn't configured on this server.")
    try:
        user_id = auth._decode_token(token)
    except AuthError as e:
        raise HTTPException(401, str(e))

    state = _store_oauth_state(intent="link", user_id=user_id)
    return RedirectResponse(github_oauth.build_authorize_url(state))


@app.get("/auth/github/callback")
def github_callback(code: str, state: str):
    # getdel is atomic get+delete -- the state is checked and consumed in
    # one step, so the same state value can never be replayed even if two
    # requests somehow raced on it.
    raw_state = get_redis().getdel(f"oauth_state:{state}")
    if not raw_state:
        raise HTTPException(400, "Invalid or expired login attempt -- please try again.")
    state_data = json.loads(raw_state)
    intent = state_data["intent"]

    try:
        access_token = github_oauth.exchange_code_for_token(code)
        gh_user = github_oauth.fetch_github_user(access_token)

        if intent == "link":
            user_id = state_data["user_id"]
            auth.link_github_account(
                user_id=user_id,
                github_id=gh_user["id"],
                github_login=gh_user["login"],
                access_token=access_token,
            )
        else:
            user = auth.find_or_create_github_user(
                github_id=gh_user["id"],
                github_login=gh_user["login"],
                email=gh_user["email"],
                access_token=access_token,
            )
            user_id = user["id"]
    except (github_oauth.GitHubOAuthError, AuthError) as e:
        raise HTTPException(400, str(e))

    token = auth.create_access_token(user_id)

    # Hand the browser back to the frontend with the issued JWT as a query
    # param -- the frontend needs a small page at this path that reads
    # ?token=..., stores it exactly like a normal /auth/login response
    # would, then redirects into the app. This is the standard pattern for
    # a full-page-redirect OAuth flow talking to a separately-hosted SPA;
    # there's no way to hand back a JSON body directly here, since GitHub
    # is the one redirecting the browser to this URL, not the frontend
    # making a fetch() call. `linked=true` on the link path lets the
    # frontend show "GitHub connected!" instead of treating this as a
    # fresh sign-in.
    suffix = "&linked=true" if intent == "link" else ""
    return RedirectResponse(f"{settings.frontend_url}/auth/github/callback?token={token}{suffix}")


# ---------------------------------------------------------------------------
# Google OAuth ("Continue with Google") -- mirrors the GitHub OAuth section
# above exactly (same login/link split, same shared-state-payload pattern),
# just against app/google_oauth.py and its own callback URL. Google and
# GitHub each need their OWN dedicated callback route, not a single shared
# one across both providers, because each OAuth app registers one exact
# redirect_uri with its provider and that URL can't be reused for a
# different provider's exchange.
# ---------------------------------------------------------------------------

@app.get("/auth/google/login")
def google_login():
    if not settings.google_client_id:
        raise HTTPException(500, "Google OAuth isn't configured on this server.")
    state = _store_oauth_state(intent="login")
    return RedirectResponse(google_oauth.build_authorize_url(state))


@app.get("/auth/google/link/start")
def google_link_start(token: str):
    # Same query-param-token exception as /auth/github/link/start above --
    # see that route's comment for why.
    if not settings.google_client_id:
        raise HTTPException(500, "Google OAuth isn't configured on this server.")
    try:
        user_id = auth._decode_token(token)
    except AuthError as e:
        raise HTTPException(401, str(e))

    state = _store_oauth_state(intent="link", user_id=user_id)
    return RedirectResponse(google_oauth.build_authorize_url(state))


@app.get("/auth/google/callback")
def google_callback(code: str, state: str):
    raw_state = get_redis().getdel(f"oauth_state:{state}")
    if not raw_state:
        raise HTTPException(400, "Invalid or expired login attempt -- please try again.")
    state_data = json.loads(raw_state)
    intent = state_data["intent"]

    try:
        access_token = google_oauth.exchange_code_for_token(code)
        gu = google_oauth.fetch_google_user(access_token)

        if intent == "link":
            user_id = state_data["user_id"]
            auth.link_google_account(user_id=user_id, google_id=gu["id"])
        else:
            user = auth.find_or_create_google_user(
                google_id=gu["id"], email=gu["email"], email_verified=gu["email_verified"]
            )
            user_id = user["id"]
    except (google_oauth.GoogleOAuthError, AuthError) as e:
        raise HTTPException(400, str(e))

    token = auth.create_access_token(user_id)
    suffix = "&linked=true" if intent == "link" else ""
    return RedirectResponse(f"{settings.frontend_url}/auth/google/callback?token={token}{suffix}")


# ---------------------------------------------------------------------------
# API-key auth (third-party developers) -- see app/api_keys.py and the
# module docstring's "API keys" section above. get_principal() is what
# /ingest, /query, and /query/stream depend on instead of plain
# get_current_user, so those three endpoints work for either a logged-in
# user OR an API customer.
# ---------------------------------------------------------------------------

def get_current_api_customer(x_api_key: str | None = Header(default=None, alias="CODEBASE12-Key")) -> dict:
    """Strict version -- used ONLY by get_principal below and by anyone
    calling /developers/* with a raw API key instead of being logged in
    (an integration calling with a raw API key instead of a login). The web app's own Developer
    API page never uses this -- see get_or_create_current_api_customer,
    just below, for that."""
    if not x_api_key:
        raise HTTPException(401, "Missing CODEBASE12-Key header.")
    customer = api_keys.authenticate_api_key(x_api_key)
    if customer is None:
        raise HTTPException(401, "Invalid or revoked API key.")
    return customer


def get_or_create_current_api_customer(user: dict = Depends(get_current_user)) -> dict:
    """Self-service Developer API auth: authenticates with your NORMAL
    CodeSage login (JWT) -- whichever of password/GitHub/Google you
    signed up with -- not a separate API key or developer signup. The
    api_customer record backing your Developer API access (API keys,
    credit balance, transaction history) is created transparently the
    first time you touch any of these endpoints.

    Every /developers/* SELF-MANAGEMENT endpoint below uses this. It is
    deliberately NOT what get_principal (used by /ingest and /query)
    accepts -- an actual API call from a third-party integration still
    authenticates with the CODEBASE12-Key this issues, same as always; this
    dependency is only for managing your own account through the web app."""
    customer = api_keys.get_or_create_api_customer_for_user(user["id"], user["email"])
    # One-time free starter credits, once the email is verified (OAuth
    # accounts are verified at signup). Idempotent, so safe on every call;
    # best-effort so a ledger hiccup never blocks the Developer page.
    if user.get("email_verified"):
        try:
            if billing.grant_starter_credits(customer["id"]) is not None:
                customer = api_keys.get_api_customer(customer["id"]) or customer
        except Exception as e:
            print(f"[developers] starter credit grant failed for {customer['id']}: {e}")
    return customer


def get_principal(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="CODEBASE12-Key"),
    x_api_key_legacy: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Accepts EITHER a logged-in user's JWT (Authorization: Bearer ...)
    OR a third-party API key (CODEBASE12-Key: cbi_...). Returns
    {"id", "kind": "user" | "api_customer"} -- `id` is used as the
    `user_id` for repo/chunk scoping either way (an api_customer's id
    and a user's id are just different opaque strings from that layer's
    point of view), while `kind` is what /ingest and /query use to
    decide whether this request needs to be metered (see
    app/billing.py). A CODEBASE12-Key header takes priority if a request
    somehow presents both, since presenting an API key at all signals a
    machine/integration call rather than a browser session carrying a
    stale Authorization header."""

    x_api_key = x_api_key or x_api_key_legacy
    if x_api_key:
        customer = api_keys.authenticate_api_key(x_api_key)
        if customer is None:
            raise HTTPException(401, "Invalid or revoked API key.")
        # Per-key rate limit (429 + Retry-After). Counted for every call
        # a key makes, metered or not.
        ratelimit.check_rate_limit(customer["api_key_id"], customer["rate_limit_per_min"])
        return {
            "id": customer["id"], "kind": "api_customer",
            "api_key_id": customer["api_key_id"],
            "monthly_credit_cap": customer["monthly_credit_cap"],
        }

    if authorization:
        user = get_current_user(authorization=authorization)
        return {"id": user["id"], "kind": "user"}

    raise HTTPException(
        401,
        "Provide either an 'Authorization: Bearer <token>' header (logged-in user) "
        "or a 'CODEBASE12-Key: cbi_...' header (API customer).",
    )


def _meter(principal: dict, cost: int, reason: str) -> None:
    """Charges one metered call to an API-key principal (no-op for a
    logged-in web user). Order matters: reserve against the key's monthly
    cap first (atomic), then take the credits; if the account can't pay,
    the cap reservation is rolled back so a 402 doesn't eat into the cap."""
    if principal["kind"] != "api_customer":
        return
    key_id = principal["api_key_id"]
    try:
        api_keys.reserve_usage(key_id, principal["monthly_credit_cap"], cost)
    except MonthlyCapExceeded as e:
        raise HTTPException(403, str(e))
    try:
        billing.deduct_credits(principal["id"], cost, reason=reason)
    except InsufficientCreditsError as e:
        api_keys.release_usage(key_id, cost)
        raise HTTPException(402, str(e))


def _precheck_billed(principal: dict) -> None:
    """Cheap upfront check for work whose cost is only known afterwards
    (ingest, upload): no credits at all, or this key's month is already spent."""
    if principal["kind"] != "api_customer":
        return
    if billing.get_balance(principal["id"]) <= 0:
        raise HTTPException(402, "Insufficient credits. Buy more at POST /developers/billing/checkout.")
    cap = api_keys.effective_monthly_cap(principal["monthly_credit_cap"])
    if cap > 0 and api_keys.month_credits_used(principal["api_key_id"]) >= cap:
        raise HTTPException(403, f"Monthly credit cap of {cap} reached for this key.")


def _validated_collection(name: str | None) -> str | None:
    if name is None or not name.strip():
        return None
    try:
        return collection_store.validate_name(name)
    except collection_store.CollectionError as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

class IngestRequest(BaseModel):
    # Paste a GitHub link here (https://github.com/user/repo, a .git URL,
    # or an ssh remote) — or, if you're running this locally, a path to a
    # folder already on disk. Auto-detected either way.
    source: str
    repo_name: str | None = None
    # Optional named collection to put this repo in (created on first use).
    # Omit to leave the repo where it is (or ungrouped, if it's new).
    collection: str | None = None


class IngestAcceptedResponse(BaseModel):
    job_id: str
    status: JobStatus


class IngestStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    source: str
    repo_name: str | None = None
    result: dict | None = None   # {"repo", "files_seen", "chunks_indexed"} once status == DONE
    error: str | None = None     # populated only if status == ERROR
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    progress: dict | None = None  # {"stage","percent","detail"} while pending/running


@app.get("/health")
def health():
    # Kept intentionally cheap (no model/index loading) so Railway's health
    # check passes immediately during a cold start, even while the first
    # real request is still warming up the embedding model.
    return {"status": "ok"}


@app.post("/ingest", response_model=IngestAcceptedResponse, status_code=202)
def ingest(
    req: IngestRequest,
    principal: dict = Depends(get_principal),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    source = req.source.strip()
    if not source:
        raise HTTPException(400, "source must not be empty")
    try:
        check_source(source)  # no local paths, allowlisted git hosts only
    except ValueError as e:
        raise HTTPException(400, str(e))
    if idempotency_key and not re.fullmatch(r"[\w\-]{8,128}", idempotency_key):
        raise HTTPException(400, "Idempotency-Key must be 8-128 chars: letters, digits, _ or -")

    billed = principal["kind"] == "api_customer"
    _precheck_billed(principal)

    # A retry carrying the same Idempotency-Key gets the original job back
    # instead of queueing (and billing) a second one.
    rkey = f"idem:ingest:{principal['id']}:{idempotency_key}" if idempotency_key else None
    if rkey:
        try:
            prev = jobs.get_redis().get(rkey)
        except Exception:
            prev = None  # Redis down: fail open, like the rate limiter
        if prev:
            prev_id = prev.decode() if isinstance(prev, bytes) else prev
            job = jobs.get_job_status(prev_id, principal["id"])
            if job:
                return IngestAcceptedResponse(job_id=prev_id, status=job["status"])

    collection = _validated_collection(req.collection)
    if collection:
        collection_store.ensure_collection(principal["id"], collection)

    # Billed callers: hold the worst-case cost NOW (atomic, can't go negative),
    # so N parallel ingests can't all pass on a 1-credit balance. The worker
    # refunds whatever the finished job didn't use (jobs._settle_hold).
    reserved = 0
    if billed:
        reserved = billing.ingest_credit_cost(settings.ingest_max_chunks_billed)
        _meter(principal, reserved, reason="ingest_hold")
    try:
        job_id = jobs.enqueue_ingest(
            source, req.repo_name, principal["id"], billed=billed,
            collection=collection, api_key_id=principal.get("api_key_id"),
            reserved_credits=reserved,
        )
    except Exception:
        if reserved:  # couldn't queue (Redis down?) -- give the hold back
            billing.grant_credits(principal["id"], reserved, reason="ingest_hold_released")
            api_keys.release_usage(principal["api_key_id"], reserved)
        raise
    if rkey:
        try:
            jobs.get_redis().set(rkey, job_id, ex=86400)
        except Exception as e:
            print(f"[ingest] could not store idempotency key: {e}")
    return IngestAcceptedResponse(job_id=job_id, status=JobStatus.PENDING)


@app.get("/ingest/status/{job_id}", response_model=IngestStatusResponse)
def ingest_status(job_id: str, principal: dict = Depends(get_principal)):
    job = jobs.get_job_status(job_id, principal["id"])
    # Same 404 whether the job_id doesn't exist at all, expired, or exists
    # but belongs to someone else — confirming "it exists, just not yours"
    # would leak that a given job_id is valid to whoever's guessing.
    if job is None:
        raise HTTPException(404, f"No job found with id '{job_id}'")

    return IngestStatusResponse(**job)


# ---------------------------------------------------------------------------
# Repo management
# ---------------------------------------------------------------------------

class RepoSummary(BaseModel):
    repo: str
    chunks_indexed: int
    collection: str | None = None


class ListReposResponse(BaseModel):
    repos: list[RepoSummary]


@app.get("/repos", response_model=ListReposResponse)
def list_repos(principal: dict = Depends(get_principal)):
    """List every repo YOU (or your org, if calling with an API key)
    have indexed, with a chunk count for each — use this to get the
    exact spelling to pass to DELETE /repos/{repo_name} or /query's
    repo_filter, rather than guessing. Never shows another account's
    repos, even if they happen to share a name with yours. Free to call
    either way -- listing/deleting isn't metered, only /ingest and
    /query are (see app/billing.py)."""
    return ListReposResponse(repos=_get_indexer().list_repos(user_id=principal["id"]))


class DeleteRepoResponse(BaseModel):
    repo: str
    chunks_deleted: int


@app.delete("/repos/{repo_name}", response_model=DeleteRepoResponse)
def delete_repo(repo_name: str, principal: dict = Depends(get_principal)):
    """Remove one of YOUR previously-ingested repos from both the vector
    store and the BM25 index. Fast (no embedding work involved), so this
    runs inline rather than as a background job — unlike /ingest. Only ever
    matches repos belonging to your own account."""
    deleted_count = _get_indexer().delete_repo(repo_name, user_id=principal["id"])
    if deleted_count == 0:
        raise HTTPException(404, f"No indexed chunks found for repo '{repo_name}'")
    return DeleteRepoResponse(repo=repo_name, chunks_deleted=deleted_count)


# ---------------------------------------------------------------------------
# Collections -- named groups of indexed sources (repos and uploads).
# Put things in one with `collection` on /ingest or /upload, then pass the
# same `collection` to /query to search only inside it.
# ---------------------------------------------------------------------------

class CreateCollectionRequest(BaseModel):
    name: str
    description: str | None = None


class CollectionSummary(BaseModel):
    name: str
    description: str | None = None
    created_at: float
    sources: int
    chunks: int


class CollectionDetail(CollectionSummary):
    source_list: list[dict]


@app.post("/collections", response_model=CollectionSummary, status_code=201)
def create_collection(req: CreateCollectionRequest, principal: dict = Depends(get_principal)):
    name = _validated_collection(req.name)
    if not name:
        raise HTTPException(400, "name must not be empty")
    try:
        row = collection_store.create_collection(principal["id"], name, req.description)
    except collection_store.CollectionError as e:
        raise HTTPException(409, str(e))
    return CollectionSummary(**row)


@app.get("/collections", response_model=list[CollectionSummary])
def list_collections(principal: dict = Depends(get_principal)):
    return [CollectionSummary(**r) for r in collection_store.list_collections(principal["id"])]


@app.get("/collections/{name}", response_model=CollectionDetail)
def get_collection(name: str, principal: dict = Depends(get_principal)):
    row = collection_store.get_collection(principal["id"], name)
    if row is None:
        raise HTTPException(404, f"No collection named '{name}'.")
    return CollectionDetail(**row, source_list=collection_store.list_sources(principal["id"], name))


class DeleteCollectionResponse(BaseModel):
    collection: str
    chunks_deleted: int


@app.delete("/collections/{name}", response_model=DeleteCollectionResponse)
def delete_collection(name: str, delete_contents: bool = False, principal: dict = Depends(get_principal)):
    """Deletes the collection. If it still holds indexed sources this
    refuses (409) unless you pass ?delete_contents=true, which also
    deletes every chunk in it -- there's no \"just ungroup them\" mode on
    purpose, so a delete can't silently leave sources behind."""
    user_id = principal["id"]
    row = collection_store.get_collection(user_id, name)
    if row is None:
        raise HTTPException(404, f"No collection named '{name}'.")
    deleted = 0
    if row["chunks"] > 0:
        if not delete_contents:
            raise HTTPException(
                409,
                f"Collection '{name}' still holds {row['sources']} source(s) / {row['chunks']} chunks. "
                "Pass ?delete_contents=true to delete them too.",
            )
        deleted = _get_indexer().delete_collection_contents(name, user_id)
    collection_store.delete_collection_row(user_id, name)
    return DeleteCollectionResponse(collection=name, chunks_deleted=deleted)


# ---------------------------------------------------------------------------
# Document upload -- index your own files (Markdown, text, HTML, PDF, CSV,
# JSON) with the prose-aware chunker in app/doc_chunker.py. Uploads become
# a "source" just like a repo: they show up in /repos, can be filtered with
# /query's repo_filter, and are removed with DELETE /repos/{name}.
# ---------------------------------------------------------------------------

class UploadResponse(BaseModel):
    source: str
    files_indexed: int
    chunks_indexed: int
    collection: str | None = None
    credits_charged: int | None = None
    credit_balance: int | None = None


_SOURCE_NAME_RE = re.compile(r"^[\w.\- ]{1,100}$")


@app.post("/upload", response_model=UploadResponse, status_code=201)
async def upload_documents(
    files: list[UploadFile] = File(...),
    source_name: str | None = Form(default=None),
    collection: str | None = Form(default=None),
    principal: dict = Depends(get_principal),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """multipart/form-data. `files`: one or more documents (up to
    MAX_UPLOAD_FILES files / MAX_UPLOAD_BYTES_TOTAL bytes combined).
    `source_name`: what to call this source (defaults to the filename for a
    single file, \"uploads\" otherwise); uploading again under the same name
    REPLACES that source. `collection`: optional named collection.

    Indexed inline (not queued), so keep uploads modest; for a large
    corpus, upload in several requests. Metered like /ingest: credits
    scale with chunks created and are charged after indexing."""
    if not files:
        raise HTTPException(400, "Attach at least one file in the 'files' field.")

    # Idempotency-Key: a retry of an upload that already succeeded returns the
    # stored response instead of indexing (and billing) it a second time.
    # Checked before the credit pre-check so a retry still works if the first
    # attempt used up the last credits. Fails open if Redis is unavailable.
    rkey = None
    if idempotency_key:
        if not re.fullmatch(r"[\w\-]{8,128}", idempotency_key):
            raise HTTPException(400, "Idempotency-Key must be 8-128 chars: letters, digits, _ or -")
        rkey = f"idem:upload:{principal['id']}:{idempotency_key}"
        try:
            r = jobs.get_redis()
            if r.set(rkey, "__pending__", nx=True, ex=600):
                pass  # we own this key
            else:
                prev = r.get(rkey)
                prev = prev.decode() if isinstance(prev, bytes) else prev
                if prev and prev != "__pending__":
                    return UploadResponse(**json.loads(prev))
                raise HTTPException(409, "An upload with this Idempotency-Key is still in progress.")
        except HTTPException:
            raise
        except Exception as e:
            print(f"[upload] idempotency store unavailable, continuing without it: {e}")
            rkey = None

    try:
        return await _handle_upload(files, source_name, collection, principal, rkey)
    except BaseException:
        if rkey:  # let the caller retry after a failure
            try:
                jobs.get_redis().delete(rkey)
            except Exception:
                pass
        raise


async def _handle_upload(files, source_name, collection, principal, rkey):
    if len(files) > settings.max_upload_files:
        raise HTTPException(413, f"Too many files: max {settings.max_upload_files} per upload.")

    _precheck_billed(principal)
    collection = _validated_collection(collection)

    loaded: list[tuple[str, bytes]] = []
    total = 0
    for f in files:
        data = await f.read(settings.max_upload_bytes_total + 1)
        total += len(data)
        if total > settings.max_upload_bytes_total:
            raise HTTPException(
                413, f"Upload too large: max {settings.max_upload_bytes_total // (1024 * 1024)} MB combined."
            )
        name = os.path.basename((f.filename or "").replace("\\", "/")) or "document"
        loaded.append((name, data))

    if source_name is None or not source_name.strip():
        source_name = loaded[0][0] if len(loaded) == 1 else "uploads"
    source_name = source_name.strip()
    if not _SOURCE_NAME_RE.match(source_name):
        raise HTTPException(400, "source_name may only contain letters, digits, spaces, '_', '.', '-' (max 100).")

    from app.doc_chunker import UnsupportedDocument

    user_id = principal["id"]
    if collection:
        collection_store.ensure_collection(user_id, collection)
    try:
        result = await run_in_threadpool(
            _get_indexer().index_documents, loaded, source_name, user_id, collection
        )
    except UnsupportedDocument as e:
        raise HTTPException(400, str(e))

    out = UploadResponse(
        source=result["repo"], files_indexed=result["files_seen"],
        chunks_indexed=result["chunks_indexed"], collection=result.get("collection"),
    )
    if principal["kind"] == "api_customer":
        cost = billing.ingest_credit_cost(out.chunks_indexed)
        out.credit_balance = billing.deduct_credits(
            user_id, cost, reason=f"upload:{source_name}", allow_negative=True
        )
        out.credits_charged = cost
        api_keys.record_usage(principal["api_key_id"], cost)
    if rkey:
        try:
            jobs.get_redis().set(rkey, out.model_dump_json(), ex=86400)
        except Exception as e:
            print(f"[upload] could not store idempotency result: {e}")
    return out


# ---------------------------------------------------------------------------
# Querying
# ---------------------------------------------------------------------------

class QueryRequest(BaseModel):
    question: str
    repo_filter: str | None = None
    # Pass the id of a conversation from POST /conversations to have
    # earlier turns replayed to the LLM as context (see app/graph.py's
    # generate_node) and have this turn appended to that same thread.
    # Omit for a stateless one-off question -- unchanged from before
    # conversations existed.
    conversation_id: str | None = None
    # Restrict retrieval to one named collection (see /collections).
    collection: str | None = None


class QueryResponse(BaseModel):
    answer: str
    citations: list[str]
    # The verify node's own read on this answer: "high", "medium", or
    # "low" -- see app/graph.py's verify_node/_score_confidence. Surface
    # this in your UI (e.g. a small badge) rather than only showing the
    # bare answer text, since a "low" answer is still returned as-is
    # rather than hidden.
    confidence: str
    history_id: str  # pass to POST /answers/{history_id}/share to get a permalink


def _load_conversation_context(user_id: str, conversation_id: str | None) -> tuple[list[dict], dict | None]:
    """Returns (history_turns_for_llm, conversation_row) for an optional
    conversation_id -- (empty list, None) if none was given, which is
    exactly the "no conversation" shape ask()/ask_stream() already
    default to. Raises 404 if conversation_id was given but doesn't
    belong to this user, same "don't confirm someone else's id exists"
    posture as the rest of this file's ownership checks."""
    if not conversation_id:
        return [], None
    convo = conversations.get_conversation(user_id, conversation_id)
    if convo is None:
        raise HTTPException(404, f"No conversation found with id '{conversation_id}'")
    history = auth.conversation_history_for_llm(user_id, conversation_id)
    return history, convo


@app.post("/query", response_model=QueryResponse)
def query(req: QueryRequest, principal: dict = Depends(get_principal)):
    if not req.question.strip():
        raise HTTPException(400, "question must not be empty")

    user_id = principal["id"]
    collection = _validated_collection(req.collection)
    if collection and collection_store.get_collection(user_id, collection) is None:
        raise HTTPException(404, f"No collection named '{collection}'.")
    # Flat, known-upfront cost -- charged BEFORE any retrieval/LLM work
    # (unlike /ingest, whose cost isn't known until after the fact).
    _meter(principal, billing.query_credit_cost(), reason="query")

    history, _convo = _load_conversation_context(user_id, req.conversation_id)
    repo_filter = req.repo_filter

    state = ask(
        req.question, repo_filter=repo_filter, user_id=user_id, history=history,
        collection=collection,
    )
    answer = state.get("answer", "")
    citations = state.get("citations", [])
    confidence = state.get("confidence", "low")

    # Logged after generation so a failed/empty answer still shows up in
    # history rather than silently vanishing — useful for the user to see
    # what they tried, even if it didn't find anything that time.
    history_id = auth.log_query(
        user_id, req.question, answer, repo_filter, citations,
        conversation_id=req.conversation_id, confidence=confidence,
    )
    if req.conversation_id:
        conversations.touch_conversation(req.conversation_id, title_if_unset=req.question)

    return QueryResponse(answer=answer, citations=citations, confidence=confidence, history_id=history_id)


@app.post("/query/stream")
def query_stream(req: QueryRequest, principal: dict = Depends(get_principal)):
    """Same inputs/semantics as POST /query, but the answer is streamed
    back as it's generated instead of waiting for the whole thing --
    lets a UI render tokens as they arrive rather than a long blank pause
    on a big answer. Server-Sent Events (text/event-stream): each event
    is `data: <json>\\n\\n`, one of
        {"type": "token", "text": "..."}          -- zero or more
        {"type": "done", "answer", "citations", "confidence", "history_id"}  -- exactly one, last
        {"type": "error", "message": "..."}        -- only on failure, in place of "done"
    """
    if not req.question.strip():
        raise HTTPException(400, "question must not be empty")

    user_id = principal["id"]
    collection = _validated_collection(req.collection)
    if collection and collection_store.get_collection(user_id, collection) is None:
        raise HTTPException(404, f"No collection named '{collection}'.")
    _meter(principal, billing.query_credit_cost(), reason="query_stream")

    history, _convo = _load_conversation_context(user_id, req.conversation_id)
    repo_filter = req.repo_filter

    def event_source():
        answer = ""
        citations: list[str] = []
        confidence = "low"
        try:
            for event in ask_stream(req.question, repo_filter=repo_filter, user_id=user_id, history=history, collection=collection):
                if event["type"] == "token":
                    yield f"data: {json.dumps(event)}\n\n"
                else:  # "done" from the graph -- hold it back, history_id isn't known yet
                    answer, citations, confidence = event["answer"], event["citations"], event["confidence"]
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
            return

        history_id = auth.log_query(
            user_id, req.question, answer, repo_filter, citations,
            conversation_id=req.conversation_id, confidence=confidence,
        )
        if req.conversation_id:
            conversations.touch_conversation(req.conversation_id, title_if_unset=req.question)

        done = {
            "type": "done", "answer": answer, "citations": citations,
            "confidence": confidence, "history_id": history_id,
        }
        yield f"data: {json.dumps(done)}\n\n"

    return StreamingResponse(event_source(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Query history — each user's own record of what they've asked and been
# answered, retrievable across sessions/devices.
# ---------------------------------------------------------------------------

class HistoryEntry(BaseModel):
    id: str
    question: str
    answer: str
    repo_filter: str | None
    citations: list[str]
    created_at: float
    conversation_id: str | None = None
    confidence: str | None = None


class HistoryResponse(BaseModel):
    history: list[HistoryEntry]


def _link_entry(user_id: str, entry: dict) -> dict:
    """Adds GitHub links to citations of answers saved before links existed
    (read-time only; the stored rows are untouched)."""
    try:
        entry = dict(entry)
        entry["citations"] = repo_registry.backfill_citation_links(
            user_id, entry.get("repo_filter"), entry.get("citations") or []
        )
    except Exception:
        pass
    return entry


@app.get("/history", response_model=HistoryResponse)
def get_history(limit: int = 50, current_user: dict = Depends(get_current_user)):
    """Your own past questions and answers, most recent first. Nothing here
    is ever visible to another account."""
    entries = auth.get_query_history(current_user["id"], limit=limit)
    return HistoryResponse(history=[HistoryEntry(**_link_entry(current_user["id"], e)) for e in entries])


# ---------------------------------------------------------------------------
# Conversations -- multi-turn threads. See app/conversations.py; the turns
# themselves are query_history rows tagged with a conversation_id (see
# POST /query above and app/auth.py's conversation_history_for_llm).
# ---------------------------------------------------------------------------

class CreateConversationRequest(BaseModel):
    repo_filter: str | None = None
    title: str | None = None


class ConversationSummary(BaseModel):
    id: str
    title: str | None
    repo_filter: str | None
    created_at: float
    updated_at: float


@app.post("/conversations", response_model=ConversationSummary, status_code=201)
def create_conversation(req: CreateConversationRequest, current_user: dict = Depends(get_current_user)):
    convo = conversations.create_conversation(
        current_user["id"], repo_filter=req.repo_filter, title=req.title
    )
    return ConversationSummary(**convo)


@app.get("/conversations", response_model=list[ConversationSummary])
def list_conversations(limit: int = 50, current_user: dict = Depends(get_current_user)):
    return [ConversationSummary(**c) for c in conversations.list_conversations(current_user["id"], limit=limit)]


@app.get("/conversations/{conversation_id}", response_model=HistoryResponse)
def get_conversation_messages(conversation_id: str, current_user: dict = Depends(get_current_user)):
    """A conversation's turns, oldest first -- the reverse order of
    GET /history's most-recent-first, since a conversation reads top to
    bottom like a chat transcript."""
    if conversations.get_conversation(current_user["id"], conversation_id) is None:
        raise HTTPException(404, f"No conversation found with id '{conversation_id}'")
    entries = auth.get_query_history(current_user["id"], conversation_id=conversation_id, limit=200)
    return HistoryResponse(history=[HistoryEntry(**_link_entry(current_user["id"], e)) for e in entries])


# ---------------------------------------------------------------------------
# Shareable answer permalinks. See app/shares.py -- GET /share/{id} is the
# only unauthenticated endpoint in this file besides /health and the two
# /auth/github/* redirects, and it's careful to never leak the owner's
# identity or other repos (see that module's docstring).
# ---------------------------------------------------------------------------

class ShareResponse(BaseModel):
    id: str
    question: str
    answer: str
    citations: list[str]
    confidence: str | None
    repo_filter: str | None
    created_at: float


@app.post("/answers/{history_id}/share", response_model=ShareResponse, status_code=201)
def share_answer(history_id: str, current_user: dict = Depends(get_current_user)):
    """Turns one of YOUR past answers (by the history_id POST /query or
    POST /query/stream returned) into a public permalink anyone with the
    link can open, unauthenticated, via GET /share/{id}. Copies the
    content at share time rather than linking live -- see app/shares.py's
    docstring for why."""
    entry = auth.get_history_entry(current_user["id"], history_id)
    if entry is None:
        raise HTTPException(404, f"No history entry found with id '{history_id}'")

    shared = shares.create_share(
        owner_user_id=current_user["id"],
        question=entry["question"],
        answer=entry["answer"],
        citations=repo_registry.backfill_citation_links(
            current_user["id"], entry["repo_filter"], entry["citations"]
        ),
        confidence=entry["confidence"],
        repo_filter=entry["repo_filter"],
    )
    return ShareResponse(**shared)


@app.get("/share/{share_id}", response_model=ShareResponse)
def view_shared_answer(share_id: str):
    shared = shares.get_share(share_id)
    if shared is None:
        raise HTTPException(404, "This shared answer doesn't exist or was removed.")
    return ShareResponse(**shared)


class SharedLinkItem(BaseModel):
    id: str
    question: str
    created_at: float


class SharedLinksResponse(BaseModel):
    shares: list[SharedLinkItem]


@app.get("/shares", response_model=SharedLinksResponse)
def list_my_shares(current_user: dict = Depends(get_current_user)):
    """Your own active public links, so the UI can offer a Revoke button."""
    return SharedLinksResponse(shares=[SharedLinkItem(**r) for r in shares.list_shares(current_user["id"])])


@app.delete("/share/{share_id}", status_code=204)
def revoke_shared_answer(share_id: str, current_user: dict = Depends(get_current_user)):
    if not shares.delete_share(current_user["id"], share_id):
        raise HTTPException(404, f"No shared answer found with id '{share_id}'")


# ---------------------------------------------------------------------------
# Developer platform -- API keys + usage-based billing for third-party
# developers calling /ingest and /query directly. See app/api_keys.py
# (key issuance/auth) and app/billing.py (credit ledger + Stripe). None
# of this touches the `users`/JWT auth model above at all -- an
# api_customer is its own principal (see get_principal() and the module
# docstring's "API keys" section).
# ---------------------------------------------------------------------------

class CreateApiKeyRequest(BaseModel):
    label: str | None = None
    # Both optional. Omit to use the server defaults
    # (DEFAULT_KEY_RATE_LIMIT_PER_MIN / DEFAULT_KEY_MONTHLY_CREDIT_CAP).
    # 0 means unlimited.
    rate_limit_per_min: int | None = None
    monthly_credit_cap: int | None = None


class ApiKeyCreatedResponse(BaseModel):
    api_key: str  # shown exactly once


def _check_limits(rate: int | None, cap: int | None) -> None:
    if rate is not None and not (0 <= rate <= 100_000):
        raise HTTPException(400, "rate_limit_per_min must be between 0 (unlimited) and 100000.")
    if cap is not None and not (0 <= cap <= 1_000_000_000):
        raise HTTPException(400, "monthly_credit_cap must be 0 (unlimited) or a positive number of credits.")


@app.post("/developers/api-keys", response_model=ApiKeyCreatedResponse, status_code=201)
def create_developer_api_key(
    req: CreateApiKeyRequest, customer: dict = Depends(get_or_create_current_api_customer)
):
    """Mints a key for your account -- authenticated with your normal
    CodeSage login. Your first call here (e.g. clicking "New key" with
    zero keys yet) transparently creates your Developer API access; every
    call after that just mints an additional key (e.g. one per
    environment/service) against the same account. Optionally sets the
    key's own rate limit and monthly credit cap."""
    _check_limits(req.rate_limit_per_min, req.monthly_credit_cap)
    raw_key = api_keys.create_api_key(
        customer["id"], label=req.label,
        rate_limit_per_min=req.rate_limit_per_min, monthly_credit_cap=req.monthly_credit_cap,
    )
    return ApiKeyCreatedResponse(api_key=raw_key)


class ApiKeySummary(BaseModel):
    id: str
    key_prefix: str
    label: str | None
    created_at: float
    last_used_at: float | None
    revoked_at: float | None
    # Limits as set on the key (None = server default) and the limits
    # actually in force (0 = unlimited).
    rate_limit_per_min: int | None = None
    monthly_credit_cap: int | None = None
    effective_rate_limit_per_min: int = 0
    effective_monthly_credit_cap: int = 0
    # Metered calls (/query, /ingest, /upload) and credits, this UTC month and all time.
    requests_this_month: int = 0
    credits_this_month: int = 0
    requests_total: int = 0
    credits_total: int = 0


@app.get("/developers/api-keys", response_model=list[ApiKeySummary])
def list_developer_api_keys(customer: dict = Depends(get_or_create_current_api_customer)):
    """Your keys, masked to their prefix only -- the full key is never
    retrievable after the moment it was created."""
    out = []
    for k in api_keys.list_api_keys(customer["id"]):
        k["effective_rate_limit_per_min"] = ratelimit.effective_rate_limit(k["rate_limit_per_min"])
        k["effective_monthly_credit_cap"] = api_keys.effective_monthly_cap(k["monthly_credit_cap"])
        out.append(ApiKeySummary(**k))
    return out


class UpdateApiKeyRequest(BaseModel):
    label: str | None = None
    rate_limit_per_min: int | None = None
    monthly_credit_cap: int | None = None


@app.patch("/developers/api-keys/{key_id}", status_code=204)
def update_developer_api_key(
    key_id: str, req: UpdateApiKeyRequest,
    customer: dict = Depends(get_or_create_current_api_customer),
):
    """Change a key's label or limits. Only fields you send are changed;
    send an explicit null to put a limit back to the server default."""
    fields = {k: getattr(req, k) for k in req.model_fields_set}
    if not fields:
        raise HTTPException(400, "Nothing to update.")
    _check_limits(fields.get("rate_limit_per_min"), fields.get("monthly_credit_cap"))
    if not api_keys.update_api_key_limits(customer["id"], key_id, fields):
        raise HTTPException(404, f"No active API key found with id '{key_id}'")


@app.delete("/developers/api-keys/{key_id}", status_code=204)
def revoke_developer_api_key(
    key_id: str, customer: dict = Depends(get_or_create_current_api_customer)
):
    revoked = api_keys.revoke_api_key(customer["id"], key_id)
    if not revoked:
        raise HTTPException(404, f"No API key found with id '{key_id}'")


class UsageResponse(BaseModel):
    credit_balance: int
    recent_transactions: list[dict]


@app.get("/developers/usage", response_model=UsageResponse)
def get_developer_usage(customer: dict = Depends(get_or_create_current_api_customer)):
    """Current credit balance plus a recent audit trail of every grant
    (purchase) and debit (a metered /ingest or /query call) -- see
    app/billing.py's credit_transactions table."""
    return UsageResponse(
        credit_balance=billing.get_balance(customer["id"]),
        recent_transactions=billing.get_recent_transactions(customer["id"]),
    )


class CreditPackSummary(BaseModel):
    id: str
    name: str
    credits: int
    price_cents: int


@app.get("/developers/billing/packs", response_model=list[CreditPackSummary])
def list_credit_packs():
    """Public pricing -- no auth needed, so it can be shown on a pricing
    page before someone signs up. See app/billing.py's CREDIT_PACKS."""
    return [
        CreditPackSummary(id=pack_id, **pack)
        for pack_id, pack in billing.CREDIT_PACKS.items()
    ]


class CheckoutRequest(BaseModel):
    pack_id: str


class CheckoutResponse(BaseModel):
    checkout_url: str


@app.post("/developers/billing/checkout", response_model=CheckoutResponse)
def create_billing_checkout(
    req: CheckoutRequest, customer: dict = Depends(get_or_create_current_api_customer)
):
    """Creates a Stripe Checkout session for a one-time credit-pack
    purchase and returns the URL to redirect your browser/user to.
    Credits land in your balance once Stripe confirms payment via
    POST /webhooks/stripe below -- typically within a few seconds of
    completing checkout."""
    try:
        checkout_url = billing.create_checkout_session(customer["id"], req.pack_id)
    except BillingError as e:
        raise HTTPException(400, str(e))
    return CheckoutResponse(checkout_url=checkout_url)


@app.post("/webhooks/stripe", status_code=200)
async def stripe_webhook(request: Request):
    """Stripe calls this on checkout.session.completed (and other event
    types, which are acknowledged and ignored). Deliberately
    unauthenticated -- no Bearer/API-key header, since Stripe itself is
    the caller -- and instead verified via the Stripe-Signature header
    against STRIPE_WEBHOOK_SECRET (see app/billing.py's
    construct_webhook_event). Always returns 200 for a signature-valid
    request, even for an event type there's nothing to do with, so
    Stripe doesn't keep retrying an event this app was never going to
    act on."""
    body = await request.body()
    signature = request.headers.get("Stripe-Signature")
    try:
        event = billing.construct_webhook_event(body, signature)
    except BillingError as e:
        raise HTTPException(400, str(e))

    if event["type"] == "checkout.session.completed":
        try:
            billing.fulfill_checkout_session(event["data"]["object"])
        except BillingError as e:
            # Malformed/unexpected metadata on an otherwise
            # signature-valid event -- log it for investigation rather
            # than 500ing (a 500 here just makes Stripe retry the exact
            # same payload forever).
            print(f"[stripe_webhook] Couldn't fulfill session: {e}")

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# GitHub push webhooks -- incremental re-indexing. See
# app/github_webhooks.py for signature verification / payload parsing,
# app/jobs.py for the queued incremental job itself, and app/repos.py for
# the registry this looks the repo up in. Deliberately unauthenticated
# (no Bearer token -- GitHub itself is the caller) and instead verified
# via the per-repo HMAC secret stored when the webhook was registered.
# ---------------------------------------------------------------------------

@app.post("/webhooks/github/{indexed_repo_id}", status_code=202)
async def github_push_webhook(indexed_repo_id: str, request: Request):
    row = repo_registry.get_indexed_repo_by_id(indexed_repo_id)
    if row is None or not row.get("webhook_secret"):
        # Same "don't confirm what exists" posture as everywhere else --
        # an unregistered/unknown id just looks like any other bad request.
        raise HTTPException(404, "Unknown webhook.")

    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    if not github_webhooks.verify_signature(row["webhook_secret"], body, signature):
        raise HTTPException(401, "Invalid webhook signature.")

    event_name = request.headers.get("X-GitHub-Event", "")
    if event_name == "ping":
        # GitHub sends this once, right after a webhook is created, to
        # confirm the endpoint is reachable -- no push to process yet.
        return {"status": "pong"}
    if event_name != "push":
        return {"status": "ignored", "event": event_name}

    payload = json.loads(body)
    push_event = github_webhooks.parse_push_event(payload)

    if not push_event["changed_files"] and not push_event["removed_files"]:
        return {"status": "ignored", "reason": "no file changes in this push"}

    job_id = jobs.enqueue_incremental_ingest(indexed_repo_id, push_event)
    return {"status": "queued", "job_id": job_id}


if __name__ == "__main__":
    # Lets you also run `python app/main.py` locally; Railway uses the
    # Dockerfile CMD / railway.json startCommand instead.
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))