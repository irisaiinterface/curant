# Curant — Security Audit

**Date:** 6 August 2026
**Scope:** `curant-cloud/server/app.py`, `curant-current/curant-cli`, at commit `7cb43c5`
**Method:** Static review plus live exploitation testing in an isolated sandbox. Every finding marked *Confirmed* was demonstrated by actually executing it, not inferred from reading code.

---

## Severity summary

| # | Finding | Severity | Status |
|---|---------|----------|--------|
| 1 | Customer login requires only an email address — no password | **Critical** | **FIXED** — SMS code |
| 2 | Shell allowlist trivially bypassed → arbitrary command execution | **Critical** | **FIXED** |
| 3 | Shell "sandbox" does not contain anything | **Critical** | **FIXED** — macOS sandbox-exec containment added (untested on real hardware); wording corrected |
| 4 | Vapi webhook unauthenticated → memory disclosure + poisoning | **High** | **FIXED** |
| 5 | `/vapi-llm/<customer_id>` unauthenticated → API-key abuse | **High** | **FIXED** |
| 6 | Telnyx signature verification fails **open** | **High** | **FIXED** — fails closed |
| 7 | OAuth callbacks not bound to session → account-linking CSRF | **High** | **FIXED** |
| 8 | Indirect prompt injection reaches consequential tools | **High** | Mitigated — tool description warns explicitly; shell now contained + network-denied. Inherent to the pattern; not "solved" |
| 9 | No replay protection on Telnyx webhooks | Medium | **FIXED** — 5-min window |
| 10 | `browse_page` has no SSRF protection | Medium | **FIXED** — scheme + resolved-IP checks, enforced on redirects/subresources too |
| 11 | `FLASK_DEBUG` env var can enable Werkzeug debugger (RCE) | Medium | **FIXED** — also requires CURANT_DEV_MODE |
| 12 | Rate limiting keyed on spoofable `remote_addr` | Low | **FIXED** — TRUSTED_PROXY_COUNT-aware `client_ip()` |
| 13 | Sensitive-field blocklist has real gaps | Low | **FIXED** — IBAN/SWIFT, passport, tax ID, DOB, credentials added |
| 14 | OAuth exception text returned to the browser | Low | **FIXED** |

### Also found during remediation (not in the original 14)

| # | Finding | Severity | Status |
|---|---------|----------|--------|
| 15 | `phone_routing` stored the assigned DID, but lookups use the sender's number — **all inbound SMS and voice routing was broken**, and the customer's own number was never collected anywhere | **Critical (functional)** | **FIXED** |

Finding 15 surfaced while designing the SMS login: there was no customer phone number to send a code to. Tracing why revealed that provisioning inserted Curant's own DID into `phone_routing` while `get_customer_by_phone()` is called with the *sender's* number — so no inbound message could ever match a customer. Every text would have been answered "this number is not associated with an active Curant account," and every call would have fallen through to the unknown-caller branch. Collecting and routing on the customer's own mobile fixes both that and finding 1, which is why they were done together. It is also the safer key: routing on the dialled DID instead would mean anyone who discovered a customer's Curant number was treated as that customer.

---

## 1. Customer login requires only an email address — **Critical**

`curant-cloud/server/app.py`, `customer_login()` (~L5094):

```python
email = request.form.get("email", "").strip().lower()
cust  = get_customer_by_email(email)
if cust and cust["active"]:
    session["customer_id"] = cust["id"]
    return redirect(url_for("cloud_dashboard"))
```

There is no password, magic link, or verification code — and confirmed by schema inspection, **the `customers` table has no password/credential column at all.** Authentication is "know an email address."

Anyone who knows a customer's email gets the full dashboard: connected-tool OAuth connections (Outlook, Teams, Jira, GitHub…), the ability to connect and disconnect accounts, billing state, spend caps, persona instructions, and API-key settings. Email addresses are not secrets — they appear on business cards and in every message the customer has ever sent.

The 5-attempts-per-5-minutes rate limit is irrelevant; no brute force is required.

**Fix:** a real authentication factor before Cloud touches a real customer. Emailed magic link is the smallest change that fits the existing architecture (a utility Workspace account already exists to send from). This is a launch blocker.

---

## 2. Shell allowlist trivially bypassed — **Critical, introduced this session**

`curant-current/curant-cli`, `_shell_command_is_allowlisted()`.

