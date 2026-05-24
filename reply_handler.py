"""
handle_reply(conv_id, message, turn_number, history, merchant, category,
             ended_convs, suppressed_merchants, merchant_id) → action dict

4 deterministic checks BEFORE any LLM call:
  1. Auto-reply detection
  2. Opt-out / stop intent
  3. Intent transition (join / yes let's do it)
  4. Off-topic / beyond scope

Then LLM fallback.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

import anthropic

MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-4-6")
_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    return _client


# ── auto-reply fingerprints ───────────────────────────────────────────────────
# These are the canonical WhatsApp Business canned-reply patterns.
# Vera should NOT burn a full LLM turn responding to these.

_AUTO_REPLY_PHRASES = [
    "thank you for contacting",
    "thanks for contacting",
    "aapki jaankari ke liye bahut",
    "main aapki yeh sabhi baatein",
    "automated assistant",
    "automated message",
    "auto-reply",
    "out of office",
    "ek automated",
    "automated response",
    "this is an automated",
    "madad ke liye shukriya",
    "we have received your message",
    "your message has been received",
    "we will get back to you",
    "hum jald hi aapse sampark karenge",
]

_OPT_OUT_PHRASES = [
    "stop", "unsubscribe", "opt out", "opt-out", "do not contact",
    "band karo", "mat bhejo", "nahi chahiye", "not interested",
    "remove me", "please stop", "stop messaging", "don't message",
    "don't contact", "please don't",
    "do not message", "baas karo", "bas karo", "rok do",
]

_JOIN_INTENT_PHRASES = [
    "i want to join", "mujhe judrna hai", "mujhe join karna hai",
    "i'd like to join", "sign me up",
    "let's do it", "lets do it", "yes let's", "yes lets",
    "yes, let's", "yes go ahead", "please proceed",
    "whats next", "what's next", "what next", "next step",
    "haan chalo", "haan kar do", "ok do it", "ok kar do", "aage badho",
    "confirm karta hoon", "confirm karti hoon", "i confirm",
    "i agree", "agreed", "yes i want to", "haan chahiye",
    "interested", "intrested", "please start", "go ahead",
]

_OFF_TOPIC_PHRASES = [
    "weather", "cricket score", "recipe", "job vacancy", "vacancy",
    "loan", "insurance", "stock market", "share price",
    "news about", "bollywood", "politics", "election",
]


def _normalize(text: str) -> str:
    return text.lower().strip()


def _is_auto_reply(message: str, history: list) -> bool:
    msg = _normalize(message)

    # Direct phrase match
    if any(p in msg for p in _AUTO_REPLY_PHRASES):
        return True

    # Repeated identical message (3+ occurrences = auto-reply)
    merchant_msgs = [t["body"] for t in history if t.get("role") == "merchant"]
    if merchant_msgs.count(message) >= 3:
        return True

    # Long uniform greeting with no question or context (>80 chars, no "?")
    if len(message) > 80 and "?" not in message and len(message.split()) < 20:
        # If it matches formal-letter pattern (no merchant-specific content)
        if not any(word in msg for word in ["views", "calls", "offer", "bata", "help", "karo"]):
            return True

    return False


def _is_opt_out(message: str) -> bool:
    msg = _normalize(message)
    return any(p in msg for p in _OPT_OUT_PHRASES)


def _is_join_intent(message: str) -> bool:
    msg = _normalize(message)
    return any(p in msg for p in _JOIN_INTENT_PHRASES)


def _is_off_topic(message: str) -> bool:
    msg = _normalize(message)
    return any(p in msg for p in _OFF_TOPIC_PHRASES)


# ── suppression helper ────────────────────────────────────────────────────────

def _suppress(merchant_id: str, hours: int, suppressed_merchants: dict) -> None:
    import time
    suppressed_merchants[merchant_id] = time.time() + hours * 3600


# ── main handler ──────────────────────────────────────────────────────────────

async def handle_reply(
    conv_id: str,
    message: str,
    turn_number: int,
    history: list,
    merchant: dict | None,
    category: dict | None,
    ended_convs: set,
    suppressed_merchants: dict,
    merchant_id: str | None,
) -> dict:

    # ── 1. Auto-reply detection ───────────────────────────────────────────────
    if _is_auto_reply(message, history):
        # Attempt once-more probe on first detection, then exit
        vera_turns = [t for t in history if t.get("role") == "vera"]
        if len(vera_turns) >= 2:
            # Already tried to engage after auto-reply — graceful exit
            if merchant_id:
                _suppress(merchant_id, 24, suppressed_merchants)
            ended_convs.add(conv_id)
            return {
                "action": "end",
                "rationale": "Auto-reply detected after re-engagement attempt. Graceful exit.",
            }
        else:
            # First time — try a direct probe
            owner = (merchant or {}).get("identity", {}).get("owner_first_name", "")
            probe = (
                f"{owner + ', ' if owner else ''}"
                "yeh message aaya mujhe — kya aap khud baat karna chahenge? "
                "Sirf ek minute chahiye. Chalega?"
            )
            return {
                "action": "send",
                "body": probe,
                "cta": "yes_stop",
                "send_as": "vera",
                "template_name": "vera_auto_reply_probe_v1",
                "template_params": [owner] if owner else [],
                "rationale": "Auto-reply detected. One probe attempt before exit.",
            }

    # ── 2. Opt-out ────────────────────────────────────────────────────────────
    if _is_opt_out(message):
        if merchant_id:
            _suppress(merchant_id, 24 * 30, suppressed_merchants)  # 30-day cool-off
        ended_convs.add(conv_id)
        owner = (merchant or {}).get("identity", {}).get("owner_first_name", "")
        farewell = (
            f"Samajh gayi{', ' + owner if owner else ''}. "
            "Aapko aur messages nahi bhejungi. "
            "Agar kabhi zaroorat ho, Vera ko yahan message kar sakte hain. 🙂"
        )
        return {
            "action": "end",
            "body": farewell,
            "send_as": "vera",
            "template_name": "vera_opt_out_ack_v1",
            "template_params": [owner] if owner else [],
            "rationale": "Merchant opted out. Suppressing for 30 days.",
        }

    # ── 3. Join / action intent transition ───────────────────────────────────
    if _is_join_intent(message):
        return await _handle_join_intent(
            conv_id, message, history, merchant, category, merchant_id
        )

    # ── 4. Off-topic ─────────────────────────────────────────────────────────
    if _is_off_topic(message) and turn_number <= 2:
        owner = (merchant or {}).get("identity", {}).get("owner_first_name", "")
        redirect = (
            f"{owner + ', ' if owner else ''}"
            "yeh Vera hai — main aapke magicpin business ke baare mein help karta hoon. "
            "Aapke listing, offers, ya customers ke baare mein kuch chahiye?"
        )
        return {
            "action": "send",
            "body": redirect,
            "cta": "open_ended",
            "send_as": "vera",
            "template_name": "vera_off_topic_redirect_v1",
            "template_params": [owner] if owner else [],
            "rationale": "Off-topic message redirected to business scope.",
        }

    # ── 5. LLM fallback ───────────────────────────────────────────────────────
    return await _llm_reply(conv_id, message, turn_number, history, merchant, category, merchant_id)


# ── join intent handler ───────────────────────────────────────────────────────

async def _handle_join_intent(
    conv_id: str,
    message: str,
    history: list,
    merchant: dict | None,
    category: dict | None,
    merchant_id: str | None,
) -> dict:
    owner = (merchant or {}).get("identity", {}).get("owner_first_name", "")
    merchant_name = (merchant or {}).get("identity", {}).get("name", "")
    plan = (merchant or {}).get("subscription", {}).get("plan", "Pro")

    # Build action-oriented response
    body = (
        f"{'Bilkul, ' + owner + '!' if owner else 'Bilkul!'} "
        f"Main {merchant_name or 'aapka account'} ke liye "
        f"magicpin {plan} subscription set up kar rahi hoon. "
        "Please share karein: (1) registered mobile number, "
        "(2) business address pin code. "
        "Sab set hone mein 5 minute lagenge."
    )
    return {
        "action": "send",
        "body": body,
        "cta": "open_ended",
        "send_as": "vera",
        "template_name": "vera_join_onboarding_v1",
        "template_params": [owner or merchant_name, plan],
        "rationale": "Merchant confirmed intent to join. Routing directly to onboarding — no re-qualifying.",
    }


# ── LLM reply ─────────────────────────────────────────────────────────────────

async def _llm_reply(
    conv_id: str,
    message: str,
    turn_number: int,
    history: list,
    merchant: dict | None,
    category: dict | None,
    merchant_id: str | None,
) -> dict:
    system = _reply_system_prompt(merchant, category)
    user_turn = _build_reply_user_turn(message, turn_number, history, merchant, category)

    try:
        client = _get_client()
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=500,
            temperature=0,
            system=system,
            messages=[{"role": "user", "content": user_turn}],
        )
        raw = resp.content[0].text.strip()
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("LLM reply failed: %s", e)
        return {
            "action": "send",
            "body": "Ek second — main check kar rahi hoon aur wapas aaungi. 🙂",
            "cta": "open_ended",
            "send_as": "vera",
            "rationale": "LLM error fallback.",
        }

    return _parse_reply_output(raw)


def _reply_system_prompt(merchant: dict | None, category: dict | None) -> str:
    ident = (merchant or {}).get("identity", {})
    cat_slug = (merchant or {}).get("category_slug", "general")
    voice = (category or {}).get("voice", {})

    return f"""You are Vera — magicpin's merchant-AI assistant on WhatsApp.
