# Curant Beta — What's Still Rough

Thanks for trying this out. It's a real beta, not a finished product — a few things are worth knowing before you start.

## No legal terms yet

There's no signed Terms of Service or Privacy Policy. There's a plain-language draft of what those documents *should* say once a lawyer reviews and formalizes them, but nothing here is a binding agreement. You're testing this as a favor, on the understanding that it's pre-launch software — not as a customer of a governed service.

## What actually stays on your Mac vs. what doesn't

Your persona, standing instructions, memory, and conversation history never leave your Mac. Your AI provider API key (Anthropic/OpenAI/Gemini) is stored locally and used directly from your machine — Curant's server never sees it. The one thing that does touch a server: license/billing checks, which are bypassed entirely for this beta (`CURANT_DEV_UNLICENSED=1`) since there's no live license server yet.

## The one real security caveat: connected content + tool access

If you connect Gmail or turn on Dean's `autonomous` shell mode, be aware of this specific risk: content from outside sources (an email someone else wrote, a web page) becomes part of what the model reads in the same turn it has access to tools that can take real actions. There's a genuine safeguard already in place — nothing sends, deletes, or modifies anything without your explicit confirmation, enforced in code, not just by the model's judgment — but that protection is only as strong as actually reading what you're confirming before you approve it. It's fully bypassed in Dean's `allowlist`/`autonomous` shell modes by design (that's the tradeoff those modes explicitly make), so `confirm` mode is the safer default if you're unsure.

IRIS's Gmail inbox-check (if you set it up) is a narrower case: it can only read and draft, never send — every drafted reply sits in a queue until you personally approve it in a normal conversation.

## What's in scope for this beta vs. not

**Works:** texting Curant, all ten personas, daily briefings, memory, Gmail connect (optional), the shell-access modes for Dean.

**Not set up:** FaceTime call answering. It needs manual permission grants and a physical audio driver that can't be scripted — texting works completely without it. If you want it, ask and we can walk through it separately.

## If something breaks

Logs live in `/tmp/curant-*.log` and `/tmp/curant-*-error.log` for each background service. Screenshot or copy what's in there along with what you were doing, and send it over — that's genuinely useful, not an inconvenience. This is exactly the kind of thing the beta is for.