I wrote a prefix matcher and a code comment asserting that shell metacharacters "break the match rather than sneaking past it." **That claim is false.** The metacharacter only breaks the match when it lands immediately after the allowlisted prefix. Add any argument first and the whole string passes.

Confirmed by execution, in `allowlist` mode, with **no confirmation prompt**:

| Command | Allowlisted? |
|---|---|
| `git status; rm -rf /tmp/PWNED` | False (as intended) |
| `git log --oneline; echo INJECTED` | **True** |
| `ls && echo INJECTED` | **True** |
| `echo hi \| bash` | **True** |
| ``ls `echo INJECTED` `` | **True** |
| `git diff $(echo INJECTED)` | **True** |
| `cat /etc/passwd` | **True** |

Live run:

```
shell_exec("git log --oneline; echo PWNED_ARBITRARY_EXECUTION", confirmed=False)
→ [exit code 0]
  PWNED_ARBITRARY_EXECUTION
```

`allowlist` mode — the option a customer would reasonably pick believing it is the *safe* middle setting — is in practice equivalent to `autonomous`, while telling them the opposite.

Contributing factor: `subprocess.run(command, shell=True)` means the shell interprets `;`, `&&`, `|`, backticks and `$()`. A prefix allowlist and `shell=True` are fundamentally incompatible.

**Fix:** reject any command containing shell metacharacters before allowlist matching, and/or drop `shell=True` in favour of `shlex.split` + `shell=False`. Also drop `cat` from the allowlist (see below).

---

## 3. The shell "sandbox" does not contain anything — **Critical, introduced this session**

The `cwd` guard only validates the `cwd` **parameter**. The command string itself may reference any absolute path. Confirmed:

```
shell_exec("cat /etc/hostname", confirmed=False, mode=allowlist)  → claude
shell_exec("echo owned > /tmp/sec_escape_proof.txt", confirmed=True)
→ file written outside the workspace: True
```

`cat` is on the allowlist, so **reading any file on the customer's Mac requires no confirmation at all** — SSH keys, browser cookie databases, `~/.aws/credentials`, the Curant config file holding their own Anthropic API key.

The word "sandboxed" appears in the code comments, the tool description shown to the model, and the README I wrote. It is not accurate: `cwd` is a starting directory, not a boundary. That false assurance is worse than the missing control, because it discourages scrutiny.

**Fix:** either describe it honestly as unsandboxed and rely on confirmation, or implement real containment (macOS `sandbox-exec` profile, or a container). Until then, correct the wording everywhere it appears.

---

## 4. Vapi webhook is unauthenticated — **High**