You are in an ongoing conversation with the owner of {ident.get('name', 'a merchant')}.
Category: {cat_slug}. Voice tone: {voice.get('tone', 'friendly-professional')}.

RULES:
1. Continue the conversation naturally — DO NOT re-introduce yourself.
2. No URLs in the body.
3. Single CTA per message.
4. If merchant asked a question about their business, answer concretely using context.
5. If you cannot help further, offer to follow up or gracefully close.
6. Max 3 unanswered nudges — after that, offer a graceful exit.
7. Language: match the merchant's language from the conversation.

Respond ONLY with JSON (no markdown):
{{
  "action": "<send|end>",
  "body": "<message text or empty string if action=end>",
  "cta": "<open_ended|yes_stop|none>",
  "send_as": "vera",
  "rationale": "<one sentence>"
}}"""


def _build_reply_user_turn(
    message: str,
    turn_number: int,
    history: list,
    merchant: dict | None,
    category: dict | None,
) -> str:
    parts = []

    # Recent history (last 6 turns)
    recent = history[-6:] if len(history) > 6 else history
    conv_text = []
    for t in recent:
        role_label = "VERA" if t.get("role") == "vera" else "MERCHANT"
        conv_text.append(f"[{role_label}] {t.get('body', '')}")
    if conv_text:
        parts.append("CONVERSATION SO FAR:\n" + "\n".join(conv_text))

    parts.append(f"\nMERCHANT'S LATEST MESSAGE (turn {turn_number}):\n{message}")

    # Merchant context summary
    if merchant:
        ident = merchant.get("identity", {})
        perf = merchant.get("performance", {})
        parts.append(f"\nMERCHANT: {ident.get('name','')} | {ident.get('locality','')}, {ident.get('city','')}")
        parts.append(f"PERFORMANCE: views={perf.get('views',0)}, calls={perf.get('calls',0)}, ctr={perf.get('ctr',0):.3f}")
        active_offers = [o for o in merchant.get("offers", []) if o.get("status") == "active"]
        if active_offers:
            parts.append(f"ACTIVE OFFERS: {'; '.join(o.get('name','') for o in active_offers)}")
        if merchant.get("signals"):
            parts.append(f"SIGNALS: {', '.join(merchant.get('signals', []))}")

    parts.append("\nRespond with the JSON object.")
    return "\n".join(parts)


def _parse_reply_output(raw: str) -> dict:
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE)

    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return {
            "action": "send",
            "body": "Theek hai! Koi bhi sawaal ho toh Vera se puchh sakte hain. 🙂",
            "cta": "open_ended",
            "send_as": "vera",
            "rationale": "Fallback response due to parse error.",
        }

    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return {
            "action": "send",
            "body": "Theek hai! Koi bhi sawaal ho toh Vera se puchh sakte hain. 🙂",
            "cta": "open_ended",
            "send_as": "vera",
            "rationale": "Fallback response due to JSON error.",
        }

    # Strip URLs from body
    body = data.get("body", "")
    body = re.sub(r"https?://\S+|www\.\S+", "", body).strip()
    body = re.sub(r"  +", " ", body)
    data["body"] = body

    action = data.get("action", "send")
    if action not in ("send", "end"):
        action = "send"

    return {
        "action": action,
        "body": data.get("body", ""),
        "cta": data.get("cta", "open_ended"),
        "send_as": data.get("send_as", "vera"),
        "rationale": data.get("rationale", ""),
    }
