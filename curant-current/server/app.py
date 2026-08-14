"""
Curant server — license/billing gate ONLY.

This server intentionally knows almost nothing about any customer. It
answers exactly one question: "is this license key valid and active, and
what has this customer paid for?" It never sees a message, a persona, an
instruction, a memory, or an API key. All of that now lives locally on
the customer's own Mac (see mac/curant-watcher.py and curant-cli) —
Claude is called directly from the customer's machine using their own
Anthropic API key, which never leaves that machine.

Implements:
  POST /v1/activate        — verify a license key, return account/billing basics
  GET  /v1/status          — check subscription/activation status (called
                             periodically by curant-cli to re-verify, cached
                             locally between checks)
  POST /v1/usage-report    — accepts a message COUNT only, never content
  POST /v1/error-report    — accepts an enumerated error code only, from a
                             closed allowlist — never free text
  POST /v1/release-request — logs a request to release a device binding
                             (e.g. after a new Mac). NOT self-serve — this
                             only logs the request; an admin reviews and
                             calls approve_release_request() after
                             verifying it's legitimate (by phone/support
                             contact, as intended), same as a customer
                             calling in directly.
  GET  /health              — liveness check

Also implements a small web UI:
  /login, /dashboard        — customer login (by license key) and a
                             dashboard showing plan/status/device binding,
                             plus a button to submit a release request
                             (same non-automatic review flow as above).
  /owner                    — a SEPARATE login gated by CURANT_ADMIN_PASSWORD
                             (env var, no default — login is disabled
                             outright if unset, never falls back to a
                             guessable password). Shows pending release
                             requests with Approve/Deny buttons and a
                             read-only customer list.
Both use signed session cookies (CURANT_SECRET_KEY — set this to a real
persistent random value before deploying), a CSRF token on every form,
and a stricter rate limit on login attempts specifically (5 per 5 min
per IP) than the general API rate limit.

Backup of persona/instructions/memories/settings is entirely local now —
`curant-cli backup-now`/`backup-restore` write/read an encrypted file at a
path the customer chooses (their own disk, an external drive, an iCloud
Drive folder, etc.). This server has no backup endpoints and never sees
that data in any form, encrypted or otherwise.

What's stored here, per customer, and nothing else:
  - license_key       (the identifier)
  - customer_name     (so activation can say "Welcome, <name>")
  - plan              (billing tier)
  - active            (subscription status — the actual product gate)
  - unlocked_addons    (what they've paid for — gates capabilities client-side)

Storage: SQLite for now (fine up to a few hundred customers; swap for
Postgres before real scale).

Setup:
  pip install flask --break-system-packages
  python app.py

Note there is no encryption-key setup step anymore, and no `anthropic`
dependency — this server never holds a secret worth encrypting or calls
Claude on anyone's behalf.
"""

from __future__ import annotations  # PEP 604 `X | Y` unions used throughout this file need this on Python < 3.10 (e.g. the Xcode Command Line Tools' bundled python3.9).

import os
import re
import sqlite3
import secrets
import json
import time
import sys
from contextlib import closing
from functools import wraps
from flask import Flask, request, jsonify, session, redirect, url_for, render_template_string
import stripe
import requests

app = Flask(__name__)
DB_PATH = os.environ.get("CURANT_DB_PATH", "curant.db")

# Session signing key. MUST be set to a real, persistent, random value in
# any real deployment (`export CURANT_SECRET_KEY=$(python3 -c "import
# secrets; print(secrets.token_hex(32))")`) — a value that changes on
# every restart would invalidate all sessions constantly, and a
# predictable one would let anyone forge session cookies.
_secret_key = os.environ.get("CURANT_SECRET_KEY")
if not _secret_key:
    print("WARNING: CURANT_SECRET_KEY not set — generating a temporary key for "
          "this run only. Set a real, persistent key before deploying for real use.",
          file=sys.stderr)
    _secret_key = secrets.token_hex(32)
app.secret_key = _secret_key

# Explicit session cookie hardening rather than relying on Flask defaults:
#   HTTPONLY — JavaScript can't read the session cookie (defends against
#              a stray XSS anywhere ever being able to steal a session).
#   SAMESITE=Lax — the cookie isn't sent on cross-site requests initiated
#              by other sites, which blunts CSRF further alongside the
#              token-based check below.
#   SECURE — only sent over HTTPS. This is NOT turned on by default here
#              because there's no real HTTPS hosting yet (see README) and
#              forcing it would silently break local testing over plain
#              http. Set CURANT_HTTPS=true once real HTTPS is in place —
#              deploying for real without doing this leaves session
#              cookies (and therefore login sessions) exposed to anyone
#              who can see the network traffic.
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("CURANT_HTTPS", "").lower() == "true"
if not app.config["SESSION_COOKIE_SECURE"]:
    print("WARNING: CURANT_HTTPS is not set to 'true' — session cookies will be sent "
          "over plain HTTP. Fine for local testing, NOT fine for a real deployment. "
          "Set CURANT_HTTPS=true once this is behind real HTTPS.", file=sys.stderr)

# The owner/admin password. Deliberately has NO default — if this isn't
# set, the owner login is disabled outright rather than falling back to
# some guessable default. Set via:
#   export CURANT_ADMIN_PASSWORD="something long and random, not reused elsewhere"
ADMIN_PASSWORD = os.environ.get("CURANT_ADMIN_PASSWORD")

# Real feature added 2026-08-14 ("make sure customer can add emails without
# me"): a customer connecting their own Gmail account previously required
# THEM to create their own Google Cloud project and OAuth client -- utterly
# unreasonable for a non-technical customer. The fix is the standard
# "one app, many users" OAuth pattern (same as any real "Sign in with
# Google" button): ONE shared OAuth client, registered once by the owner,
# handed out to any ACTIVATED customer via /v1/gmail-oauth-config below so
# their own curant-cli can build a gcp-oauth.keys.json locally and run the
# real per-customer browser login/consent step themselves -- that one step
# (an actual human approving access to their own inbox) can never be
# removed, and shouldn't be; everything BEFORE it now can be.
# No default on purpose, same reasoning as ADMIN_PASSWORD above -- if
# unset, the feature is cleanly unavailable (a clear error, not a
# silently-broken OAuth flow) rather than serving out empty credentials.
# Set via:
#   export CURANT_GMAIL_OAUTH_CLIENT_ID="....apps.googleusercontent.com"
#   export CURANT_GMAIL_OAUTH_CLIENT_SECRET="...."
#   export CURANT_GMAIL_OAUTH_PROJECT_ID="...."  (optional, cosmetic only)
GMAIL_OAUTH_CLIENT_ID = os.environ.get("CURANT_GMAIL_OAUTH_CLIENT_ID")
GMAIL_OAUTH_CLIENT_SECRET = os.environ.get("CURANT_GMAIL_OAUTH_CLIENT_SECRET")
GMAIL_OAUTH_PROJECT_ID = os.environ.get("CURANT_GMAIL_OAUTH_PROJECT_ID", "")

# Separate, stricter rate limit for login attempts (both customer and
# owner) — this is the endpoint most worth protecting against brute force,
# so it gets its own budget rather than sharing the general API one.
_login_rate_limit_window = {}
LOGIN_RATE_LIMIT_MAX_ATTEMPTS = 5
LOGIN_RATE_LIMIT_WINDOW_SECONDS = 300  # 5 attempts per 5 minutes per IP


def check_login_rate_limit(ip):
    now = time.time()
    timestamps = _login_rate_limit_window.get(ip, [])
    timestamps = [t for t in timestamps if now - t < LOGIN_RATE_LIMIT_WINDOW_SECONDS]
    if len(timestamps) >= LOGIN_RATE_LIMIT_MAX_ATTEMPTS:
        return False
    timestamps.append(now)
    _login_rate_limit_window[ip] = timestamps
    return True


# --- Simple per-key rate limiting ---
# In-memory is fine for a single-process deployment; if this ever runs as
# multiple worker processes, move to Redis. Purpose here is defensive —
# guarding against license-key enumeration/brute force on /v1/activate, and
# a runaway status-check loop — not handling adversarial traffic at scale.
_rate_limit_window = {}
RATE_LIMIT_MAX_REQUESTS = 30
RATE_LIMIT_WINDOW_SECONDS = 60


def check_rate_limit(key):
    now = time.time()
    timestamps = _rate_limit_window.get(key, [])
    timestamps = [t for t in timestamps if now - t < RATE_LIMIT_WINDOW_SECONDS]
    if len(timestamps) >= RATE_LIMIT_MAX_REQUESTS:
        return False
    timestamps.append(now)
    _rate_limit_window[key] = timestamps
    return True


