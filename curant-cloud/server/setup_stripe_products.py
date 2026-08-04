"""
One-time setup script — creates the Curant Cloud Products and Prices in
Stripe. Run this ONCE per Stripe account (once for test mode, once for
live mode when you're ready to go live).

Usage:
    STRIPE_SECRET_KEY=sk_test_... python3 setup_stripe_products.py

Prints the resulting Price IDs — copy these into your .env as
STRIPE_PRICE_BASE and STRIPE_PRICE_EXECUTIVE. Re-running this script
creates DUPLICATE Products/Prices in Stripe (there's no built-in
idempotency here beyond Stripe's optional idempotency-key mechanism),
so don't run it more than once per environment — if you need to change
a price, create a new Price under the same Product instead of re-running
this script (Stripe Prices are immutable once created; that's normal).
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

    print("\n" + "=" * 60)
    print("Add these to your .env:")
    print(f"STRIPE_PRICE_BASE={base_price.id}")
    print(f"STRIPE_PRICE_EXECUTIVE={exec_price.id}")
    print("=" * 60)
    print(
        "\nAdd-on prices aren't created here — add them the same way "
        "(create_plan with the add-on's own name/price), then add each "
        "resulting Price ID to billing.py's ADDON_PRICE_MAP by hand."
    )
