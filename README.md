# Vera++ — merchant engagement bot (magicpin AI Challenge)

**Approach.** `compose(category, merchant, trigger, customer?)` dispatches on `trigger.kind` into ~25 small composers (research, compliance, perf dip/spike, IPL, festival, competitor, recall, refill, win-back, …). Each one pulls **only facts present in the four contexts** (or simple arithmetic on them), adds one piece of judgement, and ends with a single low-friction CTA:
- *Grounded specificity* — digest items resolved by id (source + numbers), peer benchmarks (`your CTR 2.2% vs 4% for metro unisex salons`), merchant history (`18 orders/day`, "you asked me to focus on aligners").
- *Judgement, not templating* — weekend IPL match → skip the dine-in promo, push delivery (cites the digest); Diwali 188 days out → early-bird planning, not a discount; competitor at ₹199 → defend on quality using the merchant's positive review theme; refill containing a recalled molecule → reassure the batch is clean.
- *Placeholder triggers* (16 of the 30 test pairs carry no payload) anchor on the merchant's real numbers + the category's seasonal beats/catalog instead of inventing a festival, competitor or appointment time.
- *Voice* — dentists get `Dr. {name}` + clinical peer tone; Hindi-belt merchants (or anyone who wrote to us in Hindi) get natural hi-en code-mix; customers get their `language_pref` (hi / hi-en / ta-en greeting…), name, preferred slots and consent honoured. Category taboos and URLs are scrubbed.

**Multi-turn** (`conversation_handlers.respond`) is rule-first and deterministic: auto-replies are caught by canned phrasing *or* verbatim repeats and counted **per merchant** (nudge once → wait 24h → end); "ok let's do it / haan kar do / judna hai" switches straight to action mode and delivers the draft; STOP/not-interested ends and suppresses the merchant; abuse gets one apology with an easy STOP; off-topic (GST, loans) is declined and redirected; no body is ever sent twice in a conversation. An optional LLM (Anthropic or any OpenAI-compatible API, temperature 0, cached) answers free-form questions only, and its output is rejected if it contains a URL or any number not in the grounding facts.

**Tick policy.** Rank available triggers by urgency, dedupe on `suppression_key`, at most one merchant-facing send per merchant per tick with a 10-sim-minute cooldown (urgency ≥ 4 bypasses), skip customers without context or with `reminder_opt_in: false`, skip opted-out / auto-reply-backed-off merchants.

**Tradeoffs.** Templates over free generation: deterministic, < 5 ms per message, zero fabrication risk and no API dependency during the test — at the cost of some phrasing variety. Trigger expiry is not enforced (the judge's `available_triggers` is treated as the source of truth, and the local simulator sends wall-clock `now`).

**What would have helped.** Real slot availability per merchant, which offers a merchant is *willing* to run (catalog ≠ consent), a consistent simulated clock (several payloads disagree on "today" and on weekday labels), and populated payloads for the generated triggers.

## Run
```bash
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8080
python selftest.py http://localhost:8080      # contract + replay scenarios
python make_submission.py                     # -> submission.jsonl (30 test pairs)
```
**Web UI** at `/` (same server): *Overview* (health, context counts, submission URL, drag-and-drop dataset loader, reset), *Playground* (pick any trigger → see the exact message + rationale, then chat with the bot as the merchant/customer in an isolated sandbox), *Live* (the judge's conversations as they happen). Set `UI_ENABLED=0` to turn it off.

Endpoints: `POST /v1/context` · `POST /v1/tick` · `POST /v1/reply` · `GET /v1/healthz` · `GET /v1/metadata` · `POST /v1/teardown`. Config via env (see `.env.example`). Deploy anywhere always-on (Dockerfile / Procfile / render.yaml included) — avoid free tiers that sleep, since 3 failed health checks disqualify.
# MagicPin-
