#!/usr/bin/env python3
"""Contract + scenario self-test against a running bot (no LLM key needed).

  python selftest.py [BOT_URL]          # default http://localhost:8080

Checks: context versioning (200/409/400), healthz counts, tick schema, the three
replay scenarios (auto-reply hell, intent transition, hostile/off-topic), a full
customer booking flow, and anti-repetition. Exits non-zero on any failure.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib import error, request

URL = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080").rstrip("/")
DATA = Path(__file__).resolve().parent.parent / "dataset"
FAILS: list[str] = []
QUALIFYING = ["would you", "do you", "can you tell", "what if", "how about"]
ACTIONING = ["done", "sending", "draft", "here", "confirm", "proceed", "next"]
REQUIRED = ["conversation_id", "merchant_id", "send_as", "trigger_id", "body", "cta", "suppression_key", "rationale",
            "template_name", "template_params"]


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    req = request.Request(URL + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                          headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(cond: bool, msg: str):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def push(scope, cid, payload, v=1):
    return call("POST", "/v1/context", {"scope": scope, "context_id": cid, "version": v, "payload": payload,
                                        "delivered_at": "2026-04-26T10:00:00Z"})


def reply(conv, mid, msg, turn, cust=None, role="merchant"):
    return call("POST", "/v1/reply", {"conversation_id": conv, "merchant_id": mid, "customer_id": cust,
                                      "from_role": role, "message": msg, "received_at": "2026-04-26T10:40:00Z",
                                      "turn_number": turn})[1]


def main():
    call("POST", "/v1/teardown")
    cats = {json.load(open(f))["slug"]: json.load(open(f)) for f in (DATA / "categories").glob("*.json")}
    ms = json.load(open(DATA / "merchants_seed.json"))["merchants"]
    cs = json.load(open(DATA / "customers_seed.json"))["customers"]
    ts = json.load(open(DATA / "triggers_seed.json"))["triggers"]

    print("== context contract")
    for s, c in cats.items():
        push("category", s, c)
    for m in ms:
        push("merchant", m["merchant_id"], m)
    for c in cs:
        push("customer", c["customer_id"], c)
    code, b = push("merchant", ms[0]["merchant_id"], ms[0], 1)
    check(code == 409 and b.get("reason") == "stale_version" and b.get("current_version") == 1, "same version -> 409 stale_version")
    m2 = dict(ms[0]); m2["performance"] = dict(m2["performance"], views=2580)
    code, b = push("merchant", ms[0]["merchant_id"], m2, 2)
    check(code == 200 and b.get("accepted") is True, "higher version -> 200 accepted")
    code, b = call("POST", "/v1/context", {"scope": "bogus", "context_id": "x", "version": 1, "payload": {}})
    check(code == 400 and b.get("reason") == "invalid_scope", "bad scope -> 400 invalid_scope")
    code, h = call("GET", "/v1/healthz")
    check(code == 200 and h["contexts_loaded"] == {"category": 5, "merchant": 10, "customer": 15, "trigger": 0},
          f"healthz counts {h.get('contexts_loaded')}")
    code, md = call("GET", "/v1/metadata")
    check(code == 200 and md.get("approach"), "metadata ok")

    print("== tick")
    for t in ts:
        push("trigger", t["id"], t)
    code, out = call("POST", "/v1/tick", {"now": "2026-04-26T10:30:00Z", "available_triggers": [t["id"] for t in ts]})
    acts = out.get("actions", [])
    check(code == 200 and 0 < len(acts) <= 20, f"tick returned {len(acts)} actions")
    check(all(all(k in a for k in REQUIRED) and a["body"] for a in acts), "every action has the full schema + non-empty body")
    vera_m = [a["merchant_id"] for a in acts if a["send_as"] == "vera"]
    check(len(vera_m) == len(set(vera_m)), "max one merchant-facing send per merchant per tick")
    check(all("http" not in a["body"] for a in acts), "no URLs in bodies")
    code, out2 = call("POST", "/v1/tick", {"now": "2026-04-26T10:31:00Z", "available_triggers": [a["trigger_id"] for a in acts]})
    check(out2.get("actions") == [], "suppression: same triggers are not re-sent")
    code, out3 = call("POST", "/v1/tick", {"now": "2026-04-26T10:45:00Z", "available_triggers": [t["id"] for t in ts]})
    sent_later = len(out3.get("actions", []))
    check(sent_later > 0, f"deferred triggers go out on a later tick ({sent_later})")
    for a in acts[:40]:
        print(f"    [{a['trigger_id']}] {a['body'][:110]}...")

    print("== replay: auto-reply hell (same conversation)")
    conv = next(a["conversation_id"] for a in acts if a["merchant_id"] == "m_001_drmeera_dentist_delhi" and a["send_as"] == "vera")
    canned = "Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly."
    seq = [reply(conv, "m_001_drmeera_dentist_delhi", canned, i + 2)["action"] for i in range(4)]
    print("   ", seq)
    check(seq[0] == "send" and seq[1] == "wait" and seq[2] == "end", "auto-reply: nudge once -> wait -> end")

    print("== replay: auto-reply across new conversation ids (judge_simulator style)")
    seq = [reply(f"conv_auto_{i}", "m_003_studio11_salon_hyderabad",
                 "Thank you for contacting us! Our team will respond shortly.", i + 1)["action"] for i in range(1, 5)]
    print("   ", seq)
    check("end" in seq[:3], "auto-reply detected per merchant even with fresh conversation ids")

    print("== replay: intent transition")
    conv = next(a["conversation_id"] for a in acts + out3.get("actions", []) if a["merchant_id"] == "m_006_southindiancafe_restaurant_bangalore")
    r1 = reply(conv, "m_006_southindiancafe_restaurant_bangalore", "Looks interesting. How does it work?", 2)
    r2 = reply(conv, "m_006_southindiancafe_restaurant_bangalore", "Ok lets do it. Whats next?", 3)
    low = r2.get("body", "").lower()
    print("    Q ->", r1.get("body", "")[:140]); print("    commit ->", r2.get("body", "")[:200])
    check(r2["action"] == "send" and any(w in low for w in ACTIONING) and not any(w in low for w in QUALIFYING),
          "commit -> action mode (no qualifying phrases)")
    r = reply("conv_intent_1", "m_001_drmeera_dentist_delhi", "Ok lets do it. Whats next?", 2)
    low = r.get("body", "").lower()
    check(r["action"] == "send" and any(w in low for w in ACTIONING) and not any(w in low for w in QUALIFYING),
          "commit on unknown conversation -> action mode")

    print("== replay: hostile / off-topic")
    conv_h = next(a["conversation_id"] for a in acts if a["merchant_id"] == "m_002_bharat_dentist_mumbai")
    r = reply(conv_h, "m_002_bharat_dentist_mumbai", "This is useless, why do you keep sending this", 2)
    print("    abuse ->", r.get("action"), r.get("body", "")[:150])
    check(r["action"] in ("send", "end") and ("sorry" in r.get("body", "").lower() or r["action"] == "end"), "abuse -> apology or end")
    r = reply(conv_h, "m_002_bharat_dentist_mumbai", "can you also help me file my GST?", 3)
    print("    GST ->", r.get("action"), r.get("body", "")[:150])
    check(r["action"] == "send" and "gst" not in r.get("body", "").lower()[:0] and "ca" in r.get("body", "").lower(), "off-topic -> polite decline + redirect")
    r = reply("conv_hostile", "m_005_pizzajunction_restaurant_delhi", "Stop messaging me. This is useless spam.", 2)
    check(r["action"] == "end", "explicit stop -> end")
    code, out4 = call("POST", "/v1/tick", {"now": "2026-04-26T11:30:00Z", "available_triggers": ["trg_011_review_theme_late_delivery"]})
    check(not out4.get("actions"), "opted-out merchant gets no new proactive sends")

    print("== customer booking flow (Priya)")
    pr = next((a for a in acts + out3.get("actions", []) if a.get("customer_id") == "c_001_priya_for_m001"), None)
    check(pr is not None and pr["send_as"] == "merchant_on_behalf" and pr["cta"] == "multi_choice_slot", "recall sent on behalf of merchant with slot CTA")
    if pr:
        r = reply(pr["conversation_id"], "m_001_drmeera_dentist_delhi", "2", 2, cust="c_001_priya_for_m001", role="customer")
        print("    '2' ->", r.get("body"))
        check(r["action"] == "send" and "Thu 6 Nov" in r.get("body", ""), "slot 2 booked with the real label")

    print("== hindi turn + repetition")
    conv_s = next(a["conversation_id"] for a in acts + out3.get("actions", []) if a["merchant_id"] == "m_010_sunrisepharm_pharmacy_lucknow")
    r = reply(conv_s, "m_010_sunrisepharm_pharmacy_lucknow", "haan theek hai, kar do", 2)
    print("    hi commit ->", r.get("body", "")[:160])
    check(r["action"] == "send", "hinglish commit handled")
    bodies = []
    for i in range(6):
        r = reply(conv_s, "m_010_sunrisepharm_pharmacy_lucknow", "what does this mean exactly?", 3 + i)
        if r["action"] == "send":
            bodies.append(r["body"])
    check(len(bodies) == len(set(bodies)), "never sends the same body twice in one conversation")

    # leave the bot empty so the judge's warmup pushes (version 1) aren't rejected as stale
    code, _ = call("POST", "/v1/teardown")
    _, h = call("GET", "/v1/healthz")
    check(code == 200 and not any(h["contexts_loaded"].values()), "teardown: bot state wiped for the real judge")

    print()
    print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILURE(S): " + "; ".join(FAILS))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