# --- Storage setup ---

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(get_db()) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS customers (
            license_key TEXT PRIMARY KEY,
            customer_name TEXT,
            email TEXT,
            phone_number TEXT,
            plan TEXT DEFAULT 'base',
            active INTEGER DEFAULT 1,
            unlocked_addons TEXT DEFAULT '[]',  -- JSON array of addon ids
            device_id TEXT UNIQUE,               -- the one Mac this license is bound to
            total_messages INTEGER DEFAULT 0     -- cumulative count, never content
        );

        CREATE TABLE IF NOT EXISTS error_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            license_key TEXT,
            error_code TEXT,     -- enumerated code only, e.g. "llm_call_failed" —
                                  -- NEVER free text, NEVER message content
            component TEXT,      -- e.g. "watcher", "curant-cli", "proactive-check"
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS release_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            license_key TEXT,
            status TEXT DEFAULT 'pending',  -- 'pending' | 'approved' | 'denied'
            requested_at TEXT DEFAULT CURRENT_TIMESTAMP,
            resolved_at TEXT
        );

        CREATE TABLE IF NOT EXISTS feature_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            license_key TEXT,
            message TEXT,
            status TEXT DEFAULT 'open',  -- 'open' | 'reviewed' | 'done'
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS processed_stripe_events (
            event_id TEXT PRIMARY KEY,
            processed_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS server_generation_costs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            license_key TEXT,
            service TEXT,          -- 'flux' | 'ideogram' (Veo intentionally not here yet -- see AUGUST_TIER_CONFIG's docstring)
            estimated_cost_usd REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        -- Replaces the old customers.device_id single-column 1:1 binding
        -- for enforcement purposes (that column is left in place for
        -- backward compat / informational display, but bind_device below
        -- no longer writes meaningful enforcement data there). Most
        -- customers still get exactly one device (device_limit_for -- see
        -- get_device_limit_for_customer), but Grace customers can bind
        -- more than one device to the same license. A device can still
        -- only ever belong to ONE license (UNIQUE on device_id) --
        -- multi-device is about a license having several devices, never
        -- about a device serving several licenses.
        CREATE TABLE IF NOT EXISTS licensed_devices (
            license_key TEXT NOT NULL,
            device_id TEXT NOT NULL UNIQUE,
            bound_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (license_key, device_id)
        );

        -- Owner-facing queue of capability gaps a Grace customer hit that
        -- Curant has drafted a CODE proposal for (see /v1/propose-code-fix
        -- and the "Curant drafts, owner approves" design -- nothing here
        -- is ever auto-merged or auto-deployed; a human has to read and
        -- explicitly approve every row before any code changes).
        CREATE TABLE IF NOT EXISTS code_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            license_key TEXT,
            gap_description TEXT,     -- what the customer asked for that Curant couldn't do
            proposed_diff TEXT,       -- Curant's drafted patch, as unified diff text -- NEVER executed automatically
            status TEXT DEFAULT 'pending_review',  -- 'pending_review' | 'approved' | 'rejected'
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TEXT
        );

        -- Grace-only: capability gaps that skip the "call for a custom
        -- quote" phone-number wall and instead land here directly,
        -- flagged urgent, separate from the regular feature_requests
        -- queue (which is customer-initiated suggestions, not "I hit a
        -- wall right now" gaps).
        CREATE TABLE IF NOT EXISTS urgent_capability_gaps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            license_key TEXT,
            description TEXT,
            status TEXT DEFAULT 'open',  -- 'open' | 'resolved'
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
        conn.commit()

    # Backfill: any pre-existing single-device bindings (from before
    # licensed_devices existed) get carried over so an already-activated
    # customer doesn't appear to have zero bound devices after this
    # upgrade.
    with closing(get_db()) as conn:
        conn.execute(
            """INSERT OR IGNORE INTO licensed_devices (license_key, device_id)
               SELECT license_key, device_id FROM customers
               WHERE device_id IS NOT NULL AND device_id != ''"""
        )
        conn.commit()

    # Lightweight migration: installs that created customers before the
    # preferences columns existed won't have them -- CREATE TABLE IF NOT
    # EXISTS above won't add a column to an already-existing table (same
    # pattern curant-cli itself uses for its own local.db migrations).
    #
    # These three columns (persona/instructions/voice_tier) are a
    # deliberate, narrow exception to this server's "sees nothing
    # customer-specific" design (see the module docstring) -- they hold
    # only what a customer explicitly sets via the web dashboard, never
    # what Curant has LEARNED about them or the people in their life
    # (that stays local-only, same as before). preferences_updated_at
    # lets curant-cli tell whether the server's copy is newer than what
    # it last pulled, piggybacked onto its existing /v1/status poll
    # rather than adding a new network round-trip.
    with closing(get_db()) as conn:
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(customers)")}
        if "persona" not in existing_cols:
            conn.execute("ALTER TABLE customers ADD COLUMN persona TEXT DEFAULT 'curant'")
        if "instructions" not in existing_cols:
            conn.execute("ALTER TABLE customers ADD COLUMN instructions TEXT DEFAULT ''")
        if "voice_tier" not in existing_cols:
            conn.execute("ALTER TABLE customers ADD COLUMN voice_tier TEXT DEFAULT 'standard'")
        if "preferences_updated_at" not in existing_cols:
            conn.execute("ALTER TABLE customers ADD COLUMN preferences_updated_at TEXT")
        if "customer_apple_id" not in existing_cols:
            # The phone number/email/Apple ID Curant listens for messages
            # from on the customer's own Mac -- purely a convenience so a
            # customer can set this from the web dashboard instead of
            # hand-editing ~/.curant/config.json or exporting an env var.
            # Synced down the same way as persona/instructions/voice_tier
            # (piggybacked on /v1/status and /v1/activate, gated by the
            # same preferences_updated_at timestamp) -- this is still just
            # a convenience relay, not the server "knowing" the customer;
            # curant-watcher.py only ever reads it from local config.
            conn.execute("ALTER TABLE customers ADD COLUMN customer_apple_id TEXT")
        if "customer_handles" not in existing_cols:
            conn.execute("ALTER TABLE customers ADD COLUMN customer_handles TEXT DEFAULT '[]'")
        if "email" not in existing_cols:
            conn.execute("ALTER TABLE customers ADD COLUMN email TEXT")
        if "created_at" not in existing_cols:
            # SQLite's ALTER TABLE ADD COLUMN specifically disallows
            # CURRENT_TIMESTAMP as a default (only CREATE TABLE allows
            # that) -- add the column plain, then backfill existing rows
            # with the time of this migration. That backfilled value is
            # NOT each customer's real original activation time -- an
            # honest limitation worth knowing about if you're auditing
            # "activated licenses" against this column for pre-migration
            # customers; only customers created after this migration ran
            # get an accurate created_at.
            conn.execute("ALTER TABLE customers ADD COLUMN created_at TEXT")
            conn.execute(
                "UPDATE customers SET created_at = ? WHERE created_at IS NULL",
                (time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),),
            )
        conn.commit()


# --- Addon catalog ---
# Deliberately a small, hardcoded list here rather than a DB table --
# these are product capabilities defined in code (curant-cli's own addon
# gates, see `unlocked_addons` checks in curant-cli), not arbitrary data,
# so keeping them in code keeps this file the one place both sides of an
# addon id have to agree. Add a `stripe_payment_link` once you've created
# a real Stripe Payment Link for that addon (Stripe Dashboard > Payment
# Links) -- until then the dashboard shows a "contact to unlock" prompt
# instead of a broken/missing button.
ADDON_CATALOG = {
    "august_standard": {
        "label": "August Standard ($4.99/mo)",
        "description": "The specialist creative persona, with $2/mo of image generation "
                        "(FLUX/Ideogram) included -- covered by us, not billed to your own "
                        "provider accounts. No video generation on this tier.",
        "stripe_payment_link": None,
    },
    "august_pro": {
        "label": "August Pro ($10.99/mo)",
        "description": "Everything in Standard, plus video generation (Veo) and a $5/mo "
                        "covered generation budget across images and video.",
        "stripe_payment_link": None,
    },
    "august_max": {
        "label": "August Max ($20.99/mo)",
        "description": "Everything in Pro, plus a $10/mo covered generation budget and access "
                        "to additional creative features as they're built.",
        "stripe_payment_link": None,
    },
    "browser_automation": {
        "label": "Browser automation",
        "description": "Lets Curant browse webpages, click through them (pagination, expanding "
                        "content) to read further, and fill out/submit web forms on your behalf "
                        "(with confirmation). Always refuses to click through or fill in a login "
                        "or payment/checkout flow, regardless of confirmation.",
        "stripe_payment_link": None,
    },
    "grace": {
        "label": "Grace ($50.99/mo, flagship)",
        "description": "The most advanced tier of Curant, and a genuinely different tier, not "
                        "just August Max plus extras. Everything in August Max (full creative "
                        "generation suite, images and video) with NO monthly cap on covered "
                        "generation, plus: browser automation, every persona unlocked, a custom "
                        "cloned voice, multi-device support, no cap on standing instructions "
                        "length, priority handling with capability gaps skipping the custom-quote "
                        "wall and going straight to a human-reviewed code proposal, weekly "
                        "executive rollups with a stalled-items digest, meeting-prep briefs, "
                        "VIP contact escalation, delegate access (a second person can use the "
                        "same install), bounded auto-reply for specific approved contacts, a "
                        "second-opinion review pass on high-stakes replies, full encrypted data "
                        "export, automatic weekly backups, and email-aware order-status lookups. "
                        "It also just talks differently -- cut of the usual AI-assistant tells, "
                        "permission to disagree or say it doesn't know something, and a "
                        "relationship-aware tone that adjusts to how you actually communicate.",
        "stripe_payment_link": None,
    },
}

# --- August tier -> covered-generation-budget config ---
# Deliberately separate from ADDON_CATALOG (which is customer-facing
# display text) -- this is the actual enforcement data: how much of
# FLUX/Ideogram/Veo cost is covered per month for each tier, and whether
# video is allowed at all. A customer's tier is whichever ONE of these
# three keys appears in their unlocked_addons (set by provision_customer
# via PRICE_PLAN_MAP, same as any other addon).
#
# Veo is NOT proxied yet -- video_allowed here reflects the PRICING
# promise (Pro/Max tiers include video in what they pay for), but the
# actual server-side Veo proxy (holding a Google API key, tracking an
# async job, hosting the finished file for retrieval) hasn't been built.
# Until it is, video_allowed=True tiers should still fail gracefully if
# a video is requested -- see the "not yet implemented" note on the
# generate-video path once/if that's added.
AUGUST_TIER_CONFIG = {
    "august_standard": {"monthly_cap_usd": 2.0, "video_allowed": False},
    "august_pro": {"monthly_cap_usd": 5.0, "video_allowed": True},
    "august_max": {"monthly_cap_usd": 10.0, "video_allowed": True},
    # Grace: the flagship tier, explicitly confirmed with the owner to
    # have NO monthly $ cap on covered generation (monthly_cap_usd=None,
    # same "None means uncapped" convention curant-cli already uses for
    # its own local spend cap). Real cost exposure here is bounded only
    # by the shared per-license-key rate limit in _august_proxy_precheck
    # (30 req/min as of this writing) -- there is deliberately no dollar
    # ceiling. Flagged here in case that combination ever needs
    # revisiting: a sustained abusive burst at the rate limit is a real,
    # if unlikely, unbounded cost, not just a theoretical one.
    "grace": {"monthly_cap_usd": None, "video_allowed": True},
}

# Server's own FLUX/Ideogram keys -- these are what actually get spent
# against a customer's tier cap, never sent to any customer's Mac. Set
# these as real env vars before this proxy can do anything; a missing
# key fails closed (see generate_image_flux_proxy/generate_image_ideogram_proxy).
FLUX_API_KEY = os.environ.get("FLUX_API_KEY")
IDEOGRAM_API_KEY = os.environ.get("IDEOGRAM_API_KEY")

# Same verified-Aug-2026 rates as curant-cli's own ESTIMATED_COST_USD
# (kept as a separate constant, not imported, since the server and
# curant-cli are separate deployables with no shared module) -- FLUX is
# BFL's own stated per-image price; Ideogram is V4 Default's per-image
# API rate. Both re-verified via web search, not assumed.
ESTIMATED_GENERATION_COST_USD = {
    "flux": 0.04,
    "ideogram": 0.06,
}

# Contact shown once a customer has used their tier's covered budget for
# the month -- placeholder, same unfilled pattern as curant-cli's own
# QUOTE_PHONE_NUMBER / GENERATION_TOPUP_CONTACT. Needs a real value.
GENERATION_SALES_CONTACT = "REPLACE_WITH_REAL_SALES_CONTACT"


def get_customer_august_tier(customer):
    """Returns the customer's august_* tier key (one of AUGUST_TIER_CONFIG's
    keys) or None if they don't have any August tier unlocked. A customer
    should only ever have ONE of the three -- if somehow more than one is
    present (shouldn't happen via normal provisioning), the highest cap
    tier wins, since refusing to serve someone who's clearly paid for
    *something* would be a worse failure mode than picking generously."""
    unlocked = json.loads(customer["unlocked_addons"] or "[]")
    tiers = [a for a in unlocked if a in AUGUST_TIER_CONFIG]
    if not tiers:
        return None
    # None (uncapped, i.e. Grace) must sort as the highest possible tier,
    # not crash trying to compare None > a float or get treated as the
    # lowest -- float("inf") makes it win over every real $ cap.
    return max(
        tiers,
        key=lambda t: (
            float("inf") if AUGUST_TIER_CONFIG[t]["monthly_cap_usd"] is None
            else AUGUST_TIER_CONFIG[t]["monthly_cap_usd"]
        ),
    )


def get_server_monthly_spend(license_key):
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT SUM(estimated_cost_usd) as total FROM server_generation_costs "
            "WHERE license_key = ? AND created_at >= date('now', 'start of month')",
            (license_key,),
        ).fetchone()
        return row["total"] or 0.0


