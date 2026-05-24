"""
compose_for_trigger(bundle, conv_id) → action dict or None

Calls the LLM with a trigger-kind-specific system prompt.
Post-processes: URL strip, dedup, required-field validation.
"""

from __future__ import annotations

import json
import os
import re

import anthropic

MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-4-6")
_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    return _client


# ── main entry point ─────────────────────────────────────────────────────────

async def compose_for_trigger(bundle: dict, conv_id: str) -> dict | None:
    from prompts.dispatcher import get_system_prompt

    system_prompt = get_system_prompt(bundle)
    user_turn = _build_user_turn(bundle)

    try:
        client = _get_client()
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=700,
            temperature=0,
            system=system_prompt,
            messages=[{"role": "user", "content": user_turn}],
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("LLM call failed: %s", e)
        return None

    raw = resp.content[0].text.strip()
    action = _parse_output(raw, bundle, conv_id)
    if action is None:
        return None
    return _postprocess(action, bundle, conv_id)


# ── user turn builder ────────────────────────────────────────────────────────

def _build_user_turn(bundle: dict) -> str:
    parts: list[str] = []

    # Trigger
    parts.append(f"TRIGGER KIND: {bundle['trigger_kind']}")
    parts.append(f"TRIGGER URGENCY: {bundle['trigger_urgency']}")
    if bundle.get("trigger_payload"):
        parts.append(f"TRIGGER PAYLOAD:\n{json.dumps(bundle['trigger_payload'], indent=2)}")

    # Merchant identity
    parts.append(f"\nMERCHANT: {bundle['merchant_name']} ({bundle['merchant_id']})")
    parts.append(f"OWNER: {bundle['owner_name']}")
    parts.append(f"LOCATION: {bundle['locality']}, {bundle['city']}")
    parts.append(f"CATEGORY: {bundle['category_slug']}")
    parts.append(f"LANGUAGES: {', '.join(bundle.get('languages', ['en']))}")
    parts.append(f"SUBSCRIPTION: {bundle['subscription_status']} ({bundle['plan']}, {bundle['subscription_days']} days remaining)")

    # Performance
    parts.append(f"\nPERFORMANCE (30d): views={bundle['views']}, calls={bundle['calls']}, "
                 f"ctr={bundle['ctr']:.3f}, leads={bundle['leads']}, directions={bundle['directions']}")
    parts.append(f"DELTAS (7d): views={bundle['views_delta']:+.1f}%, calls={bundle['calls_delta']:+.1f}%")
    parts.append(f"PEER BENCHMARKS: avg_ctr={bundle['peer_ctr']:.3f}, avg_views={bundle['peer_avg_views']}, "
                 f"avg_calls={bundle['peer_avg_calls']}, retention_6mo={bundle['peer_retention']:.1f}%")

    if bundle.get("days_since_post") is not None:
        parts.append(f"DAYS SINCE LAST POST: {bundle['days_since_post']}")

    # Signals
    if bundle.get("signals"):
        parts.append(f"SIGNALS: {', '.join(bundle['signals'])}")

    # Review themes
    if bundle.get("review_themes"):
        themes = bundle["review_themes"]
        if isinstance(themes, list) and themes:
            parts.append(f"REVIEW THEMES: {json.dumps(themes)}")

    # Active offers
    if bundle.get("active_offers"):
        offers_text = "; ".join(
            o.get("title") or o.get("name") or str(o) for o in bundle["active_offers"]
        )
        parts.append(f"ACTIVE OFFERS: {offers_text}")

    # Customer aggregate
    if bundle.get("customer_agg"):
        agg = bundle["customer_agg"]
        parts.append(f"CUSTOMER AGGREGATE: {json.dumps(agg)}")

    # Category voice
    parts.append(f"\nVOICE: tone={bundle.get('voice_tone','')}, register={bundle.get('voice_register','')}, "
                 f"code_mix={bundle.get('code_mix','')}")
    if bundle.get("vocab_allowed"):
        parts.append(f"VOCAB ALLOWED: {', '.join(bundle['vocab_allowed'][:10])}")
    if bundle.get("vocab_taboo"):
        parts.append(f"VOCAB TABOO: {', '.join(bundle['vocab_taboo'][:10])}")

    # Digest (top 2 items)
    digest = bundle.get("digest", [])
    if digest:
        top = digest[:2]
        parts.append(f"\nCATEGORY DIGEST (top items):\n{json.dumps(top, indent=2)}")

    # Top digest item from trigger
    if bundle.get("top_digest_item"):
        parts.append(f"\nTRIGGER TOP DIGEST ITEM:\n{json.dumps(bundle['top_digest_item'], indent=2)}")

    # Seasonal beats & trends
    if bundle.get("seasonal_beats"):
        parts.append(f"SEASONAL BEATS: {json.dumps(bundle['seasonal_beats'][:3])}")
    if bundle.get("trend_signals"):
        parts.append(f"TREND SIGNALS: {json.dumps(bundle['trend_signals'][:2])}")

    # Last Vera message (anti-repetition)
    if bundle.get("last_vera_body"):
        parts.append(f"\nLAST VERA MESSAGE (do NOT repeat this):\n{bundle['last_vera_body']}")

    # Last engagement
    if bundle.get("last_engagement"):
        parts.append(f"LAST MERCHANT ENGAGEMENT: {bundle['last_engagement']}")

    # Customer context (for customer-facing)
    customer = bundle.get("customer")
    if customer:
        parts.append(f"\nCUSTOMER: {customer.get('name', '')} ({customer.get('customer_id', '')})")
        parts.append(f"  state: {customer.get('state', '')}")
        parts.append(f"  last_visit: {customer.get('last_visit', '')}, visits_total: {customer.get('visits_total', 0)}")
        parts.append(f"  services_received: {', '.join(customer.get('services_received', []))}")
        parts.append(f"  language_pref: {customer.get('language_pref', 'en')}")
        parts.append(f"  preferred_slots: {customer.get('preferred_slots', '')}")
        parts.append(f"  channel: {customer.get('channel', 'whatsapp')}")
        parts.append(f"  consent_scope: {', '.join(customer.get('consent_scope', []))}")

    # Output format instruction
    parts.append("""
Respond ONLY with a JSON object (no markdown fences):
{
  "body": "<WhatsApp message text>",
  "cta": "<open_ended|yes_stop|none>",
  "send_as": "<vera|merchant_on_behalf>",
  "template_name": "<snake_case_template_id>",
  "template_params": ["param1", "param2"],
  "rationale": "<one sentence: why this message, what it achieves>"
}""")

    return "\n".join(parts)


