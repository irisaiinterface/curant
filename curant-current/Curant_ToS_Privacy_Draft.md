# Curant — Terms of Service & Privacy Policy (Plain-Language Draft)

*This is a plain-English draft of what our policies should say, written to guide a lawyer in drafting the real legal documents. This is not itself a legal document.*

---

## Terms of Service (Plain-Language Draft)

### What Curant Is
Curant gives you a personal AI secretary ("your Curant") that you can reach by call, text, or the Curant website. Your Curant is tailored to you — your preferences, your contacts, your working style — and it works only for you, not for a team or company as a whole.

### Your Account
- You must be 18 or older to create a Curant account.
- You're responsible for keeping your account and license key secure. If you believe your account has been compromised, contact us immediately.
- Curant is for one person's personal or professional use. It is not a shared or multi-user tool.

### Your Claude API Key
- Every Curant account requires you to connect your own API key from a supported AI provider (currently Anthropic/Claude or OpenAI/GPT — you choose, and can switch at any time without losing your Curant's persona, instructions, or memory).
- Your usage bills directly to your own account with that provider, based on your own usage. Curant does not mark up, resell, or profit from your API usage.
- You are responsible for keeping that provider account in good standing and funded. If your key stops working, your Curant will stop working until it's resolved.

### What Your Curant Can and Cannot Do
- Your Curant can draft emails, documents, and messages on your behalf. **It will not send anything on your behalf without your explicit confirmation**, unless you specifically opt in to autonomous sending for a defined, recurring task.
- Your Curant may proactively reach out to you (reminders, check-ins) based on the frequency you set during onboarding. You can change or turn this off at any time.
- Your Curant will ask for your confirmation before taking any action that is difficult or impossible to undo.

### Payment and Subscriptions
- Curant is billed on a subscription basis, with optional add-ons priced individually and shown clearly before you purchase them.
- If your subscription payment fails or lapses, your Curant will stop responding until payment is resolved. We will make a reasonable effort to notify you before this happens.
- You can cancel your subscription at any time. Cancelling stops future billing; it does not retroactively refund the current billing period unless required by law.

### Acceptable Use
- You may not use Curant to harass, deceive, impersonate, or harm others.
- You may not attempt to reverse-engineer, extract, or misuse the underlying system beyond what's needed for your own personal use.
- We reserve the right to suspend accounts that violate these terms, with notice where reasonably possible.

### Liability
- Curant is a tool that assists you — you remain responsible for reviewing and approving anything sent or acted on in your name.
- We are not liable for indirect, incidental, or consequential damages arising from use of Curant, to the fullest extent permitted by law.
- Curant is provided "as is." While we work to keep it reliable, we do not guarantee uninterrupted availability.

### Changes to These Terms
- We will notify you of material changes to these terms before they take effect.

---

## Privacy Policy (Plain-Language Draft)

### Our Privacy Principle
We believe you should always know exactly what we store, why, and how to remove it. This policy is written to be read, not skimmed past.

### What We Collect (on our servers)
- **Account information:** your name, phone number, and payment details (processed securely through our payment provider — we do not store full card numbers ourselves).
- **License and billing status:** your license key, plan, and which add-ons you've unlocked.
- **Device binding:** a license activates on exactly one Mac, and a Mac can run exactly one license. We store an identifier for that pairing so we can enforce this and so you can request it be released if you get a new Mac.
- **Usage volume, not content:** a running count of how many messages your Curant has handled, reported periodically. We never see what was said — only how many exchanges happened.
- **Error signals, not details:** if something breaks (a crash, a failed setup step), we may receive a generic error code and which part of the system it came from — never the error's specific text, never a stack trace, never message content. This list of possible codes is fixed and closed; nothing outside it can reach our servers through this channel.
- **Device release requests:** if you ask to release your license's device binding (for example, after getting a new Mac), we log that request and its outcome (pending, approved, or denied) so it can be reviewed and acted on.

### What Lives On Your Own Mac Instead (not on our servers, at all)
- **Your Curant's tailored profile:** persona, standing instructions, important contacts, and communication style.
- **Ongoing memory:** observations your Curant learns through conversation over time. Each one is stored as a distinct, readable entry on your own machine — not a hidden or opaque log, and not something we can see.
- **Recent conversation, briefly:** your last ~10 exchanges, kept locally only so your Curant can follow a back-and-forth conversation, and automatically deleted after 48 hours regardless of how many messages that is. **If you use the Grace persona**, this window is deliberately larger — up to ~100 exchanges, kept for up to 2 weeks — since Grace's executive-assistant role benefits from a longer working memory of your recent back-and-forth. Both are still local-only, still auto-expiring, and never reach our servers.
- **Your AI provider API key(s):** stored locally on your Mac only, used to call your chosen provider (Anthropic or OpenAI) directly from your own machine. Never transmitted to or stored on our servers in any form — encrypted or otherwise, because we simply never receive it. This remains true even if you use the backup feature below — API keys are deliberately never included in it. If you switch providers, your persona, instructions, and memories carry over unchanged; only the key itself is provider-specific.
- **Your backup, if you choose to make one:** an encrypted file containing your persona, standing instructions, and preference settings — not your memories or important contacts, which are deliberately excluded — created and stored entirely on your own Mac (or wherever you choose to save it — an external drive, an iCloud Drive or Dropbox folder, etc.). We are never involved in this in any way and never receive a copy of it.

### Local Backup (entirely on your own device)
- You can create an encrypted backup of your persona, standing instructions, and preference settings at any time. You choose a passphrase (entered directly, never typed where it could be logged, and never stored anywhere including by us) and the file is encrypted before it ever touches disk, using a deliberately slow, memory-intensive method chosen to resist brute-force attempts against the file.
- This does not include your memories or important contacts — those are excluded on purpose, since they're meaningfully more sensitive than a settings choice. Restoring a backup brings back how your Curant is configured, not what it has learned about you.
- This file lives wherever you put it — by default alongside Curant's other local files, but you can point it anywhere, including a location that's backed up or synced elsewhere.
- We are not involved in this feature in any way — we never receive the file, the passphrase, or anything about it. We cannot help you recover it if you lose the passphrase or the file itself.
- Your AI provider API key(s), your memories, your important contacts, and recent conversation history are never included in a backup.
- Worth knowing plainly: a backup that only ever lives on the same Mac does not protect you against losing that Mac. It only serves that purpose if you save or copy it somewhere else.

### Portable Context Export (a separate, optional feature — entirely on your own device)
- If you want to bring your Curant's context into a conversation with a different AI, you can export it as an encrypted file, then decrypt it to your screen when you want to copy it out.
- Unlike the local backup above, this export does include your memories and important contacts — carrying that information is the entire purpose of this feature, since it's what makes the pasted context useful.
- The decrypted content never contains anything that could grant access to your account or Curant — no license key, no API key. It also never contains our proprietary persona wording, only a short plain description of your Curant's style.
- The file is encrypted the same way as the local backup (a passphrase you choose, never sent to or stored by us), and the decrypted text is only ever shown on your screen, never written back to disk unencrypted.
- We are not involved in this feature at all — we never see the file, the passphrase, or the content, encrypted or not.

### What We Do Not Do
- We do not sell your data to third parties.
- We do not use your conversations to train AI models — we never see your conversations in the first place.
- We have no ability to read or review your conversations, persona, instructions, memory, or backups — none of it reaches our servers in any form.
- If you've connected Calendar or Reminders, that information is read live on your own Mac at the moment your Curant needs it — it is never sent to us, written to our servers, or stored anywhere beyond that moment.

### Where Your Data Goes
- Your messages and requests go directly from your own Mac to your chosen AI provider, using your own API key, to generate your Curant's responses. This usage is billed to and visible in your own account with that provider. Our servers are never in this path.
- Your license/billing record is isolated per customer on our servers — never mixed with another customer's data.

### Your Control Over Your Data
- Your memory, persona, instructions, important contacts, and any backups live in files on your own Mac (or wherever you've chosen to store a backup) — you can view, edit, or delete them directly at any time, no request to us required.
- You can request deletion of your account/license record, usage counts, and error reports from our servers at any time.
- If you get a new Mac, you can submit a request to release the device binding on your license (or contact us directly) so you can activate on the new one. This is reviewed by a person before anything changes — it is not instant or fully automatic, by design.
- If you replace or wipe your Mac without having saved a backup somewhere else beforehand, your locally-stored memory and settings do not transfer automatically and cannot be recovered by us, since we never had them.

### Data Retention
- On our servers: we retain your license/billing record, device binding, cumulative usage count, and error reports for as long as your account is active, plus a limited period after cancellation in case you choose to reactivate, after which they are deleted.
- On your Mac: memory persists until you delete it yourself; recent conversation history auto-expires after 48 hours / 10 exchanges as described above; any backup file persists until you delete it, wherever you've chosen to keep it.

### Security
- Our servers hold no secrets worth encrypting at rest — no API keys, no message content, no memory.
- Your local `~/.curant/` files are restricted to your own user account on your Mac (standard file permissions, no other user or process on your machine can read them without separate access to your account).
- Access to our servers' license/billing data is limited to what's operationally necessary and logged.

### Changes to This Policy
- We will notify you of material changes before they take effect.

---

*Next step: have this reviewed and formalized by a licensed attorney before publishing or requiring customer agreement. This draft reflects product decisions made as of the current build of Curant and should be revisited if those decisions change.*
