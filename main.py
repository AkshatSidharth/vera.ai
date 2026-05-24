import os
import time
import asyncio
from datetime import datetime, timezone
from typing import Any
from fastapi import FastAPI, Response
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Vera", version="1.0.0")
START_TIME = time.time()

# ── stores ──────────────────────────────────────────────────────────────────
# (scope, context_id) -> {version, payload}
contexts: dict[tuple[str, str], dict] = {}
# conversation_id -> list of turn dicts {role, body, turn_number, ts}
conversations: dict[str, list] = {}
# conversation_ids that have been ended — no further sends
ended_convs: set[str] = set()
# merchant_id -> unix timestamp until which they are suppressed
suppressed_merchants: dict[str, float] = {}


# ── request models ───────────────────────────────────────────────────────────
class ContextBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str


class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: str | None = None
    customer_id: str | None = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


# ── endpoints ────────────────────────────────────────────────────────────────
@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _) in contexts:
        if scope in counts:
            counts[scope] += 1
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TIME),
        "contexts_loaded": counts,
    }


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Vera",
        "team_members": ["Akshat Sidharth"],
        "model": os.environ.get("LLM_MODEL", "claude-sonnet-4-6"),
        "approach": (
            "trigger-kind dispatch → per-category system prompt → "
            "Claude Sonnet (temp=0) → post-process (URL strip, dedup, field validation). "
            "Reply handler: 4 deterministic patterns (auto-reply, opt-out, intent-transition, "
            "off-topic) before LLM fallback."
        ),
        "contact_email": "akshat.sidharth@kapturecrm.com",
        "version": "1.0.0",
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/v1/context")
async def push_context(body: ContextBody, response: Response):
    valid_scopes = {"category", "merchant", "customer", "trigger"}
    if body.scope not in valid_scopes:
        response.status_code = 400
        return {
            "accepted": False,
            "reason": "invalid_scope",
            "details": f"scope must be one of {sorted(valid_scopes)}",
        }

    key = (body.scope, body.context_id)
    cur = contexts.get(key)
    if cur and cur["version"] >= body.version:
        response.status_code = 409
        return {
            "accepted": False,
            "reason": "stale_version",
            "current_version": cur["version"],
        }

    contexts[key] = {"version": body.version, "payload": body.payload}
    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": datetime.now(timezone.utc).isoformat() + "Z",
    }


@app.post("/v1/tick")
async def tick(body: TickBody):
    from composer import compose_for_trigger
    from resolver import resolve_context

    actions = []
    seen_merchants: set[str] = set()

    # Sort by urgency descending so highest-urgency triggers go first
    ranked: list[tuple[str, int, dict]] = []
    for tid in body.available_triggers:
        entry = contexts.get(("trigger", tid))
        if entry:
            trg = entry["payload"]
            ranked.append((tid, trg.get("urgency", 1), trg))
    ranked.sort(key=lambda x: x[1], reverse=True)

    for tid, _urgency, trg in ranked:
        merchant_id = trg.get("merchant_id")
        if not merchant_id:
            continue

        # Skip suppressed merchants
        suppressed_until = suppressed_merchants.get(merchant_id, 0)
        if time.time() < suppressed_until:
            continue

        # One action per merchant per tick
        if merchant_id in seen_merchants:
            continue

        conv_id = f"conv_{merchant_id}_{tid}"

        # Skip ended conversations
        if conv_id in ended_convs:
            continue

        # Skip if suppression_key already fired in this conversation
        s_key = trg.get("suppression_key", "")
        if s_key and any(
            t.get("suppression_key") == s_key
            for t in conversations.get(conv_id, [])
        ):
            continue

        bundle = resolve_context(tid, contexts)
        if not bundle:
            continue

        action = await compose_for_trigger(bundle, conv_id)
        if not action:
            continue

        actions.append(action)
        seen_merchants.add(merchant_id)

        # Record in conversation store
        conversations.setdefault(conv_id, []).append({
            "role": "vera",
            "body": action["body"],
            "suppression_key": s_key,
            "turn_number": 1,
            "ts": body.now,
        })

    return {"actions": actions}


@app.post("/v1/reply")
async def reply(body: ReplyBody, response: Response):
    from reply_handler import handle_reply

    conv_id = body.conversation_id

    # Ended conversations silently ignore further input
    if conv_id in ended_convs:
        return {"action": "end", "rationale": "Conversation was previously ended."}

    # Store the inbound message
    conversations.setdefault(conv_id, []).append({
        "role": "merchant",
        "body": body.message,
        "turn_number": body.turn_number,
        "ts": body.received_at,
    })

    merchant_id = body.merchant_id
    merchant_payload = None
    category_payload = None

    if merchant_id:
        merchant_entry = contexts.get(("merchant", merchant_id))
        if merchant_entry:
            merchant_payload = merchant_entry["payload"]
            cat_slug = merchant_payload.get("category_slug", "")
            cat_entry = contexts.get(("category", cat_slug))
            if cat_entry:
                category_payload = cat_entry["payload"]

    history = conversations[conv_id]

    result = await handle_reply(
        conv_id=conv_id,
        message=body.message,
        turn_number=body.turn_number,
        history=history,
        merchant=merchant_payload,
        category=category_payload,
        ended_convs=ended_convs,
        suppressed_merchants=suppressed_merchants,
        merchant_id=merchant_id,
    )

    # Record Vera's response; mark ended conversations
    if result.get("action") == "send":
        conversations[conv_id].append({
            "role": "vera",
            "body": result.get("body", ""),
            "turn_number": body.turn_number + 1,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
    elif result.get("action") == "end":
        ended_convs.add(conv_id)

    return result
