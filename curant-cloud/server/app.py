"""
Curant Cloud — hosted AI Secretary via SMS + Voice.

Architecture:
  - Customers text/call a Telnyx phone number
  - This server handles webhooks, runs the Curant brain, replies via Telnyx
  - API key storage is dual-mode, customer's choice:
      Option A: encrypted server-side (Fernet + scrypt), Curant answers
                immediately including proactive check-ins
      Option B: browser-held (Web Crypto in the web UI), server stores only
                ciphertext it can't read — customer unlocks once per session
                by visiting a link; proactive check-ins require an active session
  - Memory, personas, routing rules, routing log all stored per-customer
    in the same SQLite schema as Curant Home (same brain, different channel)
  - Docker-friendly: all config via environment variables, no Mac/iMessage deps
  - Database is encrypted at rest with SQLCipher (AES-256), so a raw copy
    of the DB file is unreadable without the key

Environment variables required:
  CLOUD_SECRET_KEY        — Flask session signing key (persistent, random)
  CLOUD_DB_KEY            — SQLCipher database encryption key (AES-256, at-rest
                            encryption for the entire DB file — generate with:
                            python3 -c "import secrets; print(secrets.token_hex(32))")
  CLOUD_ENCRYPTION_KEY    — Fernet key for Option A server-side key storage
                            (generate: python3 -c "from cryptography.fernet
                             import Fernet; print(Fernet.generate_key().decode())")
  CLOUD_ADMIN_PASSWORD    — owner dashboard password
  TELNYX_API_KEY          — your Telnyx API key (for provisioning/sending SMS)
  TELNYX_WEBHOOK_SECRET   — Telnyx webhook signature secret (for verifying
                            incoming webhooks are really from Telnyx)
  VAPI_API_KEY            — Vapi API key for voice prototype (optional)
  DATABASE_PATH           — path to SQLite DB (default: curant_cloud.db)
"""

import os
import re
import sys
import json
import hmac
import hashlib
import secrets
import sqlite3
import time
import threading
from contextlib import closing
from functools import wraps
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from flask import (Flask, request, jsonify, session, redirect,
                   url_for, render_template_string, abort, Response)
import requests as http

# ── App setup ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
DB_PATH = os.environ.get("DATABASE_PATH", "curant_cloud.db")

# Database encryption key (SQLCipher AES-256, at-rest encryption for the entire DB file)
# Generate: python3 -c "import secrets; print(secrets.token_hex(32))"
DB_KEY = os.environ.get("CLOUD_DB_KEY")
if not DB_KEY:
    print("WARNING: CLOUD_DB_KEY not set — database is NOT encrypted at rest. "
          "Set this before any real customer data is stored.",
          file=sys.stderr)

# Session signing
_secret = os.environ.get("CLOUD_SECRET_KEY")
if not _secret:
    print("WARNING: CLOUD_SECRET_KEY not set — using temporary key, all sessions "
          "will be lost on restart.", file=sys.stderr)
    _secret = secrets.token_hex(32)
app.secret_key = _secret
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("CLOUD_HTTPS", "").lower() == "true",
)

# Server-side encryption (Option A customers only)
_enc_key_raw = os.environ.get("CLOUD_ENCRYPTION_KEY")
if _enc_key_raw:
    _fernet = Fernet(_enc_key_raw.encode())
else:
    print("WARNING: CLOUD_ENCRYPTION_KEY not set — Option A key storage will "
          "fail. Generate one with: python3 -c \"from cryptography.fernet import "
          "Fernet; print(Fernet.generate_key().decode())\"", file=sys.stderr)
    _fernet = None

TELNYX_API_KEY        = os.environ.get("TELNYX_API_KEY", "")
TELNYX_WEBHOOK_SECRET = os.environ.get("TELNYX_WEBHOOK_SECRET", "")
VAPI_API_KEY          = os.environ.get("VAPI_API_KEY", "")
# This server's own public URL — needed so Vapi's Custom LLM provider
# knows where to send voice-call LLM requests back to us (see the
# vapi_custom_llm() route). Without this set correctly, voice calls
# will fail for any customer, since Vapi can't reach a localhost URL.
CLOUD_PUBLIC_URL      = os.environ.get("CLOUD_PUBLIC_URL", "http://localhost:5051")
ADMIN_PASSWORD        = os.environ.get("CLOUD_ADMIN_PASSWORD")

TELNYX_API_BASE = "https://api.telnyx.com/v2"
TELNYX_MSG_BASE = "https://api.telnyx.com/v2/messages"

# ── Rate limiting — real implementation is below get_db(), since it
# needs the shared database connection to work across worker processes.

# ── Database ───────────────────────────────────────────────────────────────────

def get_db():
    """
    Returns a database connection. Uses SQLCipher (AES-256 encryption of
    the entire DB file) when CLOUD_DB_KEY is set; falls back to plain
    sqlite3 when it isn't (development only — never acceptable in
    production where real customer data lives). The rest of the codebase
    never knows which one it's talking to: same API, same row_factory,
    same PRAGMAs either way.
    """
    if DB_KEY:
        import sqlcipher3 as _sqlite
    else:
        import sqlite3 as _sqlite

    conn = _sqlite.connect(DB_PATH)
    if DB_KEY:
        # The key PRAGMA must be the very first thing sent to the connection —
        # any other command before this will fail or, worse, create a plaintext DB.
        conn.execute(f"PRAGMA key='{DB_KEY}'")
    conn.row_factory = _sqlite.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with closing(get_db()) as conn:
        conn.executescript("""
        -- One row per Cloud customer
        CREATE TABLE IF NOT EXISTS customers (
            id              TEXT PRIMARY KEY,           -- UUID
            name            TEXT,
            email           TEXT UNIQUE,
            phone_number    TEXT UNIQUE,                -- Telnyx DID assigned to them
            phone_sid       TEXT,                       -- Telnyx phone number ID (for release)
            workspace_email TEXT,                       -- Curant's utility email for this customer (account signups)
            workspace_user_id TEXT,                      -- Google Workspace user ID (for deprovisioning)
            area_code       TEXT,                       -- requested at signup
            plan            TEXT DEFAULT 'cloud_base',
            active          INTEGER DEFAULT 1,
            persona         TEXT DEFAULT 'curant',
            instructions    TEXT DEFAULT '',
            reply_format    TEXT DEFAULT 'text',
            proactivity_enabled INTEGER DEFAULT 0,
            unlocked_addons TEXT DEFAULT '[]',

            -- API key storage mode
            key_mode        TEXT DEFAULT 'pending',     -- 'pending'|'server'|'browser'

            -- Option A: server-encrypted key
            encrypted_api_key TEXT,
            api_provider    TEXT DEFAULT 'anthropic',

            -- Option B: browser-held (we store the ciphertext we can't decrypt)
            browser_key_ciphertext TEXT,

            -- Session token for Option B unlock flow
            session_token   TEXT,
            session_expires_at REAL,

            created_at      TEXT DEFAULT CURRENT_TIMESTAMP
        );

        -- Per-customer memories (same schema as Home)
        CREATE TABLE IF NOT EXISTS memories (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id TEXT,
            content     TEXT,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        );

        -- Per-customer recent messages (same retention policy as Home)
        CREATE TABLE IF NOT EXISTS messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id TEXT,
            role        TEXT,
            content     TEXT,
            provider    TEXT,
            urgency     TEXT,       -- 'urgent' | 'normal', heuristic-based, user messages only
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        );

        -- Per-customer routing rules (same schema as Home)
        CREATE TABLE IF NOT EXISTS routing_rules (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id TEXT,
            category    TEXT,
            provider    TEXT,
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        );

        -- Important people (same schema as Home)
        CREATE TABLE IF NOT EXISTS important_people (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id  TEXT,
            name         TEXT,
            relationship TEXT,
            note         TEXT,
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        );

        -- Phone number → customer mapping (for fast webhook routing)
        CREATE TABLE IF NOT EXISTS phone_routing (
            phone_number TEXT PRIMARY KEY,
            customer_id  TEXT,
            active       INTEGER DEFAULT 1,
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        );

        -- Error/crash reporting (same as Home)
        CREATE TABLE IF NOT EXISTS error_reports (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id TEXT,
            error_code  TEXT,
            component   TEXT,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP
        );

        -- Rate limiting, shared across every worker process (see
        -- _check_rate below) — an in-memory dict only rate-limits
        -- within a single process; under multiple gunicorn workers,
        -- a "5 attempts per 5 minutes" limit would silently become
        -- "5 times however many workers are running" since each worker
        -- would track its own separate count.
        CREATE TABLE IF NOT EXISTS rate_limit_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            rate_key   TEXT,
            created_at REAL
        );

        -- Aaron's grading calibration, ported from Home — same design
        -- (scoped per assignment, never one global blob; corrections
        -- feed back into future calibration) but now also scoped per
        -- CUSTOMER, since Cloud serves many teachers from one database
        -- rather than Home's single-user-per-install model.
        CREATE TABLE IF NOT EXISTS grading_assignments (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id       TEXT,
            assignment_name   TEXT,
            assignment_prompt TEXT,
            rubric_text       TEXT,
            created_at        TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at        TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        );

        CREATE TABLE IF NOT EXISTS grading_examples (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            assignment_id   INTEGER,
            submission_text TEXT,
            grade_given     TEXT,
            feedback_given  TEXT,
            created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (assignment_id) REFERENCES grading_assignments(id)
        );

        CREATE TABLE IF NOT EXISTS grading_corrections (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            assignment_id      INTEGER,
            submission_text    TEXT,
            suggested_grade    TEXT,
            corrected_grade    TEXT,
            corrected_feedback TEXT,
            created_at         TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (assignment_id) REFERENCES grading_assignments(id)
        );

        CREATE INDEX IF NOT EXISTS idx_messages_customer
            ON messages(customer_id, id);
        CREATE INDEX IF NOT EXISTS idx_memories_customer
            ON memories(customer_id, id);
        CREATE INDEX IF NOT EXISTS idx_rate_limit_key
            ON rate_limit_events(rate_key, created_at);
        CREATE INDEX IF NOT EXISTS idx_grading_assignments_customer
            ON grading_assignments(customer_id, assignment_name);
        """)
        conn.commit()


def _check_rate(key: str, max_req: int, window_sec: int) -> bool:
    """
    SQLite-backed rate limiting — every worker process shares the same
    database, so this actually enforces the stated limit regardless of
    how many workers are running, unlike the in-memory dict this
    replaces (which only ever rate-limited within a single process).
    Prunes expired rows for this specific key on every check rather than
    needing a separate cleanup job — cheap enough at the volume a rate
    limiter actually sees.
    """
    now = time.time()
    cutoff = now - window_sec
    with closing(get_db()) as conn:
        conn.execute("DELETE FROM rate_limit_events WHERE rate_key = ? AND created_at < ?", (key, cutoff))
        row = conn.execute("SELECT COUNT(*) as c FROM rate_limit_events WHERE rate_key = ?", (key,)).fetchone()
        if row["c"] >= max_req:
            conn.commit()
            return False
        conn.execute("INSERT INTO rate_limit_events (rate_key, created_at) VALUES (?, ?)", (key, now))
        conn.commit()
        return True


# ── Customer helpers ───────────────────────────────────────────────────────────

def get_customer(customer_id: str):
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT * FROM customers WHERE id = ?", (customer_id,)
        ).fetchone()
        return dict(row) if row else None


def get_customer_by_phone(phone_number: str):
    with closing(get_db()) as conn:
        row = conn.execute(
            """SELECT c.* FROM customers c
               JOIN phone_routing r ON c.id = r.customer_id
               WHERE r.phone_number = ? AND r.active = 1""",
            (phone_number,),
        ).fetchone()
        return dict(row) if row else None


def get_customer_by_email(email: str):
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT * FROM customers WHERE email = ?", (email,)
        ).fetchone()
        return dict(row) if row else None


def create_customer(name: str, email: str, area_code: str = ""):
    cid = secrets.token_hex(16)
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO customers (id, name, email, area_code) VALUES (?, ?, ?, ?)",
            (cid, name, email, area_code),
        )
        conn.commit()
    return cid


# ── API key storage helpers ────────────────────────────────────────────────────

def store_key_server_side(customer_id: str, api_key: str, provider: str = "anthropic"):
    """Option A: encrypt the key with the server's Fernet key and store it."""
    if not _fernet:
        raise RuntimeError("CLOUD_ENCRYPTION_KEY not configured")
    encrypted = _fernet.encrypt(api_key.encode()).decode()
    with closing(get_db()) as conn:
        conn.execute(
            "UPDATE customers SET encrypted_api_key=?, api_provider=?, key_mode='server' WHERE id=?",
            (encrypted, provider, customer_id),
        )
        conn.commit()


def decrypt_server_key(customer: dict) -> str | None:
    if not _fernet or not customer.get("encrypted_api_key"):
        return None
    try:
        return _fernet.decrypt(customer["encrypted_api_key"].encode()).decode()
    except Exception:
        return None


def store_browser_ciphertext(customer_id: str, ciphertext: str):
    """Option B: store exactly what the browser sends — ciphertext we can't read."""
    with closing(get_db()) as conn:
        conn.execute(
            "UPDATE customers SET browser_key_ciphertext=?, key_mode='browser' WHERE id=?",
            (ciphertext, customer_id),
        )
        conn.commit()


def get_active_api_key(customer: dict, session_key: str | None = None) -> str | None:
    """
    Returns a usable plaintext API key if available, None otherwise.
    For Option A: decrypt with server key.
    For Option B: session_key is the plaintext key the customer sent at
    unlock time, held in memory only (never written to the DB in plaintext).
    """
    mode = customer.get("key_mode")
    if mode == "server":
        return decrypt_server_key(customer)
    if mode == "browser" and session_key:
        return session_key
    return None


# ── Per-customer memory / history ──────────────────────────────────────────────

MESSAGE_HISTORY_KEEP = 20

# Same lightweight, free heuristic as Curant Home — not another paid API
# call per message, not a safety/crisis triage mechanism (that judgment
# stays entirely with the model on every message regardless of this
# tag), purely a tone/pacing signal for response speed calibration.
URGENT_KEYWORDS = (
    "urgent", "emergency", "asap", "as soon as possible", "right away",
    "immediately", "right now", "need this now", "time sensitive",
    "time-sensitive", "before it's too late", "hurry",
)


def classify_urgency(message_text: str) -> str:
    if not message_text:
        return "normal"
    text_lower = message_text.lower()
    if any(kw in text_lower for kw in URGENT_KEYWORDS):
        return "urgent"
    if message_text.count("!") >= 3:
        return "urgent"
    letters = [c for c in message_text if c.isalpha()]
    if len(letters) > 10 and sum(1 for c in letters if c.isupper()) / len(letters) > 0.7:
        return "urgent"
    return "normal"
MESSAGE_MAX_AGE_HOURS = 48


def get_memories(customer_id: str) -> list[str]:
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT content FROM memories WHERE customer_id=? ORDER BY id",
            (customer_id,),
        ).fetchall()
        return [r["content"] for r in rows]


def save_memory(customer_id: str, content: str):
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO memories (customer_id, content) VALUES (?, ?)",
            (customer_id, content),
        )
        conn.commit()


