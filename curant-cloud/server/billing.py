"""
Curant Cloud — Stripe billing.

Handles subscription checkout, the customer self-service portal, and
webhook-driven subscription state sync. Kept as its own module (same
separation-of-concerns pattern as the rest of Cloud) rather than folded
into app.py, since billing logic doesn't need anything else in app.py
except a DB connection and the customer row.

Pricing model (see reference-documents/CostCalculator.jsx for the cost
side of this):
  - Two flat-rate plan Prices: cloud_base ($29/mo), cloud_executive ($149/mo)
  - À-la-carte add-on Prices ($5-$25/mo each), added as extra subscription
    items on the SAME subscription — one Stripe subscription, one invoice,
    one customer-facing charge, no matter how many add-ons are attached.
  - August's metered generation usage rides as a Billing-Meter-backed
    price on that same subscription (flat + usage-based prices are
    supported together on one Subscription object).

This module never talks to the DB directly beyond the two small helpers
below — the caller (app.py routes / webhook handler) owns the actual
customer row updates, same as the rest of the codebase's style.

Environment variables required:
  STRIPE_SECRET_KEY       — Stripe secret key (sk_live_... / sk_test_...)
  STRIPE_WEBHOOK_SECRET    — signing secret for the /webhooks/stripe endpoint
                             (from the Stripe Dashboard's webhook config, or
                             `stripe listen` output in development)
  STRIPE_PRICE_BASE        — Price ID for the $29/mo base plan
  STRIPE_PRICE_EXECUTIVE   — Price ID for the $149/mo Executive plan
  CLOUD_PUBLIC_URL          — same var Vapi already needs; reused here for
                             Checkout's success/cancel redirect URLs
"""

import os
import sys

import stripe

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
PRICE_BASE = os.environ.get("STRIPE_PRICE_BASE")
PRICE_EXECUTIVE = os.environ.get("STRIPE_PRICE_EXECUTIVE")
PUBLIC_URL = os.environ.get("CLOUD_PUBLIC_URL", "").rstrip("/")

if not STRIPE_SECRET_KEY:
    print("WARNING: STRIPE_SECRET_KEY not set — billing routes will fail "
          "until this is configured.", file=sys.stderr)
stripe.api_key = STRIPE_SECRET_KEY

# Plan name -> Price ID. Add-on Prices aren't listed here since they're
# looked up dynamically (customers pick add-ons in the dashboard, not at
# initial signup) — see ADDON_PRICE_MAP below, filled in once you've
# created the add-on Prices in Stripe and know their IDs.
PLAN_PRICE_MAP = {
    "cloud_base": PRICE_BASE,
    "cloud_executive": PRICE_EXECUTIVE,
}

# Fill in once add-on Prices exist in Stripe (see setup_stripe_products.py).
# Keyed by the same add-on identifiers used in `customers.unlocked_addons`.
ADDON_PRICE_MAP = {
    # "grace_persona": "price_xxx",
    # "extra_voice_minutes": "price_xxx",
}


def create_checkout_session(customer_id, plan, email, addon_ids=None):
    """
    Creates a Stripe Checkout Session for a new subscription. `plan` is
    'cloud_base' or 'cloud_executive'. `addon_ids` is an optional list of
    keys into ADDON_PRICE_MAP, added as extra line items on the same
    subscription so everything lands on one invoice.

    Returns the Checkout Session's hosted URL — the caller redirects the
    customer there. Nothing is written to the DB here; the customer row
    gets its stripe_subscription_id and status from the webhook once
    Stripe confirms the subscription actually exists (never trust the
    client-side redirect alone — see handle_webhook_event's
    checkout.session.completed branch for why).
    """
    price_id = PLAN_PRICE_MAP.get(plan)
    if not price_id:
        raise ValueError(f"Unknown plan '{plan}' — no Price ID configured for it")

    line_items = [{"price": price_id, "quantity": 1}]
    for addon in (addon_ids or []):
        addon_price = ADDON_PRICE_MAP.get(addon)
        if addon_price:
            line_items.append({"price": addon_price, "quantity": 1})
        else:
            print(f"WARNING: unknown add-on '{addon}' skipped at checkout "
                  f"(no Price ID configured)", file=sys.stderr)

    checkout_session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=line_items,
        customer_email=email,
        client_reference_id=customer_id,  # ties the session back to our customer row
        success_url=f"{PUBLIC_URL}/cloud/signup/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{PUBLIC_URL}/cloud/signup/billing",
        # Lets the webhook (and later, the portal) find/reuse the same
        # Stripe Customer object rather than creating a duplicate each time.
        metadata={"curant_customer_id": customer_id},
        subscription_data={"metadata": {"curant_customer_id": customer_id}},
    )
    return checkout_session.url


def create_portal_session(stripe_customer_id, return_url):
    """
    Creates a Stripe-hosted Customer Portal session — upgrade/downgrade/
    cancel/update payment method, no custom UI needed on our side.
    """
    portal_session = stripe.billing_portal.Session.create(
        customer=stripe_customer_id,
        return_url=return_url,
    )
    return portal_session.url


def verify_webhook(payload, sig_header):
    """
    Verifies a Stripe webhook signature and returns the parsed Event.
    Raises stripe.error.SignatureVerificationError on a bad signature —
    the caller should catch this and return 400, same pattern as the
    existing Telnyx/Vapi webhook signature checks elsewhere in app.py.
    """
    return stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)


def extract_subscription_update(event):
    """
    Pulls the (customer_id, stripe_customer_id, stripe_subscription_id,
    status) tuple out of a Stripe event, for the handful of event types
    that change a customer's subscription state. Returns None for event
    types the caller doesn't need to act on — app.py's webhook route just
    no-ops in that case rather than every event type needing its own
    branch there.

    Handled event types:
      checkout.session.completed   — new subscription just started
      customer.subscription.updated — plan change, renewal, past_due, etc.
      customer.subscription.deleted — cancellation took effect
      invoice.payment_failed        — a renewal charge failed (customer
                                       stays active during Stripe's Smart
                                       Retries; only subscription.deleted
                                       means "actually cut off")
    """
    obj = event["data"]["object"]
    event_type = event["type"]

    if event_type == "checkout.session.completed":
        curant_customer_id = obj.get("client_reference_id") or obj.get("metadata", {}).get("curant_customer_id")
        return {
            "curant_customer_id": curant_customer_id,
            "stripe_customer_id": obj.get("customer"),
            "stripe_subscription_id": obj.get("subscription"),
            "status": "active",
        }

    if event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
        curant_customer_id = obj.get("metadata", {}).get("curant_customer_id")
        status = "canceled" if event_type == "customer.subscription.deleted" else obj.get("status")
        return {
            "curant_customer_id": curant_customer_id,
            "stripe_customer_id": obj.get("customer"),
            "stripe_subscription_id": obj.get("id"),
            "status": status,
        }

    if event_type == "invoice.payment_failed":
        # Informational only — don't deactivate here. Stripe's Smart
        # Retries handles the retry cadence; we only act on the
        # eventual subscription.deleted if retries exhaust.
        return None

    return None
