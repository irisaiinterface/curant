# Setting up FaceTime auto-answer calls (EXPERIMENTAL)

This feature is fundamentally different from everything else in Curant. Text-in/text-back (`curant-watcher.py`) is built on documented, verifiable APIs — a readable SQLite database and AppleScript's Messages support. FaceTime has neither: no API, no AppleScript dictionary, and no audio-routing hooks. Everything below is either UI automation (can silently break on a macOS update) or manual system configuration only you can do on your actual Mac. Budget time to iterate — this is not a five-minute setup.

Do these steps in order. Steps 1–3 are one-time. Step 4 is per-call (until you confirm FaceTime remembers your choice).

---

## 1. Install BlackHole (two separate virtual devices)

Curant uses **two** BlackHole devices so your outgoing (synthesized) voice and the caller's incoming voice never mix into the same audio stream:

- **BlackHole 2ch** → carries Curant's synthesized speech *to* FaceTime (used as FaceTime's Microphone)
- **BlackHole 16ch** → carries the caller's voice *out of* FaceTime so the script can record and transcribe it (used as FaceTime's Speaker/Output)

```bash
brew install blackhole-2ch blackhole-16ch switchaudio-osx ffmpeg
```

After installing, **restart your Mac** (or at least log out/in) — BlackHole devices sometimes don't appear in Sound settings until CoreAudio restarts.

Verify both appear:

```bash
system_profiler SPAudioDataType | grep -A2 BlackHole
```

You should see both `BlackHole 2ch` and `BlackHole 16ch` listed.

---

## 2. Grant permissions

- **System Settings → Privacy & Security → Accessibility** — add and enable Terminal (or whatever runs the script). Without this, the script cannot click FaceTime's Accept/Decline buttons at all.
- **System Settings → Privacy & Security → Microphone** — FaceTime needs this regardless.
- Keep **FaceTime.app open and signed in** whenever the answerer is running.

---

## 3. Verify the script can even detect FaceTime's window structure

Before trusting it to answer anything, run in dry-run mode and place a real test call to yourself from another device/Apple ID:

```bash
python3 curant-facetime-answerer.py --dry-run --apple-id "your.apple.id@icloud.com"
```

Call the Mac's FaceTime number/Apple ID from your phone. Watch the terminal output. It should print the incoming call's window text. If it prints `no_facetime_process` or nothing at all when you know FaceTime is ringing, the UI-detection heuristic in `poll_for_incoming_call()` doesn't match your macOS version — this is the part most likely to need adjusting. Useful next step: run

```bash
osascript -e 'tell application "System Events" to tell process "FaceTime" to get name of every window'
```

while a call is ringing, and compare what it prints against what `DETECT_AND_DESCRIBE_SCRIPT` in the script expects.

**Do not proceed to live answering until dry-run reliably detects an incoming call.**

---

## 4. Route FaceTime's audio through BlackHole (per-call, manual)

FaceTime lets you pick input/output devices from its menu bar while a call is active: **Video menu → Microphone** and **Video menu → Speaker** (or, on some macOS versions, in FaceTime's own in-call controls).

During a live call (or immediately after the script auto-accepts one):
1. **Video → Microphone → BlackHole 2ch** — this makes FaceTime send whatever plays into BlackHole 2ch (i.e., Curant's synthesized speech) to the caller, instead of your real mic.
2. **Video → Speaker → BlackHole 16ch** — this makes FaceTime send the caller's voice into BlackHole 16ch instead of your speakers, where the script records and transcribes it.

This selection is **per FaceTime window/call** on most macOS versions — it may not persist automatically between calls. If it turns out your version does remember it, great, one less manual step; verify by placing a second test call and checking the Video menu's current selection before assuming it stuck.

The script itself only automates one piece of this: it sets your **system default output** to `BlackHole 2ch` right when it accepts a call (`set_system_output_device`), since `afplay`/`say` always play to whatever the system default output is — it cannot select FaceTime's per-call speaker/microphone settings for you. Steps 1 and 2 above still need to be set by hand.

---

## 5. Confirm the transcription dependency

Call transcription uses OpenAI's Whisper API regardless of which provider (Anthropic/Gemini/OpenAI) you use for replies. Set it once:

```bash
curant-cli set-api-key sk-... --provider openai
```

This is a real, ongoing cost per call minute (Whisper API pricing) on top of whatever your reply-generation provider costs — factor that in before leaving this running unattended.

---

## 6. Run it for real

```bash
python3 curant-facetime-answerer.py --apple-id "your.customer@icloud.com"
```

Leave it running (a `launchd` plist like `com.curant.watcher.plist` could wrap this later, once it's actually working reliably — not set up yet, since there's no point auto-starting something still being debugged live).

**Expect to iterate.** Realistic failure modes, roughly most-to-least likely:
- The Accept-button click never fires (UI structure guess is wrong for your macOS version) — fix `ACCEPT_CALL_SCRIPT`/`poll_for_incoming_call` against what step 3's `osascript` probe actually shows.
- Call gets answered but the caller hears silence (BlackHole 2ch not selected as FaceTime's Microphone, or `SwitchAudioSource` didn't actually flip system output — check `system_profiler SPAudioDataType` and `SwitchAudioSource -c` mid-call).
- Script "hears" nothing from the caller (BlackHole 16ch not selected as FaceTime's Speaker, or the ffmpeg device index lookup in `_find_avfoundation_audio_device_index` picked the wrong device — run `ffmpeg -f avfoundation -list_devices true -i ""` yourself and compare).
- Echo/garbled audio if both directions end up on the same BlackHole device by mistake — re-check step 4.

Report back exactly what you see (terminal output + what happened on the call) and it can be fixed — this really is a "build it experimentally, you test live" feature, same as we agreed.
