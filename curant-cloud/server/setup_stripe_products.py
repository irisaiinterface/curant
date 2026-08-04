"""
One-time setup script — creates the Curant Cloud Products and Prices in
Stripe. Run this ONCE per Stripe account (once for test mode, once for
live mode when you're ready to go live).

Usage:
    STRIPE_SECRET_KEY=sk_test_... python3 setup_stripe_products.py

Prints the resulting Price IDs — copy these into your .env as
STRIPE_PRICE_BASE, STRIPE_PRICE_EXECUTIVE, STRIPE_PRICE_BROWSER_AUTOMATION,
and STRIPE_PRICE_AUGUST. Re-running this script creates DUPLICATE
Products/Prices in Stripe (there's no built-in idempotency here beyond
Stripe's optional idempotency-key mechanism), so don't run it more than
once per environment — if you need to change a price, create a new
Price under the same Product instead of re-running this script (Stripe
Prices are immutable once created; that's normal).

Add-on prices below ($10/mo browser automation, $15/mo August) are
placeholders, not a business decision made on your behalf — picked to
sit in the $5-25/mo range already used elsewhere for add-on pricing
(see Curant_Summary.docx), with August priced higher given it unlocks
a broader set of generation tools. Adjust the unit_amount_cents values
below before running this for real.

Only these two add-ons are created here because they're the only two
actually gated in the current codebase (get_browser_automation_tools,
get_august_tools) — every other add-on name that's floated in earlier
reference docs (email_send, multilang, etc.) was never wired up as a
real feature gate, so there's nothing for a Stripe Price to unlock yet.
"""

import os
import sys

import stripe

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
if not stripe.api_key:
    print("Set STRIPE_SECRET_KEY before running this script.", file=sys.stderr)
    sys.exit(1)


def create_plan(name, description, unit_amount_cents):
    product = stripe.Product.create(name=name, description=description)
    price = stripe.Price.create(
        product=product.id,
        unit_amount=unit_amount_cents,
        currency="usd",
        recurring={"interval": "month"},
    )
    return product, price


if __name__ == "__main__":
    print("Creating Curant Cloud base plan...")
    base_product, base_price = create_plan(
        "Curant Cloud — Base",
        "AI secretary reachable by SMS and voice call. One persona, core skillset.",
        2900,  # $29.00
    )
    print(f"  Product: {base_product.id}")
    print(f"  Price:   {base_price.id}")

    print("\nCreating Curant Cloud Executive plan...")
    exec_product, exec_price = create_plan(
        "Curant Cloud — Executive",
        "All-inclusive tier: every persona, every add-on, unlimited job bundles.",
        14900,  # $149.00
    )
    print(f"  Product: {exec_product.id}")
    print(f"  Price:   {exec_price.id}")

    print("\nCreating Browser Automation add-on...")
    browser_product, browser_price = create_plan(
        "Curant Cloud — Browser Automation",
        "Fill in and submit real web forms on your behalf, with explicit "
        "confirmation required and payment/sensitive-ID fields hard-blocked.",
        1000,  # $10.00 — placeholder, see module docstring
    )
    print(f"  Product: {browser_product.id}")
    print(f"  Price:   {browser_price.id}")

    print("\nCreating August (creative generation) add-on...")
    august_product, august_price = create_plan(
        "Curant Cloud — August",
        "Image, voice, and video generation (FLUX, Ideogram, ElevenLabs, Veo), "
        "delivered by email. Requires your own API key per service and a "
        "provisioned Workspace utility email.",
        1500,  # $15.00 — placeholder, see module docstring
    )
    print(f"  Product: {august_product.id}")
    print(f"  Price:   {august_price.id}")

    print("\n" + "=" * 60)
    print("Add these to your .env:")
    print(f"STRIPE_PRICE_BASE={base_price.id}")
    print(f"STRIPE_PRICE_EXECUTIVE={exec_price.id}")
    print(f"STRIPE_PRICE_BROWSER_AUTOMATION={browser_price.id}")
    print(f"STRIPE_PRICE_AUGUST={august_price.id}")
    print("=" * 60)
    print(
        "\nThese four cover every add-on actually gated in the current "
        "codebase. If a new gated add-on is added later, follow the same "
        "pattern: create_plan(...) here, then add its Price ID to "
        "billing.py's ADDON_PRICE_MAP by hand."
    )