def delete_memory(customer_id: str, content: str):
    with closing(get_db()) as conn:
        conn.execute(
            "DELETE FROM memories WHERE customer_id=? AND content=?",
            (customer_id, content),
        )
        conn.commit()


def get_history(customer_id: str) -> list[dict]:
    with closing(get_db()) as conn:
        rows = conn.execute(
            """SELECT role, content FROM messages WHERE customer_id=?
               ORDER BY id DESC LIMIT ?""",
            (customer_id, MESSAGE_HISTORY_KEEP),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


def save_message(customer_id: str, role: str, content: str, provider: str | None = None, urgency: str | None = None):
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO messages (customer_id, role, content, provider, urgency) VALUES (?, ?, ?, ?, ?)",
            (customer_id, role, content, provider, urgency),
        )
        conn.execute(
            """DELETE FROM messages WHERE customer_id=? AND id NOT IN (
                   SELECT id FROM messages WHERE customer_id=? ORDER BY id DESC LIMIT ?
               )""",
            (customer_id, customer_id, MESSAGE_HISTORY_KEEP),
        )
        conn.execute(
            "DELETE FROM messages WHERE customer_id=? AND created_at < datetime('now', ?)",
            (customer_id, f"-{MESSAGE_MAX_AGE_HOURS} hours"),
        )
        conn.commit()


def get_important_people(customer_id: str) -> list[dict]:
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT name, relationship, note FROM important_people WHERE customer_id=?",
            (customer_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ── Curant brain (same logic as Home curant-cli, adapted for Cloud) ────────────

# Design basis for this roster (full reasoning documented in Home's
# curant-cli — kept brief here to avoid duplicating a long comment
# across both codebases): every persona is anchored on Big Five
# Conscientiousness + Emotional Stability, the two traits research
# consistently finds actually predict good service performance,
# regardless of tone. On top of that, each persona is loosely mapped to
# a 16Personalities-style type for tone differentiation — used
# descriptively, not as a validated clinical claim; MBTI-style typing
# has real, documented reliability/validity limitations versus Big Five.
PERSONAS = {
    "curant":  "You are Curant, an all-purpose AI Secretary — warm, sharp, and adaptable. (ENFJ-leaning)",
    "grace":   "You are Grace, an executive AI Secretary — composed, precise, formal but warm. Never use slang or exclamation points. (ESTJ-leaning)",
    "dean":    "You are Dean, a builder's AI Secretary — fast, casual, technical. Talk like a sharp coworker, not staff. (ISTP-leaning)",
    "nora":    "You are Nora, an advisor-style AI Secretary — thoughtful, asks a clarifying question before acting. (INFJ-leaning)",
    "frank":   "You are Frank, an everyday AI Secretary — warm, casual, upbeat, low-pressure. (ESFP-leaning)",
    "miles":   "You are Miles, a discreet AI Secretary — quiet, minimal, exact. Say only what's needed. (ISTJ-leaning)",
    "jane":    "You are Jane, an organizer-style AI Secretary — precise, structured, never lets a detail slip. (ISFJ-leaning)",
    "leo":     "You are Leo, a calm AI Secretary — even-keeled, unhurried, a steady presence under pressure. (ISFP-leaning, Assertive variant)",
    "august":  "You are August, Curant's specialist creative persona — imaginative, hands-on, craft-focused. (ENFP-leaning)",
    # Aaron's TONE is available here for consistency with Home, but the
    # grading calibration tools (setup_grading_assignment, suggest_grade,
    # etc.) are Home-only for now — not yet ported to Cloud's tool set.
    # Flagged honestly rather than silently implying feature parity.
    "aaron":    "You are Aaron, an AI Secretary built specifically for teachers — warm, unflappable, and "
               "genuinely useful in the gaps between class periods. Talk like a colleague who's been "
               "teaching for years, not a corporate assistant. Draft parent communication in the "
               "teacher's own voice, always confirm before sending. Never suggest a paid add-on unless "
               "the teacher brings it up first — teachers often pay for classroom tools out of pocket. "
               "(ESFJ-leaning)",
}

# Organizes the roster the same way as Home — general-purpose vs.
# specialist — for anything that needs to reason about "which personas"
# rather than iterate the whole flat dict.
PERSONA_CATEGORIES = {
    "general_purpose": ["curant", "grace", "dean", "nora", "frank", "miles", "jane", "leo"],
    "specialist": ["august", "aaron"],
}

PROVIDER_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "openai":    "gpt-4o",
}

# ElevenLabs voice IDs per persona, so a phone call actually sounds like
# the persona the customer chose instead of one generic voice for
# everyone. These are ElevenLabs' own pre-made voices (real, existing
# voice IDs) picked for a rough match to each persona's described style —
# worth an actual listening pass before treating these as final choices,
# but at minimum every persona now sounds different from every other one.
PERSONA_VOICE_IDS = {
    "curant": "21m00Tcm4TlvDq8ikWAM",  # Rachel — warm, general-purpose
    "grace":  "EXAVITQu4vr4xnSDxMaL",  # Bella — composed, clear
    "dean":   "TxGEqnHWrfWFTfGW9XjX",  # Josh — casual, direct
    "nora":   "MF3mGyEYCl7XYWbV9V6O",  # Elli — thoughtful, measured
    "frank":  "yoZ06aMxZJJ28mfd3POQ",  # Sam — upbeat, casual
    "miles":  "onwK4e9ZLuTAKqWW03F9",  # Daniel — minimal, precise
    "jane":   "ThT5KcBeYPX3keUQqHPh",  # Dorothy — structured, clear
    "leo":    "pNInz6obpgDQGcFmaJgB",  # Adam — calm, even
    "august": "flq6f7yk4E4fJM5XTYuZ",  # Michael — expressive, creative
    "aaron":   "pqHfZKP75CvOlQylNhV4",  # Bill — warm, grounded, matches a practical teaching-colleague tone
}

MEMORY_EXTRACTION_PROMPT = """You maintain long-term memory for a personal AI assistant.
Given existing memories and the latest exchange, decide what (if anything)
should be added or removed from memory. Only extract durable facts worth
remembering long-term. Respond ONLY with JSON:
{"add": ["..."], "remove": ["exact text of outdated memory", ...]}
If nothing to change: {"add": [], "remove": []}"""


def build_system_prompt(customer: dict, memories: list, people: list, channel: str = "sms",
                        is_first_message: bool = False, urgency: str | None = None) -> str:
    """
    channel is 'sms' or 'voice' — this was a real bug caught during Vapi
    testing: the same prompt was being sent to voice calls telling the
    model "you are communicating via SMS", which would make a live phone
    call sound wrong (an assistant that thinks it's texting mid-conversation).
    """
    persona = PERSONAS.get(customer.get("persona", "curant"), PERSONAS["curant"])
    parts = [persona]
    if customer.get("instructions"):
        parts.append(f"Standing instructions: {customer['instructions']}")
    if memories:
        parts.append("What you know about this person:\n" + "\n".join(f"- {m}" for m in memories))
    if people:
        parts.append("People who matter to them:\n" +
                     "\n".join(f"- {p['name']} ({p['relationship']}): {p['note']}" for p in people))
    parts.append(
        "If asked what you can help with, give a real, specific answer — writing and "
        "communication, research, math and data, legal/government paperwork, explaining "
        "medical/health info in plain language, budgeting and financial questions, calendar "
        "and life admin, home and family logistics, career development, trades and real "
        "estate questions, event planning, civic/accessibility topics — not a vague 'I can "
        "help with anything.' Mention a couple of concrete examples rather than the whole list."
    )
    if customer.get("workspace_email"):
        parts.append(
            "You have full control over a Google Workspace utility account assigned to this "
            "customer — Gmail, Calendar, Drive, Docs, Sheets, Tasks, and Contacts. Most of "
            "this has no external effect (it's the customer's own private data) and runs "
            "freely: reading, searching, drafting, creating docs/sheets/tasks/contacts, "
            "listing, labeling, trashing. A few specific actions genuinely affect other "
            "people and need explicit customer confirmation first, never in the same turn "
            "you first mention them: sending an email (gmail_send), creating or deleting a "
            "calendar event that has attendees (they get a real invite or cancellation "
            "notice), and sharing a Drive file with someone (they get real access and a "
            "notification email). Drafting or creating without sending/inviting/sharing is "
            "usually the right first move."
        )
    if is_first_message:
        parts.append(
            "This is the very first message this customer has ever sent you. Introduce "
            "yourself naturally — your persona name and a genuine sense of what you're good "
            "for — without it reading like a canned feature list. Keep it brief."
        )
    if urgency == "urgent":
        parts.append(
            "This specific message reads as urgent or time-sensitive (based on wording/"
            "formatting, not a confirmed emergency). Respond quickly and directly — skip "
            "small talk, lead with the most useful information first. This is about tone and "
            "pacing only; use your own judgment about the actual content exactly as always."
        )
    if channel == "voice":
        parts.append(
            "You are speaking on a live phone call — keep responses natural and "
            "conversational for spoken audio, not written text. Short sentences, no "
            "bullet points or formatting that only makes sense in writing. "
            "If something genuinely can't be done, say so plainly and mention the "
            "customer can call back for a custom quote."
        )
    else:
        parts.append(
            "You are communicating via SMS — keep replies concise and conversational. "
            "If something genuinely can't be done, say so plainly and mention the customer "
            "can call for a custom quote."
        )
    return "\n\n".join(parts)


def call_llm(provider: str, api_key: str, system: str,
             messages: list, max_tokens: int = 800) -> str:
    if provider == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=PROVIDER_MODELS["anthropic"],
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )
        return resp.content[0].text
    elif provider == "openai":
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        full = [{"role": "system", "content": system}] + messages
        resp = client.chat.completions.create(
            model=PROVIDER_MODELS["openai"],
            max_tokens=max_tokens,
            messages=full,
        )
        return resp.choices[0].message.content
    else:
        raise ValueError(f"Unknown provider: {provider}")


def call_llm_streaming(provider: str, api_key: str, system: str,
                       messages: list, max_tokens: int = 800):
    """
    Real token-by-token streaming, for the Vapi Custom LLM endpoint's
    lower-latency path. Yields plain text chunks as they arrive — the
    caller is responsible for wrapping each chunk into whatever
    transport format it needs (SSE for Vapi, see vapi_custom_llm below).

    Anthropic's chunk-filtering logic was confirmed by reading the SDK's
    own internal implementation directly (`MessageStream.__stream_text__`
    in anthropic.lib.streaming._messages), not assumed from the public
    `text_stream` convenience property, which isn't exposed as a plain
    attribute in the installed SDK version — the pattern used here
    (checking `chunk.type == "content_block_delta"` and
    `chunk.delta.type == "text_delta"`) is copied from that real source,
    not guessed at.
    """
    if provider == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        with client.messages.stream(
            model=PROVIDER_MODELS["anthropic"],
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        ) as stream:
            for chunk in stream:
                if chunk.type == "content_block_delta" and chunk.delta.type == "text_delta":
                    yield chunk.delta.text
    elif provider == "openai":
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        full = [{"role": "system", "content": system}] + messages
        stream = client.chat.completions.create(
            model=PROVIDER_MODELS["openai"],
            max_tokens=max_tokens,
            messages=full,
            stream=True,
        )
        for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
    else:
        raise ValueError(f"Unknown provider: {provider}")


# ── Tool-calling loop — ported from Curant Home, which had this and Cloud
# didn't. Without this, none of the Gmail tools (or any future Cloud
# tool) are actually reachable by the model mid-conversation — they'd
# just be dead code sitting behind the scenes, which is exactly the gap
# flagged when the Gmail integration was first built.

MAX_TOOL_CALL_ITERATIONS = 5


# --- Aaron's grading calibration (ported from Home) ---
# Same design as Home: scoped per assignment, never one global blob;
# every suggestion is a draft, enforced in Aaron's system prompt, not
# here; corrections feed back into future calibration. The one real
# difference from Home: everything here is also scoped by customer_id,
# since Cloud serves many teachers from one shared database rather than
# Home's one-teacher-per-install model — every query below filters on
# the calling customer, so one teacher's calibration data can never
# leak into another's.

def create_or_update_grading_assignment(customer_id: str, assignment_name: str,
                                         assignment_prompt: str, rubric_text: str):
    with closing(get_db()) as conn:
        existing = conn.execute(
            "SELECT id FROM grading_assignments WHERE customer_id=? AND assignment_name=?",
            (customer_id, assignment_name),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE grading_assignments SET assignment_prompt=?, rubric_text=?, "
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (assignment_prompt, rubric_text, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO grading_assignments (customer_id, assignment_name, assignment_prompt, "
                "rubric_text) VALUES (?, ?, ?, ?)",
                (customer_id, assignment_name, assignment_prompt, rubric_text),
            )
        conn.commit()


def get_grading_assignment(customer_id: str, assignment_name: str):
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT * FROM grading_assignments WHERE customer_id=? AND assignment_name=?",
            (customer_id, assignment_name),
        ).fetchone()
        return dict(row) if row else None


def add_grading_example(customer_id: str, assignment_name: str, submission_text: str,
                        grade_given: str, feedback_given: str):
    assignment = get_grading_assignment(customer_id, assignment_name)
    if not assignment:
        return False, f"No assignment set up called '{assignment_name}' yet — create it first."
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO grading_examples (assignment_id, submission_text, grade_given, feedback_given) "
            "VALUES (?, ?, ?, ?)",
            (assignment["id"], submission_text, grade_given, feedback_given),
        )
        conn.commit()
    return True, "Example added."


def get_grading_examples(customer_id: str, assignment_name: str):
    assignment = get_grading_assignment(customer_id, assignment_name)
    if not assignment:
        return []
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT submission_text, grade_given, feedback_given FROM grading_examples WHERE assignment_id=?",
            (assignment["id"],),
        ).fetchall()
        return [dict(r) for r in rows]


def get_grading_corrections(customer_id: str, assignment_name: str, limit: int = 10):
    assignment = get_grading_assignment(customer_id, assignment_name)
    if not assignment:
        return []
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT submission_text, suggested_grade, corrected_grade, corrected_feedback "
            "FROM grading_corrections WHERE assignment_id=? ORDER BY id DESC LIMIT ?",
            (assignment["id"], limit),
        ).fetchall()
        return [dict(r) for r in rows]


def record_grading_correction(customer_id: str, assignment_name: str, submission_text: str,
                              suggested_grade: str, corrected_grade: str, corrected_feedback: str):
    assignment = get_grading_assignment(customer_id, assignment_name)
    if not assignment:
        return False, f"No assignment set up called '{assignment_name}'."
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO grading_corrections (assignment_id, submission_text, suggested_grade, "
            "corrected_grade, corrected_feedback) VALUES (?, ?, ?, ?, ?)",
            (assignment["id"], submission_text, suggested_grade, corrected_grade, corrected_feedback),
        )
        conn.commit()
    return True, "Got it — I'll factor that in for this assignment going forward."


