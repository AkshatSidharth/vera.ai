# Vera — magicpin Merchant AI Assistant

A stateful HTTP bot that engages and assists merchants on WhatsApp using the 4-context framework (category + merchant + trigger + customer).

## Architecture

```
POST /v1/context   → version-gated context store (in-memory)
POST /v1/tick      → trigger-kind dispatch → Claude Sonnet (temp=0) → post-process
POST /v1/reply     → 4 deterministic patterns → LLM fallback
GET  /v1/healthz   → health check
GET  /v1/metadata  → team info
```

### Core modules

| File | Role |
|---|---|
| `main.py` | FastAPI app, endpoints, in-memory stores |
| `resolver.py` | Context resolution — the only reader of the context store |
| `composer.py` | LLM message composition with URL-strip and anti-repetition |
| `reply_handler.py` | 4 deterministic reply patterns before LLM fallback |
| `prompts/dispatcher.py` | Trigger-kind dispatch map + per-kind system prompt addenda |

### Design decisions

**Trigger-kind dispatch, not one giant prompt.** Each trigger kind gets a focused prompt addendum (20 kinds mapped). The base rules are shared; the per-kind section adds exactly what's needed for that scenario (what to cite, what CTA shape, what to anchor on).

**Resolver is the only context reader.** `compose()` and `handle_reply()` never touch the context store directly — they work from resolved flat bundles. This makes the composition logic testable in isolation.

**4 deterministic reply patterns before LLM.** Auto-reply detection, opt-out, join-intent, and off-topic are handled without burning an LLM token. Only genuine open-ended replies hit the LLM.

**Anti-repetition by token overlap.** The last Vera message body is passed into the prompt as "do NOT repeat this", and post-processing rejects outputs with >85% token overlap.

**URL stripping in post-process.** URLs are stripped from the output body to avoid the -3/URL penalty.

### Compulsion levers used

- Research digest: source citation + trial_n + page number → curiosity + reciprocity CTA
- Perf dip: reframe seasonal vs alarming, cite actual delta → anxiety pre-emption
- IPL match: counter-intuitive Saturday insight (-12% covers) → loss aversion reframe
- Winback: no-shame framing + goal reference + no-commitment trial → barrier removal
- Chronic refill: savings calculation + exact molecules + confirm-to-dispatch → specificity + minimal friction

### What additional context would have helped most

1. **Available appointment slots per merchant** — the biggest gap in customer-facing messages. We use trigger payload slots when provided but most merchant contexts don't have live slot availability.
2. **Conversation session state** (within 24h window vs. new session) — would let us correctly decide whether to use a template or free-form message without guessing.
3. **WhatsApp Business template catalog** — right now we generate template_name heuristically; a real catalog would let us use approved templates and their exact parameter slots.

## Setup

```bash
cp .env.example .env
# Edit .env: set ANTHROPIC_API_KEY
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Local testing with the judge simulator

```bash
# 1. Start your bot
uvicorn main:app --port 8000

# 2. Edit judge_simulator.py — set BOT_URL, LLM_PROVIDER, LLM_API_KEY, LLM_MODEL

# 3. Run
python judge_simulator.py
```

## Deployment (Railway / Render)

- Set `ANTHROPIC_API_KEY` and `PORT` in environment variables
- Use the `Procfile` command: `uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}`
