## Keep the top-right of the screen clear during calls

Caller-ID verification works by screenshotting the top-right region of
the screen and OCR'ing the FaceTime banner. **Any window sitting in that
region gets read instead**, and the call is refused as unverified.

This is not hypothetical: four consecutive calls were refused because a
Terminal window there was displaying Curant's own log output. The OCR
text contained both the correct phone number and the words "FaceTime
Audio" — read out of the log, not the banner — so no amount of
content-matching could distinguish it. The refusal message now detects
terminal/log content and says so explicitly rather than reporting it as
an unapproved caller.

If calls are being refused, check `/tmp/curant-facetime.log` for
`access check: REFUSED` and read the raw OCR text. If it looks like your
screen rather than a call banner, move the offending window.

`CURANT_FACETIME_CALLERID_REGION="x0,x1,y0,y1"` retunes the region if
your setup needs a different one.

## Multi-Output Device settings that actually matter (read this first)

Curant hears the caller through a Multi-Output Device named
**`Curant Call Output`** containing BlackHole 16ch. Three of its
settings are non-obvious and each one silently breaks capture when
wrong. All three were found by measurement on live calls, not from
documentation.

### 1. Primary Device must be **BlackHole 16ch**

The primary device's volume scales the entire group, including the copy
BlackHole receives. Same tone, same call, only this setting changed:

| Primary Device | Captured RMS |
|---|---|
| MacBook Air Speakers | **3.5** (unusable) |
| BlackHole 16ch | **2144.6** |

BlackHole is also the best clock in the group: Internal Fixed, 48 kHz,
unity gain, and it cannot drift the way a speaker or Bluetooth clock
can.

### 2. Sample Rate must be **48 kHz**, matching BlackHole

A Bluetooth device as primary put the group at 44.1 kHz while BlackHole
ran at 48 kHz. The result was **exactly 0.0 RMS** — not quiet, no
signal at all — while a control tone through the same device measured
5097. Several days of debugging went into what turned out to be this
mismatch. It is completely silent: nothing logs an error.

### 3. No Bluetooth devices in the group

Bluetooth headphones reconnect at 44.1 kHz and take the group with them,
recreating problem 2 at random. This is why calls "worked sometimes":
capture succeeded exactly when the headphones happened to be
disconnected.

### Verifying it without placing a call

Restart the service and read the self-test line:

```
launchctl bootout gui/$(id -u)/app.curant.facetime; sleep 3
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.curant.facetime.plist
sleep 8; grep "self-test" /tmp/curant-facetime.log | tail -1
```

- **Thousands** — correct.
- **Single/double digits** — the primary device is wrong, or its volume
  is turned down.
- **0.0** — sample-rate mismatch, or BlackHole isn't in the group.

## How Curant hears the caller — and the limits found (2026-08-21)

**Current state: hearing the caller is UNRELIABLE on this macOS version.**
Texting is unaffected. This section records what was measured so the
next person doesn't repeat three nights of the same experiments.

### What was tested, and what each attempt measured

Three independent capture routes were tried against live FaceTime calls:

| Route | Result during a live call | Control |
|---|---|---|
| Multi-Output Device → BlackHole 16ch → ffmpeg | FaceTime audio **RMS 0.0** | Tone into the same device, same moment: **RMS 5097.5** |
| ScreenCaptureKit, scoped to FaceTime.app | 489 buffers delivered, **peak 0** | — |
| ScreenCaptureKit, entire system mix | **peak 17** (~−65 dB, noise floor) | Same binary, music playing: **peak 7614–8452** |

The controls are the important column. In every case the capture
pipeline provably worked on non-FaceTime audio at the same moment,
with the same code and the same conversion path.

### Conclusion

macOS does not expose FaceTime call audio to ScreenCaptureKit (it is
treated as protected communications audio), and does not reliably
render it into the system default output device either.