def check_and_log_server_generation_cost(license_key, tier, service, cost):
    """
    Atomically checks this month's spend-plus-this-generation against the
    tier's cap AND logs it, as a single database transaction -- not two
    separate steps. This closes a real race condition: if "check the cap"
    and "log the cost" were separate operations, a burst of near-
    simultaneous requests could each see a spend total that doesn't yet
    include the others' cost, and all pass the check before any of them
    got counted -- letting total spend blow past the cap in a fast burst
    even though each individual request looked fine when it checked.

    BEGIN IMMEDIATE acquires SQLite's write lock up front, serializing
    concurrent calls to this function against each other -- the second
    concurrent request has to wait for the first's transaction to fully
    commit (cost logged) before it can even read the current total, so
    it sees the first request's spend already counted.

    Returns (ok: bool, error: str | None).
    """
    cap = AUGUST_TIER_CONFIG[tier]["monthly_cap_usd"]
    with closing(get_db()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # cap is None for Grace (uncapped by design -- see
            # AUGUST_TIER_CONFIG's comment on that tier) -- there's no
            # dollar ceiling to check against, so this still records the
            # cost for spend-tracking/reporting purposes, just never
            # blocks on it. Every other tier keeps the real comparison.
            if cap is not None:
                row = conn.execute(
                    "SELECT SUM(estimated_cost_usd) as total FROM server_generation_costs "
                    "WHERE license_key = ? AND created_at >= date('now', 'start of month')",
                    (license_key,),
                ).fetchone()
                current = row["total"] or 0.0
                projected = current + cost
                if projected > cap:
                    conn.execute("ROLLBACK")
                    return False, None, (
                        f"This would put this month's covered generation spend at an estimated "
                        f"${projected:.2f}, over your ${cap:.2f}/mo included budget. Nothing was "
                        f"generated. For more this month: {GENERATION_SALES_CONTACT}."
                    )
            cursor = conn.execute(
                "INSERT INTO server_generation_costs (license_key, service, estimated_cost_usd) VALUES (?, ?, ?)",
                (license_key, service, cost),
            )
            conn.commit()
            return True, cursor.lastrowid, None
        except Exception:
            conn.execute("ROLLBACK")
            raise


def refund_server_generation_cost(cost_row_id):
    """
    Deletes a cost row that was reserved by check_and_log_server_generation_cost
    but whose actual generation call then failed (bad response from FLUX/
    Ideogram, network error, etc.) -- a customer shouldn't be charged
    against their monthly budget for a generation that never happened.
    Reserving first and refunding on failure (rather than checking without
    reserving, then logging only after success) is what keeps the cap
    check race-free -- see check_and_log_server_generation_cost's
    docstring for why the atomic reserve matters.
    """
    with closing(get_db()) as conn:
        conn.execute("DELETE FROM server_generation_costs WHERE id = ?", (cost_row_id,))
        conn.commit()

# Persona catalog for the dashboard dropdown -- kept in sync manually with
# curant-cli's own PERSONAS dict (curant-cli is the source of truth for
# what each persona actually sounds like; this is just the list of valid
# choices for the web form). If you add a persona to curant-cli, add its
# id here too or customers won't be able to select it from the web.
PERSONA_CHOICES = ["curant", "grace", "dean", "nora", "frank", "miles", "jane", "leo", "august", "aaron"]
VOICE_TIER_CHOICES = ["standard", "natural", "realistic"]


def get_customer(license_key):
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT * FROM customers WHERE license_key = ?", (license_key,)
        ).fetchone()
        return dict(row) if row else None


# --- Helper: create a new customer (call this from your signup/payment webhook) ---

def provision_customer(customer_name, email=None, phone_number=None, plan="base"):
    """Call this after a successful Stripe payment to create a new customer
    and generate their license key. Notably: no Anthropic API key is taken
    or stored here anymore — the customer enters their own key locally on
    their Mac via `curant-cli set-api-key`, and it never reaches this server.

    `email` was added alongside the manual owner-triggered creation flow
    (see owner_create_customer()) specifically so send_license_email() has
    somewhere to send the key -- Stripe Checkout will supply this
    automatically once the webhook exists; the manual flow collects it
    directly from the owner instead."""
    license_key = "CRT-" + "-".join(
        secrets.token_hex(2).upper() for _ in range(3)
    )
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO customers (license_key, customer_name, email, phone_number, plan) VALUES (?, ?, ?, ?, ?)",
            (license_key, customer_name, email, phone_number, plan),
        )
        conn.commit()
    return license_key


# Most customers get exactly one device per license, same as always.
# Grace is the one tier that gets more (home + office + laptop is a
# realistic real-world need for an "executive" customer) -- see
# get_device_limit_for_customer.
MAX_DEVICES_DEFAULT = 1
MAX_DEVICES_GRACE = 3


def get_device_limit_for_customer(customer):
    """How many devices this customer's license may be bound to at once.
    Reads the same unlocked_addons/AUGUST_TIER_CONFIG machinery as the
    generation-cap logic, since Grace is defined the same way there."""
    if get_customer_august_tier(customer) == "grace":
        return MAX_DEVICES_GRACE
    return MAX_DEVICES_DEFAULT


def bind_device(license_key, device_id):
    """
    Enforces:
      - A device can only ever be bound to ONE license (UNIQUE on
        licensed_devices.device_id) -- this is never relaxed, for anyone.
      - A license can be bound to up to get_device_limit_for_customer(...)
        devices -- 1 for everyone except Grace (3), rather than the old
        hard 1:1 rule for all customers.
    Returns (ok: bool, error_code: str | None).
    """
    with closing(get_db()) as conn:
        customer = conn.execute(
            "SELECT * FROM customers WHERE license_key = ?", (license_key,)
        ).fetchone()
        if customer is None:
            return False, "invalid_license"

        already_bound_to_this_license = conn.execute(
            "SELECT 1 FROM licensed_devices WHERE license_key = ? AND device_id = ?",
            (license_key, device_id),
        ).fetchone()
        if already_bound_to_this_license:
            return True, None  # re-activating an already-bound device -- fine, idempotent

        other = conn.execute(
            "SELECT license_key FROM licensed_devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        if other is not None:
            return False, "device_already_bound_to_another_license"

        limit = get_device_limit_for_customer(customer)
        count_row = conn.execute(
            "SELECT COUNT(*) AS c FROM licensed_devices WHERE license_key = ?", (license_key,)
        ).fetchone()
        if count_row["c"] >= limit:
            return False, "device_limit_reached"

        conn.execute(
            "INSERT INTO licensed_devices (license_key, device_id) VALUES (?, ?)",
            (license_key, device_id),
        )
        # customers.device_id kept in sync for backward-compat display
        # only (e.g. anywhere still reading it directly) -- it's a single
        # column so it can only ever reflect ONE of a Grace customer's
        # several bound devices; licensed_devices is the real source of
        # truth for enforcement everywhere in this file.
        if not customer["device_id"]:
            conn.execute(
                "UPDATE customers SET device_id = ? WHERE license_key = ?",
                (device_id, license_key),
            )
        conn.commit()
        return True, None


def is_device_bound_to_license(license_key, device_id):
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT 1 FROM licensed_devices WHERE license_key = ? AND device_id = ?",
            (license_key, device_id),
        ).fetchone()
        return row is not None


def count_bound_devices(license_key):
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM licensed_devices WHERE license_key = ?", (license_key,)
        ).fetchone()
        return row["c"]


# --- Stripe checkout -> plan/addon mapping ---
# Fill this in with your real Stripe Price IDs once you have them (Stripe
# Dashboard > Product catalog > click into a price to see its id, looks
# like "price_1AbC..."). Left empty until then -- an unmapped price
# still provisions successfully (falls back to plan="base", no addons)
# rather than silently failing the whole checkout, but gets flagged in
# the server log so it doesn't go unnoticed.
# STRIPE_MODE controls which price map is active. Defaults to "test" on
# purpose -- a fresh checkout of this server should never accidentally
# be able to charge a real card just because an env var wasn't set.
# Stripe account KYC/identity verification (the "Verify your personal
# details" flow) hasn't been completed yet as of 2026-08-11, so live
# payments can't actually settle right now regardless of this switch --
# but the switch exists so the code is ready the moment that's done,
# without another round of edits here.
#
# Set STRIPE_MODE=live once: (a) KYC/identity verification is complete
# in the Stripe dashboard, (b) the real STRIPE_SECRET_KEY / webhook
# secret env vars are the live ones, not test ones, and (c) you're
# actually ready for real customers to be charged.
STRIPE_MODE = os.environ.get("STRIPE_MODE", "test")

# Fill these in once you've flipped the Stripe dashboard to test mode
# (toggle top-right of the dashboard) and created the same three August
# tier prices there (Product catalog > + Add product, same names/amounts
# as the live ones below). Test mode price ids look identical in shape
# to live ones (price_...) but only work with test-mode API keys/cards
# (4242 4242 4242 4242, any future expiry, any CVC) -- nothing here can
# charge a real card even by mistake.
PRICE_PLAN_MAP_TEST = {
    # "price_test_XXXXXXXXXXXXXX": {"plan": "base", "addons": ["august_standard"]},
    # "price_test_YYYYYYYYYYYYYY": {"plan": "base", "addons": ["august_pro"]},
    # "price_test_ZZZZZZZZZZZZZZ": {"plan": "base", "addons": ["august_max"]},
}

# Real August tier prices, created live in Stripe on 2026-08-11
# (product+price created together via the Stripe API -- see each
# price's product_data.name in the dashboard for "August Standard"/
# "August Pro"/"August Max"). Each is a $/mo recurring subscription
# price; the addon key here must exactly match AUGUST_TIER_CONFIG's
# keys above, since that's what actually enforces the monthly
# covered-generation cap once a customer's unlocked_addons includes it.
# NOT active by default -- see STRIPE_MODE above; Stripe account
# verification isn't complete yet, so these can't take real payments
# regardless of whether this map is selected.
PRICE_PLAN_MAP_LIVE = {
    "price_1U38kJDXZA7eiWhwjo73xuj0": {"plan": "base", "addons": ["august_standard"]},  # $4.99/mo
    "price_1U38kMDXZA7eiWhwoVZD2YRX": {"plan": "base", "addons": ["august_pro"]},       # $10.99/mo
    "price_1U38kQDXZA7eiWhwGqbpU8ay": {"plan": "base", "addons": ["august_max"]},       # $20.99/mo
}

PRICE_PLAN_MAP = PRICE_PLAN_MAP_TEST if STRIPE_MODE == "test" else PRICE_PLAN_MAP_LIVE


def set_customer_addons(license_key, addons):
    with closing(get_db()) as conn:
        conn.execute(
            "UPDATE customers SET unlocked_addons = ? WHERE license_key = ?",
            (json.dumps(addons), license_key),
        )
        conn.commit()


def is_stripe_event_processed(event_id):
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_stripe_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return row is not None


def mark_stripe_event_processed(event_id):
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO processed_stripe_events (event_id) VALUES (?)", (event_id,)
        )
        conn.commit()


def plan_and_addons_for_checkout_session(session_id):
    """
    Looks up which plan/addons a completed Checkout Session actually paid
    for, by listing its line items and matching each price id against
    PRICE_PLAN_MAP. A checkout webhook payload doesn't include line items
    by default (you'd need `expand: ["line_items"]` set at Checkout
    Session CREATION time, which this server doesn't control if Checkout
    was set up directly in the Stripe dashboard) -- so this makes a
    separate API call instead, which works regardless of how the session
    was created.

    Any price id not found in PRICE_PLAN_MAP still provisions (falls back
    to "base", no addons) rather than failing the whole checkout -- a
    customer who paid should never end up with nothing over a mapping
    typo -- but prints a warning so it doesn't go unnoticed.
    """
    plan = "base"
    addons = []
    try:
        line_items = stripe.checkout.Session.list_line_items(session_id, limit=10)
    except Exception as e:
        print(f"[stripe webhook] couldn't list line items for session {session_id}: {e}", file=sys.stderr)
        return plan, addons

    for item in line_items.get("data", []):
        price_id = (item.get("price") or {}).get("id")
        if not price_id:
            continue
        mapping = PRICE_PLAN_MAP.get(price_id)
        if mapping:
            plan = mapping.get("plan", plan)
            addons.extend(mapping.get("addons", []))
        else:
            print(f"[stripe webhook] price {price_id} isn't in PRICE_PLAN_MAP -- "
                  f"provisioning with plan={plan!r} anyway rather than failing the checkout. "
                  f"Add it to PRICE_PLAN_MAP once you know what it should unlock.", file=sys.stderr)
    return plan, list(set(addons))


# Sender address for license-delivery email -- override via env var if you
# switch senders later (e.g. once a real authenticated domain exists,
# rather than the free-tier Gmail address used to get this working).
LICENSE_EMAIL_FROM = os.environ.get("SENDGRID_FROM_EMAIL", "curant.interface@gmail.com")


