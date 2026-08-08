# Setting up FaceTime auto-answer calls (EXPERIMENTAL)

This feature is fundamentally different from everything else in Curant. Text-in/text-back (`curant-watcher.py`) is built on documented, verifiable APIs — a readable SQLite database and AppleScript's Messages support. FaceTime has none of that. Live testing on the real Mac this was built for found:

- FaceTime.app never actually runs for an incoming call — it's handled by background daemons plus a system call banner.
- That banner IS reliably detectable (a "Notification Center" window appears while it's up), but its Accept button is **not reachable via Accessibility scripting at all** — confirmed by walking its entire UI tree and finding nothing but empty nested groups five levels deep.
- So accepting a call here works **visually**: screenshot the screen, find the green Accept button by color, click that exact pixel. This is real automation, but more fragile than the Accessibility-based clicking used everywhere else in Curant — it can be thrown off by the banner appearing in a different spot, a second notification stacking above it, a display/resolution change, or a macOS visual update.
- **Caller-ID verification for calls doesn't currently exist.** The caller's number is visible as pixels but not as readable text, so there's no way to check it against your configured handles before answering. `approved` mode (the safe default) refuses every call for exactly this reason; only `open` mode answers, and it answers *everyone*.

Budget real time to iterate — this is not a five-minute setup, and it may never be as reliable as the text watcher.

Do steps 1–3 in order once. Step 4 is per-call.

---

## 1. Install everything

```bash
brew install blackhole-2ch blackhole-16ch switchaudio-osx ffmpeg cliclick
pip3 install pillow numpy google-genai requests --break-system-packages
```

- **BlackHole 2ch / 16ch** — two separate virtual audio devices, kept separate so your outgoing synthesized voice and the caller's incoming voice never mix: 2ch carries Curant's speech *to* FaceTime (set as FaceTime's Microphone), 16ch carries the caller's voice *out of* FaceTime for transcription (set as FaceTime's Speaker/Output).
- **switchaudio-osx** — lets the script flip your system's default output device programmatically.
- **cliclick** — synthesizes the actual mouse click on the detected Accept button.
- **pillow** (PIL) — screenshot loading/cropping.
- **numpy** — real template matching against `assets/facetime_accept_button.png` (a genuine screenshot crop of the actual button, not a mockup) to find the Accept button's exact position, rather than guessing at a color range. Verified: a true match scores ~74 (normalized SSD) vs. ~6600 for no match at all.
- **google-genai** — Gemini's native audio SDK, used to both transcribe the caller (step 6) and, separately, generate replies if your configured provider is Gemini. Skip only if you're using OpenAI Whisper for transcription AND Anthropic/OpenAI for replies.
- **requests** — used for the OpenAI Whisper transcription fallback.

After installing, **restart your Mac** (or at least log out/in) — BlackHole devices sometimes don't appear in Sound settings until CoreAudio restarts.

Verify the audio devices appear:

```bash
system_profiler SPAudioDataType | grep -A2 BlackHole
```

You should see both `BlackHole 2ch` and `BlackHole 16ch`.

---

## 2. Grant permissions

Two separate permission systems, both needed:

- **System Settings → Privacy & Security → Accessibility** — add and enable Terminal (or whatever runs the script). Needed for both call detection (`System Events`) and the synthesized click (`cliclick`). Note: adding it while Terminal is already open doesn't always take effect immediately — fully quit (Cmd+Q) and reopen Terminal after granting.
- **System Settings → Privacy & Security → Screen Recording** — add and enable Terminal too. Needed for the screenshot the script takes to find the Accept button. Without this, screenshots silently come back black instead of erroring — if detection seems to "not see" a clearly-visible button, check this first.

---

## 3. Verify call detection works

Run dry-run mode and place a real test call to yourself from another device/Apple ID:

```bash
cd curant-current/mac
python3 curant-facetime-answerer.py --dry-run --apple-id "your.apple.id@icloud.com"
```

While it's ringing, the terminal should print something like `Incoming call detected: 'FaceTime call banner active...'`. If it prints nothing while a call is visibly ringing, check what's actually happening directly:

```bash
osascript -e 'tell application "System Events" to tell process "NotificationCenter" to get name of every window'
```

Run that once with no call ringing, then again while one is — you're looking for a window named exactly `"Notification Center"` to appear only in the second case. If it doesn't, the detection logic in `poll_for_incoming_call()` needs adjusting for your macOS version.

**Do not proceed until dry-run reliably detects a ringing call.**

---

## 4. Find the Accept button's fallback coordinates (recommended before your first live test)

The script tries to find the Accept button visually on its own — but since this is the most fragile piece, it's worth having a manual fallback ready. While a call is ringing:

1. Hover your mouse exactly over the center of the green Accept button (don't click).
2. In a second terminal window, run:
   ```bash
   cliclick p
   ```
   This prints your current mouse position as `x,y` — those are the coordinates.
3. Set that as an environment variable before running the script for real:
   ```bash
   export CURANT_FACETIME_ACCEPT_XY="1930,140"
   ```
   (using your actual printed numbers). The script only falls back to this if visual detection fails to find the button on its own, so it's a safety net, not the primary mechanism.

---

## 5. Route FaceTime's audio through BlackHole (per-call, manual)

Once a call is answered, FaceTime.app becomes the active app with its own in-call controls (this part is a normal window, unlike the pre-answer banner). Set, from its **Video menu**:

1. **Video → Microphone → BlackHole 2ch** — sends whatever plays into BlackHole 2ch (Curant's synthesized speech) to the caller.
2. **Video → Speaker → BlackHole 16ch** — sends the caller's voice into BlackHole 16ch instead of your speakers, where the script records and transcribes it.

This selection may not persist between calls — verify on a second test call rather than assuming it stuck.

The script automates one piece of this automatically: it sets your **system default output** to `BlackHole 2ch` right when it accepts a call, since `afplay`/`say` always play to whatever the system default output is. Steps 1 and 2 above still need to be set by hand each call (for now).

---

## 6. Confirm the transcription AND reply-generation keys — these are two separate things

Getting a call to actually hear, understand, and speak needs THREE pieces working together, and it's easy to wire up only one or two of them without noticing until mid-call:

1. **Hear** (transcribe the caller) — tries Gemini's native audio understanding first, falls back to OpenAI's Whisper only if no Gemini key is configured. Set with:
   ```bash
   curant-cli set-api-key <key> --provider gemini
   ```
   or, for Whisper instead:
   ```bash
   curant-cli set-api-key sk-... --provider openai
   ```
2. **Understand/reply** (generate what to say back) — routes through `curant-cli relay`, which uses whichever provider is currently configured (`curant-cli set-provider ...`, defaults to Anthropic if you've never set one) — **not necessarily the same provider as step 1's transcription key.** If you set only a Gemini key for transcription but the configured provider is still Anthropic with no Anthropic key stored, the call will answer and transcribe correctly, then fail on every reply. To use Gemini for both:
   ```bash
   curant-cli set-provider gemini
   ```
   (Same key from step 1 above covers both, once the provider is switched — no need to set it twice.) If instead you want to keep Anthropic (or OpenAI) for replies and only use Gemini/Whisper for transcription, that's fine too — just make sure whichever provider `curant-cli set-provider` currently points to also has its own key set.
3. **Speak** — local `say`/`afplay`, no API key or account needed.

The script checks all of this itself now: on startup (not dry-run), it runs a preflight check and refuses to start with a clear error naming exactly which piece (hear or understand) is missing a key, rather than failing silently mid-call. Confirm what it sees before going live:

```bash
python3 curant-facetime-answerer.py --apple-id "your.customer@icloud.com"
# expect: "API preflight check passed — hear: ..., understand/reply: ..., speak: local (say/afplay, no API key)."
```

Either way, ongoing API usage during calls is a real, ongoing cost per call minute (transcription) plus whatever your reply-generation provider charges per message.

---

## 7. Decide on access mode before running it live

Because caller-ID verification doesn't work yet (see the top of this doc), `approved` mode will simply refuse to answer anything — which is safe, but means nothing will happen at all until you explicitly opt into `open` mode:

```bash
export CURANT_ACCESS_MODE=open
```

Understand what this means: **the Mac will attempt to auto-answer literally any incoming FaceTime call**, not just ones from your configured customer. Only set this if you're comfortable with that while testing.

---

## 8. Run it for real

While you're still actively debugging clicks/audio, run it in the foreground so you can see output live and Ctrl+C instantly:

```bash
python3 curant-facetime-answerer.py --apple-id "your.customer@icloud.com"
```

### Running texting and calling at the same time

Once the foreground testing above is stable, `com.curant.facetime.plist` runs the answerer as its own background `launchd` job — separate and independent from `com.curant.watcher.plist` (texting). Loading both means Curant answers texts and calls simultaneously; each restarts independently (`KeepAlive`) and one crashing doesn't affect the other.

```bash
cp curant-facetime-answerer.py /usr/local/bin/curant-facetime-answerer.py
chmod +x /usr/local/bin/curant-facetime-answerer.py
cp com.curant.facetime.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.curant.facetime.plist

# texting side, if not already loaded:
cp curant-watcher.py /usr/local/bin/curant-watcher.py
cp com.curant.watcher.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.curant.watcher.plist
```

Confirm both are up:

```bash
launchctl list | grep curant
# expect: app.curant.watcher   AND   app.curant.facetime
```

Two things `launchd` handles differently than a foreground terminal, both already set in `com.curant.facetime.plist`:
- **PATH** — `launchd` doesn't source your shell profile, so `cliclick`/`ffmpeg`/`SwitchAudioSource` (Homebrew) need an explicit `PATH` in the plist's `EnvironmentVariables`, covering both Apple Silicon (`/opt/homebrew/bin`) and Intel (`/usr/local/bin`) install locations.
- **`CURANT_ACCESS_MODE=open`** — the plist sets this directly, since a `launchd` job doesn't inherit `export`s from a terminal session. If you leave the default `approved` mode, this job will run but silently refuse every call — check `/tmp/curant-facetime.log` if it seems to do nothing.

If you found a manual fallback coordinate (`CURANT_FACETIME_ACCEPT_XY`, step 4) while testing in the foreground, that only lives in your terminal's environment — add it to the plist's `EnvironmentVariables` dict too if you want the background job to have it.

Logs: `/tmp/curant-facetime.log` and `/tmp/curant-facetime-error.log` (answerer), `/tmp/curant-watcher.log` and `/tmp/curant-watcher-error.log` (texting).

To stop one without touching the other:

```bash
launchctl unload ~/Library/LaunchAgents/com.curant.facetime.plist   # stop calling only
launchctl unload ~/Library/LaunchAgents/com.curant.watcher.plist    # stop texting only
```

**Expect to iterate.** Realistic failure modes, roughly most-to-least likely:
- Detection sees the call but the click misses or does nothing — check Screen Recording permission first (black screenshots look like "nothing detected"), then check whether `CURANT_FACETIME_ACCEPT_XY` is set as a fallback.
- Call gets answered but the caller hears silence — BlackHole 2ch not actually selected as FaceTime's Microphone this call, or `SwitchAudioSource` didn't flip system output (check with `SwitchAudioSource -c` mid-call).
- Script "hears" nothing from the caller — BlackHole 16ch not selected as FaceTime's Speaker, or the ffmpeg device index lookup picked the wrong device (run `ffmpeg -f avfoundation -list_devices true -i ""` yourself and compare against `_find_avfoundation_audio_device_index`).
- Echo/garbled audio — both directions ended up on the same BlackHole device by mistake; re-check step 5.
- `hang_up()` doesn't end the call — unverified separately from everything above; if it fails, the call stays connected and needs manual ending.

Report back exactly what you see (terminal output + what happened on the call) — this really is a "build it experimentally, you test live" feature.
