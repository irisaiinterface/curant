#!/usr/bin/env python3
"""
curant-watcher — runs on the Mac Mini, watches Messages for new
incoming texts or voice memos from a customer, hands them to curant-cli
(which now handles everything locally — persona, memory, and the actual
call to Claude — using the customer's own API key), and sends the reply
back. Curant's own server is only ever contacted by curant-cli for a
lightweight, infrequent license/billing check — never with message
content.

This is the piece that replaces "you texting an iCloud account and
running Claude in Terminal" with something that runs unattended.

REQUIREMENTS BEFORE THIS WORKS:
- Full Disk Access granted to Terminal/this script (System Settings >
  Privacy & Security > Full Disk Access) — needed to read chat.db
- curant-cli installed, activated, and given an Anthropic API key:
    curant-cli activate YOUR-LICENSE-KEY
    curant-cli set-api-key sk-ant-...
- For voice memo transcription: `pip install openai-whisper --break-system-packages`
  and ffmpeg installed (`brew install ffmpeg`)
- For voice replies: a TTS setup — this stub calls a placeholder
  function `text_to_speech()` — wire in ElevenLabs/OpenAI TTS/etc. later
- For calendar/reminders context: `brew install ical-buddy`

This script polls chat.db every few seconds rather than using a live
hook, which is simpler and reliable enough for a single-customer-per-Mac
setup (remember: one Curant per person, no concurrency problem here).
"""

import sqlite3
import subprocess
import time
import os
import sys
import json

# --- Configuration ---
CHAT_DB_PATH = os.path.expanduser("~/Library/Messages/chat.db")

# Who this Mac's Curant listens to. Keyed on the customer's APPLE ID (the
# email their iMessage account uses), not a phone number — the Apple ID is
# the stable identity; a number can change hands or fall back to SMS.
#
# iMessage may still deliver one person's texts under more than one "handle"
# (their Apple ID email AND phone numbers tied to that account), so
# CUSTOMER_HANDLES holds every handle that maps to this one customer: a text
# from any of them is answered, and the reply always goes back to whichever
# handle they actually used (handle_message replies to msg["sender"]).
#
# Set WITHOUT editing this file — environment takes precedence, else
# ~/.curant/config.json:
#   env:    CURANT_CUSTOMER_APPLE_ID="name@icloud.com"
#           CURANT_CUSTOMER_HANDLES="+15551234567,alt@me.com"   (optional extras)
#   config: {"customer_apple_id": "name@icloud.com",
#            "customer_handles": ["+15551234567"]}
def _read_customer_handles():
    cfg = {}
    _cfg_path = os.path.expanduser("~/.curant/config.json")
    if os.path.exists(_cfg_path):
        try:
            with open(_cfg_path) as _f:
                cfg = json.load(_f)
        except Exception:
            cfg = {}
    primary = (os.environ.get("CURANT_CUSTOMER_APPLE_ID")
               or cfg.get("customer_apple_id") or "").strip()
    extra = os.environ.get("CURANT_CUSTOMER_HANDLES") or cfg.get("customer_handles") or ""
    extra = extra if isinstance(extra, list) else [h.strip() for h in str(extra).split(",")]
    ordered, seen = [], set()
    for h in [primary, *extra]:
        h = (h or "").strip()
        if h and h not in seen:
            seen.add(h); ordered.append(h)
    # Primary (used as the proactive-message target) defaults to the first
    # handle if only CUSTOMER_HANDLES was provided.
    primary = primary or (ordered[0] if ordered else "")
    return primary, ordered


CUSTOMER_APPLE_ID, CUSTOMER_HANDLES = _read_customer_handles()