def send_license_email(customer_name, email, license_key):
    """
    Emails a newly-provisioned customer their license key via SendGrid.
    Lazy-imports the sendgrid package so this server can still start and
    serve everything else (activation, dashboards, etc.) even if it isn't
    installed or SENDGRID_API_KEY isn't set -- email delivery is a nice-
    to-have layered on top of provisioning, not something that should be
    able to take the whole server down if it's misconfigured.

    Deliberately best-effort: the license key is ALWAYS visible to the
    owner in the dashboard regardless of whether this succeeds (see
    owner_create_customer()), so a SendGrid outage or a bad API key never
    means the key is lost -- just that it has to be relayed by hand this
    once. Returns (ok: bool, error: str | None).
    """
    if not email:
        return False, "no_email_on_file"

    api_key = os.environ.get("SENDGRID_API_KEY")
    if not api_key:
        return False, "SENDGRID_API_KEY not set on this server"

    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail
    except ImportError:
        return False, "sendgrid package not installed (pip install sendgrid --break-system-packages)"

    message = Mail(
        from_email=LICENSE_EMAIL_FROM,
        to_emails=email,
        subject="Your Curant license key",
        html_content=(
            f"<p>Hi {customer_name or 'there'},</p>"
            f"<p>Thanks for signing up for Curant. Your license key is:</p>"
            f"<p style=\"font-size:1.2rem; font-family:monospace; font-weight:bold;\">{license_key}</p>"
            f"<p>Activate it on your Mac with:</p>"
            f"<p style=\"font-family:monospace;\">curant-cli activate {license_key}</p>"
            f"<p>Keep this email -- you'll need the key again if you ever set up on a new Mac.</p>"
        ),
    )
    try:
        sg = SendGridAPIClient(api_key)
        response = sg.send(message)
        if response.status_code >= 300:
            return False, f"SendGrid returned status {response.status_code}"
        return True, None
    except Exception as e:
        return False, str(e)


# Sanity bound on custom instructions length, not a hard product limit --
# just guards against a pathological form submission bloating the DB
# indefinitely. Grace gets a much higher ceiling (confirmed explicitly
# with the owner as one of Grace's differentiators) for genuinely more
# detailed executive context -- recurring meeting patterns, org chart,
# communication preferences, etc. -- that wouldn't fit in 4000 characters.
INSTRUCTIONS_MAX_CHARS_DEFAULT = 4000
INSTRUCTIONS_MAX_CHARS_GRACE = 20000


def _normalize_handle(h):
    """
    Same fix, same reasoning, as curant-cli's identical copy of this
    function (no shared module between the server and curant-cli, see
    the note on ESTIMATED_GENERATION_COST_USD above for why constants/
    small helpers get duplicated rather than imported across the two).
    chat.db always stores phone numbers in E.164 form; a customer typing
    "240-839-0687" into this dashboard form would otherwise be saved
    exactly as typed and never match a single incoming text on their
    Mac, with no error surfaced anywhere -- a real bug, found live.
    """
    h = (h or "").strip()
    if not h or "@" in h:
        return h
    if h.startswith("+"):
        return h
    digits = re.sub(r"\D", "", h)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return h


def update_customer_preferences(license_key, persona, instructions, voice_tier, customer_apple_id="", customer_handles=None):
    """
    Applies a customer's own explicit persona/instructions/voice-tier/
    identity choice from the web dashboard. Validates against the
    known-good choice lists rather than trusting form input directly --
    a bad persona id landing in the DB would silently break curant-cli's
    PERSONAS.get() fallback logic on the customer's Mac otherwise.
    Stamps preferences_updated_at so curant-cli's next /v1/status poll
    (or /v1/activate, for a first-time setup) knows there's something
    new to pull down.

    customer_apple_id/customer_handles are NOT validated against any
    choice list (they're free-form phone numbers/emails) -- just
    trimmed. Empty string/list means "not set", same as before this
    field existed, so a customer who never touches this section keeps
    whatever they've configured locally untouched (see
    apply_synced_preferences_if_newer's docstring on the same pattern).
    Returns (ok: bool, error: str | None).
    """
    if persona not in PERSONA_CHOICES:
        return False, "invalid_persona"
    if voice_tier not in VOICE_TIER_CHOICES:
        return False, "invalid_voice_tier"
    customer = get_customer(license_key)
    max_chars = (
        INSTRUCTIONS_MAX_CHARS_GRACE
        if customer and get_customer_august_tier(customer) == "grace"
        else INSTRUCTIONS_MAX_CHARS_DEFAULT
    )
    if len(instructions) > max_chars:
        return False, "instructions_too_long"
    customer_apple_id = _normalize_handle(customer_apple_id)
    customer_handles = [_normalize_handle(h) for h in (customer_handles or [])]
    with closing(get_db()) as conn:
        conn.execute(
            "UPDATE customers SET persona = ?, instructions = ?, voice_tier = ?, "
            "customer_apple_id = ?, customer_handles = ?, preferences_updated_at = ? WHERE license_key = ?",
            (persona, instructions, voice_tier, customer_apple_id, json.dumps(customer_handles),
             time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), license_key),
        )
        conn.commit()
    return True, None


def create_feature_request(license_key, message):
    """
    Logs a customer-submitted feature request from the web dashboard --
    a suggestion box, separate from curant-cli's own LOCAL capability-gap
    log (which fires automatically when Curant hits something it can't
    do, and stays on the customer's Mac). This one is explicitly
    customer-initiated and lands here specifically so you (the owner)
    can see it without needing access to any individual customer's Mac.
    """
    message = (message or "").strip()
    if not message:
        return False, "empty_message"
    if len(message) > 2000:
        return False, "message_too_long"
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO feature_requests (license_key, message) VALUES (?, ?)",
            (license_key, message),
        )
        conn.commit()
    return True, None


def list_feature_requests_for(license_key):
    """Customer-facing: just this customer's own requests, oldest first isn't
    as useful here as newest first -- so they see their most recent submission
    without scrolling."""
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT id, message, status, created_at FROM feature_requests "
            "WHERE license_key = ? ORDER BY created_at DESC",
            (license_key,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_all_feature_requests():
    """Owner-facing: every customer's requests, Grace customers' requests
    first (priority handling is one of Grace's advertised benefits),
    newest-first within each group, joined with the customer's name so
    you don't have to cross-reference a license key by hand.

    unlocked_addons is a JSON array stored as text (e.g. '["grace",
    "browser_automation"]') -- LIKE '%grace%' is a deliberately simple
    substring match rather than a real JSON containment check, which is
    fine here since "grace" isn't a substring of any other addon id in
    ADDON_CATALOG/AUGUST_TIER_CONFIG today. Revisit with json_each if
    that ever stops being true."""
    with closing(get_db()) as conn:
        rows = conn.execute(
            """SELECT feature_requests.id, feature_requests.license_key, feature_requests.message,
                      feature_requests.status, feature_requests.created_at,
                      customers.customer_name,
                      (customers.unlocked_addons LIKE '%grace%') AS is_priority
               FROM feature_requests
               JOIN customers ON customers.license_key = feature_requests.license_key
               ORDER BY is_priority DESC, feature_requests.created_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]


def set_feature_request_status(request_id, status):
    if status not in ("open", "reviewed", "done"):
        return False, "invalid_status"
    with closing(get_db()) as conn:
        conn.execute("UPDATE feature_requests SET status = ? WHERE id = ?", (status, request_id))
        conn.commit()
    return True, None


# --- August generation proxy (FLUX / Ideogram) ---
# The server holds the real keys and makes these calls itself -- a
# customer's Mac never receives FLUX_API_KEY/IDEOGRAM_API_KEY, only the
# resulting image URL, so there's nothing here for a customer to extract
# and reuse outside the tier cap. Mirrors curant-cli's own
# generate_image_flux/generate_image_ideogram request shapes exactly
# (verified against the same BFL/Ideogram integration docs those were
# built from) -- this replaces where that code runs for August-tier
# customers, it doesn't reimplement it differently.

def generate_image_flux_proxy(prompt):
    """Returns (image_url, error). Fails closed if FLUX_API_KEY isn't
    configured on the server -- never falls back to anything, since
    there's no customer key to fall back to in this proxied path."""
    if not FLUX_API_KEY:
        return None, "Image generation isn't configured on the server yet (FLUX_API_KEY unset)."
    try:
        submit = requests.post(
            "https://api.bfl.ai/v1/flux-pro-1.1",
            headers={"accept": "application/json", "x-key": FLUX_API_KEY, "Content-Type": "application/json"},
            json={"prompt": prompt, "width": 1024, "height": 1024},
            timeout=30,
        )
        submit.raise_for_status()
        submit_data = submit.json()
        task_id = submit_data.get("id")
        polling_url = submit_data.get("polling_url")
        if not task_id or not polling_url:
            return None, "FLUX didn't return an id/polling_url."
        for _ in range(60):
            time.sleep(0.5)
            result = requests.get(
                polling_url, params={"id": task_id},
                headers={"accept": "application/json", "x-key": FLUX_API_KEY}, timeout=15,
            )
            result.raise_for_status()
            data = result.json()
            status = data.get("status")
            if status == "Ready":
                image_url = (data.get("result") or {}).get("sample")
                if not image_url:
                    return None, "FLUX reported ready but returned no image URL."
                return image_url, None
            if status in ("Error", "Failed"):
                return None, "FLUX generation failed."
        return None, "FLUX generation timed out waiting for a result."
    except Exception:
        # Deliberately NOT including str(e) in what reaches the customer —
        # some providers echo request details (occasionally including
        # partial key/account info) into error bodies, and that exception
        # text could carry it. Full detail goes to the server's own
        # stderr for you to debug; the customer gets a generic message.
        print(f"[generate-image proxy] FLUX call failed", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return None, "Image generation failed. Nothing was charged."


def generate_image_ideogram_proxy(prompt):
    """Returns (image_url, error). Same fail-closed/error-sanitizing
    pattern as generate_image_flux_proxy."""
    if not IDEOGRAM_API_KEY:
        return None, "Text-in-image generation isn't configured on the server yet (IDEOGRAM_API_KEY unset)."
    try:
        resp = requests.post(
            "https://api.ideogram.ai/v1/ideogram-v4/generate",
            headers={"Api-Key": IDEOGRAM_API_KEY, "Content-Type": "application/json"},
            json={"prompt": prompt},
            timeout=60,
        )
        resp.raise_for_status()
        items = resp.json().get("data") or []
        image_url = items[0].get("url") if items else None
        if not image_url:
            return None, "Ideogram didn't return an image URL."
        return image_url, None
    except Exception:
        print(f"[generate-image-with-text proxy] Ideogram call failed", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return None, "Image generation failed. Nothing was charged."


def _august_proxy_precheck(request):
    """
    Shared setup for both generation-proxy endpoints: validates the
    license key, confirms an August tier is actually unlocked, and
    applies a tighter rate limit than the general API limit (these
    endpoints spend real money per successful call, unlike most of this
    server's other endpoints, so they get their own stricter budget).

    Returns (customer, tier, error_response_tuple). error_response_tuple
    is None on success; otherwise it's exactly what the route should
    `return` directly.
    """
    auth = request.headers.get("Authorization", "")
    license_key = auth.replace("Bearer ", "")

    if not check_rate_limit(f"generate:{license_key or 'unknown'}"):
        return None, None, (jsonify({"error": "rate_limited"}), 429)

    customer = get_customer(license_key)
    if not customer or not customer["active"]:
        return None, None, (jsonify({"error": "invalid_or_inactive_license"}), 403)

    tier = get_customer_august_tier(customer)
    if not tier:
        return None, None, (jsonify({"error": "no_august_tier_unlocked"}), 403)

    return customer, tier, None


@app.route("/v1/generate-image", methods=["POST"])
def generate_image_endpoint():
    customer, tier, err = _august_proxy_precheck(request)
    if err:
        return err

    data = request.get_json(force=True)
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "prompt_required"}), 400
    if len(prompt) > 4000:
        return jsonify({"error": "prompt_too_long"}), 400

    license_key = customer["license_key"]
    ok, cost_row_id, cap_error = check_and_log_server_generation_cost(
        license_key, tier, "flux", ESTIMATED_GENERATION_COST_USD["flux"]
    )
    if not ok:
        return jsonify({"error": "cap_exceeded", "message": cap_error}), 402

    image_url, error = generate_image_flux_proxy(prompt)
    if error:
        # Generation didn't actually happen -- don't charge the customer's
        # monthly budget for it.
        refund_server_generation_cost(cost_row_id)
        return jsonify({"error": "generation_failed", "message": error}), 502
    return jsonify({"ok": True, "image_url": image_url})


@app.route("/v1/generate-image-with-text", methods=["POST"])
def generate_image_with_text_endpoint():
    customer, tier, err = _august_proxy_precheck(request)
    if err:
        return err

    data = request.get_json(force=True)
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "prompt_required"}), 400
    if len(prompt) > 4000:
        return jsonify({"error": "prompt_too_long"}), 400

    license_key = customer["license_key"]
    ok, cost_row_id, cap_error = check_and_log_server_generation_cost(
        license_key, tier, "ideogram", ESTIMATED_GENERATION_COST_USD["ideogram"]
    )
    if not ok:
        return jsonify({"error": "cap_exceeded", "message": cap_error}), 402

    image_url, error = generate_image_ideogram_proxy(prompt)
    if error:
        refund_server_generation_cost(cost_row_id)
        return jsonify({"error": "generation_failed", "message": error}), 502
    return jsonify({"ok": True, "image_url": image_url})