# ── output parser ─────────────────────────────────────────────────────────────

def _parse_output(raw: str, bundle: dict, conv_id: str) -> dict | None:
    # Strip markdown fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE)

    # Extract the first JSON object
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None

    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return None

    # Require body
    if not data.get("body"):
        return None

    return {
        "conversation_id": conv_id,
        "merchant_id": bundle.get("merchant_id", ""),
        "customer_id": bundle.get("customer", {}).get("customer_id") if bundle.get("customer") else None,
        "send_as": data.get("send_as", "vera"),
        "trigger_id": bundle.get("trigger_id", ""),
        "template_name": data.get("template_name", _default_template_name(bundle)),
        "template_params": data.get("template_params", _auto_template_params(data.get("body", ""), bundle)),
        "body": data.get("body", ""),
        "cta": data.get("cta", "open_ended"),
        "suppression_key": bundle.get("suppression_key", ""),
        "rationale": data.get("rationale", ""),
    }


# ── post-processor ────────────────────────────────────────────────────────────

_URL_PATTERN = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)


def _postprocess(action: dict, bundle: dict, conv_id: str) -> dict | None:
    body = action.get("body", "")

    # Strip URLs (spec: -3 per URL — we strip them entirely to avoid penalty)
    body = _URL_PATTERN.sub("", body).strip()
    # Clean up any double-spaces left by URL removal
    body = re.sub(r"  +", " ", body)
    action["body"] = body

    # Validate required fields
    required = {"conversation_id", "merchant_id", "send_as", "trigger_id", "body"}
    for field in required:
        if not action.get(field):
            return None

    # Anti-repetition: bail if body is essentially the same as last Vera message
    last = bundle.get("last_vera_body", "")
    if last and _similarity(body, last) > 0.85:
        return None

    # Ensure template_params is a list
    if not isinstance(action.get("template_params"), list):
        action["template_params"] = _auto_template_params(body, bundle)

    return action


# ── helpers ───────────────────────────────────────────────────────────────────

def _default_template_name(bundle: dict) -> str:
    kind = bundle.get("trigger_kind", "generic").replace("_", "")
    cat = bundle.get("category_slug", "general").replace("_", "")
    return f"vera_{cat}_{kind}_v1"


def _auto_template_params(body: str, bundle: dict) -> list[str]:
    owner = bundle.get("owner_name", "")
    merchant_name = bundle.get("merchant_name", "")
    params = []
    if owner:
        params.append(owner)
    if merchant_name and merchant_name != owner:
        params.append(merchant_name)
    # Add first sentence of body as preview param
    first_sentence = body.split(".")[0].strip()
    if first_sentence and first_sentence not in params:
        params.append(first_sentence[:60])
    return params


def _similarity(a: str, b: str) -> float:
    """Rough token-overlap similarity, good enough for dedup."""
    if not a or not b:
        return 0.0
    ta = set(a.lower().split())
    tb = set(b.lower().split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))