# Access mode: 'approved' (default, secure) answers only CUSTOMER_HANDLES;
# 'open' answers ANY incoming text or iMessage, no allowlist at all. Set
# WITHOUT editing this file, same pattern as the handles above:
#   env:    CURANT_ACCESS_MODE="open"
#   config: {"access_mode": "open"}
# Defaults to 'approved' on anything unset or unrecognized — a typo'd value
# should fail toward the SAFER behavior, not accidentally open the door to
# anyone who texts this number.
def _read_access_mode():
    cfg = {}
    _cfg_path = os.path.expanduser("~/.curant/config.json")
    if os.path.exists(_cfg_path):
        try:
            with open(_cfg_path) as _f:
                cfg = json.load(_f)
        except Exception:
            cfg = {}
    mode = (os.environ.get("CURANT_ACCESS_MODE") or cfg.get("access_mode") or "approved").strip().lower()
    if mode not in ("approved", "open"):
        print(f"Unrecognized CURANT_ACCESS_MODE '{mode}' — falling back to 'approved' (the safe default).",
              file=sys.stderr)
        mode = "approved"
    return mode


ACCESS_MODE = _read_access_mode()
POLL_INTERVAL_SECONDS = 5
LAST_SEEN_ROWID_FILE = os.path.expanduser("~/.curant/last_seen_rowid")


def report_watcher_error(error_code):
    """Best-effort, non-blocking. Reports a known error code only — never
    the exception text itself, which could leak content-adjacent details."""
    try:
        subprocess.run(["curant-cli", "report-error", error_code, "watcher"],
                        capture_output=True, timeout=15)
    except Exception:
        pass  # telemetry reporting must never itself become a new failure


def get_last_seen_rowid():
    if os.path.exists(LAST_SEEN_ROWID_FILE):
        with open(LAST_SEEN_ROWID_FILE) as f:
            return int(f.read().strip() or 0)
    return 0


def save_last_seen_rowid(rowid):
    os.makedirs(os.path.dirname(LAST_SEEN_ROWID_FILE), exist_ok=True)
    with open(LAST_SEEN_ROWID_FILE, "w") as f:
        f.write(str(rowid))