# --- Endpoints ---

@app.route("/health", methods=["GET"])
def health():
    """Simple liveness check for whatever hosting/monitoring this ends up behind."""
    try:
        with closing(get_db()) as conn:
            conn.execute("SELECT 1").fetchone()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 503


@app.route("/v1/activate", methods=["POST"])
def activate():
    data = request.get_json(force=True)
    license_key = data.get("license_key", "")
    device_id = data.get("device_id", "")

    if not check_rate_limit(license_key or "unknown"):
        return jsonify({"valid": False, "error": "rate_limited"}), 429

    customer = get_customer(license_key)
    if not customer or not customer["active"]:
        return jsonify({"valid": False}), 403

    if not device_id:
        return jsonify({"valid": False, "error": "device_id_required"}), 400

    bound, error_code = bind_device(license_key, device_id)
    if not bound:
        return jsonify({"valid": False, "error": error_code}), 409

    return jsonify({
        "valid": True,
        "customer_id": license_key,
        "customer_name": customer["customer_name"],
        "plan": customer["plan"],
        "unlocked_addons": json.loads(customer["unlocked_addons"] or "[]"),
        # Included at activation time (not just the next /v1/status poll)
        # so a customer who filled in the dashboard's identity field
        # before activating gets it applied immediately on `curant-cli
        # activate`, rather than waiting up to STATUS_RECHECK_SECONDS.
        "customer_apple_id": customer["customer_apple_id"],
        "customer_handles": json.loads(customer["customer_handles"] or "[]"),
        "preferences_updated_at": customer["preferences_updated_at"],
    })


@app.route("/v1/status", methods=["GET"])
def status():
    auth = request.headers.get("Authorization", "")
    license_key = auth.replace("Bearer ", "")
    device_id = request.args.get("device_id", "")

    if not check_rate_limit(license_key or "unknown"):
        return jsonify({"active": False, "error": "rate_limited"}), 429

    customer = get_customer(license_key)
    if not customer:
        return jsonify({"active": False}), 404

    # Multi-device aware: a mismatch is now "this device isn't one of the
    # license's bound devices" rather than "doesn't match the one device
    # on file" -- correct for both a normal 1-device customer and a
    # Grace customer with several bound devices. Only flagged as a
    # mismatch if the license actually HAS at least one bound device and
    # this one isn't among them (an unbound license with no devices yet
    # isn't a mismatch, just not-yet-activated).
    if device_id and count_bound_devices(license_key) > 0 and not is_device_bound_to_license(license_key, device_id):
        return jsonify({"active": False, "error": "device_mismatch"}), 409

    return jsonify({
        "active": bool(customer["active"]),
        "plan": customer["plan"],
        "unlocked_addons": json.loads(customer["unlocked_addons"] or "[]"),
        # Piggybacked onto this existing poll rather than a new endpoint --
        # see update_customer_preferences()'s docstring for why these three
        # fields (and only these three) are the one thing this server
        # deliberately does hold about a customer beyond billing basics.
        # preferences_updated_at is null until a customer has ever touched
        # the dashboard's preferences form, so curant-cli can tell "never
        # set via web, leave my local config alone" apart from a real update.
        "persona": customer["persona"],
        "instructions": customer["instructions"],
        "voice_tier": customer["voice_tier"],
        "customer_apple_id": customer["customer_apple_id"],
        "customer_handles": json.loads(customer["customer_handles"] or "[]"),
        "preferences_updated_at": customer["preferences_updated_at"],
    })


@app.route("/v1/gmail-oauth-config", methods=["GET"])
def gmail_oauth_config():
    """
    Hands out the shared Gmail OAuth client (see GMAIL_OAUTH_CLIENT_ID's
    comment above) to any activated customer, so their own curant-cli
    (see connect_email_cmd) can build a valid gcp-oauth.keys.json locally
    without them ever touching Google Cloud Console. This is app-level
    OAuth client config, not a per-customer secret and not message
    content -- same category as the license/billing basics this server
    already handles, not a departure from "server never sees message
    content."

    Gated on an active license (same auth pattern as /v1/status) purely
    to keep this from being a fully open, unauthenticated endpoint
    anyone could hit -- the client_id/secret pair for an installed/
    desktop-app OAuth client isn't treated as fully confidential by
    Google's own model (this is the same category of credential every
    CLI tool that does "sign in with Google" ships publicly), but there's
    no reason to serve it to a request with no license key at all either.
    """
    auth = request.headers.get("Authorization", "")
    license_key = auth.replace("Bearer ", "")

    if not check_rate_limit(license_key or "unknown"):
        return jsonify({"error": "rate_limited"}), 429

    customer = get_customer(license_key)
    if not customer or not customer["active"]:
        return jsonify({"error": "not_activated"}), 404

    if not GMAIL_OAUTH_CLIENT_ID or not GMAIL_OAUTH_CLIENT_SECRET:
        return jsonify({"error": "gmail_oauth_not_configured"}), 503

    return jsonify({
        "client_id": GMAIL_OAUTH_CLIENT_ID,
        "client_secret": GMAIL_OAUTH_CLIENT_SECRET,
        "project_id": GMAIL_OAUTH_PROJECT_ID,
    })


@app.route("/v1/usage-report", methods=["POST"])
def usage_report():
    """
    Reports a message COUNT since the last report — never content, never
    timestamps of individual messages, just "N more messages happened."
    Called by curant-cli piggybacked on its periodic status check, not on
    every message (that would leak a usage pattern/timing signal for no
    real benefit).
    """
    auth = request.headers.get("Authorization", "")
    license_key = auth.replace("Bearer ", "")

    if not check_rate_limit(license_key or "unknown"):
        return jsonify({"ok": False, "error": "rate_limited"}), 429

    customer = get_customer(license_key)
    if not customer:
        return jsonify({"ok": False}), 404

    data = request.get_json(force=True)
    count = data.get("message_count", 0)
    if not isinstance(count, int) or count < 0 or count > 10000:
        # Sanity bound — a bad/malicious client shouldn't be able to send
        # a garbage value that corrupts the running total.
        return jsonify({"ok": False, "error": "invalid_count"}), 400

    with closing(get_db()) as conn:
        conn.execute(
            "UPDATE customers SET total_messages = total_messages + ? WHERE license_key = ?",
            (count, license_key),
        )
        conn.commit()
    return jsonify({"ok": True})


VALID_ERROR_CODES = {
    "llm_call_failed", "no_api_key_set", "inactive_or_invalid_license",
    "watcher_crash", "chatdb_read_failed", "transcription_failed",
    "tts_failed", "attachment_not_found", "unexpected_watcher_error",
}
VALID_COMPONENTS = {"watcher", "curant-cli", "proactive-check"}


@app.route("/v1/error-report", methods=["POST"])
def error_report():
    """
    Records that a known, enumerated error happened — never the exception
    message, never a stack trace, never message content. This exists so a
    customer's broken setup can be noticed before they have to complain,
    not so we can debug the exact failure remotely.
    """
    auth = request.headers.get("Authorization", "")
    license_key = auth.replace("Bearer ", "")

    if not check_rate_limit(license_key or "unknown"):
        return jsonify({"ok": False, "error": "rate_limited"}), 429

    customer = get_customer(license_key)
    if not customer:
        return jsonify({"ok": False}), 404

    data = request.get_json(force=True)
    error_code = data.get("error_code", "")
    component = data.get("component", "")

    # Only accept codes/components from a known, closed set — this is the
    # enforcement mechanism that keeps this endpoint from ever becoming a
    # vector for free-text (and therefore potentially content-bearing) data.
    if error_code not in VALID_ERROR_CODES or component not in VALID_COMPONENTS:
        return jsonify({"ok": False, "error": "unrecognized_code_or_component"}), 400

    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO error_reports (license_key, error_code, component) VALUES (?, ?, ?)",
            (license_key, error_code, component),
        )
        conn.commit()
    return jsonify({"ok": True})


@app.route("/v1/stripe-webhook", methods=["POST"])
def stripe_webhook():
    """
    Real Stripe webhook -- provisions a customer and emails their license
    key automatically after a successful Checkout, no manual owner action
    needed (see owner_create_customer() for the manual fallback path this
    complements, not replaces).

    Signature verification happens BEFORE anything else touches the
    request body, using the raw bytes (request.get_data(), not
    request.get_json()) -- Stripe's signature is computed over the exact
    raw payload, and re-serializing parsed JSON can produce different
    bytes (key order, whitespace) that would fail verification even for
    a genuine Stripe request. This is what stops a malicious actor from
    POSTing a fake "payment succeeded" event and getting a free license.

    Idempotent by Stripe event id (see is_stripe_event_processed()) --
    Stripe redelivers events on timeout/non-200 responses, and can
    legitimately send the same event more than once even without an
    error on your end. Without this check, a redelivered
    checkout.session.completed would provision a second customer (and
    send a second license email) for the same payment.

    Returns 200 quickly on every recognized outcome (including "already
    processed" and "unhandled event type") -- Stripe treats a slow or
    non-200 response as failure and retries, so this deliberately never
    does slow work (like actually sending the email) before responding...
    except that it currently does, inline, synchronously. That's an
    honest limitation: send_license_email() making a real network call to
    SendGrid inside the request handler means a SendGrid slowdown could
    approach Stripe's timeout and trigger a retry (which the idempotency
    check makes SAFE, just not efficient). Moving email sending to a
    background thread/queue would fix this properly but is more
    infrastructure than this deployment has right now -- worth revisiting
    before real volume, not before.
    """
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET")

    if not webhook_secret:
        print("[stripe webhook] STRIPE_WEBHOOK_SECRET not set -- refusing to process "
              "(would mean accepting webhook requests with no way to verify they're really "
              "from Stripe).", file=sys.stderr)
        return jsonify({"error": "webhook_not_configured"}), 500

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except ValueError:
        return jsonify({"error": "invalid_payload"}), 400
    except stripe.error.SignatureVerificationError:
        return jsonify({"error": "invalid_signature"}), 400

    if is_stripe_event_processed(event["id"]):
        return jsonify({"ok": True, "status": "already_processed"}), 200

    if event["type"] == "checkout.session.completed":
        session_obj = event["data"]["object"]
        customer_details = session_obj.get("customer_details") or {}
        customer_name = customer_details.get("name") or "Customer"
        email = customer_details.get("email")

        plan, addons = plan_and_addons_for_checkout_session(session_obj["id"])
        license_key = provision_customer(customer_name, email=email, plan=plan)
        if addons:
            set_customer_addons(license_key, addons)

        if email:
            ok, error = send_license_email(customer_name, email, license_key)
            if not ok:
                print(f"[stripe webhook] provisioned {license_key} for {customer_name} but the "
                      f"license email failed ({error}) -- visible in the owner dashboard, but "
                      f"nothing was sent to the customer automatically. Relay it by hand.",
                      file=sys.stderr)
        else:
            print(f"[stripe webhook] provisioned {license_key} for {customer_name} but Stripe "
                  f"gave no email on the checkout session -- relay the key by hand.", file=sys.stderr)

        mark_stripe_event_processed(event["id"])
        return jsonify({"ok": True, "license_key": license_key}), 200

    # Any other event type is acknowledged, not an error -- e.g.
    # payment_method.attached, customer.updated, etc. that this server
    # doesn't act on yet. Returning 200 (not 4xx/5xx) tells Stripe not to
    # retry something there was never going to be a handler for.
    mark_stripe_event_processed(event["id"])
    return jsonify({"ok": True, "status": "unhandled_event_type"}), 200


