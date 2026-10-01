"""Example request bodies (for the UI's API tester and the Postman collection).

The sample contexts mirror the challenge brief's running example (Dr. Meera's clinic),
trimmed to the fields the composer uses.
"""
from __future__ import annotations

import copy

CATEGORY = {
    "slug": "dentists",
    "voice": {"tone": "peer_clinical", "vocab_taboo": ["guaranteed", "100% safe", "completely cure"]},
    "offer_catalog": [
        {"title": "Dental Cleaning @ ₹299", "type": "service_at_price"},
        {"title": "Teeth Whitening @ ₹1,499", "type": "service_at_price"},
        {"title": "Free Consultation", "type": "free_service"},
    ],
    "peer_stats": {"scope": "metro_solo_practices_2026", "avg_rating": 4.4, "avg_review_count": 62,
                   "avg_views_30d": 1820, "avg_calls_30d": 12, "avg_ctr": 0.030},
    "digest": [{
        "id": "d_2026W17_jida_fluoride", "kind": "research",
        "title": "3-month fluoride varnish recall outperforms 6-month for high-risk adult caries",
        "source": "JIDA Oct 2026, p.14", "trial_n": 2100, "patient_segment": "high_risk_adults",
        "summary": "Multi-center Indian trial shows 38% lower caries recurrence with 3-month vs 6-month recall in adults "
                   "with active decay history. No effect in low-risk patients.",
        "actionable": "Reassess recall interval for adults flagged high-risk in your charting",
    }],
    "seasonal_beats": [{"month_range": "Oct-Dec", "note": "wedding whitening peak — bookings 2x baseline"}],
}

MERCHANT = {
    "merchant_id": "m_001_drmeera_dentist_delhi",
    "category_slug": "dentists",
    "identity": {"name": "Dr. Meera's Dental Clinic", "city": "Delhi", "locality": "Lajpat Nagar",
                 "verified": True, "languages": ["en", "hi"], "owner_first_name": "Meera"},
    "subscription": {"status": "active", "plan": "Pro", "days_remaining": 82},
    "performance": {"window_days": 30, "views": 2410, "calls": 18, "directions": 45, "ctr": 0.021,
                    "delta_7d": {"views_pct": 0.18, "calls_pct": -0.05}},
    "offers": [{"id": "o_meera_001", "title": "Dental Cleaning @ ₹299", "status": "active"}],
    "conversation_history": [
        {"from": "vera", "body": "Want me to draft 3 Google posts you can review?", "engagement": "merchant_replied"},
        {"from": "merchant", "body": "Yes please, focus on whitening and aligners", "engagement": "intent_action"},
    ],
    "customer_aggregate": {"total_unique_ytd": 540, "lapsed_180d_plus": 78, "high_risk_adult_count": 124},
    "signals": ["stale_posts:22d", "ctr_below_peer_median", "high_risk_adult_cohort"],
}

TRIGGER = {
    "id": "trg_001_research_digest_dentists", "scope": "merchant", "kind": "research_digest", "source": "external",
    "merchant_id": "m_001_drmeera_dentist_delhi", "customer_id": None,
    "payload": {"category": "dentists", "top_item_id": "d_2026W17_jida_fluoride"},
    "urgency": 2, "suppression_key": "research:dentists:2026-W17", "expires_at": "2026-05-03T00:00:00Z",
}

CUSTOMER = {
    "customer_id": "c_001_priya_for_m001", "merchant_id": "m_001_drmeera_dentist_delhi",
    "identity": {"name": "Priya", "language_pref": "hi-en mix"},
    "relationship": {"first_visit": "2025-11-04", "last_visit": "2026-05-12", "visits_total": 4},
    "state": "lapsed_soft",
    "preferences": {"preferred_slots": "weekday_evening", "channel": "whatsapp", "reminder_opt_in": True},
    "consent": {"opted_in_at": "2025-11-04", "scope": ["recall_reminders", "appointment_reminders"]},
}