def fetch_new_messages(since_rowid):
    """
    Query chat.db for new incoming messages from the customer's Apple ID.
    chat.db schema varies slightly by macOS version — this targets the
    common structure; verify column names against your OS version with
    `sqlite3 ~/Library/Messages/chat.db ".schema message"` if this breaks.
    """
    if ACCESS_MODE != "open" and not CUSTOMER_HANDLES:
        return []  # no customer identity configured — nothing to match (see main()'s guard)
    conn = sqlite3.connect(f"file:{CHAT_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    base_query = """
        SELECT message.ROWID as rowid,
               message.text as text,
               message.is_from_me as is_from_me,
               message.cache_has_attachments as has_attachment,
               handle.id as sender
        FROM message
        JOIN handle ON message.handle_id = handle.ROWID
        WHERE message.ROWID > ?
          AND message.is_from_me = 0
    """
    if ACCESS_MODE == "open":
        # No allowlist at all — answers whoever texts in. Loudly flagged at
        # startup in main(); this is the one place that filter is actually
        # skipped.
        cur = conn.execute(base_query + " ORDER BY message.ROWID ASC", (since_rowid,))
    else:
        # placeholders is our own controlled "?,?" string — values stay parameterized.
        placeholders = ",".join("?" for _ in CUSTOMER_HANDLES)
        cur = conn.execute(
            base_query + f" AND handle.id IN ({placeholders}) ORDER BY message.ROWID ASC",
            (since_rowid, *CUSTOMER_HANDLES),
        )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".gif", ".webp", ".bmp", ".tiff"}
AUDIO_EXTENSIONS = {".caf", ".m4a", ".mp3", ".wav", ".aac"}


def get_attachment_path(rowid):
    """Look up the file path of an attachment (voice memo or image) on a given message."""
    conn = sqlite3.connect(f"file:{CHAT_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """
        SELECT attachment.filename
        FROM message_attachment_join
        JOIN attachment ON message_attachment_join.attachment_id = attachment.ROWID
        WHERE message_attachment_join.message_id = ?
        """,
        (rowid,),
    )
    row = cur.fetchone()
    conn.close()
    if row and row["filename"]:
        return os.path.expanduser(row["filename"])
    return None


def classify_attachment(file_path):
    """Returns 'image', 'audio', or 'other' based on file extension —
    determines whether the watcher transcribes it (audio) or passes it
    straight through for the model to actually look at (image)."""
    if not file_path:
        return "other"
    ext = os.path.splitext(file_path)[1].lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    return "other"


def transcribe_voice_memo(file_path):
    """
    Local transcription via Whisper — keeps audio on-device, matching
    the honesty/privacy brand commitment. Requires: pip install
    openai-whisper, and ffmpeg installed via brew.
    """
    try:
        import whisper
        model = whisper.load_model("base")  # "small"/"medium" for better accuracy, more RAM/CPU
        result = model.transcribe(file_path)
        return result["text"].strip()
    except ImportError:
        print("Whisper not installed — run: pip install openai-whisper --break-system-packages")
        return None


def get_calendar_and_reminders_context():
    """
    Pull today's/tomorrow's calendar events and open reminders so Curant can
    answer with real awareness of the customer's actual schedule ("you have
    a 2pm with Jamie") instead of only what's in the message itself.

    Uses icalBuddy (brew install ical-buddy) rather than AppleScript here —
    it's faster and gives cleaner plain-text output for this. Falls back to
    an empty string (never breaks message handling) if it's not installed
    or Calendar/Reminders access hasn't been granted yet.
    """
    context_parts = []
    try:
        events = subprocess.run(
            ["icalBuddy", "-nc", "-nrd", "eventsToday+7"],
            capture_output=True, text=True, timeout=10,
        )
        if events.returncode == 0 and events.stdout.strip():
            context_parts.append("Upcoming calendar events:\n" + events.stdout.strip())
    except FileNotFoundError:
        print("icalBuddy not installed — skipping calendar context. "
              "Run: brew install ical-buddy", file=sys.stderr)
    except Exception as e:
        print(f"Calendar lookup failed (non-fatal): {e}", file=sys.stderr)

    try:
        reminders = subprocess.run(
            ["icalBuddy", "-nc", "uncompletedTasks"],
            capture_output=True, text=True, timeout=10,
        )
        if reminders.returncode == 0 and reminders.stdout.strip():
            context_parts.append("Open reminders:\n" + reminders.stdout.strip())
    except FileNotFoundError:
        pass  # already warned above
    except Exception as e:
        print(f"Reminders lookup failed (non-fatal): {e}", file=sys.stderr)

    return "\n\n".join(context_parts)


def relay_to_curant(text, context="", image_path=None):
    """Call curant-cli, which now handles this entirely locally: builds the
    system prompt from local persona/memory/instructions, calls Claude
    directly, and returns the reply. No message content is sent to
    Curant's own server. Passes CUSTOMER_APPLE_ID through so an async
    tool (Veo video generation) knows who to deliver its result to later,
    once the background job actually finishes."""
    args = ["curant-cli", "relay", text, "--apple-id", CUSTOMER_APPLE_ID]
    if context:
        args += ["--context", context]
    if image_path:
        args += ["--image", image_path]
    result = subprocess.run(args, capture_output=True, text=True)
    return result.stdout.strip()


def _load_curant_config():
    """Reads curant-cli's config.json directly rather than importing
    curant-cli as a module — the two are separate scripts, and this is
    the same file/format curant-cli itself reads and writes."""
    config_path = os.path.expanduser("~/.curant/config.json")
    if not os.path.exists(config_path):
        return {}
    try:
        with open(config_path) as f:
            return json.load(f)
    except Exception:
        return {}


def _get_config_api_key(config, provider):
    keys = config.get("api_keys", {})
    if keys.get(provider):
        return keys[provider]
    if provider == "anthropic":
        return config.get("anthropic_api_key")  # legacy field
    return None


# Rough per-use cost estimates for the paid TTS tiers — same honesty
# standard as August's generation costs: not independently verified
# against current pricing pages, meant to give a general sense of spend,
# not an exact bill. "standard" (macOS say) is free and never logged.
TTS_ESTIMATED_COST_USD = {
    "openai_tts": 0.015,      # ESTIMATE per typical reply — not verified against current pricing
    "elevenlabs_tts": 0.02,   # ESTIMATE per typical reply — not verified against current pricing
}


def _log_tts_cost(service):
    """
    Writes to the SAME generation_costs table August's tools use (same
    local.db, same schema) — a customer asking "how much have I spent"
    gets one honest total across both, not two separate hidden numbers.
    Separate service keys ('openai_tts'/'elevenlabs_tts') from August's
    ('openai'/'elevenlabs') since regular voice replies are much higher
    volume than occasional image/voice generation and worth distinguishing.
    """
    db_path = os.path.expanduser("~/.curant/local.db")
    if not os.path.exists(db_path):
        return  # local.db not initialized yet — nothing to log against
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO generation_costs (service, estimated_cost_usd) VALUES (?, ?)",
            (service, TTS_ESTIMATED_COST_USD.get(service, 0.0)),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"TTS cost logging failed (non-fatal): {e}", file=sys.stderr)


def _tts_macos_say(text):
    """
    'standard' tier — macOS's built-in `say` command. Free, fully local,
    no API key needed, works on every Mac out of the box. This is the
    right default: voice replies shouldn't require an extra paid service
    just to function at all.
    """
    import tempfile
    fd, aiff_path = tempfile.mkstemp(suffix=".aiff")
    os.close(fd)
    try:
        subprocess.run(["say", "-o", aiff_path, text], check=True, timeout=30)
        m4a_path = aiff_path.replace(".aiff", ".m4a")
        subprocess.run(
            ["afconvert", "-f", "m4af", "-d", "aac", aiff_path, m4a_path],
            check=True, timeout=30,
        )
        return m4a_path
    finally:
        if os.path.exists(aiff_path):
            os.remove(aiff_path)


def _tts_openai(text):
    """
    'natural' tier — OpenAI TTS. Verified against current OpenAI API docs
    before implementing: POST /v1/audio/speech, model gpt-4o-mini-tts
    (their current recommended model), returns raw MP3 bytes directly.
    """
    import requests, tempfile
    config = _load_curant_config()
    api_key = _get_config_api_key(config, "openai")
    if not api_key:
        raise RuntimeError(
            "No OpenAI API key set for the 'natural' voice tier. "
            "Run: curant-cli set-api-key <key> --provider openai"
        )
    resp = requests.post(
        "https://api.openai.com/v1/audio/speech",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": "gpt-4o-mini-tts", "input": text, "voice": "alloy"},
        timeout=30,
    )
    resp.raise_for_status()
    fd, path = tempfile.mkstemp(suffix=".mp3")
    with os.fdopen(fd, "wb") as f:
        f.write(resp.content)
    _log_tts_cost("openai_tts")
    return path


def _tts_elevenlabs(text):
    """'realistic' tier — ElevenLabs, same endpoint already verified and
    used for August's voice generation tool."""
    import requests, tempfile
    config = _load_curant_config()
    api_key = _get_config_api_key(config, "elevenlabs")
    if not api_key:
        raise RuntimeError(
            "No ElevenLabs API key set for the 'realistic' voice tier. "
            "Run: curant-cli set-api-key <key> --provider elevenlabs"
        )
    resp = requests.post(
        "https://api.elevenlabs.io/v1/text-to-speech/21m00Tcm4TlvDq8ikWAM",
        headers={"xi-api-key": api_key, "Content-Type": "application/json"},
        json={"text": text, "model_id": "eleven_multilingual_v2"},
        timeout=60,
    )
    resp.raise_for_status()
    fd, path = tempfile.mkstemp(suffix=".mp3")
    with os.fdopen(fd, "wb") as f:
        f.write(resp.content)
    _log_tts_cost("elevenlabs_tts")
    return path


