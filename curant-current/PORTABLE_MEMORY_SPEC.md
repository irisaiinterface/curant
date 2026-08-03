# Curant Portable Memory Format (CPMF) — v1.0

## What this is

A versioned, model-agnostic JSON format for personal AI context: persona,
preferences, standing instructions, long-term memories, and important
people. The design goal is specific and deliberate: **the file should be
equally useful to Claude, GPT, Gemini, or a model that doesn't exist
yet, with zero knowledge of Curant.**

This is the technical backbone of a strategic bet: that a person's
accumulated context with an AI should be a portable asset *they* own,
not a retention mechanic locked inside one vendor's product. Every major
AI lab's current memory feature is architected the opposite way — memory
that only works inside their app, which is what actually creates
switching costs, not model quality. This format is a deliberate bet
against that pattern.

## Design principles (binding — a change that violates these isn't a
   patch, it's a new major version)

1. **No vendor-specific fields.** No OpenAI-specific function-calling
   schemas, no Anthropic-specific system-prompt conventions, no
   embeddings tied to one provider's vector space. Everything is plain
   strings a human — or any model — can read directly.
2. **Self-contained.** A consuming tool needs nothing outside this file
   to build a working system prompt. No callbacks to Curant's server, no
   external schema fetches.
3. **Human-readable, not just machine-readable.** Every field should
   make sense to a person reading the raw JSON, not just to code parsing
   it. This is also why memories are free-text sentences, not structured
   key-value facts — natural language is the actual interlingua between
   different models' training, not a custom ontology.
4. **Additive versioning.** New optional fields can be added in minor
   versions. Anything that would break an old consumer (renaming a
   field, changing a type, removing a field) requires a major version
   bump. Old files stay readable by new tools; that's the whole point of
   a portable format outliving any one implementation.
5. **No secrets, ever.** No API keys, no license keys, no device
   identifiers, no anything that grants access to an account. Worst case
   if this file leaked: someone reads preferences and memories the
   owner would be pasting into a chat window themselves anyway.

## Schema (v1.0)

```json
{
  "cpmf_version": "1.0",
  "generated_at": "2026-07-26T14:32:00Z",
  "persona": {
    "style_summary": "Composed, precise, and formal. No slang or exclamation points.",
    "instructions": "Keep replies under 3 sentences. Always confirm before sending anything."
  },
  "preferences": {
    "reply_format": "text",
    "proactivity_enabled": true
  },
  "memories": [
    {
      "content": "Has a big presentation next Tuesday",
      "created_at": "2026-07-20T09:15:00Z"
    }
  ],
  "people": [
    {
      "name": "Jamie",
      "relationship": "business partner",
      "note": "Based in Austin, usually free after 5pm"
    }
  ]
}
```

### Field reference

| Field | Type | Required | Notes |
|---|---|---|---|
| `cpmf_version` | string | yes | Semver-ish, e.g. `"1.0"`. Consumers should check this before parsing. |
| `generated_at` | string (ISO 8601 UTC) | yes | When this snapshot was produced. |
| `persona.style_summary` | string | yes | Plain-language description of tone/style. Deliberately NOT a vendor's actual system-prompt wording — that's implementation IP, this is the portable gloss of it. |
| `persona.instructions` | string | no | Standing instructions, in the owner's own words. |
| `preferences.reply_format` | string | no | Free-form hint (`"text"`, `"voice"`, etc.) — consumers that don't support a given value should ignore it, not error. |
| `preferences.proactivity_enabled` | boolean | no | Whether unprompted check-ins are welcome. |
| `memories` | array of objects | yes (may be empty) | Each: `content` (string, required), `created_at` (string, optional). |
| `people` | array of objects | yes (may be empty) | Each: `name`, `relationship`, `note` — all strings. |

### What's deliberately NOT in this format

- **API keys, license keys, device/account identifiers.** This file
  grants access to nothing. Whatever key a consuming tool needs to
  actually call a model is that tool's own problem, not this format's.
- **Raw conversation history / message logs.** This is a snapshot of
  distilled context, not a transcript. Consumers wanting conversational
  continuity should build that themselves from their own interaction
  history — mixing "durable facts" and "recent chat log" into one file
  defeats the purpose of a stable, portable snapshot.
- **Embeddings or vector representations.** Tied to one model's
  embedding space, meaningless to a different model. Plain text is the
  actual portable representation.
- **Nested/vendor-specific extension blocks.** If a future version needs
  a genuinely new capability, it becomes a new top-level optional field
  in a minor version, documented here — not a vendor-namespaced
  passthrough blob that only one consumer understands.

## Reference implementation

`curant-cli context-export --format json` produces a CPMF v1.0 file
(encrypted at rest — see below), and `curant-cli context-show --format
json` decrypts and validates one. This is the reference implementation,
not the only legitimate one — any tool that reads/writes valid CPMF JSON
is a conforming implementation, whether or not it has anything to do
with Curant.

## Encryption is a Curant implementation detail, not part of the spec

The CPMF JSON schema above is what any tool should be able to read and
write. Whether a given implementation encrypts it at rest, and how, is
outside the spec's scope — Curant's own implementation encrypts with a
customer-chosen passphrase (scrypt-derived key, Fernet) before writing
to disk, but a CPMF file could just as validly exist as plaintext JSON
if a different context called for that.

## Versioning policy

- **v1.0 (this version):** initial schema.
- Future minor versions (v1.1, v1.2, ...) may add optional fields.
  Existing consumers should be able to ignore fields they don't
  recognize rather than fail.
- A future major version (v2.0) would be reserved for a breaking change
  — a renamed/removed/retyped field — and should remain a deliberate,
  rare event given the entire value of the format is long-term
  stability across tools and time.