RECALL_TRIGGER = {
    "id": "trg_003_recall_due_priya", "scope": "customer", "kind": "recall_due", "source": "internal",
    "merchant_id": "m_001_drmeera_dentist_delhi", "customer_id": "c_001_priya_for_m001",
    "payload": {"service_due": "6_month_cleaning", "last_service_date": "2026-05-12", "due_date": "2026-11-12",
                "available_slots": [{"iso": "2026-11-05T18:00:00+05:30", "label": "Wed 5 Nov, 6pm"},
                                    {"iso": "2026-11-06T17:00:00+05:30", "label": "Thu 6 Nov, 5pm"}]},
    "urgency": 3, "suppression_key": "recall:c_001_priya_for_m001:6mo", "expires_at": "2026-11-30T00:00:00Z",
}

NOW = "2026-04-26T10:00:00Z"


def presets() -> list[dict]:
    """Ready-to-send requests, in the order shown in the UI / Postman."""
    c = copy.deepcopy
    return [
        {"id": "compose", "name": "Compose a message (test data in, message out)", "method": "POST", "path": "/v1/compose",
         "body": {"category": c(CATEGORY), "merchant": c(MERCHANT), "trigger": c(TRIGGER), "customer": None, "now": NOW}},
        {"id": "compose_customer", "name": "Compose a customer message (recall)", "method": "POST", "path": "/v1/compose",
         "body": {"category": c(CATEGORY), "merchant": c(MERCHANT), "trigger": c(RECALL_TRIGGER),
                  "customer": c(CUSTOMER), "now": NOW}},
        {"id": "converse", "name": "Simulate replies (messages in, bot replies out)", "method": "POST", "path": "/v1/converse",
         "body": {"category": c(CATEGORY), "merchant": c(MERCHANT), "trigger": c(TRIGGER), "now": NOW,
                  "messages": ["How does it work?", "Ok let's do it. What's next?"]}},
        {"id": "converse_autoreply", "name": "Simulate an auto-reply loop", "method": "POST", "path": "/v1/converse",
         "body": {"category": c(CATEGORY), "merchant": c(MERCHANT), "trigger": c(TRIGGER), "now": NOW,
                  "messages": ["Thank you for contacting us! Our team will respond shortly."] * 3}},
        {"id": "context", "name": "Judge: push a context", "method": "POST", "path": "/v1/context",
         "body": {"scope": "merchant", "context_id": MERCHANT["merchant_id"], "version": 1, "payload": c(MERCHANT),
                  "delivered_at": NOW}},
        {"id": "tick", "name": "Judge: tick", "method": "POST", "path": "/v1/tick",
         "body": {"now": "2026-04-26T10:30:00Z", "available_triggers": [TRIGGER["id"]]}},
        {"id": "reply", "name": "Judge: merchant reply", "method": "POST", "path": "/v1/reply",
         "body": {"conversation_id": "conv_test_001", "merchant_id": MERCHANT["merchant_id"], "customer_id": None,
                  "from_role": "merchant", "message": "Yes please send the abstract",
                  "received_at": "2026-04-26T10:45:00Z", "turn_number": 2}},
        {"id": "healthz", "name": "Judge: health check", "method": "GET", "path": "/v1/healthz", "body": None},
        {"id": "metadata", "name": "Judge: bot metadata", "method": "GET", "path": "/v1/metadata", "body": None},
    ]


def postman_collection(base_url: str) -> dict:
    items = []
    for p in presets():
        req = {"method": p["method"], "url": "{{baseUrl}}" + p["path"], "header": []}
        if p["body"] is not None:
            req["header"] = [{"key": "Content-Type", "value": "application/json"}]
            req["body"] = {"mode": "raw", "raw": _dumps(p["body"]), "options": {"raw": {"language": "json"}}}
        items.append({"name": p["name"], "request": req})
    return {
        "info": {"name": "Vera++ bot", "description": "Requests for the Vera++ merchant engagement bot. "
                                                      "Set the baseUrl variable to your deployed URL.",
                 "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
        "variable": [{"key": "baseUrl", "value": base_url}],
        "item": items,
    }


def _dumps(o) -> str:
    import json
    return json.dumps(o, ensure_ascii=False, indent=2)