def create_release_request(license_key):
    """
    Core logic for logging a release request, shared by the /v1/release-request
    API endpoint (used by curant-cli) and the customer web dashboard below.
    Does NOT release anything automatically — only logs the request.
    Existing pending requests aren't duplicated. Returns a dict shaped
    like the API response: {"ok", "status", "request_id"} or {"ok": False, "error"}.
    """
    customer = get_customer(license_key)
    if not customer:
        return {"ok": False, "error": "invalid_license"}

    with closing(get_db()) as conn:
        existing = conn.execute(
            "SELECT id FROM release_requests WHERE license_key = ? AND status = 'pending'",
            (license_key,),
        ).fetchone()
        if existing:
            return {"ok": True, "status": "already_pending", "request_id": existing["id"]}

        conn.execute(
            "INSERT INTO release_requests (license_key) VALUES (?)",
            (license_key,),
        )
        conn.commit()
        request_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    return {"ok": True, "status": "pending", "request_id": request_id}


def create_urgent_capability_gap(license_key, description):
    """
    Grace-exclusive: when a Grace customer's Curant hits something it
    can't do, this skips the normal "call for a custom quote" phone-
    number wall entirely and lands the gap directly in your owner
    dashboard, flagged urgent -- separate from the regular
    feature_requests queue, which is customer-initiated suggestions, not
    "I hit a wall just now" gaps. You still build it yourself (this
    never auto-writes or auto-deploys code -- see code_proposals for the
    separate, human-gated code-drafting feature); this only removes the
    phone-call step for Grace customers specifically.

    Silently no-ops (returns False) for a non-Grace license or an
    invalid one, rather than erroring -- curant-cli calls this best-
    effort and shouldn't surface a network/auth failure to the customer
    over something that already has a local fallback (the .xlsx export
    + local text notice still happen either way).
    """
    customer = get_customer(license_key)
    if not customer or not customer["active"]:
        return False
    if get_customer_august_tier(customer) != "grace":
        return False
    description = (description or "").strip()
    if not description:
        return False
    if len(description) > 2000:
        description = description[:2000]
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO urgent_capability_gaps (license_key, description) VALUES (?, ?)",
            (license_key, description),
        )
        conn.commit()
    return True


def list_urgent_capability_gaps():
    with closing(get_db()) as conn:
        rows = conn.execute(
            """SELECT urgent_capability_gaps.id, urgent_capability_gaps.license_key,
                      urgent_capability_gaps.description, urgent_capability_gaps.status,
                      urgent_capability_gaps.created_at, customers.customer_name
               FROM urgent_capability_gaps
               JOIN customers ON customers.license_key = urgent_capability_gaps.license_key
               ORDER BY urgent_capability_gaps.status ASC, urgent_capability_gaps.created_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]


def create_code_proposal(license_key, gap_description, proposed_diff):
    """
    Grace-exclusive. Stores a model-DRAFTED proposed implementation for
    manual owner review -- see propose_code_fix_for_gap's docstring in
    curant-cli for the full safety design. This function only ever
    writes a row to code_proposals with status='pending_review'; nothing
    here (or anywhere else in this file) reads proposed_diff and
    executes, applies, or merges it. The only thing that ever happens to
    that text is a human reading it on /owner/dashboard and deciding
    what to do about it themselves, entirely outside this system.
    """
    customer = get_customer(license_key)
    if not customer or not customer["active"]:
        return False
    if get_customer_august_tier(customer) != "grace":
        return False
    gap_description = (gap_description or "").strip()[:2000]
    proposed_diff = (proposed_diff or "").strip()[:8000]
    if not gap_description or not proposed_diff:
        return False
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO code_proposals (license_key, gap_description, proposed_diff) VALUES (?, ?, ?)",
            (license_key, gap_description, proposed_diff),
        )
        conn.commit()
    return True


def list_code_proposals():
    with closing(get_db()) as conn:
        rows = conn.execute(
            """SELECT code_proposals.id, code_proposals.license_key, code_proposals.gap_description,
                      code_proposals.proposed_diff, code_proposals.status, code_proposals.created_at,
                      customers.customer_name
               FROM code_proposals
               JOIN customers ON customers.license_key = code_proposals.license_key
               ORDER BY (code_proposals.status = 'pending_review') DESC, code_proposals.created_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]


@app.route("/v1/propose-code-fix", methods=["POST"])
def submit_code_proposal():
    """
    Grace-exclusive endpoint -- accepts a model-drafted proposal and
    stores it, inert, for manual review. See create_code_proposal's
    docstring: nothing here ever executes or applies proposed_diff.
    """
    auth = request.headers.get("Authorization", "")
    license_key = auth.replace("Bearer ", "")

    if not check_rate_limit(f"code-proposal:{license_key or 'unknown'}"):
        return jsonify({"ok": False, "error": "rate_limited"}), 429

    data = request.get_json(force=True)
    created = create_code_proposal(license_key, data.get("gap_description", ""), data.get("proposed_diff", ""))
    if not created:
        return jsonify({"ok": False}), 403
    return jsonify({"ok": True})


@app.route("/v1/urgent-capability-gap", methods=["POST"])
def submit_urgent_capability_gap():
    """
    Grace-exclusive endpoint -- called by curant-cli's log_capability_gap
    instead of (in addition to, really -- the local logging still always
    happens) telling the customer to call for a quote. Auth is the same
    Bearer-license-key pattern as every other customer-facing endpoint.
    """
    auth = request.headers.get("Authorization", "")
    license_key = auth.replace("Bearer ", "")

    if not check_rate_limit(f"urgent-gap:{license_key or 'unknown'}"):
        return jsonify({"ok": False, "error": "rate_limited"}), 429

    data = request.get_json(force=True)
    description = data.get("description", "")

    created = create_urgent_capability_gap(license_key, description)
    if not created:
        # Deliberately vague (invalid license vs. wrong tier vs. empty
        # description all collapse to the same response) -- this is an
        # internal best-effort call from curant-cli, not something a
        # customer directly sees the raw error from.
        return jsonify({"ok": False}), 403
    return jsonify({"ok": True})


@app.route("/v1/release-request", methods=["POST"])
def submit_release_request():
    """
    Logs a request to release this license's device binding — does NOT
    release it automatically. This is deliberately not self-serve: the
    customer submits a request here (or calls support directly), and an
    admin reviews and approves it via approve_release_request() below.
    Existing pending requests aren't duplicated — resubmitting just
    confirms the existing one rather than piling up duplicates.
    """
    data = request.get_json(force=True)
    license_key = data.get("license_key", "")

    if not check_rate_limit(license_key or "unknown"):
        return jsonify({"ok": False, "error": "rate_limited"}), 429

    result = create_release_request(license_key)
    status_code = 200 if result.get("ok") else 404
    return jsonify(result), status_code


# --- Admin-only functions (not exposed as endpoints — call directly, e.g.
# from a Python shell or a future admin tool, once you've verified the
# request by phone/support contact as intended) ---

def list_pending_release_requests():
    """Admin helper: see what's waiting on a decision."""
    with closing(get_db()) as conn:
        rows = conn.execute(
            """SELECT release_requests.id, release_requests.license_key,
                      customers.customer_name, customers.phone_number,
                      release_requests.requested_at
               FROM release_requests
               JOIN customers ON customers.license_key = release_requests.license_key
               WHERE release_requests.status = 'pending'
               ORDER BY release_requests.requested_at"""
        ).fetchall()
        return [dict(r) for r in rows]


def approve_release_request(request_id):
    """Admin action: actually clears the device binding, freeing the
    license to activate on a new Mac. Only call this after verifying the
    request is legitimate (the whole point of requiring a call/request
    instead of automatic self-serve)."""
    with closing(get_db()) as conn:
        req = conn.execute(
            "SELECT license_key, status FROM release_requests WHERE id = ?", (request_id,)
        ).fetchone()
        if req is None:
            return False, "request_not_found"
        if req["status"] != "pending":
            return False, f"request_already_{req['status']}"

        # Releases ALL of this license's bound devices, not just one --
        # for a normal 1-device customer this is identical to the old
        # behavior; for a Grace customer with several devices bound, a
        # release request clears the whole set and they re-activate
        # whichever ones they want, up to their limit again. Approving a
        # release is already a manual, reviewed action (see this
        # function's docstring) so clearing everything rather than
        # guessing which one device was meant is the safer default.
        conn.execute("DELETE FROM licensed_devices WHERE license_key = ?", (req["license_key"],))
        conn.execute(
            "UPDATE customers SET device_id = NULL WHERE license_key = ?",
            (req["license_key"],),
        )
        conn.execute(
            "UPDATE release_requests SET status = 'approved', resolved_at = CURRENT_TIMESTAMP WHERE id = ?",
            (request_id,),
        )
        conn.commit()
    return True, None


def deny_release_request(request_id):
    """Admin action: mark a request denied without releasing anything."""
    with closing(get_db()) as conn:
        req = conn.execute(
            "SELECT status FROM release_requests WHERE id = ?", (request_id,)
        ).fetchone()
        if req is None:
            return False, "request_not_found"
        if req["status"] != "pending":
            return False, f"request_already_{req['status']}"

        conn.execute(
            "UPDATE release_requests SET status = 'denied', resolved_at = CURRENT_TIMESTAMP WHERE id = ?",
            (request_id,),
        )
        conn.commit()
    return True, None


# --- Web UI: customer login/dashboard, and a separately-gated owner page ---
# Both use Flask's signed session cookies (see app.secret_key above).
# Jinja autoescaping is on by default for render_template_string (Flask
# enables it whenever the template has no filename), so customer-supplied
# values like customer_name are safe to interpolate directly — no manual
# escaping needed.

BASE_STYLE = """
<style>
  body { font-family: -apple-system, sans-serif; max-width: 640px; margin: 60px auto; padding: 0 20px; color: #1a1a1a; }
  h1 { font-size: 1.4rem; }
  .card { border: 1px solid #ddd; border-radius: 8px; padding: 20px; margin: 16px 0; }
  .row { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid #eee; }
  .row:last-child { border-bottom: none; }
  .label { color: #666; }
  input[type=text], input[type=password] { width: 100%; padding: 8px; margin: 8px 0; box-sizing: border-box; }
  button { padding: 8px 16px; cursor: pointer; }
  .error { color: #b00020; }
  .muted { color: #888; font-size: 0.9rem; }
  table { width: 100%; border-collapse: collapse; margin: 12px 0; }
  td, th { text-align: left; padding: 6px 8px; border-bottom: 1px solid #eee; font-size: 0.9rem; }
  form.inline { display: inline; }
</style>
"""


def get_csrf_token():
    """Generated once per session, reused for every form in it. Prevents
    a malicious third-party page from submitting actions (like approving
    a release request) on behalf of a logged-in owner/customer without
    their knowledge."""
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]