That said, the BlackHole path **did** succeed on several real calls
(RMS 275, 694, 57, 35) before failing on others with identical
settings. So FaceTime *sometimes* renders into the capturable device.
The most likely remaining explanation is FaceTime's own per-call audio
route — it maintains its own output selection (seen in the call
window's audio control, and observed naming a device different from
the system default). When that route happens to be the Multi-Output
Device, capture works; when it's Bluetooth headphones or another
device, capture gets nothing.

### What to check if a call is silent

1. During the call, open the FaceTime call window's audio control (or
   Control Centre → Sound) and see which output device the CALL is
   using — not the system default, the call's own route.
2. If it names anything other than `Curant Call Output`, switch it.
3. Disconnect Bluetooth headphones before testing; they are a frequent
   cause of FaceTime picking a different route mid-session.

### The ScreenCaptureKit tap

`mac/curant-facetime-audiotap.swift` is kept because it is correct,
working code — the control test proves it captures real audio fine —
and because Apple's handling of this may change. It is **disabled by
default**, since enabling it guarantees silence on FaceTime calls.

Opt in with `CURANT_FACETIME_ENABLE_AUDIOTAP=1` (and optionally
`CURANT_FACETIME_SYSTEM_AUDIO=1` for the whole-system mix). Its
diagnostics go to `/tmp/curant-facetime-audiotap.log`, including a
heartbeat with running peak amplitude, which is the fastest way to
tell "the OS is sending nothing" from "the OS is sending silence".

## Why this changed

The original design routed FaceTime's audio into a Multi-Output Device
that fed BlackHole 16ch, and recorded that virtual device. That was
debugged over several nights and finally disproven with one decisive
measurement during a live, connected call:

- A test tone played into the system default output was captured back
  at **RMS 5097.5** — the capture path was provably working, mid-call.
- FaceTime's own call audio in that same window measured **exactly
  0.0** — zero samples, not a low level.

The only explanation consistent with both numbers is that FaceTime
never renders call audio into the system default output device.
It's a VoIP client using the OS communications audio path, which
bypasses aggregate and virtual output devices. That is why rebuilding
the Multi-Output Device, enabling drift correction, switching devices
per-call vs. at startup, and correcting the input device all failed —
and why a few calls appeared to work briefly (coincidence, not the fix
taking effect).

### What this means for setup

- **BlackHole 2ch is still required.** It's how Curant *speaks* —
  FaceTime reads the system input device as its microphone, so Curant's
  replies are played into BlackHole 2ch. That direction never had a
  problem.
- **BlackHole 16ch and the "Curant Call Output" Multi-Output Device are
  no longer needed for hearing.** They're only used if the
  ScreenCaptureKit tap isn't built, as a fallback.
- **Your Mac's speakers keep working.** Previously the service
  commandeered the default output for its entire lifetime. With the tap,
  only the *input* is switched.
- **No new permission.** The tap uses Screen Recording, which this
  feature already required for visual call detection.

### Requirements

- macOS 13 or newer
- Apple command line tools (`xcode-select --install`) so `swiftc` exists

`setup-facetime.command` builds the tap automatically. To check it's in
use, look for this line in `/tmp/curant-facetime.log` at startup:

```
Capture backend: ScreenCaptureKit app tap (hearing does NOT depend on BlackHole...)
```

If you instead see `Capture backend: BlackHole/ffmpeg`, the tap wasn't
built — check `/tmp/curant-audiotap-build.log`.

To force the old backend for comparison, set
`CURANT_FACETIME_DISABLE_AUDIOTAP=1` in the launchd plist.


# Setting up FaceTime auto-answer calls (EXPERIMENTAL)

