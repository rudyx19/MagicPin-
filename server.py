"""HTTP surface for the magicpin judge harness.

  POST /v1/context   versioned context push (409 on stale/same version)
  POST /v1/tick      proactive sends (rank by urgency, dedupe, per-merchant cooldown)
  POST /v1/reply     multi-turn replies (send / wait / end)
  GET  /v1/healthz   liveness + context counts
  GET  /v1/metadata  bot identity
  POST /v1/teardown  wipe all state
  POST /v1/compose   stateless: send category/merchant/trigger/customer JSON, get the message back (Postman-friendly)
  POST /v1/converse  stateless: same inputs + merchant messages, get the bot's replies back
  GET  /             web UI (API tester, playground, live conversations, status) — UI_ENABLED=0 to disable
  GET  /postman.json Postman collection for this server

Run:  uvicorn server:app --host 0.0.0.0 --port ${PORT:-8080}
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

import examples
import llm
from bot import COMPOSER_VERSION, compose_full
from conversation_handlers import respond
from store import SCOPES, Store
from util import merchant_lang, parse_dt

store = Store(state_file=os.getenv("STATE_FILE") or None)
app = FastAPI(title="Vera bot", version=os.getenv("BOT_VERSION", "1.0.0"))
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

MAX_ACTIONS_PER_TICK = 20
MERCHANT_COOLDOWN_MIN = int(os.getenv("MERCHANT_COOLDOWN_MIN", "10"))   # sim-minutes between proactive vera sends
TICK_BUDGET_S = float(os.getenv("TICK_BUDGET_S", "8"))
UI_ENABLED = os.getenv("UI_ENABLED", "1").strip() not in ("0", "false", "no")
UI_FILE = Path(__file__).with_name("ui.html")

_conv_locks: dict[str, threading.Lock] = {}
_conv_locks_guard = threading.Lock()


def _conv_lock(cid: str) -> threading.Lock:
    with _conv_locks_guard:
        return _conv_locks.setdefault(cid, threading.Lock())


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def _json(request: Request) -> Optional[dict]:
    try:
        data = json.loads((await request.body()).decode("utf-8") or "{}")
        return data if isinstance(data, dict) else None
    except Exception:
        return None


# =============================================================================
# health + metadata
# =============================================================================

@app.get("/")
async def root():
    if UI_ENABLED and UI_FILE.exists():
        return FileResponse(UI_FILE, media_type="text/html")
    return await api_info()


@app.get("/api")
async def api_info():
    return {"service": "vera-bot",
            "endpoints": ["/v1/context", "/v1/tick", "/v1/reply", "/v1/healthz", "/v1/metadata"],
            "try_it": {"POST /v1/compose": "send {category, merchant, trigger, customer?} -> composed message",
                       "POST /v1/converse": "same + {messages: [...]} -> bot replies",
                       "GET /postman.json": "Postman collection with ready-made examples"}}


@app.get("/v1/healthz")
async def healthz():
    return {"status": "ok", "uptime_seconds": int(time.time() - store.started), "contexts_loaded": store.counts()}


@app.get("/v1/metadata")
async def metadata():
    members = [m.strip() for m in os.getenv("TEAM_MEMBERS", "").split(",") if m.strip()]
    return {
        "team_name": os.getenv("TEAM_NAME", "Vera++"),
        "team_members": members,
        "model": llm.model_name(),
        "approach": ("Deterministic 4-context composer: dispatch on trigger.kind into ~25 grounded composers "
                     "(facts only from category/merchant/trigger/customer; placeholder payloads anchor on merchant "
                     "numbers + category pack), peer-benchmark + seasonal judgement, hi-en code-mix by city/history. "
                     "Rule-first multi-turn handler (auto-reply streaks per merchant, intent->action, opt-out, "
                     "off-topic redirect) with optional grounded LLM for free-form questions."),
        "contact_email": os.getenv("CONTACT_EMAIL", ""),
        "version": os.getenv("BOT_VERSION", "1.0.0"),
        "composer_version": COMPOSER_VERSION,
        "submitted_at": os.getenv("SUBMITTED_AT", "2026-10-01T00:00:00Z"),
    }


# =============================================================================
# context
# =============================================================================

@app.post("/v1/context")
async def push_context(request: Request):
    body = await _json(request)
    if body is None:
        return JSONResponse({"accepted": False, "reason": "malformed", "details": "body must be a JSON object"}, 400)
    scope, cid, version, payload = body.get("scope"), body.get("context_id"), body.get("version"), body.get("payload")
    if scope not in SCOPES:
        return JSONResponse({"accepted": False, "reason": "invalid_scope", "details": f"scope must be one of {SCOPES}"}, 400)
    if not cid or not isinstance(cid, str):
        return JSONResponse({"accepted": False, "reason": "invalid_context_id", "details": "context_id required"}, 400)
    try:
        version = int(version)
    except (TypeError, ValueError):
        return JSONResponse({"accepted": False, "reason": "invalid_version", "details": "version must be an integer"}, 400)
    if not isinstance(payload, dict):
        return JSONResponse({"accepted": False, "reason": "invalid_payload", "details": "payload must be an object"}, 400)
    code, err = store.put_context(scope, cid, version, payload, body.get("delivered_at"))
    if code != 200:
        return JSONResponse(err, code)
    return {"accepted": True, "ack_id": f"ack_{cid}_v{version}", "stored_at": _utcnow()}


# =============================================================================
# tick
# =============================================================================

def _short(mid: str) -> str:
    parts = (mid or "m").split("_")
    return "_".join(parts[:3])[:28]


def _conv_id(mid: str, kind: str, cust: Optional[str]) -> str:
    base = f"conv_{_short(mid)}_{kind}" + (f"_{cust.split('_')[1]}" if cust and "_" in cust else "")
    cid, n = base, 1
    while store.conv(cid):
        n += 1
        cid = f"{base}_{n}"
    return cid


def _tick_work(body: dict) -> dict:
    t0 = time.time()
    now_s = body.get("now") or _utcnow()
    now = parse_dt(now_s) or datetime.now(timezone.utc)
    seen, cands = set(), []
    for tid in body.get("available_triggers") or []:
        if not isinstance(tid, str) or tid in seen:
            continue
        seen.add(tid)
        trg = store.get("trigger", tid)
        if not trg:
            continue
        mid = trg.get("merchant_id") or (trg.get("payload") or {}).get("merchant_id")
        merchant = store.get("merchant", mid)
        category = store.category_for(merchant, trg)
        if not merchant or not category:
            continue
        cust_id = trg.get("customer_id")
        customer = store.get("customer", cust_id) if cust_id else None
        is_customer = trg.get("scope") == "customer" or bool(cust_id)
        if is_customer and not customer:
            continue  # never message a customer we have no context for
        if customer and (customer.get("preferences") or {}).get("reminder_opt_in") is False:
            continue
        supp = trg.get("suppression_key") or tid
        if supp in store.sent_suppression:
            continue
        ms = store.merchant_state(mid)
        if ms.get("opted_out"):
            continue
        if not is_customer:
            bu = parse_dt(ms.get("backoff_until"))
            if bu and now < bu:
                continue
            last = parse_dt(ms.get("last_vera_send"))
            if last and now - last < timedelta(minutes=MERCHANT_COOLDOWN_MIN) and int(trg.get("urgency") or 0) < 4:
                continue
        cands.append((int(trg.get("urgency") or 0), len(cands), tid, trg, merchant, category, customer, is_customer))

    cands.sort(key=lambda x: (-x[0], x[1]))
    actions, used_merchants, used_customers = [], set(), set()
    for urg, _, tid, trg, merchant, category, customer, is_customer in cands:
        if len(actions) >= MAX_ACTIONS_PER_TICK or time.time() - t0 > TICK_BUDGET_S:
            break
        mid = merchant.get("merchant_id") or trg.get("merchant_id")
        key = (customer or {}).get("customer_id") if is_customer else mid
        if (is_customer and key in used_customers) or (not is_customer and key in used_merchants):
            continue
        try:
            prior = bool(store.merchant_state(mid).get("conv_ids"))
            out = compose_full(category, merchant, trg, customer, now=now_s, prior_contact=prior)
        except Exception:
            continue
        if not out.get("body"):
            continue
        cust_id = (customer or {}).get("customer_id") if customer else None
        conv_id = _conv_id(mid, str(trg.get("kind") or "msg"), cust_id)
        action = {
            "conversation_id": conv_id, "merchant_id": mid, "customer_id": cust_id,
            "send_as": out["send_as"], "trigger_id": tid,
            "template_name": out["template_name"], "template_params": out["template_params"],
            "body": out["body"], "cta": out["cta"], "suppression_key": out["suppression_key"],
            "rationale": out["rationale"],
        }
        actions.append(action)
        with store.lock:
            store.new_conv(conv_id, merchant_id=mid, customer_id=cust_id, trigger_id=tid, kind=trg.get("kind"),
                           family=out["family"], followup=out["followup"], send_as=out["send_as"], lang=out["lang"],
                           stage="pitched")
            conv = store.conv(conv_id)
            conv["turns"].append({"from": "bot", "body": out["body"]})
            conv["bot_bodies"].append(out["body"])
            store.sent_suppression.add(out["suppression_key"])
            if not is_customer:
                store.merchant_state(mid)["last_vera_send"] = now.isoformat()
            store.touch()
        (used_customers if is_customer else used_merchants).add(key)
    return {"actions": actions}


@app.post("/v1/tick")
async def tick(request: Request):
    body = await _json(request) or {}
    try:
        return await run_in_threadpool(_tick_work, body)
    except Exception:
        return {"actions": []}


# =============================================================================
# reply
# =============================================================================

def _reply_work(body: dict) -> dict:
    conv_id = body.get("conversation_id") or f"conv_adhoc_{body.get('merchant_id') or 'unknown'}"
    role = body.get("from_role") or ("customer" if body.get("customer_id") else "merchant")
    msg = body.get("message") if isinstance(body.get("message"), str) else ""
    with _conv_lock(conv_id):
        conv = store.conv(conv_id)
        if conv is None:
            mid = body.get("merchant_id")
            merchant = store.get("merchant", mid)
            conv = store.new_conv(conv_id, merchant_id=mid, customer_id=body.get("customer_id"), trigger_id=None,
                                  kind=None, family=None, followup=None,
                                  send_as="merchant_on_behalf" if role == "customer" else "vera",
                                  lang=merchant_lang(merchant) if merchant else "en", stage="pitched")
        mid = conv.get("merchant_id") or body.get("merchant_id")
        merchant = store.get("merchant", mid)
        category = store.category_for(merchant)
        customer = store.get("customer", conv.get("customer_id") or body.get("customer_id"))
        mstate = store.merchant_state(mid)
        action = respond(conv, msg, merchant, category, customer, mstate, role)
        if action["action"] == "end" and mstate.get("backoff") == "auto_reply":
            at = parse_dt(body.get("received_at")) or datetime.now(timezone.utc)
            mstate["backoff_until"] = (at + timedelta(hours=24)).isoformat()
            mstate["backoff"] = None
        store.touch()
    if action["action"] == "send" and not action.get("body"):
        action = {"action": "wait", "wait_seconds": 3600, "rationale": "No safe body composed; waiting instead."}
    return action


@app.post("/v1/reply")
async def reply(request: Request):
    body = await _json(request)
    if body is None:
        return {"action": "wait", "wait_seconds": 1800, "rationale": "Malformed reply payload; waiting."}
    try:
        return await run_in_threadpool(_reply_work, body)
    except Exception:
        return {"action": "wait", "wait_seconds": 1800, "rationale": "Internal error handling this reply; backing off."}


@app.post("/v1/teardown")
async def teardown():
    store.wipe()
    return {"status": "wiped"}


# =============================================================================
# web UI helpers (read-only views + an isolated sandbox; never touch judge state)
# =============================================================================

_sandbox: dict[str, dict] = {}
_sandbox_lock = threading.Lock()


def _ui_guard():
    return None if UI_ENABLED else JSONResponse({"detail": "UI disabled"}, 404)


@app.get("/ui/api/contexts")
async def ui_contexts():
    if (g := _ui_guard()):
        return g
    with store.lock:
        items = list(store.contexts.items())
        sent = set(store.sent_suppression)
    merchants, customers, triggers, cats = {}, {}, [], []
    for (scope, cid), v in items:
        p = v.get("payload") or {}
        if scope == "category":
            cats.append(cid)
        elif scope == "merchant":
            ident = p.get("identity") or {}
            merchants[cid] = {"name": ident.get("name"), "category": p.get("category_slug"), "city": ident.get("city"),
                              "locality": ident.get("locality"), "owner": ident.get("owner_first_name"),
                              "version": v.get("version")}
        elif scope == "customer":
            customers[cid] = {"name": (p.get("identity") or {}).get("name"), "merchant_id": p.get("merchant_id"),
                              "state": p.get("state")}
        elif scope == "trigger":
            triggers.append({"id": cid, "kind": p.get("kind"), "scope": p.get("scope"), "merchant_id": p.get("merchant_id"),
                             "customer_id": p.get("customer_id"), "urgency": p.get("urgency"),
                             "placeholder": bool((p.get("payload") or {}).get("placeholder")),
                             "sent": (p.get("suppression_key") or cid) in sent})
    triggers.sort(key=lambda t: (-(t["urgency"] or 0), t["id"]))
    return {"counts": store.counts(), "categories": sorted(cats), "merchants": merchants, "customers": customers,
            "triggers": triggers}


def _resolve(trigger_id: str):
    trg = store.get("trigger", trigger_id)
    if not trg:
        return None
    merchant = store.get("merchant", trg.get("merchant_id"))
    category = store.category_for(merchant, trg)
    customer = store.get("customer", trg.get("customer_id")) if trg.get("customer_id") else None
    if not merchant or not category:
        return None
    return trg, merchant, category, customer


def _preview(body: dict) -> dict:
    r = _resolve(str(body.get("trigger_id") or ""))
    if not r:
        return {"error": "Trigger, merchant or category not loaded yet."}
    trg, merchant, category, customer = r
    out = compose_full(category, merchant, trg, customer, now=body.get("now") or None)
    out.pop("followup", None)
    out.update({"merchant_name": (merchant.get("identity") or {}).get("name"),
                "customer_name": ((customer or {}).get("identity") or {}).get("name"),
                "trigger": {k: trg.get(k) for k in ("id", "kind", "source", "urgency", "payload")},
                "customer_missing": bool(trg.get("customer_id")) and not customer})
    return out


@app.post("/ui/api/preview")
async def ui_preview(request: Request):
    if (g := _ui_guard()):
        return g
    return await run_in_threadpool(_preview, await _json(request) or {})


def _sandbox_start(body: dict) -> dict:
    r = _resolve(str(body.get("trigger_id") or ""))
    if not r:
        return {"error": "Trigger, merchant or category not loaded yet."}
    trg, merchant, category, customer = r
    out = compose_full(category, merchant, trg, customer, now=body.get("now") or None)
    sid = f"ui_{int(time.time() * 1000)}"
    conv = {"conversation_id": sid, "merchant_id": merchant.get("merchant_id"),
            "customer_id": (customer or {}).get("customer_id"), "trigger_id": trg.get("id"), "kind": trg.get("kind"),
            "family": out["family"], "followup": out["followup"], "send_as": out["send_as"], "lang": out["lang"],
            "stage": "pitched", "status": "active", "turns": [{"from": "bot", "body": out["body"]}],
            "bot_bodies": [out["body"]], "mstate": {}}
    with _sandbox_lock:
        if len(_sandbox) > 200:
            _sandbox.pop(next(iter(_sandbox)))
        _sandbox[sid] = conv
    out.pop("followup", None)
    return {"sandbox_id": sid, "message": out,
            "reply_role": "customer" if out["send_as"] == "merchant_on_behalf" else "merchant"}


@app.post("/ui/api/sandbox/start")
async def ui_sandbox_start(request: Request):
    if (g := _ui_guard()):
        return g
    return await run_in_threadpool(_sandbox_start, await _json(request) or {})


def _sandbox_reply(body: dict) -> dict:
    with _sandbox_lock:
        conv = _sandbox.get(str(body.get("sandbox_id") or ""))
    if not conv:
        return {"error": "Sandbox conversation not found — start a new one."}
    merchant = store.get("merchant", conv["merchant_id"]) or {}
    category = store.category_for(merchant) or {}
    customer = store.get("customer", conv.get("customer_id")) if conv.get("customer_id") else None
    role = "customer" if conv["send_as"] == "merchant_on_behalf" else "merchant"
    action = respond(conv, str(body.get("message") or ""), merchant, category, customer, conv["mstate"], role)
    return {"action": action, "status": conv.get("status"), "stage": conv.get("stage"),
            "label": (conv.get("labels") or [None])[-1]}


@app.post("/ui/api/sandbox/reply")
async def ui_sandbox_reply(request: Request):
    if (g := _ui_guard()):
        return g
    return await run_in_threadpool(_sandbox_reply, await _json(request) or {})


@app.get("/ui/api/conversations")
async def ui_conversations():
    if (g := _ui_guard()):
        return g
    with store.lock:
        convs = copy.deepcopy(list(store.conversations.values()))
    out = []
    for c in sorted(convs, key=lambda c: -(c.get("created") or 0))[:150]:
        m = store.get("merchant", c.get("merchant_id")) or {}
        cu = store.get("customer", c.get("customer_id")) if c.get("customer_id") else None
        out.append({"conversation_id": c.get("conversation_id"), "merchant_id": c.get("merchant_id"),
                    "merchant_name": (m.get("identity") or {}).get("name"),
                    "customer_name": ((cu or {}).get("identity") or {}).get("name"),
                    "kind": c.get("kind"), "send_as": c.get("send_as"), "status": c.get("status"),
                    "stage": c.get("stage"), "turns": c.get("turns", []), "created": c.get("created")})
    return {"conversations": out}


# =============================================================================
# stateless test endpoints (Postman-friendly; never touch judge state)
# =============================================================================

def _bundle(body: dict, need_trigger: bool = True):
    """Resolve the 4 contexts from inline objects and/or ids of pushed contexts."""
    errors, warnings = [], []
    obj = lambda k: body.get(k) if isinstance(body.get(k), dict) else None
    trg = obj("trigger") or (store.get("trigger", body.get("trigger_id")) if body.get("trigger_id") else None)
    mid = body.get("merchant_id") or (trg or {}).get("merchant_id")
    merchant = obj("merchant") or (store.get("merchant", mid) if mid else None)
    slug = body.get("category_slug") or (merchant or {}).get("category_slug") or ((trg or {}).get("payload") or {}).get("category")
    category = obj("category") or (store.get("category", slug) if slug else None)
    cid = body.get("customer_id") or (trg or {}).get("customer_id")
    customer = obj("customer") or (store.get("customer", cid) if cid else None)
    if not merchant:
        errors.append("merchant missing — send a `merchant` object, or `merchant_id` of a context pushed via /v1/context")
    if need_trigger and not trg:
        errors.append("trigger missing — send a `trigger` object (needs `kind`), or `trigger_id` of a pushed trigger")
    if trg is not None and not trg.get("kind"):
        errors.append("trigger.kind is required (e.g. research_digest, perf_dip, recall_due)")
    if merchant and not category:
        category = {"slug": slug or ""}
        warnings.append("no category context — composed from merchant + trigger only (fewer facts available)")
    if trg and (trg.get("scope") == "customer" or trg.get("customer_id")) and not customer:
        warnings.append("customer-scoped trigger without a customer — drafted without personal details")
    return category, merchant, trg, customer, errors, warnings


def _example_hint() -> dict:
    return {"example_body": {"category": {"slug": "dentists", "...": "..."}, "merchant": {"merchant_id": "...", "...": "..."},
                             "trigger": {"kind": "research_digest", "payload": {}, "...": "..."}, "customer": None},
            "full_examples": "GET /postman.json"}


def _compose_api(body: dict):
    category, merchant, trg, customer, errors, warnings = _bundle(body)
    if errors:
        return 400, {"error": "invalid input", "details": errors, **_example_hint()}
    out = compose_full(category, merchant, trg, customer, now=body.get("now"))
    msg = {k: out[k] for k in ("body", "cta", "send_as", "suppression_key", "rationale", "template_name",
                               "template_params")}
    msg["language"] = out["lang"]
    msg["used"] = {"category": category.get("slug"), "merchant_id": merchant.get("merchant_id"),
                   "trigger_id": trg.get("id"), "trigger_kind": trg.get("kind"),
                   "customer_id": (customer or {}).get("customer_id")}
    if warnings:
        msg["warnings"] = warnings
    return 200, msg


@app.post("/v1/compose")
async def compose_api(request: Request):
    body = await _json(request)
    if body is None:
        return JSONResponse({"error": "body must be a JSON object", **_example_hint()}, 400)
    code, out = await run_in_threadpool(_compose_api, body)
    return JSONResponse(out, code)


def _converse_api(body: dict):
    category, merchant, trg, customer, errors, warnings = _bundle(body, need_trigger=False)
    msgs = body.get("messages")
    if msgs is None and body.get("message") is not None:
        msgs = [body.get("message")]
    if not isinstance(msgs, list) or not msgs:
        errors.append("send `messages` (list of merchant/customer replies) or a single `message`")
    if errors:
        return 400, {"error": "invalid input", "details": errors, **_example_hint()}
    conv = {"conversation_id": "converse", "merchant_id": merchant.get("merchant_id"),
            "customer_id": (customer or {}).get("customer_id"), "status": "active", "stage": "pitched",
            "turns": [], "bot_bodies": [], "family": None, "followup": None, "send_as": "vera",
            "lang": merchant_lang(merchant)}
    opening = None
    if trg:
        out = compose_full(category, merchant, trg, customer, now=body.get("now"))
        conv.update(family=out["family"], followup=out["followup"], send_as=out["send_as"], lang=out["lang"],
                    kind=trg.get("kind"))
        conv["turns"].append({"from": "bot", "body": out["body"]})
        conv["bot_bodies"].append(out["body"])
        opening = {k: out[k] for k in ("body", "cta", "send_as", "rationale")}
    role = body.get("from_role") or ("customer" if conv["send_as"] == "merchant_on_behalf" else "merchant")
    mstate: dict = {}
    replies = []
    for m in msgs[:20]:
        action = respond(conv, str(m), merchant, category, customer, mstate, role)
        replies.append({"message": m, "read_as": (conv.get("labels") or [None])[-1], **action})
    res = {"opening_message": opening, "replies": replies, "final_status": conv.get("status"),
           "transcript": conv["turns"]}
    if warnings:
        res["warnings"] = warnings
    return 200, res


@app.post("/v1/converse")
async def converse_api(request: Request):
    body = await _json(request)
    if body is None:
        return JSONResponse({"error": "body must be a JSON object", **_example_hint()}, 400)
    code, out = await run_in_threadpool(_converse_api, body)
    return JSONResponse(out, code)


def _public_base(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto.split(',')[0].strip()}://{host}"


@app.get("/postman.json")
async def postman(request: Request):
    return JSONResponse(examples.postman_collection(_public_base(request)),
                        headers={"Content-Disposition": 'attachment; filename="vera-bot.postman_collection.json"'})


@app.get("/ui/api/presets")
async def ui_presets():
    if (g := _ui_guard()):
        return g
    return {"presets": examples.presets()}


@app.get("/ui/api/bundle")
async def ui_bundle(trigger_id: str = ""):
    if (g := _ui_guard()):
        return g
    r = _resolve(trigger_id)
    if not r:
        return JSONResponse({"error": "trigger not loaded"}, 404)
    trg, merchant, category, customer = r
    return {"category": category, "merchant": merchant, "trigger": trg, "customer": customer}


# =============================================================================
# GET on POST-only endpoints -> usage help instead of 405 (browsers send GET)
# =============================================================================

_USAGE = {p["path"]: p for p in examples.presets() if p["method"] == "POST"}
_DESCRIPTIONS = {
    "/v1/context": "Store a context. Judge pushes category/merchant/customer/trigger here (versioned).",
    "/v1/tick": "Judge wake-up. Bot returns the proactive messages it wants to send now.",
    "/v1/reply": "A merchant/customer replied. Bot returns send / wait / end.",
    "/v1/compose": "Test data in, message out. No setup needed.",
    "/v1/converse": "Test data + replies in, the bot's side of the conversation out.",
}


def _usage(path: str) -> dict:
    p = _USAGE.get(path)
    return {"endpoint": path, "method": "POST", "what_it_does": _DESCRIPTIONS.get(path, ""),
            "how_to_call": "Send a POST request with header Content-Type: application/json and a JSON body like example_body "
                           "(Postman: method POST, Body -> raw -> JSON). Opening this URL in a browser sends GET, "
                           "which is why you see this help instead.",
            "example_body": p["body"] if p else None,
            "postman_collection": "/postman.json"}


for _path in _DESCRIPTIONS:
    app.add_api_route(_path, (lambda path: (lambda: _usage(path)))(_path), methods=["GET"], include_in_schema=False)