def validate_csrf():
    submitted = request.form.get("csrf_token", "")
    return secrets.compare_digest(submitted, session.get("csrf_token", ""))


def require_customer_login(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("license_key"):
            return redirect(url_for("customer_login"))
        return view(*args, **kwargs)
    return wrapped


def require_admin(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("owner_page"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def customer_login():
    error = None
    if request.method == "POST":
        client_ip = request.remote_addr or "unknown"
        if not check_login_rate_limit(f"customer:{client_ip}"):
            error = "Too many attempts. Try again in a few minutes."
        elif not validate_csrf():
            error = "Session expired — please try again."
        else:
            license_key = request.form.get("license_key", "").strip()
            customer = get_customer(license_key)
            if customer and customer["active"]:
                session["license_key"] = license_key
                return redirect(url_for("customer_dashboard"))
            error = "That license key isn't valid or isn't active."

    return render_template_string(
        BASE_STYLE + """
        <h1>Curant Account Login</h1>
        <div class="card">
          {% if error %}<p class="error">{{ error }}</p>{% endif %}
          <form method="post">
            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
            <label>License key</label>
            <input type="text" name="license_key" placeholder="CRT-XXXX-XXXX-XXXX" required autofocus>
            <button type="submit">Log in</button>
          </form>
        </div>
        <p class="muted"><a href="/owner">Owner login</a></p>
        """,
        error=error, csrf_token=get_csrf_token(),
    )


@app.route("/logout")
def customer_logout():
    session.pop("license_key", None)
    return redirect(url_for("customer_login"))


@app.route("/dashboard", methods=["GET", "POST"])
@require_customer_login
def customer_dashboard():
    license_key = session["license_key"]
    customer = get_customer(license_key)
    if not customer:
        # License was removed/invalidated after login — don't trust the
        # stale session further.
        session.pop("license_key", None)
        return redirect(url_for("customer_login"))

    message = None
    if request.method == "POST":
        if not validate_csrf():
            message = "Session expired — please try again."
        else:
            result = create_release_request(license_key)
            if result.get("status") == "already_pending":
                message = "You already have a release request pending review."
            elif result.get("ok"):
                message = "Release request submitted — a person will review it, this doesn't happen automatically."
            else:
                message = "Couldn't submit the request. Contact support directly."

    with closing(get_db()) as conn:
        pending = conn.execute(
            "SELECT id FROM release_requests WHERE license_key = ? AND status = 'pending'",
            (license_key,),
        ).fetchone()

    unlocked = json.loads(customer["unlocked_addons"] or "[]")
    addons_view = [
        {"id": addon_id, **info, "unlocked": addon_id in unlocked}
        for addon_id, info in ADDON_CATALOG.items()
    ]

    return render_template_string(
        BASE_STYLE + """
        <h1>Welcome, {{ customer.customer_name }}</h1>
        <div class="card">
          <div class="row"><span class="label">Plan</span><span>{{ customer.plan }}</span></div>
          <div class="row"><span class="label">Status</span><span>{{ "Active" if customer.active else "Inactive" }}</span></div>
          <div class="row"><span class="label">Device</span><span>{{ "Bound to a Mac" if customer.device_id else "Not yet activated on a Mac" }}</span></div>
          <div class="row"><span class="label">Activated</span><span>{{ customer.created_at or "-" }}</span></div>
        </div>

        <div class="card">
          <h2 style="font-size:1.1rem;">Preferences</h2>
          <p class="muted">Sets what Curant picks up on this Mac — it never sees what Curant has
          learned about you or the people in your life, only these three choices.
          Changes are picked up next time curant-cli checks in (up to a few hours,
          not instant).</p>
          {% if prefs_message %}<p class="muted">{{ prefs_message }}</p>{% endif %}
          <form method="post" action="/dashboard/preferences">
            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
            <label>Persona</label>
            <select name="persona" style="width:100%; padding:8px; margin:8px 0;">
              {% for p in persona_choices %}
              <option value="{{ p }}" {% if customer.persona == p %}selected{% endif %}>{{ p|capitalize }}</option>
              {% endfor %}
            </select>
            <label>Standing instructions</label>
            <textarea name="instructions" rows="4" style="width:100%; padding:8px; margin:8px 0; box-sizing:border-box; font-family:inherit;" placeholder="e.g. Always confirm before sending anything to my mom.">{{ customer.instructions or "" }}</textarea>
            <label>Voice reply tier</label>
            <select name="voice_tier" style="width:100%; padding:8px; margin:8px 0;">
              {% for v in voice_tier_choices %}
              <option value="{{ v }}" {% if customer.voice_tier == v %}selected{% endif %}>{{ v|capitalize }}</option>
              {% endfor %}
            </select>
            <label>Your phone number or Apple ID</label>
            <input type="text" name="customer_apple_id" style="width:100%; padding:8px; margin:8px 0; box-sizing:border-box;" placeholder="e.g. +1 555 123 4567 or you@icloud.com" value="{{ customer.customer_apple_id or '' }}">
            <p class="muted" style="margin-top:-4px;">The number or email Curant listens for messages/calls from on your Mac.</p>
            <label>Extra handles (optional)</label>
            <input type="text" name="customer_handles" style="width:100%; padding:8px; margin:8px 0; box-sizing:border-box;" placeholder="Comma-separated, e.g. a second email or number" value="{{ customer_handles_display }}">
            <button type="submit">Save preferences</button>
          </form>
        </div>

        <div class="card">
          <h2 style="font-size:1.1rem;">Add-ons</h2>
          {% for addon in addons %}
          <div class="row">
            <span class="label">{{ addon.label }}</span>
            <span>
              {% if addon.unlocked %}
                Active
              {% elif addon.stripe_payment_link %}
                <a href="{{ addon.stripe_payment_link }}">Unlock</a>
              {% else %}
                <span class="muted">Contact support to add this</span>
              {% endif %}
            </span>
          </div>
          {% endfor %}
        </div>

        <div class="card">
          <h2 style="font-size:1.1rem;">Feature requests</h2>
          <p class="muted">Something Curant can't do yet that you wish it could? Tell us here —
          this is separate from the automatic capability-gap log on your own Mac; this one, we
          can actually see.</p>
          {% if feature_message %}<p class="muted">{{ feature_message }}</p>{% endif %}
          <form method="post" action="/dashboard/feature-request">
            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
            <textarea name="feature_message" rows="3" style="width:100%; padding:8px; margin:8px 0; box-sizing:border-box; font-family:inherit;" placeholder="What do you wish Curant could do?"></textarea>
            <button type="submit">Submit</button>
          </form>
          {% if my_feature_requests %}
          <table>
            <tr><th>Submitted</th><th>Message</th><th>Status</th></tr>
            {% for r in my_feature_requests %}
            <tr><td>{{ r.created_at }}</td><td>{{ r.message }}</td><td>{{ r.status }}</td></tr>
            {% endfor %}
          </table>
          {% endif %}
        </div>

        <div class="card">
          <p>If you've moved to a new Mac and need this license released from the old one:</p>
          {% if pending %}
            <p class="muted">You have a release request pending review.</p>
          {% else %}
            <form method="post">
              <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
              <button type="submit">Request device release</button>
            </form>
          {% endif %}
          {% if message %}<p class="muted">{{ message }}</p>{% endif %}
          <p class="muted">This is reviewed by a person before anything changes — it's not automatic.</p>
        </div>
        <p><a href="/logout">Log out</a></p>
        """,
        customer=customer, pending=pending, message=message, csrf_token=get_csrf_token(),
        addons=addons_view, persona_choices=PERSONA_CHOICES, voice_tier_choices=VOICE_TIER_CHOICES,
        prefs_message=request.args.get("prefs_message"), feature_message=request.args.get("feature_message"),
        my_feature_requests=list_feature_requests_for(license_key),
        customer_handles_display=", ".join(json.loads(customer["customer_handles"] or "[]")),
    )


@app.route("/dashboard/preferences", methods=["POST"])
@require_customer_login
def dashboard_update_preferences():
    license_key = session["license_key"]
    if not validate_csrf():
        return redirect(url_for("customer_dashboard", prefs_message="Session expired — please try again."))

    persona = request.form.get("persona", "curant")
    instructions = request.form.get("instructions", "")
    voice_tier = request.form.get("voice_tier", "standard")
    customer_apple_id = request.form.get("customer_apple_id", "")
    customer_handles = [h.strip() for h in request.form.get("customer_handles", "").split(",") if h.strip()]
    ok, error = update_customer_preferences(
        license_key, persona, instructions, voice_tier,
        customer_apple_id=customer_apple_id, customer_handles=customer_handles,
    )
    if ok:
        msg = "Saved — Curant will pick this up next time it checks in with the server."
    else:
        msg = f"Couldn't save ({error})."
    return redirect(url_for("customer_dashboard", prefs_message=msg))


@app.route("/dashboard/feature-request", methods=["POST"])
@require_customer_login
def dashboard_submit_feature_request():
    license_key = session["license_key"]
    if not validate_csrf():
        return redirect(url_for("customer_dashboard", feature_message="Session expired — please try again."))

    ok, error = create_feature_request(license_key, request.form.get("feature_message", ""))
    if ok:
        msg = "Submitted — thanks, we'll take a look."
    else:
        msg = "Couldn't submit that (it looked empty or too long)."
    return redirect(url_for("customer_dashboard", feature_message=msg))


@app.route("/owner", methods=["GET", "POST"])
def owner_page():
    if session.get("is_admin"):
        return redirect(url_for("owner_dashboard"))

    error = None
    if request.method == "POST":
        client_ip = request.remote_addr or "unknown"
        if not ADMIN_PASSWORD:
            error = "Owner login is disabled — CURANT_ADMIN_PASSWORD isn't set on the server."
        elif not check_login_rate_limit(f"owner:{client_ip}"):
            error = "Too many attempts. Try again in a few minutes."
        elif not validate_csrf():
            error = "Session expired — please try again."
        else:
            submitted = request.form.get("password", "")
            # compare_digest prevents a timing attack from being able to
            # guess the password one byte at a time via response latency.
            if secrets.compare_digest(submitted, ADMIN_PASSWORD):
                session["is_admin"] = True
                return redirect(url_for("owner_dashboard"))
            error = "Incorrect password."

    return render_template_string(
        BASE_STYLE + """
        <h1>Owner Login</h1>
        <div class="card">
          {% if error %}<p class="error">{{ error }}</p>{% endif %}
          <form method="post">
            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
            <input type="password" name="password" placeholder="Owner password" required autofocus>
            <button type="submit">Log in</button>
          </form>
        </div>
        """,
        error=error, csrf_token=get_csrf_token(),
    )


@app.route("/owner/logout")
def owner_logout():
    session.pop("is_admin", None)
    return redirect(url_for("owner_page"))


@app.route("/owner/dashboard")
@require_admin
def owner_dashboard():
    pending = list_pending_release_requests()
    with closing(get_db()) as conn:
        customers = conn.execute(
            "SELECT license_key, customer_name, email, plan, active, device_id, total_messages, "
            "unlocked_addons, persona, created_at FROM customers ORDER BY created_at DESC"
        ).fetchall()
    customers = [dict(c) for c in customers]
    for c in customers:
        c["addons_display"] = ", ".join(json.loads(c["unlocked_addons"] or "[]")) or "—"

    feature_requests = list_all_feature_requests()
    urgent_gaps = list_urgent_capability_gaps()
    code_proposals = list_code_proposals()

    return render_template_string(
        BASE_STYLE + """
        <h1>Owner Dashboard</h1>

        <div class="card">
          <h2 style="font-size:1.1rem;">Add a customer</h2>
          <p class="muted">Manual provisioning -- generates a license key and, if an
          email is given and SendGrid is configured, sends it automatically. This is
          separate from (and doesn't require) the Stripe webhook -- useful for
          testing delivery, or provisioning by hand until that's wired up.</p>
          {% if create_message %}<p class="muted">{{ create_message }}</p>{% endif %}
          <form method="post" action="/owner/create-customer">
            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
            <label>Name</label>
            <input type="text" name="customer_name" required>
            <label>Email</label>
            <input type="text" name="email" placeholder="optional, but required to send the license by email">
            <label>Phone</label>
            <input type="text" name="phone_number" placeholder="optional">
            <label>Plan</label>
            <input type="text" name="plan" value="base">
            <button type="submit">Create + email license</button>
          </form>
        </div>

        <div class="card">
          <h2 style="font-size:1.1rem;">Pending device release requests</h2>
          {% if pending %}
          <table>
            <tr><th>Customer</th><th>Phone</th><th>Requested</th><th></th></tr>
            {% for r in pending %}
            <tr>
              <td>{{ r.customer_name }}</td>
              <td>{{ r.phone_number or "—" }}</td>
              <td>{{ r.requested_at }}</td>
              <td>
                <form class="inline" method="post" action="/owner/approve/{{ r.id }}">
                  <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                  <button type="submit">Approve</button>
                </form>
                <form class="inline" method="post" action="/owner/deny/{{ r.id }}">
                  <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                  <button type="submit">Deny</button>
                </form>
              </td>
            </tr>
            {% endfor %}
          </table>
          {% else %}
          <p class="muted">None right now.</p>
          {% endif %}
        </div>

        <div class="card">
          <h2 style="font-size:1.1rem;">Pending code proposals (Grace)</h2>
          <p class="muted">Model-drafted proposals for capability gaps Grace customers hit. These
          are DRAFTS ONLY -- nothing here is ever automatically merged, applied, or deployed.
          "Approve" just marks it reviewed and worth building; you still implement and ship it
          yourself, the normal way.</p>
          {% if code_proposals %}
          {% for p in code_proposals %}
          <div style="border:1px solid var(--border); border-radius:6px; padding:0.75rem; margin-bottom:0.75rem;">
            <p><strong>{{ p.customer_name }}</strong> asked for: {{ p.gap_description }}</p>
            <pre style="white-space:pre-wrap; font-size:0.85rem; background:var(--bg-subtle); padding:0.5rem; border-radius:4px; max-height:300px; overflow:auto;">{{ p.proposed_diff }}</pre>
            <p class="muted">Status: {{ p.status }} — {{ p.created_at }}</p>
            {% if p.status == "pending_review" %}
            <form class="inline" method="post" action="/owner/code-proposal/{{ p.id }}/approved">
              <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
              <button type="submit">Mark reviewed / worth building</button>
            </form>
            <form class="inline" method="post" action="/owner/code-proposal/{{ p.id }}/rejected">
              <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
              <button type="submit">Reject</button>
            </form>
            {% endif %}
          </div>
          {% endfor %}
          {% else %}
          <p class="muted">None yet.</p>
          {% endif %}
        </div>

        <div class="card">
          <h2 style="font-size:1.1rem;">Urgent (Grace)</h2>
          <p class="muted">Capability gaps from Grace customers -- these skip the "call for a
          quote" step and land here directly.</p>
          {% if urgent_gaps %}
          <table>
            <tr><th>Customer</th><th>What they needed</th><th>Status</th><th>Hit</th><th></th></tr>
            {% for g in urgent_gaps %}
            <tr>
              <td>{{ g.customer_name }}</td>
              <td>{{ g.description }}</td>
              <td>{{ g.status }}</td>
              <td>{{ g.created_at }}</td>
              <td>
                {% if g.status != "resolved" %}
                <form class="inline" method="post" action="/owner/urgent-gap/{{ g.id }}/resolve">
                  <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                  <button type="submit">Mark resolved</button>
                </form>
                {% endif %}
              </td>
            </tr>
            {% endfor %}
          </table>
          {% else %}
          <p class="muted">None yet.</p>
          {% endif %}
        </div>

        <div class="card">
          <h2 style="font-size:1.1rem;">Feature requests</h2>
          {% if feature_requests %}
          <table>
            <tr><th>Customer</th><th>Request</th><th>Status</th><th>Submitted</th><th></th></tr>
            {% for r in feature_requests %}
            <tr>
              <td>{{ r.customer_name }}{% if r.is_priority %} <span title="Grace customer -- priority">&#9733;</span>{% endif %}</td>
              <td>{{ r.message }}</td>
              <td>{{ r.status }}</td>
              <td>{{ r.created_at }}</td>
              <td>
                {% if r.status != "reviewed" %}
                <form class="inline" method="post" action="/owner/feature-request/{{ r.id }}/reviewed">
                  <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                  <button type="submit">Mark reviewed</button>
                </form>
                {% endif %}
                {% if r.status != "done" %}
                <form class="inline" method="post" action="/owner/feature-request/{{ r.id }}/done">
                  <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                  <button type="submit">Mark done</button>
                </form>
                {% endif %}
              </td>
            </tr>
            {% endfor %}
          </table>
          {% else %}
          <p class="muted">None right now.</p>
          {% endif %}
        </div>

        <div class="card">
          <h2 style="font-size:1.1rem;">Activated licenses</h2>
          <table>
            <tr><th>Name</th><th>Email</th><th>Plan</th><th>Persona</th><th>Add-ons</th><th>Active</th><th>Device bound</th><th>Activated</th><th>Messages</th></tr>
            {% for c in customers %}
            <tr>
              <td>{{ c.customer_name }}</td>
              <td>{{ c.email or "—" }}</td>
              <td>{{ c.plan }}</td>
              <td>{{ c.persona }}</td>
              <td>{{ c.addons_display }}</td>
              <td>{{ "Yes" if c.active else "No" }}</td>
              <td>{{ "Yes" if c.device_id else "No" }}</td>
              <td>{{ c.created_at }}</td>
              <td>{{ c.total_messages }}</td>
            </tr>
            {% endfor %}
          </table>
        </div>

        <p><a href="/owner/logout">Log out</a></p>
        """,
        pending=pending, customers=customers, feature_requests=feature_requests, urgent_gaps=urgent_gaps, code_proposals=code_proposals, csrf_token=get_csrf_token(),
        create_message=request.args.get("create_message"),
    )


@app.route("/owner/create-customer", methods=["POST"])
@require_admin
def owner_create_customer():
    """
    Manual provisioning path for the owner -- generates a license key
    the same way Stripe's eventual webhook will, and immediately shows
    it in the flash message REGARDLESS of whether the email send
    succeeds (see send_license_email()'s docstring on why this is
    deliberately best-effort) -- so a bad SendGrid key or a customer
    who didn't give an email never means the key is lost, just that it
    has to be relayed by hand.
    """
    if not validate_csrf():
        return redirect(url_for("owner_dashboard", create_message="Session expired — please try again."))

    customer_name = request.form.get("customer_name", "").strip()
    email = request.form.get("email", "").strip() or None
    phone_number = request.form.get("phone_number", "").strip() or None
    plan = request.form.get("plan", "base").strip() or "base"

    if not customer_name:
        return redirect(url_for("owner_dashboard", create_message="Name is required — nothing created."))

    license_key = provision_customer(customer_name, email=email, phone_number=phone_number, plan=plan)

    if email:
        ok, error = send_license_email(customer_name, email, license_key)
        if ok:
            msg = f"Created {license_key} for {customer_name} — license emailed to {email}."
        else:
            msg = f"Created {license_key} for {customer_name} — email NOT sent ({error}). Relay the key by hand."
    else:
        msg = f"Created {license_key} for {customer_name} — no email on file, relay the key by hand."

    return redirect(url_for("owner_dashboard", create_message=msg))


@app.route("/owner/feature-request/<int:request_id>/<status>", methods=["POST"])
@require_admin
def owner_set_feature_request_status(request_id, status):
    if not validate_csrf():
        return redirect(url_for("owner_dashboard"))
    set_feature_request_status(request_id, status)
    return redirect(url_for("owner_dashboard"))


@app.route("/owner/urgent-gap/<int:gap_id>/resolve", methods=["POST"])
@require_admin
def owner_resolve_urgent_gap(gap_id):
    if not validate_csrf():
        return redirect(url_for("owner_dashboard"))
    with closing(get_db()) as conn:
        conn.execute("UPDATE urgent_capability_gaps SET status = 'resolved' WHERE id = ?", (gap_id,))
        conn.commit()
    return redirect(url_for("owner_dashboard"))


@app.route("/owner/code-proposal/<int:proposal_id>/<status>", methods=["POST"])
@require_admin
def owner_set_code_proposal_status(proposal_id, status):
    """
    status is either 'approved' or 'rejected' -- IMPORTANT: this ONLY
    updates a status column for display/tracking on this dashboard. It
    does not merge, apply, deploy, or execute proposed_diff anywhere,
    under any circumstance. Actually building an approved proposal is a
    separate, entirely manual action you take yourself (read the draft,
    write real code, test it, commit it) -- exactly like every other
    feature in this codebase has been built throughout this project.
    """
    if status not in ("approved", "rejected"):
        return redirect(url_for("owner_dashboard"))
    if not validate_csrf():
        return redirect(url_for("owner_dashboard"))
    with closing(get_db()) as conn:
        conn.execute(
            "UPDATE code_proposals SET status = ?, reviewed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, proposal_id),
        )
        conn.commit()
    return redirect(url_for("owner_dashboard"))


@app.route("/owner/approve/<int:request_id>", methods=["POST"])
@require_admin
def owner_approve(request_id):
    if not validate_csrf():
        return redirect(url_for("owner_dashboard"))
    approve_release_request(request_id)
    return redirect(url_for("owner_dashboard"))


@app.route("/owner/deny/<int:request_id>", methods=["POST"])
@require_admin
def owner_deny(request_id):
    if not validate_csrf():
        return redirect(url_for("owner_dashboard"))
    deny_release_request(request_id)
    return redirect(url_for("owner_dashboard"))


def _open_browser_once_server_is_up(port):
    """
    Auto-opens the customer portal (/login) in the default browser shortly
    after startup -- purely a local-dev convenience (this is why it's
    gated on __name__ == "__main__" below, never runs when a real WSGI
    server imports this module for production). Runs on a background
    thread with a short delay rather than opening immediately, since the
    Flask dev server needs a moment to actually be listening -- opening
    instantly risks a connection-refused page loading before the server's
    ready.

    Deliberately /login, not /owner: the person running `python app.py`
    day to day is almost always simulating or testing the customer
    experience, not doing owner admin work -- someone who actually needs
    the owner dashboard knows to navigate to /owner directly.

    Flask's debug-mode reloader forks a second process (the one that
    actually serves requests); WERKZEUG_RUN_MAIN is only set in that
    child, not in the initial parent process, so gating on it here stops
    the browser from popping open twice on every debug-mode restart.
    """
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true" and app.debug:
        return
    import threading
    import webbrowser

    def _open():
        webbrowser.open(f"http://localhost:{port}/login")

    threading.Timer(1.0, _open).start()


if __name__ == "__main__":
    init_db()
    PORT = 5050
    # Debug mode (reloader + auto-browser-open) is a manual-run convenience,
    # not appropriate for an unattended background service -- set
    # CURANT_SERVER_DEBUG=false when this runs under launchd (see
    # mac/com.curant.server.plist) so a crash doesn't spin up a duplicate
    # reloader child that launchd's KeepAlive then loses track of, and so
    # it doesn't try to pop open a browser window on every restart.
    debug_mode = os.environ.get("CURANT_SERVER_DEBUG", "true").lower() == "true"
    print("")
    print("Curant server starting -- open one of these once it's up:")
    print(f"  Owner dashboard:    http://localhost:{PORT}/owner")
    print(f"  Customer dashboard: http://localhost:{PORT}/login")
    print("")
    if debug_mode:
        _open_browser_once_server_is_up(PORT)
    app.run(port=PORT, debug=debug_mode)
