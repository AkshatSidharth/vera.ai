"""
resolve_context(trigger_id, contexts) → flat bundle dict

The ONLY place that reads from the context store. compose() and reply_handler
never touch contexts directly — they work from bundles.
"""

from __future__ import annotations


def resolve_context(trigger_id: str, contexts: dict) -> dict | None:
    entry = contexts.get(("trigger", trigger_id))
    if not entry:
        return None
    trg = entry["payload"]

    merchant_id = trg.get("merchant_id")
    if not merchant_id:
        return None

    merchant_entry = contexts.get(("merchant", merchant_id))
    if not merchant_entry:
        return None
    merchant = merchant_entry["payload"]

    cat_slug = merchant.get("category_slug", "")
    cat_entry = contexts.get(("category", cat_slug))
    category = cat_entry["payload"] if cat_entry else {}

    customer_id = trg.get("customer_id")
    customer = None
    if customer_id:
        cust_entry = contexts.get(("customer", customer_id))
        customer = cust_entry["payload"] if cust_entry else None

    return _flatten(trigger_id, trg, merchant, category, customer)


def resolve_from_merchant(merchant_id: str, contexts: dict) -> dict | None:
    merchant_entry = contexts.get(("merchant", merchant_id))
    if not merchant_entry:
        return None
    merchant = merchant_entry["payload"]
    cat_slug = merchant.get("category_slug", "")
    cat_entry = contexts.get(("category", cat_slug))
    category = cat_entry["payload"] if cat_entry else {}
    return _flatten("", {}, merchant, category, None)


def _flatten(
    trigger_id: str,
    trg: dict,
    merchant: dict,
    category: dict,
    customer: dict | None,
) -> dict:
    identity = merchant.get("identity", {})
    perf = merchant.get("performance", {})
    peer = category.get("peer_stats", {})
    delta = perf.get("delta_7d", {})

    # Parse days_since_last_post out of signals
    days_since_post = None
    for sig in merchant.get("signals", []):
        if sig.startswith("stale_posts:"):
            try:
                days_since_post = int(sig.split(":")[1].rstrip("d"))
            except Exception:
                pass

    # Last Vera message and engagement from conversation_history
    hist = merchant.get("conversation_history", [])
    last_vera_body = next(
        (h["body"] for h in reversed(hist) if h.get("from") == "vera"), ""
    )
    last_engagement = hist[-1].get("engagement", "") if hist else ""

    # Digest items from category
    digest = category.get("digest", [])

    # Resolve top digest item if trigger references one
    trg_payload = trg.get("payload", {})
    top_item_id = trg_payload.get("top_item_id")
    top_digest_item = None
    if top_item_id:
        top_digest_item = next((d for d in digest if d.get("id") == top_item_id), None)

    return {
        # ── trigger ──────────────────────────────────────────────────────────
        "trigger_id": trigger_id,
        "trigger_kind": trg.get("kind", ""),
        "trigger_scope": trg.get("scope", "merchant"),
        "trigger_urgency": trg.get("urgency", 1),
        "trigger_payload": trg_payload,
        "suppression_key": trg.get("suppression_key", ""),
        "expires_at": trg.get("expires_at", ""),
        # ── merchant ─────────────────────────────────────────────────────────
        "merchant_id": merchant.get("merchant_id", ""),
        "merchant_name": identity.get("name", ""),
        "owner_name": identity.get("owner_first_name", identity.get("name", "").split()[0]),
        "city": identity.get("city", ""),
        "locality": identity.get("locality", ""),
        "languages": identity.get("languages", ["en"]),
        "verified": identity.get("verified", False),
        "subscription_status": merchant.get("subscription", {}).get("status", ""),
        "subscription_days": merchant.get("subscription", {}).get("days_remaining", 0),
        "plan": merchant.get("subscription", {}).get("plan", ""),
        # ── performance ──────────────────────────────────────────────────────
        "views": perf.get("views", 0),
        "calls": perf.get("calls", 0),
        "directions": perf.get("directions", 0),
        "ctr": perf.get("ctr", 0.0),
        "leads": perf.get("leads", 0),
        "views_delta": delta.get("views_pct", 0.0),
        "calls_delta": delta.get("calls_pct", 0.0),
        "peer_ctr": peer.get("avg_ctr", 0.0),
        "peer_avg_views": peer.get("avg_views_30d", 0),
        "peer_avg_calls": peer.get("avg_calls_30d", 0),
        "peer_retention": peer.get("retention_6mo_pct", 0.0),
        # ── offers ───────────────────────────────────────────────────────────
        "active_offers": [o for o in merchant.get("offers", []) if o.get("status") == "active"],
        "all_offers": merchant.get("offers", []),
        "offer_catalog": category.get("offer_catalog", []),
        # ── customer aggregate ───────────────────────────────────────────────
        "customer_agg": merchant.get("customer_aggregate", {}),
        # ── signals & reviews ────────────────────────────────────────────────
        "signals": merchant.get("signals", []),
        "review_themes": merchant.get("review_themes", []),
        "days_since_post": days_since_post,
        "last_engagement": last_engagement,
        "last_vera_body": last_vera_body,
        # ── category ─────────────────────────────────────────────────────────
        "category_slug": category.get("slug", merchant.get("category_slug", "")),
        "voice_tone": category.get("voice", {}).get("tone", ""),
        "voice_register": category.get("voice", {}).get("register", ""),
        "code_mix": category.get("voice", {}).get("code_mix", ""),
        "vocab_allowed": category.get("voice", {}).get("vocab_allowed", []),
        "vocab_taboo": category.get("voice", {}).get("vocab_taboo", []),
        "salutation_examples": category.get("voice", {}).get("salutation_examples", []),
        "peer_stats": peer,
        "digest": digest,
        "top_digest_item": top_digest_item,
        "seasonal_beats": category.get("seasonal_beats", []),
        "trend_signals": category.get("trend_signals", []),
        "patient_content_library": category.get("patient_content_library", []),
        # ── customer (optional) ──────────────────────────────────────────────
        "customer": _flatten_customer(customer) if customer else None,
    }


def _flatten_customer(c: dict) -> dict:
    ident = c.get("identity", {})
    rel = c.get("relationship", {})
    prefs = c.get("preferences", {})
    consent = c.get("consent", {})
    return {
        "customer_id": c.get("customer_id", ""),
        "name": ident.get("name", ""),
        "language_pref": ident.get("language_pref", "en"),
        "state": c.get("state", ""),
        "first_visit": rel.get("first_visit", ""),
        "last_visit": rel.get("last_visit", ""),
        "visits_total": rel.get("visits_total", 0),
        "services_received": rel.get("services_received", []),
        "preferred_slots": prefs.get("preferred_slots", ""),
        "channel": prefs.get("channel", "whatsapp"),
        "consent_scope": consent.get("scope", []),
    }