def text_to_speech(text, voice_tier="standard"):
    """
    Three tiers, matching the voice_tier setting already in the product:
      - "standard"  → macOS `say`, free and local, no API key required
      - "natural"   → OpenAI TTS, needs an OpenAI key
      - "realistic" → ElevenLabs, needs an ElevenLabs key, highest quality
    Unknown tiers fall back to "standard" rather than failing — a typo'd
    setting shouldn't be the reason voice replies stop working entirely.
    Returns a path to a generated audio file; raises on real failure
    (caller wraps this in try/except and reports tts_failed).
    """
    if voice_tier == "natural":
        return _tts_openai(text)
    elif voice_tier == "realistic":
        return _tts_elevenlabs(text)
    else:
        return _tts_macos_say(text)


def send_text_reply(to_apple_id, text):
    """Send a plain iMessage text back, via AppleScript."""
    # Escape double quotes for AppleScript string safety
    safe_text = text.replace('"', '\\"')
    script = f'''
    tell application "Messages"
        set targetService to 1st service whose service type = iMessage
        set targetBuddy to buddy "{to_apple_id}" of targetService
        send "{safe_text}" to targetBuddy
    end tell
    '''
    subprocess.run(["osascript", "-e", script])


def send_voice_reply(to_apple_id, audio_file_path):
    """Send a generated voice memo back, via AppleScript file attachment."""
    script = f'''
    tell application "Messages"
        set targetService to 1st service whose service type = iMessage
        set targetBuddy to buddy "{to_apple_id}" of targetService
        send (POSIX file "{audio_file_path}") to targetBuddy
    end tell
    '''
    subprocess.run(["osascript", "-e", script])


