# Curant Cloud — Privacy Policy (Draft)

**Last updated:** July 2026
**Note:** This is a working draft. Have a lawyer review before publishing, especially the
data-controller language, retention periods, and any jurisdiction-specific requirements
(GDPR, CCPA, etc.) that apply to your customer base.

---

## Who we are

Curant Cloud is operated by [Your Legal Entity Name], a [state/country] [LLC/Corp].
We provide a personal AI Secretary service reachable by SMS and voice call.
We are the data controller for information collected through Curant Cloud.

Contact: [your@email.com]

---

## What we collect and why

### Account information
- Your name, email address, and preferred area code — collected at signup to create
  your account and match you with a local phone number.
- Your subscription and billing status — managed through our payment processor
  (Stripe). We do not store full card numbers.

### Your phone number
- We provision a dedicated Telnyx phone number matched to your area code and assign
  it to your account for as long as you're subscribed. This number is released
  immediately upon cancellation — it is never held after your account closes.

### Your AI provider API key
How your key is stored depends on the choice you made at signup:

**Option A — We store it:** Your key is encrypted at rest using AES-256 (Fernet,
with a server-side key we control). We use it only to answer your messages. It is
never shared with any third party, and it is deleted when you cancel.

**Option B — You hold it:** Your key is encrypted in your browser using Web Crypto
(PBKDF2, 310,000 iterations, AES-GCM) with a passphrase only you know, before it
ever leaves your device. We store only the resulting encrypted blob. We cannot decrypt
it under any circumstances. We have no way to recover it if you lose your passphrase.

### Your conversations and memory
When you text your Curant number, we process your message to generate a reply.
We store:
- **Conversation history:** your last ~20 messages (roughly 10 exchanges), retained
  for 48 hours, used only to give Curant conversational context. Automatically deleted
  on a rolling basis — we do not build a permanent transcript of your conversations.
- **Long-term memory:** facts Curant extracts from your conversations to remember
  long-term (preferences, relationships, ongoing projects). Stored until you delete
  them — visible and editable in your dashboard at any time.
- **Important people:** names and relationships you tell Curant about. Same retention
  as long-term memory.
- **Standing instructions and persona settings:** how you've configured your Curant.
  Stored until you change or delete them.

### Technical and operational data
- Error codes and component identifiers (e.g. "LLM call failed") used to detect
  and fix broken setups. Never includes message content.
- Request timestamps and phone numbers, used for rate limiting and abuse prevention.

---

## What we do not collect

- We do not store your full conversation history beyond the 48-hour rolling window.
- We do not use your conversations to train AI models. Your messages go directly
  to your chosen AI provider (Anthropic or OpenAI) under your own API key — billed
  to and visible in your own account with that provider.
- We do not read your conversations except as technically necessary to route messages
  and generate replies. No human at Curant reads your messages in the normal course
  of operations.
- We do not sell your data to third parties.
- We do not run ads in Curant.

---

## Where your data goes

**AI providers:** When you send a message, it goes from our server to your chosen
AI provider (Anthropic or OpenAI) via your API key to generate a reply. Your usage
is billed to and governed by your own account with that provider. Their privacy
policies apply to that processing.

**Telnyx:** Your phone number is provisioned through Telnyx, and your messages
are routed through their infrastructure. Telnyx's own privacy policy governs
their handling of SMS metadata and routing information.

**Vapi (voice calls):** Incoming voice calls are handled through Vapi, which
transcribes speech and routes it to our server. Vapi's privacy policy governs
their handling of call audio and transcription. We save a summary of call content
for memory purposes, subject to the same retention rules as text conversations.

---

## Database encryption

The database storing your account information, memories, and conversation history is
encrypted at rest using SQLCipher (AES-256). A copy of the raw database file is
unreadable without the encryption key, which is stored separately from the data.

Individual API keys (Option A customers) are additionally encrypted at the field
level using Fernet (AES-128 in CBC mode with HMAC-SHA256), with a separate key from
the database encryption key, providing defense in depth.

---

## Data retention and deletion

| Data | Retained until |
|---|---|
| Conversation history | 48 hours from each message (rolling) |
| Long-term memories | Until you delete them (dashboard or request) |
| Important people | Until you delete them (dashboard or request) |
| Settings and persona | Until you change or cancel |
| Account information | Until cancellation + [30] days (in case of reactivation) |
| API key (Option A) | Deleted immediately on cancellation |
| API key ciphertext (Option B) | Deleted immediately on cancellation |
| Error reports | [90] days, then deleted |

**On cancellation:** your phone number is released to Telnyx immediately. The
routing entry linking that number to your account is deactivated at the same moment —
a number reassigned to a future customer can never route to your account. Your account
data is marked inactive and permanently deleted after [30] days.

**Account deletion requests:** email [your@email.com] and we will delete your account
and all associated data within [5] business days.

---

## Your rights

You can, at any time:
- **View** your memories, important people, and settings in your dashboard
- **Delete** individual memories or people from your dashboard
- **Update** your settings, persona, and instructions
- **Request deletion** of your entire account and all associated data
- **Export** your data — email us and we'll provide a copy within [10] business days
- **Switch key storage modes** — contact us; this requires re-entering your API key

If you are in the EU or UK, you also have the right to data portability, to restrict
processing, and to lodge a complaint with your local supervisory authority.
If you are in California, you have rights under CCPA — contact us to exercise them.

---

## Security

- Database encrypted at rest with AES-256 (SQLCipher)
- All data in transit encrypted with TLS (HTTPS)
- API keys doubly encrypted (field-level Fernet + database-level SQLCipher) for
  Option A customers
- Session cookies set with HttpOnly, SameSite=Lax, and Secure flags
- CSRF tokens on all forms
- Rate limiting on all login and webhook endpoints
- We run as a non-root user inside Docker containers
- We do not log message content in any operational logs

If you discover a security vulnerability, please email [security@yourdomain.com].
We will respond within 48 hours and work to fix confirmed issues promptly.

---

## Changes to this policy

We will notify you by email at least [14] days before any material change to this
policy takes effect. The "last updated" date at the top will always reflect the
current version.

---

## Contact

[Your Legal Entity Name]
[Address]
[your@email.com]

*[Note for legal review: confirm jurisdiction, add applicable legal basis for
processing under GDPR if serving EU customers (likely legitimate interest for
service delivery, consent for optional features), add CCPA-specific disclosures
if serving California customers, confirm retention periods with counsel, add
governing law and dispute resolution clause before publishing.]*