`/webhooks/vapi` has no signature verification, no shared secret, no auth of any kind — while the Telnyx and Stripe handlers both verify. (The Stripe handler's docstring even claims it follows "the same signature-verification-before-trusting-payload pattern as the existing Telnyx/Vapi webhook handlers" — for Vapi, that pattern does not exist.)

Two consequences:

- **Memory disclosure.** POST `assistant-request` with a guessed customer phone number and the response body contains `systemPrompt`, built by `build_system_prompt(customer, memories, people)` — the customer's stored memories and the people who matter to them, returned to an unauthenticated caller.
- **Memory poisoning.** POST `end-of-call-report` with a fabricated transcript and it is written into the customer's history via `save_message()`, becoming context for future replies.

**Fix:** verify Vapi's webhook secret, fail closed.

---

## 5. `/vapi-llm/<customer_id>` is unauthenticated — **High**

Anyone who can present a valid `customer_id` can POST arbitrary message arrays and receive completions **billed to that customer's own Anthropic key**, with their system prompt. The only protection is that `customer_id` is a 32-hex-char random value — but it is not a credential: it travels to Vapi, sits in Vapi's dashboard config, and appears in logs at both ends.

**Fix:** a per-customer shared secret in the URL or an `Authorization` header set when the assistant config is returned.

---

## 6. Telnyx signature verification fails open — **High**

```python
if not TELNYX_WEBHOOK_SECRET:
    print("WARNING: ... skipping webhook signature verification. Set this in production.")
    return True  # allow in dev, never in prod
```

Nothing enforces "never in prod." A deploy that forgets one env var silently accepts **any** POST to `/webhooks/sms` — letting an attacker impersonate any customer's phone number, converse as them, and drive every tool the customer has connected. Compare Stripe's handler, which correctly fails closed.

**Fix:** refuse to start, or hard-fail the route, when the secret is missing and the app is not explicitly in dev mode.

---

## 7. OAuth callbacks are not bound to the session — **High**

Neither `mcp_oauth_callback` nor `graph_oauth_callback` compares the pending row's `customer_id` to `session["customer_id"]`; the `state` lookup alone is treated as authentication.

This enables classic **account-linking CSRF**: an attacker starts a connect flow on their own account, obtains a valid `state`, and induces the victim to complete authorization with it. The victim's Microsoft/Jira tokens are then stored against the **attacker's** `customer_id`, giving the attacker persistent read/write access to the victim's mailbox and Teams through their own Curant persona.

**Fix:** require an active session on the callback and verify it matches the pending row's `customer_id`.

---

## 8. Indirect prompt injection reaches consequential tools — **High**

Untrusted third-party content — email bodies (`gmail_read`, `graph_search_emails`), attachments, web pages (`browse_page`) — flows into model context in the same turn that tools with real side effects are available: sending mail as the customer, posting to Teams, submitting forms, and now **shell execution**.

The `confirmed=true` pattern is a genuine mitigation and is enforced in code rather than by prompt, which is the right design. But it is only as strong as the customer's willingness to read what they are approving, and in `allowlist` mode (finding #2) it is bypassed entirely.

Adding shell access materially raised the ceiling on this risk: previously the worst case was an unwanted email; now it is arbitrary code execution on the customer's Mac.

**Fix:** treat tool *output* as untrusted data explicitly in the prompt; consider disallowing shell calls in a turn whose context includes freshly-fetched external content.

---

## 9–14. Medium and low findings

- **9. Replay (Medium).** The Telnyx `timestamp` is included in the signed payload but never checked for freshness. A captured valid webhook replays indefinitely.
- **10. SSRF (Medium).** `browse_page()` passes the URL straight to Playwright with no scheme or host validation — `http://localhost`, `http://169.254.169.254/`, `file://` are all reachable. On Home this means the customer's LAN and router; on Cloud, the metadata service.
- **11. Debug RCE (Medium).** `app.run(host="0.0.0.0", debug=os.environ.get("FLASK_DEBUG") == "1")` — one env var away from an interactive Werkzeug console. Should be impossible to enable in a production image.
- **12. Rate-limit basis (Low).** Keyed on `request.remote_addr`; behind a reverse proxy this collapses to the proxy IP, breaking per-client limits on login and unlock.
- **13. Sensitive-field gaps (Low).** `_is_sensitive_field` correctly catches `card_number`, `cvv`, `ssn`, `routing_number`, `cvc`. It misses `iban`, `taxId`, `passport`, `dob`, `pan`.
- **14. Error leakage (Low).** OAuth failures return raw exception text to the browser (`return f"Couldn't complete the connection: {e}"`), which can include internal URLs or provider responses.

---

## What is genuinely solid

Worth stating plainly, because the list above is unbalanced by design:

- **No SQL injection.** Every query is parameterized. The three dynamically-built statements use hardcoded identifiers with parameterized values.
- **Stripe webhook** verifies signatures and fails closed; the webhook, not the browser redirect, is the source of truth for subscription state — closing the obvious "skip payment via the success URL" gap.
- **Admin login** uses `secrets.compare_digest`, is rate limited, CSRF-protected, and fails closed when unset.
- **CSRF coverage** is complete on every browser-session POST route; the routes without it are webhooks and token-authenticated endpoints where it does not apply.
- **Option B key handling** genuinely never exposes plaintext keys server-side.
- **Home's local dashboard** is correctly bound to `127.0.0.1`.
- **Encryption at rest** now covers customer API keys, MCP tokens, and Graph tokens consistently.
- **`confirmed=true` enforced in code** rather than by prompt instruction is the right structural choice and resists model regressions.

---

## Recommended order of work

1. **Findings 2 and 3** — shell access. Fastest to fix, and it is live in `main` right now with a materially false safety description.
2. **Finding 1** — customer authentication. Hard launch blocker for Cloud.
3. **Findings 4, 5, 6, 7** — webhook and OAuth authentication.
4. **Finding 8** — prompt-injection posture, informed by how 2 and 3 are resolved.
5. Medium/low items before public launch.

A note on process: findings 2, 3, and 14 were introduced by me earlier in this same session, and #2 and #3 shipped with code comments and README text asserting protections that testing disproved. The functional test I ran at the time exercised only the intended paths, never an adversarial one — which is exactly how a control ends up documented as working while being trivially bypassable.