def send_file_reply(to_apple_id, file_path):
    """Send any generated file (image, video, or other August output) back
    as an attachment. Identical mechanism to send_voice_reply — AppleScript's
    file-send doesn't care about file type — kept as a separate name at
    call sites for clarity about what's actually being sent."""
    send_voice_reply(to_apple_id, file_path)


def run_proactive_check():
    """
    Ask the server whether there's anything worth proactively sending right
    now, using today's live calendar/reminders context. Meant to be invoked
    on a schedule (see com.curant.proactive.plist), not from the message
    poll loop. Most invocations should result in nothing being sent — that's
    the expected, correct outcome, not a failure.
    """
    live_context = get_calendar_and_reminders_context()
    result = subprocess.run(
        ["curant-cli", "proactive-check", "--context", live_context],
        capture_output=True, text=True,
    )
    try:
        decision = json.loads(result.stdout.strip())
    except (json.JSONDecodeError, TypeError):
        print(f"Unexpected proactive-check output (length {len(result.stdout)})", file=sys.stderr)
        return

    if not decision.get("should_send"):
        return

    message_text = decision.get("message", "")
    if not message_text:
        return

    if decision.get("reply_format") == "voice":
        try:
            audio_path = text_to_speech(message_text, decision.get("voice_tier", "standard"))
            send_voice_reply(CUSTOMER_APPLE_ID, audio_path)
        except Exception:
            report_watcher_error("tts_failed")
    else:
        send_text_reply(CUSTOMER_APPLE_ID, message_text)