**Most testers should run `setup-facetime.command`** (in the folder above
this one, next to `install.command`) instead of following this by hand --
it automates steps 1-3 and 7 below, generates a correct per-user
`com.curant.facetime.plist` (the copy checked into this `mac/` folder is
hardcoded to the original developer's username and Apple Silicon path --
copying it as-is to another Mac will silently fail to run), and defaults
to the safer `approved` access mode described in step 7 below (this
document previously said `approved` mode refuses every call outright --
that was true when this was first written, but OCR-based caller-ID
verification was added since, and `approved` now works and is the
recommended default; `open` mode, which answers literally anyone, should
only be used if OCR verification isn't available). Come back to this
document for anything the script can't do for you -- granting
permissions, creating the Multi-Output audio device, live troubleshooting,
and the full technical background.

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
brew install blackhole-2ch blackhole-16ch switchaudio-osx ffmpeg cliclick sox
pip3 install pillow numpy google-genai requests --break-system-packages
```

- **BlackHole 2ch / 16ch** — two separate virtual audio devices, kept separate so your outgoing synthesized voice and the caller's incoming voice never mix: 2ch carries Curant's speech *to* FaceTime (its system-default microphone the whole time the script runs), 16ch carries the caller's voice *out of* FaceTime (its system-default output the whole time) for transcription.
- **switchaudio-osx** — sets the system default input/output devices ONCE at startup (see step 5) — deliberately not per-call or per-turn anymore; hot-swapping either mid-call was confirmed live to drop the call.
- **sox** — plays Curant's synthesized speech directly into BlackHole 2ch (`sox file -t coreaudio "BlackHole 2ch"`), bypassing the system default output entirely. This is what makes it safe to leave the system output fixed at BlackHole 16ch for the whole call instead of switching it — confirmed live, switching output mid-call dropped calls the same way switching input did.
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

## 5. Audio routing — automatic, for FaceTime AUDIO calls specifically

This feature targets **FaceTime Audio calls**, not Video calls. Confirmed against Apple's own FaceTime User Guide and live testing: an audio-only call's menu bar shows an "Audio" menu with only Mic Mode (Voice Isolation/Wide Spectrum) — there's no per-call camera/microphone/output device picker the way FaceTime Video's **Video menu** has. So there's no manual per-call selection to make here at all; FaceTime Audio just uses whatever your Mac's SYSTEM default input/output devices are, the same fallback behavior Apple documents for when no explicit device is chosen.

The script drives that automatically now — no manual step needed each call, and no per-call or per-turn switching either (both were tried and both dropped live calls — see below):

- At **startup** (before it even starts polling for calls, not per-call), it fixes your **system default input** to `BlackHole 2ch` and your **system default output** to `BlackHole 16ch` — and leaves both alone for as long as the process runs.
  - Input stuck on `BlackHole 2ch` the whole time is what lets FaceTime treat Curant's synthesized speech as if it were your real microphone.
  - Output stuck on `BlackHole 16ch` the whole time is what lets FaceTime's own call audio (the caller's voice) always be available for the script to record, with nothing to switch.
- Curant's own speech instead goes directly into `BlackHole 2ch` via **SoX** (`sox file -t coreaudio "BlackHole 2ch"`), targeting that device explicitly rather than relying on the system default output — that's what makes it safe to leave output fixed at `BlackHole 16ch` instead of switching it during playback.

This landed here after two real, live-confirmed failure modes: switching input *after* `Accept` (instead of before) dropped every call at a fixed ~4 seconds in — FaceTime appears to lock its microphone in at connect time, and changing it afterward looks like the mic disappeared. Fixing that but still switching *output* per-turn dropped calls too, just later. Fixing both devices permanently at startup, with SoX bypassing the need to ever touch output at all, was the combination that actually held.

You won't hear either side of the call through your normal speakers while this is running, and neither will any other app that wants your real microphone (Zoom, Voice Memos, etc.) — that's expected the whole time the script is running, not just during a call. If you place a FaceTime **Video** call instead, detection/accept should still work (the banner looks the same either way), but this routing has NOT been verified for video calls — they may still need the old manual **Video menu → Microphone/Speaker** approach instead, since a video call's Video menu could override the system defaults this script sets.

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
mkdir -p /usr/local/bin/assets
cp assets/facetime_accept_button.png /usr/local/bin/assets/
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