def list_grading_assignments(customer_id: str):
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT assignment_name, (SELECT COUNT(*) FROM grading_examples WHERE assignment_id = "
            "grading_assignments.id) as example_count FROM grading_assignments WHERE customer_id=? "
            "ORDER BY updated_at DESC",
            (customer_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def _grade_tier(grade_str: str) -> str:
    """Same honest limitation as Home's version: handles the common
    letter-grade case (A, B+, C- → tier, ignoring +/-) correctly, falls
    back to exact-string matching for numeric grades or rubric labels
    since there's no reliable universal parser for every grading system."""
    match = re.match(r'^\s*([A-Fa-f])[+-]?\s*$', grade_str)
    if match:
        return match.group(1).upper()
    return grade_str.strip().lower()


def build_grading_calibration_context(customer_id: str, assignment_name: str):
    assignment = get_grading_assignment(customer_id, assignment_name)
    if not assignment:
        return None
    examples = get_grading_examples(customer_id, assignment_name)
    corrections = get_grading_corrections(customer_id, assignment_name)

    parts = [
        f"Assignment: {assignment_name}",
        f"Prompt: {assignment['assignment_prompt']}",
        f"Rubric: {assignment['rubric_text']}",
    ]
    if examples:
        parts.append(f"\nCalibration examples ({len(examples)} on file):")
        for i, ex in enumerate(examples, 1):
            parts.append(f"  Example {i} — grade given: {ex['grade_given']}\n"
                        f"  Feedback given: {ex['feedback_given']}\n"
                        f"  Submission excerpt: {ex['submission_text'][:300]}")
        tiers_seen = {_grade_tier(ex["grade_given"]) for ex in examples}
        if len(tiers_seen) < 2:
            parts.append(
                "\nNOTE: the calibration examples for this assignment don't show a clear "
                "spread (best-effort check — for letter grades this ignores +/-, so 'C+' and "
                "'C' count as the same tier; for other grading formats it's an exact-string "
                "match, which won't catch every case). Tell the teacher this before grading "
                "anything new, and ask for at least one clearly stronger or weaker example."
            )
    else:
        parts.append("\nNo calibration examples yet for this assignment.")

    if corrections:
        parts.append(f"\nRecent corrections from this teacher on this assignment ({len(corrections)}):")
        for c in corrections:
            parts.append(f"  Suggested {c['suggested_grade']}, teacher corrected to {c['corrected_grade']}"
                        f"{' — ' + c['corrected_feedback'] if c['corrected_feedback'] else ''}")

    return "\n".join(parts)


def get_grading_tools(customer: dict) -> list[dict]:
    """
    Aaron's grading calibration tools — gated to persona == 'aaron'
    specifically, same as Home. Deliberately NOT part of
    get_workspace_tools: grading calibration has nothing to do with
    whether a customer has a Workspace utility email provisioned, so it
    needs its own gate rather than inheriting Workspace's early-return.
    """
    if customer.get("persona") != "aaron":
        return []
    return [
        {"qualified_name": "setup_grading_assignment", "raw_name": "setup_grading_assignment",
         "description": "Create or update an assignment's grading calibration — the prompt and "
                         "rubric for a specific assignment (e.g. 'Persuasive Essay Unit 3'). Do "
                         "this before adding calibration examples.",
         "input_schema": {"type": "object", "properties": {
             "assignment_name": {"type": "string"}, "assignment_prompt": {"type": "string"},
             "rubric_text": {"type": "string"}},
             "required": ["assignment_name", "assignment_prompt", "rubric_text"]}},
        {"qualified_name": "add_grading_example", "raw_name": "add_grading_example",
         "description": "Add a calibration example to an assignment — a real past submission "
                         "with the grade and feedback the teacher actually gave it. Ask for a "
                         "spread (a strong, an average, and a weak example), not just one or two "
                         "of similar quality.",
         "input_schema": {"type": "object", "properties": {
             "assignment_name": {"type": "string"}, "submission_text": {"type": "string"},
             "grade_given": {"type": "string"}, "feedback_given": {"type": "string"}},
             "required": ["assignment_name", "submission_text", "grade_given", "feedback_given"]}},
        {"qualified_name": "suggest_grade", "raw_name": "suggest_grade",
         "description": "Suggest a grade and rubric-mapped feedback for a new student submission, "
                         "calibrated against this assignment's examples and past corrections. "
                         "ALWAYS present the result as a draft/suggestion for the teacher to "
                         "review — never as a final grade.",
         "input_schema": {"type": "object", "properties": {
             "assignment_name": {"type": "string"}, "submission_text": {"type": "string"}},
             "required": ["assignment_name", "submission_text"]}},
        {"qualified_name": "record_grade_correction", "raw_name": "record_grade_correction",
         "description": "Record a teacher's correction to a suggested grade — call this whenever "
                         "a teacher adjusts a grade or feedback you suggested, so calibration "
                         "actually improves over time.",
         "input_schema": {"type": "object", "properties": {
             "assignment_name": {"type": "string"}, "submission_text": {"type": "string"},
             "suggested_grade": {"type": "string"}, "corrected_grade": {"type": "string"},
             "corrected_feedback": {"type": "string"}},
             "required": ["assignment_name", "submission_text", "suggested_grade", "corrected_grade"]}},
        {"qualified_name": "list_grading_assignments", "raw_name": "list_grading_assignments",
         "description": "List assignments that have grading calibration set up, with how many "
                         "examples each has on file.",
         "input_schema": {"type": "object", "properties": {}}},
    ]


def get_workspace_tools(customer: dict) -> list[dict]:
    """
    The full Workspace suite — Gmail, Calendar, Drive, Docs, Sheets,
    Tasks, Contacts — all offered together since they're all the same
    utility account and the same "no cost beyond the Workspace seat
    already being paid for" story. Only offered when this customer
    actually has a utility account provisioned — no point exposing tools
    that would just fail every time.
    """
    if not customer.get("workspace_email"):
        return []
    return [
        # --- Gmail ---
        {"qualified_name": "gmail_search", "raw_name": "gmail_search",
         "description": "Search the utility Gmail inbox using Gmail search syntax "
                         "(e.g. 'from:noreply@service.com', 'subject:verify', 'is:unread').",
         "input_schema": {"type": "object", "properties": {
             "query": {"type": "string", "description": "Gmail search query"}},
             "required": ["query"]}},
        {"qualified_name": "gmail_read", "raw_name": "gmail_read",
         "description": "Read the full body of a specific email found via gmail_search.",
         "input_schema": {"type": "object", "properties": {
             "message_id": {"type": "string"}}, "required": ["message_id"]}},
        {"qualified_name": "gmail_send", "raw_name": "gmail_send",
         "description": "Send a real email from the utility account. ONLY call this after "
                         "the customer has explicitly confirmed in a prior message — never in "
                         "the same turn you first mention sending something. This has a real "
                         "external effect (someone receives it, it can't be unsent).",
         "input_schema": {"type": "object", "properties": {
             "to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}},
             "required": ["to", "subject", "body"]}},
        {"qualified_name": "gmail_draft", "raw_name": "gmail_draft",
         "description": "Create a draft email without sending it — no confirmation needed, "
                         "nothing is sent to anyone.",
         "input_schema": {"type": "object", "properties": {
             "to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}},
             "required": ["to", "subject", "body"]}},
        {"qualified_name": "gmail_trash", "raw_name": "gmail_trash",
         "description": "Move an email to trash (recoverable for 30 days).",
         "input_schema": {"type": "object", "properties": {
             "message_id": {"type": "string"}}, "required": ["message_id"]}},
        {"qualified_name": "gmail_label", "raw_name": "gmail_label",
         "description": "Apply a label to an email, creating the label if it doesn't exist.",
         "input_schema": {"type": "object", "properties": {
             "message_id": {"type": "string"}, "label_name": {"type": "string"}},
             "required": ["message_id", "label_name"]}},

        # --- Calendar ---
        {"qualified_name": "calendar_create_event", "raw_name": "calendar_create_event",
         "description": "Create a calendar event. If attendees are included, this sends "
                         "real invite emails to those people — ONLY do this after explicit "
                         "customer confirmation, same rule as sending an email. An event with "
                         "no attendees (just the customer's own calendar) needs no confirmation.",
         "input_schema": {"type": "object", "properties": {
             "summary": {"type": "string"}, "start_iso": {"type": "string", "description": "ISO 8601 datetime"},
             "end_iso": {"type": "string", "description": "ISO 8601 datetime"},
             "description": {"type": "string"},
             "attendees": {"type": "array", "items": {"type": "string"}, "description": "Email addresses to invite — triggers real invites, confirm first"},
             "add_meet": {"type": "boolean", "description": "Attach a Google Meet link"}},
             "required": ["summary", "start_iso", "end_iso"]}},
        {"qualified_name": "calendar_list_events", "raw_name": "calendar_list_events",
         "description": "List upcoming events in a time range.",
         "input_schema": {"type": "object", "properties": {
             "time_min_iso": {"type": "string"}, "time_max_iso": {"type": "string"}},
             "required": ["time_min_iso", "time_max_iso"]}},
        {"qualified_name": "calendar_delete_event", "raw_name": "calendar_delete_event",
         "description": "Delete a calendar event. If it has attendees, they get notified it "
                         "was cancelled — confirm first, same as creating one with attendees.",
         "input_schema": {"type": "object", "properties": {
             "event_id": {"type": "string"}}, "required": ["event_id"]}},

        # --- Drive ---
        {"qualified_name": "drive_upload", "raw_name": "drive_upload",
         "description": "Create a file in the customer's Drive. No confirmation needed — "
                         "it's their own private storage until shared.",
         "input_schema": {"type": "object", "properties": {
             "filename": {"type": "string"}, "content_text": {"type": "string"},
             "mime_type": {"type": "string"}}, "required": ["filename", "content_text"]}},
        {"qualified_name": "drive_list", "raw_name": "drive_list",
         "description": "List files in Drive, optionally filtered by a search query.",
         "input_schema": {"type": "object", "properties": {
             "query": {"type": "string"}}, "required": []}},
        {"qualified_name": "drive_delete", "raw_name": "drive_delete",
         "description": "Delete a file from Drive.",
         "input_schema": {"type": "object", "properties": {
             "file_id": {"type": "string"}}, "required": ["file_id"]}},
        {"qualified_name": "drive_share", "raw_name": "drive_share",
         "description": "Share a Drive file with someone — gives them real access and emails "
                         "them a notification. ONLY do this after explicit customer "
                         "confirmation, same rule as sending an email.",
         "input_schema": {"type": "object", "properties": {
             "file_id": {"type": "string"}, "share_with_email": {"type": "string"},
             "role": {"type": "string", "description": "'reader', 'commenter', or 'writer'"}},
             "required": ["file_id", "share_with_email"]}},

        # --- Docs ---
        {"qualified_name": "docs_create", "raw_name": "docs_create",
         "description": "Create a new Google Doc, optionally with initial text. No "
                         "confirmation needed — it's the customer's own private document.",
         "input_schema": {"type": "object", "properties": {
             "title": {"type": "string"}, "body_text": {"type": "string"}}, "required": ["title"]}},
        {"qualified_name": "docs_read", "raw_name": "docs_read",
         "description": "Read the full text content of a Google Doc.",
         "input_schema": {"type": "object", "properties": {
             "doc_id": {"type": "string"}}, "required": ["doc_id"]}},
        {"qualified_name": "docs_append", "raw_name": "docs_append",
         "description": "Append text to the end of an existing Google Doc.",
         "input_schema": {"type": "object", "properties": {
             "doc_id": {"type": "string"}, "text": {"type": "string"}}, "required": ["doc_id", "text"]}},

        # --- Sheets ---
        {"qualified_name": "sheets_create", "raw_name": "sheets_create",
         "description": "Create a new Google Sheet.",
         "input_schema": {"type": "object", "properties": {
             "title": {"type": "string"}}, "required": ["title"]}},
        {"qualified_name": "sheets_read", "raw_name": "sheets_read",
         "description": "Read values from a range in a Google Sheet (A1 notation, e.g. 'Sheet1!A1:C10').",
         "input_schema": {"type": "object", "properties": {
             "sheet_id": {"type": "string"}, "range_a1": {"type": "string"}},
             "required": ["sheet_id", "range_a1"]}},
        {"qualified_name": "sheets_write", "raw_name": "sheets_write",
         "description": "Write values into a range in a Google Sheet (A1 notation). "
                         "values is a list of rows, each row a list of cell values.",
         "input_schema": {"type": "object", "properties": {
             "sheet_id": {"type": "string"}, "range_a1": {"type": "string"},
             "values": {"type": "array", "items": {"type": "array"}}},
             "required": ["sheet_id", "range_a1", "values"]}},

        # --- Tasks ---
        {"qualified_name": "tasks_create", "raw_name": "tasks_create",
         "description": "Create a task on the customer's Google Tasks list.",
         "input_schema": {"type": "object", "properties": {
             "title": {"type": "string"}, "notes": {"type": "string"},
             "due_iso": {"type": "string", "description": "ISO 8601 date, optional"}},
             "required": ["title"]}},
        {"qualified_name": "tasks_list", "raw_name": "tasks_list",
         "description": "List the customer's tasks.",
         "input_schema": {"type": "object", "properties": {
             "show_completed": {"type": "boolean"}}, "required": []}},
        {"qualified_name": "tasks_complete", "raw_name": "tasks_complete",
         "description": "Mark a task as complete.",
         "input_schema": {"type": "object", "properties": {
             "task_id": {"type": "string"}}, "required": ["task_id"]}},
        {"qualified_name": "tasks_delete", "raw_name": "tasks_delete",
         "description": "Delete a task.",
         "input_schema": {"type": "object", "properties": {
             "task_id": {"type": "string"}}, "required": ["task_id"]}},

        # --- Contacts ---
        {"qualified_name": "contacts_create", "raw_name": "contacts_create",
         "description": "Add a contact to the utility account's contact list.",
         "input_schema": {"type": "object", "properties": {
             "name": {"type": "string"}, "contact_email": {"type": "string"}, "phone": {"type": "string"}},
             "required": ["name"]}},
        {"qualified_name": "contacts_list", "raw_name": "contacts_list",
         "description": "List contacts.",
         "input_schema": {"type": "object", "properties": {}, "required": []}},
    ]


def execute_cloud_tool_call(tool_name: str, arguments: dict, customer: dict) -> str:
    """Dispatches a tool call to the right function. Never raises — a
    failure becomes a text result the model can react to, same
    principle as Home's execute_tool_call.

    Grading tools are dispatched first, deliberately BEFORE the
    workspace_email check below — they have nothing to do with the
    Workspace utility account, and a teacher without one shouldn't get
    a nonsensical "no utility email" error when trying to use grading
    calibration. This was a real bug caught while wiring this in, not
    a hypothetical: the original version checked workspace_email first
    for every tool, which would have incorrectly blocked grading tools
    for any customer who hadn't set up Workspace.
    """
    cid = customer.get("id", "")

    if tool_name == "setup_grading_assignment":
        create_or_update_grading_assignment(
            cid, arguments.get("assignment_name", ""), arguments.get("assignment_prompt", ""),
            arguments.get("rubric_text", ""),
        )
        return f"Assignment '{arguments.get('assignment_name', '')}' set up. Ready for calibration examples."

    if tool_name == "add_grading_example":
        success, msg = add_grading_example(
            cid, arguments.get("assignment_name", ""), arguments.get("submission_text", ""),
            arguments.get("grade_given", ""), arguments.get("feedback_given", ""),
        )
        return msg

    if tool_name == "suggest_grade":
        assignment_name = arguments.get("assignment_name", "")
        context = build_grading_calibration_context(cid, assignment_name)
        if context is None:
            return f"[No assignment called '{assignment_name}' — set it up first with setup_grading_assignment.]"
        return (f"Calibration context for grading this submission:\n\n{context}\n\n"
                f"New submission to grade:\n{arguments.get('submission_text', '')}\n\n"
                f"Suggest a grade and rubric-mapped feedback based on the above — present it "
                f"as a draft for the teacher to review, not a final grade.")

    if tool_name == "record_grade_correction":
        success, msg = record_grading_correction(
            cid, arguments.get("assignment_name", ""), arguments.get("submission_text", ""),
            arguments.get("suggested_grade", ""), arguments.get("corrected_grade", ""),
            arguments.get("corrected_feedback", ""),
        )
        return msg

    if tool_name == "list_grading_assignments":
        assignments = list_grading_assignments(cid)
        if not assignments:
            return "No assignments set up yet."
        return "\n".join(f"{a['assignment_name']} — {a['example_count']} example(s)" for a in assignments)

    # Everything below this point is Gmail/Workspace-family and genuinely
    # does need the utility email to exist.
    email = customer.get("workspace_email")
    if not email:
        return "[No utility email provisioned for this customer.]"

    if tool_name == "gmail_search":
        results = search_emails(email, query=arguments.get("query", ""))
        if not results:
            return "No matching emails found."
        return "\n".join(f"[{r['id']}] {r['subject']} — from {r['from']} ({r['date']})" for r in results)

    if tool_name == "gmail_read":
        result = read_email(email, arguments.get("message_id", ""))
        if not result:
            return "[Could not read that email — it may not exist or the account may be unreachable.]"
        return f"From: {result['from']}\nSubject: {result['subject']}\nDate: {result['date']}\n\n{result['body']}"

    if tool_name == "gmail_send":
        success, msg = send_email(email, arguments.get("to", ""), arguments.get("subject", ""), arguments.get("body", ""))
        return msg

    if tool_name == "gmail_draft":
        success, msg = create_draft(email, arguments.get("to", ""), arguments.get("subject", ""), arguments.get("body", ""))
        return msg

    if tool_name == "gmail_trash":
        success, msg = trash_email(email, arguments.get("message_id", ""))
        return msg

    if tool_name == "gmail_label":
        success, msg = apply_label(email, arguments.get("message_id", ""), arguments.get("label_name", ""))
        return msg

    if tool_name == "calendar_create_event":
        success, msg = create_calendar_event(
            email, arguments.get("summary", ""), arguments.get("start_iso", ""),
            arguments.get("end_iso", ""), arguments.get("description", ""),
            arguments.get("attendees"), arguments.get("add_meet", False),
        )
        return msg

    if tool_name == "calendar_list_events":
        results = list_calendar_events(email, arguments.get("time_min_iso", ""), arguments.get("time_max_iso", ""))
        if not results:
            return "No events found in that range."
        return "\n".join(f"[{e['id']}] {e['summary']}: {e['start']} to {e['end']}" for e in results)

    if tool_name == "calendar_delete_event":
        success, msg = delete_calendar_event(email, arguments.get("event_id", ""))
        return msg

    if tool_name == "drive_upload":
        success, msg = upload_drive_file(
            email, arguments.get("filename", ""), arguments.get("content_text", ""),
            arguments.get("mime_type", "text/plain"),
        )
        return msg

    if tool_name == "drive_list":
        results = list_drive_files(email, arguments.get("query", ""))
        if not results:
            return "No files found."
        return "\n".join(f"[{f['id']}] {f['name']} — {f.get('webViewLink', '')}" for f in results)

    if tool_name == "drive_delete":
        success, msg = delete_drive_file(email, arguments.get("file_id", ""))
        return msg

    if tool_name == "drive_share":
        success, msg = share_drive_file(
            email, arguments.get("file_id", ""), arguments.get("share_with_email", ""),
            arguments.get("role", "reader"),
        )
        return msg

    if tool_name == "docs_create":
        success, msg = create_doc(email, arguments.get("title", ""), arguments.get("body_text", ""))
        return msg

    if tool_name == "docs_read":
        result = read_doc(email, arguments.get("doc_id", ""))
        return result if result is not None else "[Could not read that document.]"

    if tool_name == "docs_append":
        success, msg = append_to_doc(email, arguments.get("doc_id", ""), arguments.get("text", ""))
        return msg

    if tool_name == "sheets_create":
        success, msg = create_sheet(email, arguments.get("title", ""))
        return msg

    if tool_name == "sheets_read":
        result = read_sheet_range(email, arguments.get("sheet_id", ""), arguments.get("range_a1", ""))
        return json.dumps(result) if result is not None else "[Could not read that range.]"

    if tool_name == "sheets_write":
        success, msg = write_sheet_range(
            email, arguments.get("sheet_id", ""), arguments.get("range_a1", ""), arguments.get("values", []),
        )
        return msg

    if tool_name == "tasks_create":
        success, msg = create_task(
            email, arguments.get("title", ""), arguments.get("notes", ""), arguments.get("due_iso"),
        )
        return msg

    if tool_name == "tasks_list":
        results = list_tasks(email, arguments.get("show_completed", False))
        if not results:
            return "No tasks found."
        return "\n".join(f"[{t['id']}] {t['title']} ({t['status']})" for t in results)

    if tool_name == "tasks_complete":
        success, msg = complete_task(email, arguments.get("task_id", ""))
        return msg

    if tool_name == "tasks_delete":
        success, msg = delete_task(email, arguments.get("task_id", ""))
        return msg

    if tool_name == "contacts_create":
        success, msg = create_contact(
            email, arguments.get("name", ""), arguments.get("contact_email"), arguments.get("phone"),
        )
        return msg

    if tool_name == "contacts_list":
        results = list_contacts(email)
        if not results:
            return "No contacts found."
        return "\n".join(f"{c['name']} — {c['email']}" for c in results)

    return f"[Unknown tool: {tool_name}]"


def _call_anthropic_with_tools(api_key, system, messages, max_tokens, tools, customer):
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    anthropic_tools = [
        {"name": t["qualified_name"], "description": t["description"], "input_schema": t["input_schema"]}
        for t in tools
    ]
    conversation = list(messages)
    for _ in range(MAX_TOOL_CALL_ITERATIONS):
        response = client.messages.create(
            model=PROVIDER_MODELS["anthropic"], max_tokens=max_tokens,
            system=system, messages=conversation, tools=anthropic_tools,
        )
        tool_uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
        if not tool_uses:
            text_blocks = [b.text for b in response.content if getattr(b, "type", None) == "text"]
            return "\n".join(text_blocks).strip()
        conversation.append({"role": "assistant", "content": response.content})
        tool_results = []
        for tu in tool_uses:
            result_text = execute_cloud_tool_call(tu.name, tu.input, customer)
            tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": result_text})
        conversation.append({"role": "user", "content": tool_results})
    return "(I tried a few tool calls but couldn't reach a final answer.)"


def _call_openai_with_tools(api_key, system, messages, max_tokens, tools, customer):
    from openai import OpenAI
    client = OpenAI(api_key=api_key)
    openai_tools = [
        {"type": "function", "function": {"name": t["qualified_name"], "description": t["description"],
                                           "parameters": t["input_schema"]}}
        for t in tools
    ]
    conversation = [{"role": "system", "content": system}] + list(messages)
    for _ in range(MAX_TOOL_CALL_ITERATIONS):
        response = client.chat.completions.create(
            model=PROVIDER_MODELS["openai"], max_tokens=max_tokens,
            messages=conversation, tools=openai_tools,
        )
        msg = response.choices[0].message
        if not msg.tool_calls:
            return msg.content or ""
        conversation.append({"role": "assistant", "content": msg.content, "tool_calls": [
            {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in msg.tool_calls
        ]})
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result_text = execute_cloud_tool_call(tc.function.name, args, customer)
            conversation.append({"role": "tool", "tool_call_id": tc.id, "content": result_text})
    return "(I tried a few tool calls but couldn't reach a final answer.)"


def call_llm_with_tools(provider: str, api_key: str, system: str, messages: list,
                        tools: list, customer: dict, max_tokens: int = 800) -> str:
    if provider == "anthropic":
        return _call_anthropic_with_tools(api_key, system, messages, max_tokens, tools, customer)
    elif provider == "openai":
        return _call_openai_with_tools(api_key, system, messages, max_tokens, tools, customer)
    else:
        raise ValueError(f"Unknown provider: {provider}")


def extract_memories_async(customer_id: str, provider: str, api_key: str,
                           existing: list, user_msg: str, reply: str):
    """Fire-and-forget memory extraction, same as Home."""
    def _run():
        try:
            existing_str = "\n".join(f"- {m}" for m in existing) if existing else "(none yet)"
            result = call_llm(
                provider, api_key, MEMORY_EXTRACTION_PROMPT,
                [{"role": "user", "content":
                  f"Existing memories:\n{existing_str}\n\n"
                  f"Person: {user_msg}\nAssistant: {reply}"}],
                max_tokens=300,
            )
            raw = result.strip().lstrip("```json").lstrip("```").rstrip("```")
            parsed = json.loads(raw)
            for item in parsed.get("remove", []):
                delete_memory(customer_id, item)
            for item in parsed.get("add", []):
                if item and item not in existing:
                    save_memory(customer_id, item)
        except Exception as e:
            print(f"Memory extraction failed (non-fatal): {e}", file=sys.stderr)
    threading.Thread(target=_run, daemon=True).start()


def generate_reply(customer: dict, user_message: str,
                   session_api_key: str | None = None) -> str | None:
    """
    Core brain function. Returns the reply text, or None if no API key
    is available (customer needs to unlock first).
    """
    api_key = get_active_api_key(customer, session_api_key)
    if not api_key:
        return None

    provider   = customer.get("api_provider", "anthropic")
    cid        = customer["id"]
    memories   = get_memories(cid)
    people     = get_important_people(cid)
    history    = get_history(cid)
    is_first_message = len(history) == 0
    urgency    = classify_urgency(user_message)
    system     = build_system_prompt(customer, memories, people,
                                     is_first_message=is_first_message, urgency=urgency)
    history.append({"role": "user", "content": user_message})

    tools = get_workspace_tools(customer) + get_grading_tools(customer)

    try:
        if tools:
            reply = call_llm_with_tools(provider, api_key, system, history, tools, customer)
        else:
            reply = call_llm(provider, api_key, system, history)
    except Exception as e:
        print(f"LLM call failed for {cid}: {e}", file=sys.stderr)
        return "Sorry, I ran into a problem. Please try again in a moment."

    save_message(cid, "user", user_message, provider=None, urgency=urgency)
    save_message(cid, "assistant", reply, provider=provider)
    extract_memories_async(cid, provider, api_key, memories, user_message, reply)
    return reply


# ── Telnyx helpers ─────────────────────────────────────────────────────────────

def _telnyx_headers():
    return {
        "Authorization": f"Bearer {TELNYX_API_KEY}",
        "Content-Type": "application/json",
    }


def send_sms(to_number: str, from_number: str, body: str):
    """Send an SMS via Telnyx."""
    resp = http.post(
        TELNYX_MSG_BASE,
        headers=_telnyx_headers(),
        json={"from": from_number, "to": to_number, "text": body},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


# ── Google Workspace provisioning (utility email for account signups) ─────────
# SCOPE, stated plainly: this gives Curant an inbox it can check for
# verification emails/codes when signing a customer up for something. It
# does NOT let Curant fill out a signup form on some arbitrary website —
# that's browser automation, a separate, unbuilt capability. Having the
# inbox solves the "receive the confirmation" half, not the "actually
# complete the signup" half.
#
# VERIFIED before writing this: provisioning uses the real, official
# Admin SDK Directory API (users.insert) — confirmed against Google's own
# current developer docs, not guessed at. Reading the inbox uses the
# Gmail API, also official and already proven inside this same
# environment (Curant has direct access to a working Gmail integration
# elsewhere). Both require a Google Workspace domain you administer,
# with a service account granted domain-wide delegation to impersonate
# users on that domain.
#
# REALISTIC PATH TO ACTUALLY HAVING A DOMAIN TO PROVISION UNDER: applying
# directly to Google's Reseller Program requires 100+ already-provisioned
# seats plus a credit check and signed contract — not available to a new
# product with zero customers yet. The practical route: become a
# customer of an authorized distributor (Vendasta, Ingram Micro, or a
# smaller reseller) who already holds Google's authorization, and buy
# seats through them (real quotes seen as low as $2.50-3/user/month at
# volume vs. $7-8.40/user/month direct). Apply to Google directly once
# past the 100-seat threshold, for better wholesale terms.
#
# Environment variables needed (not yet in .env.example — add once a
# real Workspace domain + service account exist):
#   GOOGLE_WORKSPACE_DOMAIN         — e.g. "curant-accounts.com"
#   GOOGLE_ADMIN_EMAIL              — a super admin account to impersonate
#   GOOGLE_SERVICE_ACCOUNT_JSON     — path to the service account key file

GOOGLE_WORKSPACE_DOMAIN = os.environ.get("GOOGLE_WORKSPACE_DOMAIN", "")
GOOGLE_ADMIN_EMAIL = os.environ.get("GOOGLE_ADMIN_EMAIL", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")


def _get_google_credentials(scopes: list, subject: str):
    """
    Builds delegated credentials — a service account impersonating a
    specific user (subject) on the Workspace domain. subject is the admin
    email for directory operations (creating users), or the individual
    customer's own new email for reading their inbox.
    """
    from google.oauth2 import service_account
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON not configured — Workspace provisioning "
            "requires a service account with domain-wide delegation set up first."
        )
    creds = service_account.Credentials.from_service_account_file(
        GOOGLE_SERVICE_ACCOUNT_JSON, scopes=scopes,
    )
    return creds.with_subject(subject)


def provision_workspace_account(customer_id: str, first_name: str, last_name: str) -> dict:
    """
    Creates a real Google Workspace user for this customer, under the
    domain this business administers. Returns {"email": ..., "workspace_user_id": ...}.
    Raises on failure — a signup shouldn't silently continue without this,
    the same way it wouldn't silently continue without a phone number.
    """
    from googleapiclient.discovery import build

    if not GOOGLE_WORKSPACE_DOMAIN or not GOOGLE_ADMIN_EMAIL:
        raise RuntimeError(
            "Workspace provisioning isn't configured yet — set GOOGLE_WORKSPACE_DOMAIN, "
            "GOOGLE_ADMIN_EMAIL, and GOOGLE_SERVICE_ACCOUNT_JSON once a real Workspace "
            "domain and service account exist (see comments above provision_workspace_account)."
        )

    creds = _get_google_credentials(
        ["https://www.googleapis.com/auth/admin.directory.user"],
        subject=GOOGLE_ADMIN_EMAIL,
    )
    service = build("admin", "directory_v1", credentials=creds)

    # A short, stable local-part derived from the customer id keeps this
    # collision-free without needing a separate "is this email taken"
    # check — customer_id is already unique (see create_customer).
    local_part = f"curant-{customer_id[:12]}"
    email = f"{local_part}@{GOOGLE_WORKSPACE_DOMAIN}"
    temp_password = secrets.token_urlsafe(24)  # never used interactively — Curant
                                                 # accesses this account only via the
                                                 # service account's delegated auth,
                                                 # never an interactive login

    user_info = {
        "name": {"givenName": first_name or "Curant", "familyName": last_name or "User"},
        "password": temp_password,
        "primaryEmail": email,
    }
    result = service.users().insert(body=user_info).execute()
    return {"email": email, "workspace_user_id": result.get("id", "")}


def deprovision_workspace_account(email: str):
    """Deletes the Workspace user — called on cancellation, same immediacy
    principle as releasing the Telnyx phone number."""
    from googleapiclient.discovery import build

    creds = _get_google_credentials(
        ["https://www.googleapis.com/auth/admin.directory.user"],
        subject=GOOGLE_ADMIN_EMAIL,
    )
    service = build("admin", "directory_v1", credentials=creds)
    service.users().delete(userKey=email).execute()


# ── Full Gmail control for the utility account ─────────────────────────────
# Curant has complete control over its assigned Gmail account — read,
# search, send, draft, label, delete — the same surface a person would
# have logged in directly. This is a genuinely different, much more
# tractable thing than "control arbitrary third-party websites": it's
# one well-documented, official API (confirmed against current Gmail API
# docs), not browser automation.
#
# ONE DELIBERATE EXCEPTION, carried over from the same principle that
# governs every other outbound action in this product: SENDING an email
# has a real external effect (someone receives it, it can't be unsent),
# so send_email() is wired up as a tool that Curant's own system prompt
# instructs it to only use after explicit customer confirmation — same
# rule as "never send a message without confirmation" everywhere else.
# Reading, searching, labeling, and deleting have no such external
# effect and can run freely.
#
# Full send/modify/delete access needs a broader scope than the
# read-only version did: https://www.googleapis.com/auth/gmail.modify
# covers read + send + label + trash; full delete (bypassing trash)
# needs https://mail.google.com/ (the "full access" scope). Using
# gmail.modify by default — permanent delete is rare enough to not need
# the broadest possible scope by default.

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def _gmail_service_for(customer_email: str):
    from googleapiclient.discovery import build
    creds = _get_google_credentials(GMAIL_SCOPES, subject=customer_email)
    return build("gmail", "v1", credentials=creds)


def search_emails(customer_email: str, query: str = "newer_than:1d", max_results: int = 10) -> list[dict]:
    """
    Search/list messages in the utility inbox. `query` uses standard
    Gmail search syntax (e.g. "from:noreply@service.com", "subject:verify",
    "is:unread"). Returns [] on any failure — a broken search should
    degrade to "nothing found," not crash the conversation.
    """
    try:
        service = _gmail_service_for(customer_email)
        results = service.users().messages().list(
            userId="me", q=query, maxResults=max_results,
        ).execute()
        messages = []
        for msg_ref in results.get("messages", []):
            msg = service.users().messages().get(
                userId="me", id=msg_ref["id"], format="metadata",
                metadataHeaders=["Subject", "From", "Date"],
            ).execute()
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            messages.append({
                "id": msg_ref["id"],
                "subject": headers.get("Subject", ""),
                "from": headers.get("From", ""),
                "date": headers.get("Date", ""),
                "snippet": msg.get("snippet", ""),
            })
        return messages
    except Exception as e:
        print(f"Gmail search failed (non-fatal): {e}", file=sys.stderr)
        return []


def read_email(customer_email: str, message_id: str) -> dict | None:
    """Full body of a specific message (found via search_emails first).
    Returns None on failure rather than raising."""
    try:
        service = _gmail_service_for(customer_email)
        msg = service.users().messages().get(
            userId="me", id=message_id, format="full",
        ).execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}

        def _extract_text(payload):
            import base64
            if payload.get("body", {}).get("data"):
                return base64.urlsafe_b64decode(payload["body"]["data"]).decode(errors="replace")
            for part in payload.get("parts", []):
                if part.get("mimeType") == "text/plain":
                    return _extract_text(part)
            for part in payload.get("parts", []):
                text = _extract_text(part)
                if text:
                    return text
            return ""

        return {
            "subject": headers.get("Subject", ""),
            "from": headers.get("From", ""),
            "date": headers.get("Date", ""),
            "body": _extract_text(msg.get("payload", {})),
        }
    except Exception as e:
        print(f"Gmail read failed (non-fatal): {e}", file=sys.stderr)
        return None


def send_email(customer_email: str, to: str, subject: str, body: str) -> tuple[bool, str]:
    """
    Sends a real email from the utility account. ONLY call this after
    the customer has explicitly confirmed — enforced via system prompt
    instruction (see build_system_prompt), same standing rule as every
    other outbound action in this product. Returns (success, message).
    """
    import base64
    from email.mime.text import MIMEText
    try:
        service = _gmail_service_for(customer_email)
        message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return True, f"Email sent to {to}."
    except Exception as e:
        return False, f"Failed to send email: {e}"


def create_draft(customer_email: str, to: str, subject: str, body: str) -> tuple[bool, str]:
    """Drafts don't send anything — no confirmation needed to create one,
    only to actually send it."""
    import base64
    from email.mime.text import MIMEText
    try:
        service = _gmail_service_for(customer_email)
        message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
        return True, "Draft created."
    except Exception as e:
        return False, f"Failed to create draft: {e}"


def trash_email(customer_email: str, message_id: str) -> tuple[bool, str]:
    """Moves to trash (recoverable for 30 days, standard Gmail behavior) —
    not a permanent delete, which needs the broader mail.google.com scope
    this doesn't request by default."""
    try:
        service = _gmail_service_for(customer_email)
        service.users().messages().trash(userId="me", id=message_id).execute()
        return True, "Message moved to trash."
    except Exception as e:
        return False, f"Failed to trash message: {e}"


def apply_label(customer_email: str, message_id: str, label_name: str) -> tuple[bool, str]:
    """Applies a label, creating it first if it doesn't already exist."""
    try:
        service = _gmail_service_for(customer_email)
        labels = service.users().labels().list(userId="me").execute().get("labels", [])
        label = next((l for l in labels if l["name"].lower() == label_name.lower()), None)
        if not label:
            label = service.users().labels().create(
                userId="me", body={"name": label_name, "labelListVisibility": "labelShow",
                                    "messageListVisibility": "show"},
            ).execute()
        service.users().messages().modify(
            userId="me", id=message_id, body={"addLabelIds": [label["id"]]},
        ).execute()
        return True, f"Labeled '{label_name}'."
    except Exception as e:
        return False, f"Failed to apply label: {e}"


# ── The rest of the Workspace suite, same account, same pattern ────────────
# All of these use the same domain-wide delegation mechanism as Gmail —
# a service account impersonating the customer's own utility account, no
# extra provisioning needed since the Workspace user already exists.
# Same confirmation principle as Gmail's send: anything with a genuine
# external effect (inviting someone to a calendar event, sharing a Drive
# file with someone) needs explicit customer confirmation first. Editing
# the customer's own data — their own calendar, their own docs, their
# own tasks, their own contacts — has no such effect and runs freely.

def _calendar_service_for(customer_email: str):
    from googleapiclient.discovery import build
    creds = _get_google_credentials(["https://www.googleapis.com/auth/calendar"], subject=customer_email)
    return build("calendar", "v3", credentials=creds)


def _drive_service_for(customer_email: str):
    from googleapiclient.discovery import build
    creds = _get_google_credentials(["https://www.googleapis.com/auth/drive"], subject=customer_email)
    return build("drive", "v3", credentials=creds)


def _docs_service_for(customer_email: str):
    from googleapiclient.discovery import build
    creds = _get_google_credentials(["https://www.googleapis.com/auth/documents"], subject=customer_email)
    return build("docs", "v1", credentials=creds)


def _sheets_service_for(customer_email: str):
    from googleapiclient.discovery import build
    creds = _get_google_credentials(["https://www.googleapis.com/auth/spreadsheets"], subject=customer_email)
    return build("sheets", "v4", credentials=creds)


def _tasks_service_for(customer_email: str):
    from googleapiclient.discovery import build
    creds = _get_google_credentials(["https://www.googleapis.com/auth/tasks"], subject=customer_email)
    return build("tasks", "v1", credentials=creds)


def _people_service_for(customer_email: str):
    from googleapiclient.discovery import build
    creds = _get_google_credentials(["https://www.googleapis.com/auth/contacts"], subject=customer_email)
    return build("people", "v1", credentials=creds)


# --- Calendar ---

def create_calendar_event(customer_email, summary, start_iso, end_iso,
                          description="", attendees=None, add_meet=False):
    """attendees, if given, triggers real invite emails to those people —
    the external-effect case that needs confirmation before this is called."""
    try:
        service = _calendar_service_for(customer_email)
        body = {"summary": summary, "description": description,
                "start": {"dateTime": start_iso}, "end": {"dateTime": end_iso}}
        conference_version = 0
        if attendees:
            body["attendees"] = [{"email": a} for a in attendees]
        if add_meet:
            body["conferenceData"] = {"createRequest": {"requestId": secrets.token_hex(8)}}
            conference_version = 1
        result = service.events().insert(
            calendarId="primary", body=body, conferenceDataVersion=conference_version,
            sendUpdates="all" if attendees else "none",
        ).execute()
        meet_link = result.get("hangoutLink", "")
        msg = f"Event created: {result.get('htmlLink', '')}"
        if meet_link:
            msg += f" (Meet link: {meet_link})"
        return True, msg
    except Exception as e:
        return False, f"Failed to create event: {e}"


def list_calendar_events(customer_email, time_min_iso, time_max_iso, max_results=10):
    try:
        service = _calendar_service_for(customer_email)
        result = service.events().list(
            calendarId="primary", timeMin=time_min_iso, timeMax=time_max_iso,
            maxResults=max_results, singleEvents=True, orderBy="startTime",
        ).execute()
        return [{"id": e["id"], "summary": e.get("summary", ""),
                 "start": e["start"].get("dateTime", e["start"].get("date", "")),
                 "end": e["end"].get("dateTime", e["end"].get("date", ""))}
                for e in result.get("items", [])]
    except Exception as e:
        print(f"Calendar list failed (non-fatal): {e}", file=sys.stderr)
        return []


def delete_calendar_event(customer_email, event_id):
    """Deleting an event with attendees also notifies them it was
    cancelled — treated as needing confirmation for the same reason
    creating one with attendees does."""
    try:
        service = _calendar_service_for(customer_email)
        service.events().delete(calendarId="primary", eventId=event_id, sendUpdates="all").execute()
        return True, "Event deleted."
    except Exception as e:
        return False, f"Failed to delete event: {e}"


# --- Drive ---

def upload_drive_file(customer_email, filename, content_text, mime_type="text/plain"):
    try:
        from googleapiclient.http import MediaInMemoryUpload
        service = _drive_service_for(customer_email)
        media = MediaInMemoryUpload(content_text.encode(), mimetype=mime_type)
        result = service.files().create(body={"name": filename}, media_body=media, fields="id, webViewLink").execute()
        return True, f"File created: {result.get('webViewLink', '')}"
    except Exception as e:
        return False, f"Failed to upload file: {e}"


def list_drive_files(customer_email, query="", max_results=10):
    try:
        service = _drive_service_for(customer_email)
        result = service.files().list(
            q=query, pageSize=max_results, fields="files(id, name, webViewLink, modifiedTime)",
        ).execute()
        return result.get("files", [])
    except Exception as e:
        print(f"Drive list failed (non-fatal): {e}", file=sys.stderr)
        return []


def delete_drive_file(customer_email, file_id):
    try:
        service = _drive_service_for(customer_email)
        service.files().delete(fileId=file_id).execute()
        return True, "File deleted."
    except Exception as e:
        return False, f"Failed to delete file: {e}"


def share_drive_file(customer_email, file_id, share_with_email, role="reader"):
    """The real external-effect action for Drive — gives another person
    access and (by default) emails them a notification. Needs
    confirmation before this is called, same as gmail_send."""
    try:
        service = _drive_service_for(customer_email)
        service.permissions().create(
            fileId=file_id, body={"type": "user", "role": role, "emailAddress": share_with_email},
            sendNotificationEmail=True,
        ).execute()
        return True, f"Shared with {share_with_email}."
    except Exception as e:
        return False, f"Failed to share file: {e}"


# --- Docs ---

def create_doc(customer_email, title, body_text=""):
    try:
        docs_service = _docs_service_for(customer_email)
        doc = docs_service.documents().create(body={"title": title}).execute()
        doc_id = doc.get("documentId")
        if body_text:
            docs_service.documents().batchUpdate(
                documentId=doc_id,
                body={"requests": [{"insertText": {"location": {"index": 1}, "text": body_text}}]},
            ).execute()
        return True, f"Doc created: https://docs.google.com/document/d/{doc_id}/edit"
    except Exception as e:
        return False, f"Failed to create doc: {e}"


def read_doc(customer_email, doc_id):
    try:
        docs_service = _docs_service_for(customer_email)
        doc = docs_service.documents().get(documentId=doc_id).execute()
        text = ""
        for el in doc.get("body", {}).get("content", []):
            for run in el.get("paragraph", {}).get("elements", []):
                text += run.get("textRun", {}).get("content", "")
        return text
    except Exception as e:
        print(f"Doc read failed (non-fatal): {e}", file=sys.stderr)
        return None


def append_to_doc(customer_email, doc_id, text):
    try:
        docs_service = _docs_service_for(customer_email)
        doc = docs_service.documents().get(documentId=doc_id).execute()
        content = doc.get("body", {}).get("content", [])
        end_index = content[-1].get("endIndex", 1) - 1 if content else 1
        docs_service.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": [{"insertText": {"location": {"index": end_index}, "text": text}}]},
        ).execute()
        return True, "Text appended."
    except Exception as e:
        return False, f"Failed to append to doc: {e}"


# --- Sheets ---

def create_sheet(customer_email, title):
    try:
        sheets_service = _sheets_service_for(customer_email)
        result = sheets_service.spreadsheets().create(body={"properties": {"title": title}}).execute()
        sheet_id = result.get("spreadsheetId")
        return True, f"Sheet created: https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    except Exception as e:
        return False, f"Failed to create sheet: {e}"


def read_sheet_range(customer_email, sheet_id, range_a1):
    try:
        sheets_service = _sheets_service_for(customer_email)
        result = sheets_service.spreadsheets().values().get(spreadsheetId=sheet_id, range=range_a1).execute()
        return result.get("values", [])
    except Exception as e:
        print(f"Sheet read failed (non-fatal): {e}", file=sys.stderr)
        return None


def write_sheet_range(customer_email, sheet_id, range_a1, values):
    try:
        sheets_service = _sheets_service_for(customer_email)
        sheets_service.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=range_a1, valueInputOption="USER_ENTERED",
            body={"values": values},
        ).execute()
        return True, "Sheet updated."
    except Exception as e:
        return False, f"Failed to write to sheet: {e}"


# --- Tasks ---

def create_task(customer_email, title, notes="", due_iso=None):
    try:
        service = _tasks_service_for(customer_email)
        body = {"title": title, "notes": notes}
        if due_iso:
            body["due"] = due_iso
        service.tasks().insert(tasklist="@default", body=body).execute()
        return True, f"Task created: {title}"
    except Exception as e:
        return False, f"Failed to create task: {e}"


def list_tasks(customer_email, show_completed=False):
    try:
        service = _tasks_service_for(customer_email)
        result = service.tasks().list(tasklist="@default", showCompleted=show_completed).execute()
        return [{"id": t["id"], "title": t.get("title", ""), "status": t.get("status", "")}
                for t in result.get("items", [])]
    except Exception as e:
        print(f"Tasks list failed (non-fatal): {e}", file=sys.stderr)
        return []


def complete_task(customer_email, task_id):
    try:
        service = _tasks_service_for(customer_email)
        service.tasks().patch(tasklist="@default", task=task_id, body={"status": "completed"}).execute()
        return True, "Task marked complete."
    except Exception as e:
        return False, f"Failed to complete task: {e}"


def delete_task(customer_email, task_id):
    try:
        service = _tasks_service_for(customer_email)
        service.tasks().delete(tasklist="@default", task=task_id).execute()
        return True, "Task deleted."
    except Exception as e:
        return False, f"Failed to delete task: {e}"


# --- Contacts (People API) ---

def create_contact(customer_email, name, contact_email=None, phone=None):
    try:
        service = _people_service_for(customer_email)
        body = {"names": [{"givenName": name}]}
        if contact_email:
            body["emailAddresses"] = [{"value": contact_email}]
        if phone:
            body["phoneNumbers"] = [{"value": phone}]
        service.people().createContact(body=body).execute()
        return True, f"Contact created: {name}"
    except Exception as e:
        return False, f"Failed to create contact: {e}"


def list_contacts(customer_email, max_results=20):
    try:
        service = _people_service_for(customer_email)
        result = service.people().connections().list(
            resourceName="people/me", pageSize=max_results,
            personFields="names,emailAddresses,phoneNumbers",
        ).execute()
        contacts = []
        for p in result.get("connections", []):
            names = p.get("names", [{}])
            emails = p.get("emailAddresses", [{}])
            contacts.append({
                "name": names[0].get("displayName", "") if names else "",
                "email": emails[0].get("value", "") if emails else "",
            })
        return contacts
    except Exception as e:
        print(f"Contacts list failed (non-fatal): {e}", file=sys.stderr)
        return []


def check_signup_inbox(customer_email: str, search_query: str = "newer_than:1d") -> list[dict]:
    """Thin, backwards-compatible alias — the specific case of checking
    for a recent verification email is just a search, now that
    search_emails() exists as the general-purpose version."""
    return search_emails(customer_email, query=search_query, max_results=5)


def provision_phone_number(area_code: str) -> dict:
    """
    Search for and order a Telnyx DID matching the customer's area code.
    Returns dict with 'phone_number' and 'id' (the Telnyx phone number SID).
    Raises on any failure — number provisioning failing at signup is a
    hard error, not something to silently swallow.
    """
    # Step 1: search available numbers in that area code
    search = http.get(
        f"{TELNYX_API_BASE}/available_phone_numbers",
        headers=_telnyx_headers(),
        params={"filter[national_destination_code]": area_code,
                "filter[features][]": "sms", "page[size]": 1},
        timeout=15,
    )
    search.raise_for_status()
    results = search.json().get("data", [])
    if not results:
        raise ValueError(f"No SMS-capable numbers available in area code {area_code}")

    number = results[0]["phone_number"]

    # Step 2: order it
    order = http.post(
        f"{TELNYX_API_BASE}/number_orders",
        headers=_telnyx_headers(),
        json={"phone_numbers": [{"phone_number": number}]},
        timeout=15,
    )
    order.raise_for_status()
    order_data = order.json().get("data", {})
    number_records = order_data.get("phone_numbers", [{}])
    phone_sid = number_records[0].get("id", "") if number_records else ""
    return {"phone_number": number, "phone_sid": phone_sid}


def release_phone_number(phone_sid: str):
    """Release a Telnyx DID immediately on cancellation."""
    resp = http.delete(
        f"{TELNYX_API_BASE}/phone_numbers/{phone_sid}",
        headers=_telnyx_headers(),
        timeout=15,
    )
    resp.raise_for_status()


def verify_telnyx_signature(request_body: bytes, signature: str, timestamp: str) -> bool:
    """
    Verify that an incoming webhook is genuinely from Telnyx, not
    someone spoofing the endpoint. Uses the shared webhook secret.
    Returns True if valid, False otherwise.
    """
    if not TELNYX_WEBHOOK_SECRET:
        print("WARNING: TELNYX_WEBHOOK_SECRET not set — skipping webhook signature verification. "
              "Set this in production.", file=sys.stderr)
        return True  # allow in dev, never in prod
    signed_payload = timestamp + "|" + request_body.decode()
    expected = hmac.new(
        TELNYX_WEBHOOK_SECRET.encode(),
        signed_payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ── Session key store (Option B in-memory unlock cache) ───────────────────────
# Plain dict keyed by customer_id → (api_key, expires_at).
# Daemon thread cleans it up; never written to disk.

_session_keys: dict[str, tuple[str, float]] = {}
_session_lock = threading.Lock()
SESSION_TTL_SECONDS = 8 * 60 * 60  # 8 hours


def store_session_key(customer_id: str, api_key: str):
    with _session_lock:
        _session_keys[customer_id] = (api_key, time.time() + SESSION_TTL_SECONDS)


def get_session_key(customer_id: str) -> str | None:
    with _session_lock:
        entry = _session_keys.get(customer_id)
        if entry and entry[1] > time.time():
            return entry[0]
        _session_keys.pop(customer_id, None)
        return None


def _session_cleanup():
    while True:
        time.sleep(300)
        now = time.time()
        with _session_lock:
            expired = [k for k, (_, exp) in _session_keys.items() if exp <= now]
            for k in expired:
                del _session_keys[k]

threading.Thread(target=_session_cleanup, daemon=True).start()


# ── Webhook: incoming SMS ──────────────────────────────────────────────────────

@app.route("/webhooks/sms", methods=["POST"])
def sms_webhook():
    # Signature verification
    body      = request.get_data()
    sig       = request.headers.get("telnyx-signature-ed25519", "")
    timestamp = request.headers.get("telnyx-timestamp", "")
    if not verify_telnyx_signature(body, sig, timestamp):
        abort(403)

    data    = request.get_json(force=True)
    payload = data.get("data", {}).get("payload", {})
    direction = payload.get("direction", "")
    if direction != "inbound":
        return jsonify({"ok": True})  # outbound delivery receipt, ignore

    from_number = payload.get("from", {}).get("phone_number", "")
    to_number   = payload.get("to", [{}])[0].get("phone_number", "")
    user_text   = payload.get("text", "").strip()

    if not from_number or not user_text:
        return jsonify({"ok": True})

    # Rate limit: 30 messages / 60 seconds per phone number
    if not _check_rate(f"sms:{from_number}", 30, 60):
        return jsonify({"ok": True})

    customer = get_customer_by_phone(from_number)
    if not customer or not customer["active"]:
        send_sms(from_number, to_number,
                 "This number is not associated with an active Curant account. "
                 "Sign up at curant.app/cloud")
        return jsonify({"ok": True})

    # Get API key — either from server storage or in-memory session (Option B)
    session_key = get_session_key(customer["id"])
    reply = generate_reply(customer, user_text, session_api_key=session_key)

    if reply is None:
        # Option B customer with no active session — send unlock link
        token = secrets.token_urlsafe(32)
        expires = time.time() + 3600  # 1 hour to unlock
        with closing(get_db()) as conn:
            conn.execute(
                "UPDATE customers SET session_token=?, session_expires_at=? WHERE id=?",
                (token, expires, customer["id"]),
            )
            conn.commit()
        unlock_url = url_for("unlock_page", token=token, _external=True)
        send_sms(from_number, to_number,
                 f"Tap to unlock your Curant and I'll answer: {unlock_url}\n"
                 f"(Link valid for 1 hour)")
    else:
        send_sms(from_number, to_number, reply)

    return jsonify({"ok": True})


# ── Webhook: Vapi voice calls ──────────────────────────────────────────────────

@app.route("/vapi-llm/<customer_id>", methods=["POST"])
def vapi_custom_llm(customer_id):
    """
    The actual fix for the Vapi voice key-choice gap. Vapi calls this
    endpoint (instead of calling Anthropic directly with its own
    account-level key) because assistant-request told it to, with this
    customer's own ID baked into the URL. Accepts and returns the
    OpenAI-compatible chat completions shape Vapi's Custom LLM provider
    expects, but internally uses THIS customer's own key — Option A
    (decrypted server-side) or Option B (only if they have an active
    unlocked session, same real limitation the SMS unlock flow already
    has for calls arriving with no prior unlock).

    Streaming, resolved: Vapi's own docs confirm it sends `stream: true`
    in its request and can handle either a plain JSON response or a
    real SSE stream — but real reports on Vapi's own support forum show
    genuine practical trouble even with seemingly-correct non-streaming
    responses, and Vapi's own official example repos ship SSE as their
    primary recommended pattern. This honors Vapi's actual `stream` flag:
    real token-by-token SSE when requested (the common case), the
    original complete-response JSON as a fallback otherwise. The SSE
    chunk shape (`delta` field, not `message`) was confirmed by reading
    a real solved case on Vapi's own support forum, not guessed at.
    """
    customer = get_customer(customer_id)
    if not customer:
        return jsonify({"error": {"message": "Unknown customer"}}), 404

    session_key = get_session_key(customer_id)
    api_key = get_active_api_key(customer, session_key)
    if not api_key:
        # Option B customer with no active unlocked session — there's no
        # key available to answer with. Returned in OpenAI's own error
        # shape so Vapi's error handling recognizes it, rather than a
        # raw 500 that would look like our server broke.
        return jsonify({"error": {
            "message": "This customer's API key isn't unlocked right now — "
                       "Option B requires an active session, same as SMS.",
        }}), 401

    body = request.get_json(force=True)
    messages = body.get("messages", [])
    wants_stream = body.get("stream", False)
    # OpenAI-shaped messages include the system prompt as a role:"system"
    # entry in the array itself, unlike Anthropic's separate `system`
    # param — split it out here since call_llm()/call_llm_streaming()
    # expect Anthropic's shape.
    system_prompt = ""
    conversation = []
    for m in messages:
        if m.get("role") == "system":
            system_prompt = m.get("content", "")
        else:
            conversation.append({"role": m.get("role", "user"), "content": m.get("content", "")})

    provider = customer.get("api_provider", "anthropic")
    completion_id = f"chatcmpl-{secrets.token_hex(12)}"

    if wants_stream:
        def sse_generate():
            try:
                for text_chunk in call_llm_streaming(provider, api_key, system_prompt, conversation):
                    chunk_payload = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "model": "claude-sonnet-4-6",
                        "choices": [{"index": 0, "delta": {"content": text_chunk}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(chunk_payload)}\n\n"
                final_payload = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "model": "claude-sonnet-4-6",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                yield f"data: {json.dumps(final_payload)}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as e:
                print(f"Vapi streaming call failed for {customer_id}: {e}", file=sys.stderr)
                error_payload = {"error": {"message": "Upstream LLM call failed"}}
                yield f"data: {json.dumps(error_payload)}\n\n"

        return Response(sse_generate(), mimetype="text/event-stream")

    try:
        reply_text = call_llm(provider, api_key, system_prompt, conversation)
    except Exception as e:
        print(f"Vapi custom-LLM call failed for {customer_id}: {e}", file=sys.stderr)
        return jsonify({"error": {"message": "Upstream LLM call failed"}}), 502

    # OpenAI chat completions response shape — the non-streaming
    # fallback, kept working as-is for any caller that doesn't set
    # stream: true.
    return jsonify({
        "id": completion_id,
        "object": "chat.completion",
        "model": "claude-sonnet-4-6",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": reply_text},
            "finish_reason": "stop",
        }],
    })


@app.route("/webhooks/vapi", methods=["POST"])
def vapi_webhook():
    """
    Vapi webhook for voice call handling. Vapi calls this endpoint with
    call state and transcribed speech; we respond with the Curant reply.
    The customer's phone number is in the call metadata — we route it the
    same way as SMS.

    VERIFIED against Vapi's current docs before building this further:
      - "assistant-request" is confirmed as the exact message type name
        Vapi sends when it needs to know which assistant config to use
        for an incoming call.
      - Vapi enforces a HARD 7.5-second end-to-end response deadline on
        this specific webhook (the telephony provider caps at 15s, Vapi
        reserves ~7.5s of that for call setup) — this is fixed, not
        configurable. Their own guidance: return minimal config fast,
        enrich context asynchronously afterward via Live Call Control
        rather than doing anything slow here. What's implemented below
        is only local DB reads (memories/people, capped at 20 rows) plus
        string formatting — no LLM call, no network call — specifically
        so this stays well under the deadline. If this ever gets slower
        (e.g. a future version calls out to something over the network),
        that budget needs to be actively protected, not assumed.
      - NOT independently re-verified this pass: the exact JSON response
        shape ({"assistant": {...}} at the top level) and "end-of-call-report"
        as the literal type string for the post-call event. Both match
        Vapi's documented naming conventions and this is built on prior
        research, but — same standard as the FLUX/Veo work — flagging
        that this specific detail should be confirmed against a live
        test call before trusting it in front of a real customer.
    """
    data = request.get_json(force=True)
    message = data.get("message", {})
    msg_type = message.get("type", "")

    if msg_type == "assistant-request":
        caller = (message.get("call", {})
                  .get("customer", {})
                  .get("number", ""))
        customer = get_customer_by_phone(caller) if caller else None
        persona = customer.get("persona", "curant") if customer else "curant"
        system = build_system_prompt(
            customer or {},
            get_memories(customer["id"]) if customer else [],
            get_important_people(customer["id"]) if customer else [],
            channel="voice",
        )

        # THE REAL FIX for the previously-flagged gap: SMS respects each
        # customer's Option A/B key choice, but voice never did — every
        # call used whatever key was configured in Vapi's own dashboard,
        # shared across every customer. Confirmed via research this is a
        # genuine structural constraint (Vapi's "bring your own key"
        # system is account-level dashboard config, not something
        # passable per-call) — not a bug fixable with a small patch.
        # The real fix: Vapi's "Custom LLM" provider mode lets US host
        # the actual LLM-calling endpoint instead of Vapi calling
        # Anthropic directly. Since we control the URL Vapi is told to
        # call, the customer's own ID gets baked directly into that URL
        # — solving the routing problem with no extra metadata mechanism
        # needed. See vapi_custom_llm() below for the actual per-customer
        # key lookup and forwarding.
        if customer:
            model_block = {
                "provider": "custom-llm",
                "url": f"{CLOUD_PUBLIC_URL}/vapi-llm/{customer['id']}",
                "model": "claude-sonnet-4-6",
                "systemPrompt": system,
            }
        else:
            # No known customer to route a per-customer key to — falls
            # back to Vapi's own account-level key so an unrecognized
            # caller at least gets *a* response, rather than nothing.
            model_block = {
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "systemPrompt": system,
            }

        return jsonify({
            "assistant": {
                "model": model_block,
                "voice": {
                    "provider": "elevenlabs",
                    "voiceId": PERSONA_VOICE_IDS.get(persona, PERSONA_VOICE_IDS["curant"]),
                },
                "firstMessage": f"Hi, this is {persona.title()}. How can I help?",
            }
        })

    if msg_type == "end-of-call-report":
        # Call ended — save the transcript as a message pair for memory purposes
        transcript = message.get("transcript", "")
        call_info  = message.get("call", {})
        caller     = call_info.get("customer", {}).get("number", "")
        customer   = get_customer_by_phone(caller) if caller else None
        if customer and transcript:
            save_message(customer["id"], "user", f"[Voice call transcript]: {transcript}")
        return jsonify({"ok": True})

    return jsonify({"ok": True})


# ── Option B: browser unlock page ─────────────────────────────────────────────

@app.route("/unlock/<token>")
def unlock_page(token):
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT * FROM customers WHERE session_token=? AND session_expires_at > ?",
            (token, time.time()),
        ).fetchone()
    if not row:
        return "<p>This unlock link has expired or is invalid. Your Curant will send a new one next time you text.</p>", 400

    return render_template_string("""
    <!DOCTYPE html>
    <html><head><title>Unlock your Curant</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
      body { font-family: -apple-system, sans-serif; max-width: 400px;
             margin: 80px auto; padding: 0 24px; color: #1a1a1a; }
      h1 { font-size: 1.3rem; margin-bottom: 4px; }
      p  { color: #555; font-size: 0.9rem; line-height: 1.5; }
      input[type=password] { width: 100%; padding: 10px; margin: 16px 0 8px;
                             box-sizing: border-box; font-size: 1rem; border: 1px solid #ccc; border-radius: 6px; }
      button { width: 100%; padding: 12px; background: #1a1a1a; color: #fff;
               border: none; border-radius: 6px; font-size: 1rem; cursor: pointer; }
      .error { color: #b00020; font-size: 0.9rem; margin-top: 8px; }
    </style>
    </head><body>
      <h1>Unlock your Curant</h1>
      <p>Enter your passphrase to decrypt your API key so your Curant can answer your message.
         Your key is decrypted in your browser — we never see it.</p>
      <form id="f">
        <input type="password" id="pp" placeholder="Your passphrase" autofocus autocomplete="current-password">
        <button type="submit">Unlock</button>
        <p class="error" id="err" style="display:none"></p>
      </form>
      <script>
      // The customer's encrypted key blob was stored in the DB when they set up.
      // We fetch it here, decrypt in the browser with Web Crypto, and send
      // only the plaintext API key (over HTTPS) to the server for this session.
      const TOKEN = {{ token|tojson }};
      const CIPHERTEXT_B64 = {{ ciphertext|tojson }};

      async function deriveKey(passphrase, salt) {
        const enc = new TextEncoder();
        const keyMaterial = await crypto.subtle.importKey(
          "raw", enc.encode(passphrase), "PBKDF2", false, ["deriveKey"]
        );
        return crypto.subtle.deriveKey(
          { name: "PBKDF2", salt, iterations: 310000, hash: "SHA-256" },
          keyMaterial,
          { name: "AES-GCM", length: 256 },
          false, ["decrypt"]
        );
      }

      document.getElementById("f").addEventListener("submit", async (e) => {
        e.preventDefault();
        const passphrase = document.getElementById("pp").value;
        if (!passphrase) return;
        try {
          const raw    = Uint8Array.from(atob(CIPHERTEXT_B64), c => c.charCodeAt(0));
          const salt   = raw.slice(0, 16);
          const iv     = raw.slice(16, 28);
          const ct     = raw.slice(28);
          const key    = await deriveKey(passphrase, salt);
          const plain  = await crypto.subtle.decrypt({ name: "AES-GCM", iv }, key, ct);
          const apiKey = new TextDecoder().decode(plain);
          // Send the decrypted key to the server for this session
          const resp = await fetch("/unlock/submit", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ token: TOKEN, api_key: apiKey }),
          });
          const body = await resp.json();
          if (body.ok) {
            document.body.innerHTML = "<h1>Unlocked</h1><p>Your Curant will reply to your message now. You can close this tab.</p>";
          } else {
            throw new Error(body.error || "Unknown error");
          }
        } catch (err) {
          const el = document.getElementById("err");
          el.style.display = "block";
          el.textContent = "Wrong passphrase, or the key is corrupted.";
        }
      });
      </script>
    </body></html>
    """, token=token, ciphertext=row["browser_key_ciphertext"] or "")


@app.route("/unlock/submit", methods=["POST"])
def unlock_submit():
    """
    Receives the plaintext API key from the browser after it decrypts it.
    Stores it in the in-memory session cache for SESSION_TTL_SECONDS.
    Rate limited to prevent brute-force unlock attempts.
    """
    if not _check_rate(f"unlock:{request.remote_addr}", 5, 300):
        return jsonify({"ok": False, "error": "Too many attempts — try again in a few minutes."}), 429

    data  = request.get_json(force=True)
    token = data.get("token", "")
    key   = data.get("api_key", "")

    if not token or not key:
        return jsonify({"ok": False, "error": "Missing token or key"}), 400

    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT * FROM customers WHERE session_token=? AND session_expires_at > ?",
            (token, time.time()),
        ).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Token expired or invalid"}), 400

    customer = dict(row)
    store_session_key(customer["id"], key)

    # Invalidate the one-time token
    with closing(get_db()) as conn:
        conn.execute(
            "UPDATE customers SET session_token=NULL, session_expires_at=NULL WHERE id=?",
            (customer["id"],),
        )
        conn.commit()

    # If there's a pending message in the session, retry it now
    pending = session.pop("pending_message", None)
    if pending:
        reply = generate_reply(customer, pending, session_api_key=key)
        if reply and customer.get("phone_number"):
            send_sms(customer["phone_number"], customer["phone_number"], reply)

    return jsonify({"ok": True})


# ── Signup flow ────────────────────────────────────────────────────────────────

BASE_STYLE = """
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 520px; margin: 64px auto; padding: 0 24px;
         color: #111; background: #fafafa; }
  h1  { font-size: 1.5rem; font-weight: 700; margin-bottom: 4px; letter-spacing: -.02em; }
  h2  { font-size: 1.1rem; font-weight: 600; margin: 28px 0 8px; }
  p   { color: #555; font-size: 0.9rem; line-height: 1.6; margin: 0 0 16px; }
  label { display: block; font-size: 0.85rem; font-weight: 500; margin-bottom: 4px; color: #333; }
  input[type=text], input[type=email], input[type=password] {
    width: 100%; padding: 10px 12px; border: 1px solid #ddd; border-radius: 6px;
    font-size: 0.95rem; margin-bottom: 14px; background: #fff; }
  input:focus { outline: 2px solid #111; outline-offset: 2px; }
  .btn  { display: block; width: 100%; padding: 12px; background: #111; color: #fff;
          border: none; border-radius: 6px; font-size: 0.95rem; font-weight: 600;
          cursor: pointer; letter-spacing: -.01em; }
  .btn:hover { background: #333; }
  .card { background: #fff; border: 1px solid #e5e5e5; border-radius: 10px;
          padding: 20px; margin: 12px 0; cursor: pointer; transition: border-color .15s; }
  .card:hover, .card.selected { border-color: #111; }
  .card h3 { font-size: 0.95rem; font-weight: 600; margin: 0 0 4px; }
  .card p  { font-size: 0.85rem; color: #666; margin: 0; line-height: 1.5; }
  .muted  { color: #888; font-size: 0.82rem; }
  .error  { color: #b00020; font-size: 0.88rem; margin-bottom: 12px; }
  .tag    { display: inline-block; background: #f0f0f0; border-radius: 4px;
            padding: 2px 8px; font-size: 0.78rem; font-weight: 500; margin-left: 6px; }
</style>
"""

CSRF_FIELD = '<input type="hidden" name="csrf_token" value="{}">'


def get_csrf():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(32)
    return session["csrf"]


def check_csrf():
    submitted = request.form.get("csrf_token", "")
    expected  = session.get("csrf", "")
    return expected and secrets.compare_digest(submitted, expected)


@app.route("/cloud/signup", methods=["GET", "POST"])
def signup():
    error = None
    if request.method == "POST":
        if not check_csrf():
            error = "Session expired — please try again."
        else:
            name       = request.form.get("name", "").strip()
            email      = request.form.get("email", "").strip().lower()
            area_code  = request.form.get("area_code", "").strip()
            if not name or not email:
                error = "Name and email are required."
            elif get_customer_by_email(email):
                error = "An account with that email already exists."
            else:
                cid = create_customer(name, email, area_code)
                session["signup_customer_id"] = cid
                return redirect(url_for("signup_key_choice"))

    return render_template_string(BASE_STYLE + """
    <h1>Get your Curant</h1>
    <p>A personal AI Secretary, reachable by text or call — nothing to install.</p>
    {% if error %}<p class="error">{{ error }}</p>{% endif %}
    <form method="post">
      {{ csrf }}
      <label>Your name</label>
      <input type="text" name="name" placeholder="Jamie" required autofocus>
      <label>Email</label>
      <input type="email" name="email" placeholder="you@example.com" required>
      <label>Preferred area code <span class="muted">(we'll match a local number)</span></label>
      <input type="text" name="area_code" placeholder="512" maxlength="3">
      <button class="btn" type="submit">Continue</button>
    </form>
    """, error=error, csrf=CSRF_FIELD.format(get_csrf()))


@app.route("/cloud/signup/key", methods=["GET", "POST"])
def signup_key_choice():
    cid = session.get("signup_customer_id")
    if not cid:
        return redirect(url_for("signup"))

    error = None
    if request.method == "POST":
        if not check_csrf():
            error = "Session expired — please try again."
        else:
            choice = request.form.get("choice")
            session["key_choice"] = choice
            if choice == "server":
                return redirect(url_for("signup_server_key"))
            elif choice == "browser":
                return redirect(url_for("signup_browser_key"))
            else:
                error = "Please choose an option."

    return render_template_string(BASE_STYLE + """
    <h1>One more thing</h1>
    <p>How would you like to store your AI provider API key?
       You'll need one from Anthropic (or OpenAI) — we'll guide you through it next.</p>

    <form method="post">
      {{ csrf }}
      <div class="card" onclick="pick('server')" id="c-server">
        <h3>We store it <span class="tag">Simpler</span></h3>
        <p>Your key is encrypted and stored on our server. Curant answers every message
           automatically, including proactive check-ins. If our server were ever breached,
           your key would be at risk — you can revoke it from Anthropic any time.</p>
      </div>
      <div class="card" onclick="pick('browser')" id="c-browser">
        <h3>You hold it <span class="tag">More private</span></h3>
        <p>Your key is encrypted in your browser with a passphrase only you know.
           Our server never sees it. Once per browser session, you'll tap a short link
           to unlock before Curant replies. Proactive check-ins require an active session.</p>
      </div>
      <input type="hidden" name="choice" id="choice-input">
      {% if error %}<p class="error">{{ error }}</p>{% endif %}
      <button class="btn" type="submit" style="margin-top:16px">Continue</button>
    </form>
    <script>
    function pick(v) {
      document.getElementById("choice-input").value = v;
      document.getElementById("c-server").classList.toggle("selected", v === "server");
      document.getElementById("c-browser").classList.toggle("selected", v === "browser");
    }
    </script>
    """, error=error, csrf=CSRF_FIELD.format(get_csrf()))


@app.route("/cloud/signup/key/server", methods=["GET", "POST"])
def signup_server_key():
    cid = session.get("signup_customer_id")
    if not cid:
        return redirect(url_for("signup"))

    error = None
    if request.method == "POST":
        if not check_csrf():
            error = "Session expired."
        else:
            api_key  = request.form.get("api_key", "").strip()
            provider = request.form.get("provider", "anthropic")
            if not api_key:
                error = "Please enter your API key."
            else:
                store_key_server_side(cid, api_key, provider)
                return redirect(url_for("signup_provision"))

    return render_template_string(BASE_STYLE + """
    <h1>Add your API key</h1>
    <p>Get a free API key from
      <a href="https://console.anthropic.com/settings/keys" target="_blank">Anthropic</a>
      or <a href="https://platform.openai.com/api-keys" target="_blank">OpenAI</a>.
      It's stored encrypted on our server — only used to answer your messages.</p>
    {% if error %}<p class="error">{{ error }}</p>{% endif %}
    <form method="post">
      {{ csrf }}
      <label>API key</label>
      <input type="password" name="api_key" placeholder="sk-ant-..." required autocomplete="off">
      <label>Provider</label>
      <select name="provider" style="width:100%;padding:10px;margin-bottom:14px;border:1px solid #ddd;border-radius:6px;font-size:.95rem">
        <option value="anthropic">Anthropic (Claude)</option>
        <option value="openai">OpenAI (GPT-4o)</option>
      </select>
      <button class="btn" type="submit">Continue</button>
    </form>
    """, error=error, csrf=CSRF_FIELD.format(get_csrf()))


@app.route("/cloud/signup/key/browser", methods=["GET", "POST"])
def signup_browser_key():
    """
    Option B signup: the browser encrypts the API key with a customer-chosen
    passphrase using Web Crypto (PBKDF2 + AES-GCM) and posts only the
    ciphertext to this endpoint. We store it without ever seeing the plaintext.
    """
    cid = session.get("signup_customer_id")
    if not cid:
        return redirect(url_for("signup"))

    if request.method == "POST":
        if not check_csrf():
            return jsonify({"ok": False, "error": "Session expired"}), 400
        ciphertext_b64 = request.form.get("ciphertext", "").strip()
        if not ciphertext_b64:
            return jsonify({"ok": False, "error": "No ciphertext received"}), 400
        store_browser_ciphertext(cid, ciphertext_b64)
        session["browser_key_stored"] = True
        return jsonify({"ok": True})

    return render_template_string(BASE_STYLE + """
    <h1>Encrypt your key</h1>
    <p>Enter your API key and choose a passphrase. Your key is encrypted in your browser
       before anything leaves this page — we never see it.</p>
    <div id="form-area">
      <label>API key</label>
      <input type="password" id="api-key" placeholder="sk-ant-..." autocomplete="off">
      <label>Choose a passphrase <span class="muted">(you'll need this to unlock)</span></label>
      <input type="password" id="passphrase" placeholder="Something memorable" autocomplete="new-password">
      <label>Confirm passphrase</label>
      <input type="password" id="passphrase2" placeholder="Same passphrase again" autocomplete="new-password">
      <p class="error" id="err" style="display:none"></p>
      <button class="btn" id="encrypt-btn">Encrypt and continue</button>
    </div>
    <div id="done" style="display:none">
      <p>Key encrypted. Redirecting…</p>
    </div>
    <script>
    const CSRF = {{ csrf_value|tojson }};

    async function deriveKey(passphrase, salt) {
      const enc = new TextEncoder();
      const km  = await crypto.subtle.importKey("raw", enc.encode(passphrase),
                                                "PBKDF2", false, ["deriveKey"]);
      return crypto.subtle.deriveKey(
        { name: "PBKDF2", salt, iterations: 310000, hash: "SHA-256" },
        km, { name: "AES-GCM", length: 256 }, false, ["encrypt"]
      );
    }

    document.getElementById("encrypt-btn").addEventListener("click", async () => {
      const apiKey = document.getElementById("api-key").value;
      const pp     = document.getElementById("passphrase").value;
      const pp2    = document.getElementById("passphrase2").value;
      const err    = document.getElementById("err");

      if (!apiKey || !pp) { err.style.display="block"; err.textContent="Both fields required."; return; }
      if (pp !== pp2)      { err.style.display="block"; err.textContent="Passphrases don't match."; return; }
      err.style.display = "none";

      const salt  = crypto.getRandomValues(new Uint8Array(16));
      const iv    = crypto.getRandomValues(new Uint8Array(12));
      const key   = await deriveKey(pp, salt);
      const ct    = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key,
                                                new TextEncoder().encode(apiKey));
      const blob  = new Uint8Array([...salt, ...iv, ...new Uint8Array(ct)]);
      const b64   = btoa(String.fromCharCode(...blob));

      const fd = new FormData();
      fd.append("ciphertext", b64);
      fd.append("csrf_token", CSRF);
      const resp = await fetch(window.location.pathname, { method: "POST", body: fd });
      const body = await resp.json();
      if (body.ok) {
        document.getElementById("form-area").style.display = "none";
        document.getElementById("done").style.display      = "block";
        setTimeout(() => window.location = "/cloud/signup/provision", 1200);
      } else {
        err.style.display = "block";
        err.textContent   = body.error || "Something went wrong.";
      }
    });
    </script>
    """, csrf_value=get_csrf())


@app.route("/cloud/signup/provision", methods=["GET", "POST"])
def signup_provision():
    """
    Final signup step: provision a Telnyx phone number for the customer.
    On success: customer is active, gets shown their number.
    """
    cid = session.get("signup_customer_id")
    if not cid:
        return redirect(url_for("signup"))

    customer = get_customer(cid)
    if not customer:
        return redirect(url_for("signup"))

    error = None
    number_info = None

    if request.method == "POST" or request.args.get("auto"):
        try:
            area_code  = customer.get("area_code") or "800"
            number_info = provision_phone_number(area_code)
            pn  = number_info["phone_number"]
            sid = number_info["phone_sid"]
            with closing(get_db()) as conn:
                conn.execute(
                    "UPDATE customers SET phone_number=?, phone_sid=?, active=1 WHERE id=?",
                    (pn, sid, cid),
                )
                conn.execute(
                    "INSERT INTO phone_routing (phone_number, customer_id) VALUES (?, ?)",
                    (pn, cid),
                )
                conn.commit()

            # Workspace utility email — best-effort, non-blocking. The phone
            # number is the thing that actually matters for signup to
            # succeed; the utility email is an enhancement Curant uses for
            # account-signup verification codes, not something that should
            # block a customer getting activated if it's not configured yet
            # or a provisioning call fails.
            try:
                name_parts = (customer.get("name") or "").split(" ", 1)
                first = name_parts[0] if name_parts else "Curant"
                last  = name_parts[1] if len(name_parts) > 1 else "User"
                workspace_info = provision_workspace_account(cid, first, last)
                with closing(get_db()) as conn:
                    conn.execute(
                        "UPDATE customers SET workspace_email=?, workspace_user_id=? WHERE id=?",
                        (workspace_info["email"], workspace_info["workspace_user_id"], cid),
                    )
                    conn.commit()
            except Exception as e:
                print(f"Workspace provisioning skipped for {cid} (non-fatal): {e}", file=sys.stderr)

            # Send a welcome SMS — reflects real capability breadth rather
            # than a generic "text me anything" greeting, which is exactly
            # the onboarding gap this was built to close. Kept short (SMS),
            # since the model's own first-message onboarding instruction
            # (build_system_prompt's is_first_message flag) will do the
            # fuller introduction once the customer actually replies.
            send_sms(
                pn, pn,
                f"Hi, I'm your Curant! I can help with writing, research, scheduling, "
                f"budgeting, and a lot more — just text me like you would a person. "
                f"What's on your mind?",
            )
            session.pop("signup_customer_id", None)
            session["customer_id"] = cid
        except Exception as e:
            error = f"Couldn't provision a phone number: {e}"

    if not error and not number_info:
        return render_template_string(BASE_STYLE + """
        <h1>Almost there</h1>
        <p>We're getting your Curant phone number ready. This takes a few seconds.</p>
        <form method="post">{{ csrf }}
          <button class="btn" type="submit">Get my number</button>
        </form>
        """, csrf=CSRF_FIELD.format(get_csrf()))

    if error:
        return render_template_string(BASE_STYLE + """
        <h1>Something went wrong</h1>
        <p class="error">{{ error }}</p>
        <p>Please <a href="/cloud/signup/provision">try again</a> or contact support.</p>
        """, error=error)

    return render_template_string(BASE_STYLE + """
    <h1>You're all set.</h1>
    <p>Your Curant is ready. Text this number from your phone:</p>
    <div style="font-size:2rem;font-weight:700;letter-spacing:.05em;margin:24px 0">
      {{ number }}
    </div>
    <p class="muted">Save it as a contact — "My Curant" works well.
       The number is matched to your area code and is yours for as long as you're subscribed.</p>
    <p><a href="/cloud/dashboard">Go to your dashboard →</a></p>
    """, number=customer.get("phone_number", ""))


# ── Customer dashboard ─────────────────────────────────────────────────────────

def require_customer(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("customer_id"):
            return redirect(url_for("customer_login"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/cloud/login", methods=["GET", "POST"])
def customer_login():
    error = None
    if request.method == "POST":
        if not check_csrf():
            error = "Session expired."
        elif not _check_rate(f"login:{request.remote_addr}", 5, 300):
            error = "Too many attempts — try again later."
        else:
            email = request.form.get("email", "").strip().lower()
            cust  = get_customer_by_email(email)
            if cust and cust["active"]:
                session["customer_id"] = cust["id"]
                return redirect(url_for("cloud_dashboard"))
            error = "No active account with that email."

    return render_template_string(BASE_STYLE + """
    <h1>Log in to Curant</h1>
    {% if error %}<p class="error">{{ error }}</p>{% endif %}
    <form method="post">
      {{ csrf }}
      <label>Email</label>
      <input type="email" name="email" required autofocus>
      <button class="btn" type="submit">Log in</button>
    </form>
    <p class="muted" style="margin-top:16px">Don't have an account?
      <a href="/cloud/signup">Sign up →</a></p>
    """, error=error, csrf=CSRF_FIELD.format(get_csrf()))


@app.route("/cloud/dashboard", methods=["GET", "POST"])
@require_customer
def cloud_dashboard():
    cid      = session["customer_id"]
    customer = get_customer(cid)
    if not customer:
        session.pop("customer_id", None)
        return redirect(url_for("customer_login"))

    message = None
    if request.method == "POST":
        if not check_csrf():
            message = "Session expired."
        else:
            action = request.form.get("action")
            if action == "update_persona":
                persona = request.form.get("persona", "curant")
                instructions = request.form.get("instructions", "")
                with closing(get_db()) as conn:
                    conn.execute(
                        "UPDATE customers SET persona=?, instructions=? WHERE id=?",
                        (persona, instructions, cid),
                    )
                    conn.commit()
                message = "Settings saved."
            elif action == "cancel":
                return redirect(url_for("cloud_cancel"))

    memories = get_memories(cid)
    people   = get_important_people(cid)
    customer = get_customer(cid)  # refresh

    return render_template_string(BASE_STYLE + """
    <h1>Your Curant</h1>
    <p class="muted">Your number: <strong>{{ customer.phone_number or "Being provisioned…" }}</strong></p>
    {% if message %}<p style="color:#1a7a1a">{{ message }}</p>{% endif %}

    <h2>Persona & instructions</h2>
    <form method="post">
      {{ csrf }}
      <input type="hidden" name="action" value="update_persona">
      <label>Persona</label>
      <select name="persona" style="width:100%;padding:10px;margin-bottom:14px;border:1px solid #ddd;border-radius:6px;font-size:.95rem">
        {% for p in personas %}
        <option value="{{ p }}" {% if customer.persona == p %}selected{% endif %}>{{ p.title() }}</option>
        {% endfor %}
      </select>
      <label>Standing instructions <span class="muted">(optional)</span></label>
      <input type="text" name="instructions" value="{{ customer.instructions or '' }}" placeholder="e.g. Keep replies under 3 sentences">
      <button class="btn" type="submit">Save</button>
    </form>

    <h2>What Curant remembers about you</h2>
    {% if memories %}
    {% for m in memories %}
    <div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #f0f0f0">
      <span style="font-size:.9rem">{{ m }}</span>
      <form method="post" style="display:inline">
        {{ csrf }}
        <input type="hidden" name="action" value="delete_memory">
        <input type="hidden" name="content" value="{{ m }}">
        <button type="submit" style="background:none;border:none;color:#888;cursor:pointer;font-size:.8rem">Forget</button>
      </form>
    </div>
    {% endfor %}
    {% else %}
    <p class="muted">Nothing yet — Curant learns as you chat.</p>
    {% endif %}

    <h2>People who matter to you</h2>
    {% if people %}
    {% for p in people %}
    <div style="font-size:.9rem;padding:6px 0;border-bottom:1px solid #f0f0f0">
      <strong>{{ p.name }}</strong> ({{ p.relationship }}) — {{ p.note }}
    </div>
    {% endfor %}
    {% else %}
    <p class="muted">None yet — tell Curant who matters to you in a text.</p>
    {% endif %}

    <div style="margin-top:40px;padding-top:20px;border-top:1px solid #eee">
      <form method="post">
        {{ csrf }}
        <input type="hidden" name="action" value="cancel">
        <button type="submit" style="background:none;border:none;color:#888;cursor:pointer;font-size:.85rem;text-decoration:underline">
          Cancel subscription
        </button>
      </form>
    </div>
    """,
    customer=customer, memories=memories, people=people,
    personas=list(PERSONAS.keys()),
    csrf=CSRF_FIELD.format(get_csrf()))


@app.route("/cloud/cancel", methods=["GET", "POST"])
@require_customer
def cloud_cancel():
    cid      = session["customer_id"]
    customer = get_customer(cid)
    if not customer:
        return redirect(url_for("customer_login"))

    done  = False
    error = None

    if request.method == "POST":
        if not check_csrf():
            error = "Session expired."
        elif request.form.get("confirm") == "yes":
            try:
                # 1. Release the Telnyx number immediately
                if customer.get("phone_sid"):
                    release_phone_number(customer["phone_sid"])
                # 2. Delete the Workspace utility account too — best-effort,
                #    same reasoning as provisioning: shouldn't block
                #    cancellation if this fails or was never configured.
                if customer.get("workspace_email"):
                    try:
                        deprovision_workspace_account(customer["workspace_email"])
                    except Exception as e:
                        print(f"Workspace deprovisioning failed for {cid} (non-fatal): {e}", file=sys.stderr)
                # 3. Delete/archive the routing entry at the same moment —
                #    this is what prevents a reassigned number from routing
                #    to a stale account.
                with closing(get_db()) as conn:
                    conn.execute(
                        "UPDATE phone_routing SET active=0 WHERE customer_id=?",
                        (cid,),
                    )
                    conn.execute(
                        "UPDATE customers SET active=0, phone_number=NULL, phone_sid=NULL, "
                        "workspace_email=NULL, workspace_user_id=NULL WHERE id=?",
                        (cid,),
                    )
                    conn.commit()
                session.pop("customer_id", None)
                done = True
            except Exception as e:
                error = f"Something went wrong cancelling: {e}"

    if done:
        return render_template_string(BASE_STYLE + """
        <h1>Cancelled</h1>
        <p>Your number has been released and your account is closed. Thanks for trying Curant.</p>
        """)

    return render_template_string(BASE_STYLE + """
    <h1>Cancel your subscription?</h1>
    <p>Your phone number will be <strong>released immediately</strong> — you'll lose it
       and won't be able to text your Curant anymore. Your memories and settings will be
       deleted from our servers.</p>
    <p>This can't be undone.</p>
    {% if error %}<p class="error">{{ error }}</p>{% endif %}
    <form method="post">
      {{ csrf }}
      <input type="hidden" name="confirm" value="yes">
      <button class="btn" type="submit" style="background:#b00020">Yes, cancel and release my number</button>
    </form>
    <p style="margin-top:12px"><a href="/cloud/dashboard">← Go back</a></p>
    """, error=error, csrf=CSRF_FIELD.format(get_csrf()))


# ── Owner dashboard ────────────────────────────────────────────────────────────

def require_admin(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("owner_login"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/owner/login", methods=["GET", "POST"])
def owner_login():
    if session.get("is_admin"):
        return redirect(url_for("owner_dashboard"))
    error = None
    if request.method == "POST":
        if not ADMIN_PASSWORD:
            error = "Owner login is disabled — CLOUD_ADMIN_PASSWORD not set."
        elif not _check_rate(f"owner:{request.remote_addr}", 5, 300):
            error = "Too many attempts."
        elif not check_csrf():
            error = "Session expired."
        elif secrets.compare_digest(request.form.get("password", ""), ADMIN_PASSWORD):
            session["is_admin"] = True
            return redirect(url_for("owner_dashboard"))
        else:
            error = "Incorrect password."

    return render_template_string(BASE_STYLE + """
    <h1>Owner login</h1>
    {% if error %}<p class="error">{{ error }}</p>{% endif %}
    <form method="post">{{ csrf }}
      <input type="password" name="password" required autofocus>
      <button class="btn" type="submit">Log in</button>
    </form>
    """, error=error, csrf=CSRF_FIELD.format(get_csrf()))


@app.route("/owner/dashboard")
@require_admin
def owner_dashboard():
    with closing(get_db()) as conn:
        customers = conn.execute(
            """SELECT id, name, email, phone_number, plan, active,
                      key_mode, created_at
               FROM customers ORDER BY created_at DESC"""
        ).fetchall()
    return render_template_string(BASE_STYLE + """
    <h1>Cloud customers</h1>
    <table style="width:100%;border-collapse:collapse;font-size:.85rem">
      <tr style="border-bottom:2px solid #eee">
        <th style="text-align:left;padding:6px">Name</th>
        <th style="text-align:left;padding:6px">Email</th>
        <th style="text-align:left;padding:6px">Number</th>
        <th style="text-align:left;padding:6px">Key mode</th>
        <th style="text-align:left;padding:6px">Active</th>
      </tr>
      {% for c in customers %}
      <tr style="border-bottom:1px solid #f0f0f0">
        <td style="padding:6px">{{ c.name }}</td>
        <td style="padding:6px">{{ c.email }}</td>
        <td style="padding:6px">{{ c.phone_number or "—" }}</td>
        <td style="padding:6px">{{ c.key_mode }}</td>
        <td style="padding:6px">{{ "Yes" if c.active else "No" }}</td>
      </tr>
      {% endfor %}
    </table>
    <p class="muted" style="margin-top:16px">{{ customers|length }} customer(s)</p>
    <p><a href="/owner/logout">Log out</a></p>
    """, customers=customers)


@app.route("/owner/logout")
def owner_logout():
    session.pop("is_admin", None)
    return redirect(url_for("owner_login"))


# ── Health check ───────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    try:
        with closing(get_db()) as conn:
            conn.execute("SELECT 1").fetchone()
        return jsonify({"status": "ok", "tier": "cloud"})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 503


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5051))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