def handle_message(msg):
    image_path = None
    if msg["has_attachment"]:
        attachment_path = get_attachment_path(msg["rowid"])
        if not attachment_path or not os.path.exists(attachment_path):
            print(f"Attachment not found for message {msg['rowid']}, skipping.")
            report_watcher_error("attachment_not_found")
            return
        kind = classify_attachment(attachment_path)
        if kind == "image":
            # Passed straight through — curant-cli sends it to the model
            # directly rather than this script trying to describe it.
            # The message's own caption text (if any) still comes along
            # as the accompanying text; an image with no caption still
            # gets sent so the model can react to it on its own.
            image_path = attachment_path
            text = msg["text"] or ""
        elif kind == "audio":
            text = transcribe_voice_memo(attachment_path)
            if not text:
                report_watcher_error("transcription_failed")
                return
        else:
            print(f"Unrecognized attachment type for message {msg['rowid']}, skipping.")
            report_watcher_error("attachment_not_found")
            return
    else:
        text = msg["text"]

    if not text and not image_path:
        return

    print(f"Incoming message, rowid {msg['rowid']}" + (" (with image)" if image_path else ""))
    live_context = get_calendar_and_reminders_context()
    reply_json = relay_to_curant(text, context=live_context, image_path=image_path)

    try:
        reply_data = json.loads(reply_json)
    except (json.JSONDecodeError, TypeError):
        # curant-cli always emits JSON now (even on error) — this branch
        # should only fire if curant-cli itself crashed or isn't installed.
        print(f"Unexpected non-JSON output from curant-cli (length {len(reply_json)})", file=sys.stderr)
        report_watcher_error("unexpected_watcher_error")
        return

    if reply_data.get("error"):
        # ADDED after a real live gap: curant-cli's relay() actually
        # includes a human-readable detail in the "reply" field even on
        # error (e.g. "Couldn't reach anthropic on your account: <the
        # real exception>") -- this used to print only the generic
        # error CODE and silently threw that detail away, making it
        # impossible to tell from watcher logs alone whether a failure
        # was a bad/missing API key, a network problem, an invalid
        # model name, a rate limit, etc. Print both now.
        detail = reply_data.get("reply")
        if detail:
            print(f"curant-cli reported an error: {reply_data['error']} — {detail}", file=sys.stderr)
        else:
            print(f"curant-cli reported an error: {reply_data['error']}", file=sys.stderr)
        return

    reply_text = reply_data.get("reply", "")
    if not reply_text:
        # ADDED after a real live gap alongside the error-detail fix
        # above: this used to return here completely silently -- no
        # error was reported (reply_data.get("error") was falsy), but
        # there was also nothing to send, and NOTHING printed either
        # way. From the terminal alone this looked identical to "still
        # working" or "succeeded with nothing further to log," when it
        # was actually a third distinct outcome (curant-cli returned a
        # legitimately empty reply -- e.g. a genuinely blank string
        # from the model) that deserves its own visible line.
        print(f"curant-cli returned no error but an empty reply for message {msg['rowid']} -- "
              f"nothing sent. Raw response: {reply_json[:300]}", file=sys.stderr)
        return
    reply_format = reply_data.get("reply_format", "text")

    if reply_format == "voice":
        try:
            audio_path = text_to_speech(reply_text, reply_data.get("voice_tier", "standard"))
            send_voice_reply(msg["sender"], audio_path)
            print(f"Sent voice reply for message {msg['rowid']}.")
        except Exception:
            report_watcher_error("tts_failed")
    else:
        send_text_reply(msg["sender"], reply_text)
        print(f"Sent text reply for message {msg['rowid']}.")

    # August's specialist tools (image/voice/video generation) attach a
    # locally-generated file to relay()'s output when one was produced
    # this turn — send it as a follow-up attachment after the text reply.
    # This is a short-lived decrypted temp copy (curant-cli keeps the
    # long-term copy encrypted in ~/.curant/generated/) — delete it
    # immediately after sending, it has no reason to exist any longer.
    attachment_path = reply_data.get("attachment_path")
    if attachment_path:
        if os.path.exists(attachment_path):
            send_file_reply(msg["sender"], attachment_path)
            try:
                os.remove(attachment_path)
            except OSError as e:
                print(f"Could not remove temp attachment file (non-fatal): {e}", file=sys.stderr)
        else:
            print(f"Generated file path reported but not found on disk: {attachment_path}", file=sys.stderr)


def deliver_completed_background_jobs():
    """
    Checks for slow tool calls that finished since the last check and
    delivers them as a follow-up message — this is what makes the async
    pattern actually complete the loop back to the customer, rather than
    the result silently sitting in local.db forever. Uses curant-cli
    itself to query and mark jobs delivered, keeping all local.db access
    inside curant-cli rather than duplicating SQLite logic here.

    Two shapes of job reach this function:
      - Veo video generation (always async — see generate_video_veo_async)
        — result_path, a file to decrypt and send.
      - Browser automation that turned out to be slow (see
        fill_and_submit_form_hybrid) — result_text, a follow-up text
        message. Fast form submissions never reach here at all: they get
        marked delivered inside relay() itself, in the same reply, and
        pending-background-jobs correctly excludes anything already
        marked delivered.
    """
    try:
        result = subprocess.run(
            ["curant-cli", "pending-background-jobs"], capture_output=True, text=True,
        )
        jobs = json.loads(result.stdout.strip() or "[]")
    except Exception as e:
        print(f"Could not check background jobs (non-fatal): {e}", file=sys.stderr)
        return

    for job in jobs:
        apple_id = job.get("apple_id")
        if not apple_id:
            continue  # nothing to deliver to — shouldn't happen, but don't crash if it does

        if job["status"] == "done" and job.get("result_path"):
            # File-shaped result (Veo). The stored result_path is the
            # encrypted at-rest copy — decrypt to a short-lived temp file
            # for delivery, same pattern relay() already uses for
            # synchronous generations.
            try:
                decrypt_result = subprocess.run(
                    ["curant-cli", "decrypt-for-delivery", job["result_path"]],
                    capture_output=True, text=True,
                )
                temp_path = decrypt_result.stdout.strip()
                if temp_path and os.path.exists(temp_path):
                    send_text_reply(apple_id, "Your video is ready!")
                    send_file_reply(apple_id, temp_path)
                    os.remove(temp_path)
                else:
                    send_text_reply(apple_id, "Your video finished, but I couldn't prepare it for delivery — sorry about that.")
            except Exception as e:
                print(f"Failed to deliver completed video job {job['id']}: {e}", file=sys.stderr)
                send_text_reply(apple_id, "Your video finished, but something went wrong delivering it.")
        elif job["status"] == "done" and job.get("result_text"):
            # Text-shaped result — a browser automation job that took
            # longer than the synchronous wait window in relay().
            preview = job["result_text"][:500]
            send_text_reply(apple_id, f"That form went through — here's what came back:\n{preview}")
        elif job["status"] == "failed":
            tool_label = "video generation" if job.get("tool_name") == "generate_video" else "that task"
            error_msg = job.get("error") or "an unknown error"
            send_text_reply(apple_id, f"Sorry, your {tool_label} didn't work out: {error_msg}")

        try:
            subprocess.run(["curant-cli", "mark-job-delivered", str(job["id"])], capture_output=True)
        except Exception as e:
            print(f"Could not mark job {job['id']} delivered (non-fatal, may re-deliver): {e}", file=sys.stderr)


def main():
    print("curant-watcher starting — polling for new messages")
    if ACCESS_MODE == "open":
        print("ACCESS MODE: OPEN — answering ANY incoming text or iMessage, no allowlist. "
              "Set CURANT_ACCESS_MODE=approved (or remove the setting) to go back to only "
              "answering configured handles.", file=sys.stderr)
    else:
        if not CUSTOMER_HANDLES:
            print("No customer identity configured. Set the customer's Apple ID via the "
                  "CURANT_CUSTOMER_APPLE_ID env var or 'customer_apple_id' in "
                  "~/.curant/config.json (add CURANT_CUSTOMER_HANDLES / 'customer_handles' for "
                  "extra phone/email handles). Refusing to run with no one to listen to.",
                  file=sys.stderr)
            sys.exit(1)
        print(f"Access mode: approved — listening for: {', '.join(CUSTOMER_HANDLES)}")
    last_rowid = get_last_seen_rowid()

    while True:
        try:
            new_messages = fetch_new_messages(last_rowid)
            for msg in new_messages:
                handle_message(msg)
                last_rowid = msg["rowid"]
                save_last_seen_rowid(last_rowid)
            deliver_completed_background_jobs()
        except sqlite3.OperationalError as e:
            print(f"Watcher error reading chat.db: {e}", file=sys.stderr)
            report_watcher_error("chatdb_read_failed")
        except Exception as e:
            print(f"Watcher error: {e}", file=sys.stderr)
            report_watcher_error("watcher_crash")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    if "--proactive-check" in sys.argv:
        # Invoked once by com.curant.proactive.plist on a schedule — runs
        # and exits, rather than looping like the main polling watcher.
        run_proactive_check()
    else:
        main()
