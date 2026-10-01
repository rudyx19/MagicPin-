"""Vera composer — compose(category, merchant, trigger, customer?) -> message.

Design
------
* Deterministic: no randomness; the same 4 contexts always give the same message.
* Grounded: every number/name in a message comes from the contexts (or simple
  arithmetic on them). When a trigger payload is a placeholder, the composer
  anchors on the merchant's real numbers + the category pack instead of inventing
  the missing event details.
* Dispatch by trigger.kind into ~25 kind composers, grouped into families.
* Each composer also returns a `followup` plan (what was offered, what to deliver
  when the merchant says yes) that the conversation handler uses for multi-turn.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from util import (REGIONAL_GREETING, active_offers, beat_sentence, best_gap, biz_name, catalog_pick, customer_first,
                  customer_lang, customer_subject, days_between, digest_of_kind, expired_offers,
                  find_digest, first_sentence, group, human, inr, lc_first, locality, merchant_lang,
                  nice_date, owner_first, parse_dt, pct, peer_scope, perf, pick, scrub, seasonal_beat,
                  signal_map, src_fmt, strip_period, top_trend, worst_gap)

COMPOSER_VERSION = "composer_v1"

NOUN = {"dentists": ("clinic", "patients"), "salons": ("salon", "clients"),
        "restaurants": ("restaurant", "customers"), "gyms": ("gym", "members"),
        "pharmacies": ("store", "customers")}

FAMILY = {
    "research_digest": "research", "research_digest_release": "research",
    "category_research_digest_release": "research",
    "regulation_change": "compliance", "compliance_alert": "compliance",
    "cde_opportunity": "cde",
    "supply_alert": "supply_alert",
    "perf_dip": "perf_dip", "perf_spike": "perf_spike", "seasonal_perf_dip": "seasonal_dip",
    "milestone_reached": "milestone", "review_theme_emerged": "review_theme",
    "dormant_with_vera": "dormant", "winback_eligible": "winback", "renewal_due": "renewal",
    "festival_upcoming": "festival", "festival": "festival",
    "ipl_match_today": "ipl",
    "active_planning_intent": "planning",
    "curious_ask_due": "curious_ask", "scheduled_recurring": "curious_ask",
    "category_seasonal": "category_seasonal",
    "gbp_unverified": "gbp_unverified",
    "competitor_opened": "competitor",
    "category_trend_movement": "trend",
    "recall_due": "c_recall", "appointment_tomorrow": "c_appointment",
    "chronic_refill_due": "c_refill",
    "customer_lapsed_soft": "c_lapsed", "customer_lapsed_hard": "c_lapsed",
    "trial_followup": "c_trial",
    "wedding_package_followup": "c_bridal", "bridal_followup": "c_bridal",
    "unplanned_slot_open": "c_slot",
}


# =============================================================================
# context wrapper
# =============================================================================

class Ctx:
    def __init__(self, category: dict, merchant: dict, trigger: dict, customer: Optional[dict],
                 now: Any = None, prior_contact: bool = False):
        self.cat = category or {}
        self.m = merchant or {}
        self.t = trigger or {}
        self.cu = customer
        self.p = self.t.get("payload") or {}
        self.placeholder = bool(self.p.get("placeholder"))
        self.kind = str(self.t.get("kind") or "unknown")
        self.slug = self.cat.get("slug") or self.m.get("category_slug") or ""
        self.place_noun, self.people = NOUN.get(self.slug, ("business", "customers"))
        self.first = owner_first(self.m)
        self.name = biz_name(self.m)
        self.loc = locality(self.m)
        self.lang = merchant_lang(self.m)
        self.clang = customer_lang(customer)
        self.now = parse_dt(now)
        self.first_contact = not (self.m.get("conversation_history") or prior_contact)
        self.sig = signal_map(self.m)
        self.agg = self.m.get("customer_aggregate") or {}
        self.pf = perf(self.m)
        self.anchors: list[str] = []   # facts used (for rationale)
        self.levers: list[str] = []
        self.notes: list[str] = []     # judgement calls (for rationale)

    # ---- merchant-facing helpers
    def greet(self) -> str:
        raw = str((self.m.get("identity") or {}).get("owner_first_name") or "")
        if self.first and (self.slug == "dentists" or raw.lower().startswith("dr")):
            g = f"Dr. {self.first}, "
        elif self.first:
            g = f"Namaste {self.first} ji, " if self.lang == "hi-en" else f"Hi {self.first}, "
        else:
            g = f"Hi {self.name} team, "
        if self.first_contact:
            g += pick(self.lang, "Vera from magicpin here. ", "Vera, magicpin se. ")
        return g

    def yes(self, en: str, hi: str) -> str:
        return pick(self.lang, f"{en} Reply YES.", f"{hi} Bas YES reply karein.")

    def anchor(self, *xs: str):
        self.anchors.extend(x for x in xs if x)

    # ---- customer-facing helpers
    def sender(self) -> str:
        if self.slug in ("salons", "gyms") and self.first and self.first.lower() not in self.name.lower():
            return f"{self.first} from {self.name}"
        if self.slug in ("pharmacies", "restaurants") and self.loc and self.loc.lower() not in self.name.lower():
            return f"{self.name}, {self.loc}"
        return self.name

    def cgreet(self) -> str:
        cu = self.cu or {}
        prefs = cu.get("preferences") or {}
        via = "via_" in str(prefs.get("channel") or "")
        name = customer_first(cu)
        if via and not customer_subject(cu):
            # message goes to a family member whose name we don't know
            return "Namaste — " if self.clang in ("hi", "hi-en") else "Hello — "
        if self.clang == "hi":
            return f"Namaste {name} ji, "
        if self.clang in REGIONAL_GREETING:
            return f"{REGIONAL_GREETING[self.clang]} {name}! "
        return f"Hi {name}, "

    def csign(self) -> str:
        if self.clang == "hi":
            if " from " in self.sender():
                return f"{self.first}, {self.name} se. "
            return f"{self.sender()} se. "
        return f"{self.sender()} here. "


def _num(x: Any) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _price_in(title: Optional[str]) -> Optional[str]:
    m = re.search(r"₹\s?[\d,]+", title or "")
    return m.group(0).replace(" ", "") if m else None


def _offer_matching(offers: list[str], kws) -> Optional[str]:
    for kw in kws:
        for o in offers:
            if kw and kw.lower() in o.lower():
                return o
    return None


def _clock(iso: Any) -> Optional[str]:
    d = parse_dt(iso)
    if not d:
        return None
    h12 = d.hour % 12 or 12
    return f"{h12}{':%02d' % d.minute if d.minute else ''}{'am' if d.hour < 12 else 'pm'}"


def _hist_number(m: dict, pattern: str) -> Optional[str]:
    for h in reversed(m.get("conversation_history") or []):
        mm = re.search(pattern, h.get("body") or "", re.I)
        if mm:
            return mm.group(1)
    return None


def _continuation(c: Ctx, keywords) -> Optional[dict]:
    """If the merchant already said yes/asked about this exact topic, return that turn."""
    hist = c.m.get("conversation_history") or []
    for i in range(len(hist) - 1, 0, -1):
        h = hist[i]
        if h.get("from") != "merchant":
            continue
        prev = hist[i - 1]
        txt = (prev.get("body") or "").lower()
        if any(k.lower() in txt for k in keywords if k):
            return h
        break
    return None


def _segment_tie(c: Ctx, seg: Optional[str]) -> Optional[str]:
    if not seg:
        return None
    label = human(seg).replace("high risk", "high-risk")
    toks = [t for t in re.split(r"[_\s]+", seg.lower()) if t]
    stem = "_".join(toks[:2])
    for k, v in c.agg.items():
        if stem and stem in k and isinstance(v, (int, float)) and v > 1:
            c.anchor(f"{k}={v}")
            return f"the {group(v)} {label} in your {c.people[:-1] if c.people.endswith('s') else c.people} base"
    for k in c.sig:
        if stem and stem in k:
            c.anchor(f"signal {k}")
            return f"your {label} cohort"
    return None


def _ctr_per_100(c: Ctx, gap: dict) -> str:
    return (f"for every 100 people who see your listing, about {round(float(gap['mine']) * 100)} take action "
            f"vs {round(float(gap['peer']) * 100)} for the average {peer_scope(c.cat).replace(' 20', '')} listing")


def _gap_sentence(c: Ctx, gap: dict) -> str:
    c.anchor(f"{gap['metric']} {gap['mine']} vs peer {gap['peer']}")
    if gap["metric"] == "ctr" and gap["rel"] < 0:
        return f"your profile CTR is {pct(gap['mine'])} vs {pct(gap['peer'])} for {peer_scope(c.cat)} — {_ctr_per_100(c, gap)}"
    return gap["text"]


def _hero_offer(c: Ctx, keywords=()) -> Optional[str]:
    """A catalog offer to suggest (never a delivery offer if delivery isn't set up)."""
    avoid = ["delivery"] if "delivery_not_set_up" in c.sig else []
    return catalog_pick(c.cat, keywords, avoid=avoid)


INTEREST_LINKS = {  # what the merchant said they care about -> words that make an item relevant
    "aligner": ["aligner", "impression", "scan", "primescan", "trios", "cad/cam"],
    "whitening": ["whitening", "cosmetic", "veneer", "e.max", "aesthetic"],
    "implant": ["implant", "zirconia", "crown"],
    "bridal": ["bridal", "wedding", "skincare"],
    "keratin": ["keratin", "smoothening"],
    "thali": ["thali", "lunch"],
}


def _interest_tie(c: Ctx, text: str) -> str:
    """' Useful for the aligner work you asked me to focus on.' when the merchant's own
    words in conversation_history connect to this item."""
    turn = None
    for h in reversed(c.m.get("conversation_history") or []):
        if h.get("from") == "merchant":
            turn = (h.get("body") or "").lower()
            break
    if not turn:
        return ""
    low = (text or "").lower()
    for interest, words in INTEREST_LINKS.items():
        if interest in turn and any(w in low for w in words):
            c.anchor(f"merchant interest '{interest}' from history")
            return f" Useful for the {interest} work you asked me to focus on."
    return ""


def _fixes_from_signals(c: Ctx) -> tuple[list[str], list[str]]:
    """(problems, fixes) derived only from the merchant's signals + catalog."""
    probs, fixes = [], []
    if "unverified_gbp" in c.sig or (c.m.get("identity") or {}).get("verified") is False:
        probs.append("your Google profile is still unverified")
        fixes.append("start Google verification")
    if "no_active_offers" in c.sig or not active_offers(c.m):
        hero = _hero_offer(c)
        probs.append("there's no live offer on your listing")
        if hero:
            fixes.append(f"put '{hero}' live as your hero offer")
    if "stale_posts" in c.sig:
        probs.append(f"your last Google post was {c.sig['stale_posts'].replace('d', ' days')} ago")
        fixes.append("publish 2 fresh Google posts this week")
    elif "no_recent_post" in c.sig:
        probs.append("there's no recent Google post")
        fixes.append("publish 2 fresh Google posts this week")
    if "delivery_not_set_up" in c.sig:
        probs.append("home delivery isn't switched on")
        fixes.append("switch on delivery on your listing")
    if "ctr_below_peer_median" in c.sig and len(fixes) < 2:
        fixes.append("tighten your description + photos to lift CTR")
    return probs, fixes


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _result(c: Ctx, family: str, body: str, cta: str, params: list[str],
            followup: dict, send_as: str = "vera") -> dict:
    return {"family": family, "body": body, "cta": cta, "params": params, "followup": followup,
            "send_as": send_as}


# =============================================================================
# merchant-facing composers
# =============================================================================

def k_research(c: Ctx) -> dict:
    item = (find_digest(c.cat, c.p.get("top_item_id") or c.p.get("digest_item_id"))
            or digest_of_kind(c.cat, ["research", "trend", "tech"]))
    if not item:
        return k_generic(c)
    src, title = item.get("source"), strip_period(item.get("title", ""))
    c.anchor(f"digest {item.get('id')}", src)
    lead = f"one from {src} worth 2 minutes: {title}." if src else f"one research item worth 2 minutes: {title}."
    detail = ""
    if item.get("summary"):
        s = strip_period(first_sentence(item["summary"]))
        n = item.get("trial_n")
        detail = f" {s}{f' (n={group(n)})' if n else ''}."
        if "No effect" in item["summary"] or "no effect" in item["summary"]:
            detail += " " + first_sentence(item["summary"][item["summary"].lower().find("no effect"):])
    tie = _segment_tie(c, item.get("patient_segment"))
    tie_s = f" That maps straight to {tie}." if tie else _interest_tie(c, title + " " + item.get("summary", ""))
    person = {"dentists": "patient", "gyms": "member", "salons": "client"}.get(c.slug, "customer")
    cta = c.yes(f"Want me to send the 2-min summary + a {person} WhatsApp you can forward?",
                f"2-min summary + ek {person} WhatsApp draft bhej doon?")
    body = c.greet() + lead + detail + tie_s + " " + cta
    c.levers += ["specificity (source + numbers)", "curiosity", "reciprocity"]
    patient = (f"Quick note from {c.name}: new research ({src}) suggests {lc_first(title)}. "
               f"Reply here and we'll tell you what interval is right for you.")
    fu = {"offer": "send the summary + a patient WhatsApp draft",
          "fulfill_en": (f"Here's the 2-min version ({src}):\n• {strip_period(item.get('summary', title))}.\n"
                         + (f"• What to do: {strip_period(item['actionable'])}.\n" if item.get("actionable") else "")
                         + f"\nPatient WhatsApp draft:\n\"{patient}\"\n\nReply CONFIRM and I'll queue it for your {c.people}, or tell me what to change."),
          "done_en": f"Done — the patient note is queued for your {c.people}. I'll share how many read it by tomorrow.",
          "details_en": f"{title} — {strip_period(item.get('summary', ''))}. Source: {src}."}
    return _result(c, "research", body, "binary_yes_no", [c.greet().strip(" ,."), lead + detail + tie_s, cta], fu)


def k_compliance(c: Ctx) -> dict:
    item = find_digest(c.cat, c.p.get("top_item_id") or c.p.get("digest_item_id")) or digest_of_kind(c.cat, ["compliance"])
    if not item:
        return k_generic(c)
    src, title = item.get("source"), strip_period(item.get("title", ""))
    c.anchor(f"digest {item.get('id')}", src)
    deadline = c.p.get("deadline_iso")
    when = ""
    if deadline:
        d = days_between(c.now, deadline) if c.now else None
        when = (f" Deadline {nice_date(deadline, True)} — {d} days from today." if d and d > 0
                else f" Deadline: {nice_date(deadline, True)}.")
        c.anchor(f"deadline {deadline}")
    summary = f" {strip_period(item['summary'])}." if item.get("summary") else ""
    action = f" Action: {lc_first(strip_period(item['actionable']))}." if item.get("actionable") else ""
    cta = c.yes("Want a 1-page audit checklist so you can close this in 10 minutes?",
                "10 minute mein audit ho jaye, aisi 1-page checklist bhej doon?")
    body = c.greet() + f"compliance heads-up — {title} ({src}).{summary}{when}{action} {cta}"
    c.levers += ["loss aversion (deadline)", "specificity", "effort externalization"]
    fu = {"offer": "send a 1-page audit checklist",
          "fulfill_en": (f"Checklist — {title}:\n1. {strip_period(item.get('actionable') or 'Check your current setup against the new rule')}.\n"
                         f"2. Note the result + date in your SOP file (keeps you inspection-ready).\n"
                         f"3. If anything fails, book the fix before {nice_date(deadline, True) or 'the deadline'}.\n"
                         f"Source: {src}. Reply DONE once checked and I'll set a reminder 2 weeks before the deadline."),
          "done_en": "Noted — reminder set 2 weeks before the deadline.",
          "details_en": f"{title}. {strip_period(item.get('summary', ''))}. Source: {src}."}
    return _result(c, "compliance", body, "binary_yes_no", [c.greet().strip(" ,."), f"{title} ({src}){when}", cta], fu)


def k_cde(c: Ctx) -> dict:
    item = find_digest(c.cat, c.p.get("digest_item_id") or c.p.get("top_item_id")) or digest_of_kind(c.cat, ["cde"])
    if not item:
        return k_generic(c)
    title, src = strip_period(item.get("title", "")), item.get("source")
    credits = c.p.get("credits") or item.get("credits")
    fee = item.get("actionable") if re.search(r"free|₹", str(item.get("actionable", "")), re.I) else c.p.get("fee")
    when = item.get("date") or c.p.get("date")
    bits = []
    if when and nice_date(when):
        bits.append(nice_date(when) + (f", {_clock(when)}" if "T" in str(when) else ""))
    if credits:
        bits.append(f"{credits} CDE credits")
    if fee:
        bits.append(lc_first(strip_period(human(fee))))
    c.anchor(f"digest {item.get('id')}", *(bits))
    extra = f" {strip_period(item['summary'])}." if item.get("summary") else ""
    extra += _interest_tie(c, title + " " + item.get("summary", ""))
    cta = c.yes("Want me to register you and add it to your calendar?", "Register karke calendar mein daal doon?")
    body = c.greet() + f"{title} ({src}){' — ' + ' · '.join(bits) if bits else ''}.{extra} {cta}"
    c.levers += ["specificity", "low-effort yes"]
    fu = {"offer": "register you and add it to your calendar",
          "fulfill_en": f"Done — you're on the list for {title}{' (' + ', '.join(bits) + ')' if bits else ''}. Calendar hold added; I'll remind you the day before.",
          "done_en": "All set. See you there.",
          "details_en": f"{title} — {', '.join(bits)}. Source: {src}."}
    return _result(c, "cde", body, "binary_yes_no", [c.greet().strip(" ,."), title, cta], fu)


def k_supply_alert(c: Ctx) -> dict:
    item = find_digest(c.cat, c.p.get("alert_id")) or digest_of_kind(c.cat, ["alert", "supply"])
    mol = c.p.get("molecule") or ""
    batches = c.p.get("affected_batches") or []
    mfr = c.p.get("manufacturer")
    src = (item or {}).get("source")
    c.anchor(f"molecule {mol}", f"batches {', '.join(batches)}", f"mfr {mfr}", src)
    why = ""
    if item and item.get("summary"):
        s = item["summary"]
        m = re.search(r"flagged for ([^.;]+)", s)
        risk = re.search(r"(No safety risk[^.]*)", s)
        why = f" — flagged for {m.group(1)}" if m else ""
        why += f"; {lc_first(risk.group(1))}" if risk else ""
    rx = c.agg.get("chronic_rx_count")
    batch_s = " and ".join(batches) if batches else "the listed batches"
    head = f"voluntary recall on {mol} batches {batch_s}{f' ({mfr})' if mfr else ''}{why}{f' ({src})' if src else ''}."
    cont = _continuation(c, [mol, "recall"])
    cust_draft = (f"Namaste, {c.name} here. {'An' if mol[:1].lower() in 'aeiou' else 'A'} {mol} batch you may have received is under a voluntary recall "
                  f"(lower potency — not a safety risk). Please bring your strip to the store for a replacement, "
                  f"or reply here and we'll arrange it.")
    if cont is not None and str(cont.get("engagement", "")).startswith("intent"):
        c.notes.append("merchant already asked for the list — action mode, no re-pitch")
        body = (c.greet() + f"on the {mol} recall list you asked for: confirmed batches {batch_s}"
                f"{f' ({mfr})' if mfr else ''}{why}. I'm filtering your {group(rx) + ' ' if rx else ''}chronic-Rx "
                f"customers for these batches now. Customer note, ready to go:\n\"{cust_draft}\"\n"
                + pick(c.lang, "Reply CONFIRM and I'll send it to the affected customers.",
                       "Affected customers ko bhejne ke liye CONFIRM reply karein."))
        cta = "binary_confirm_cancel"
        fu = {"offer": "send the recall note to affected customers",
              "fulfill_en": f"Sending now to the customers dispensed {batch_s}. I'll share the list of who received it, and flag anyone who needs a home-delivered replacement.",
              "done_en": "Done — all notes sent. I'll report replies here.",
              "details_en": f"Recall: {mol} {batch_s} ({mfr}). {strip_period((item or {}).get('summary', ''))}."}
    else:
        tail = (f" You have {group(rx)} chronic-Rx customers on file — whoever got these batches should be told and offered a replacement."
                if rx else " Customers who got these batches should be told and offered a replacement.")
        cta_s = c.yes("Want me to filter your repeat-Rx list for these batches and draft the customer WhatsApp?",
                      "Repeat-Rx list filter karke customer WhatsApp draft kar doon?")
        body = c.greet() + "urgent: " + head + tail + " " + cta_s
        cta = "binary_yes_no"
        fu = {"offer": "filter the repeat-Rx list and draft the customer note",
              "fulfill_en": f"Filtering your repeat-Rx list for {batch_s} now. Customer note draft:\n\"{cust_draft}\"\nReply CONFIRM and I'll send it to everyone affected.",
              "done_en": "Done — notes sent to affected customers. I'll report replies here.",
              "details_en": f"Recall: {mol} {batch_s} ({mfr}). {strip_period((item or {}).get('summary', ''))}."}
    c.levers += ["urgency", "specificity (batch numbers)", "effort externalization"]
    return _result(c, "supply_alert", body, cta, [c.greet().strip(" ,."), head, "CONFIRM"], fu)


def _delta_pick(c: Ctx, sign: int) -> Optional[tuple[str, float]]:
    d7 = c.pf.get("delta_7d") or {}
    cands = [(k.replace("_pct", ""), float(v)) for k, v in d7.items() if _num(v) is not None and k != "ctr_pct"]
    cands = [x for x in cands if (x[1] < 0 if sign < 0 else x[1] > 0)]
    if not cands:
        return None
    return sorted(cands, key=lambda x: x[1] * sign)[-1]


def k_perf_dip(c: Ctx) -> dict:
    metric, delta, base = c.p.get("metric"), _num(c.p.get("delta_pct")), c.p.get("vs_baseline")
    if not metric or delta is None:
        got = _delta_pick(c, -1)
        gap = worst_gap(c.cat, c.m)
        if got and (got[1] <= -0.10 or not gap):  # a small weekly wobble is a weaker hook than a real peer gap
            metric, delta = got
    probs, fixes = _fixes_from_signals(c)
    if metric and delta is not None:
        c.anchor(f"{metric} {pct(delta, True)} 7d")
        line = f"your {metric} are down {pct(abs(delta))} this week" + (f" vs your usual baseline of {base}" if base else "") + "."
        if base:
            c.anchor(f"baseline {base}")
    else:
        g = worst_gap(c.cat, c.m)
        line = (_gap_sentence(c, g) + ".") if g else f"your last 30 days: {group(c.pf.get('views', 0))} views, {group(c.pf.get('calls', 0))} calls."
    if not fixes:
        g = worst_gap(c.cat, c.m)
        if g and g["metric"] == "ctr":
            fixes = ["rewrite your description + refresh photos to lift CTR"]
        fixes.append("publish 2 fresh Google posts this week")
    diag = f" What's likely holding you back: {_join(probs[:2])}." if probs else ""
    fix_s = _join(fixes[:2])
    cta = c.yes(f"I can {fix_s} — 10 minutes from your side. Go ahead?",
                f"Fix: {fix_s}. Main kar deti hoon — aapke bas 10 minute lagenge. Shuru karein?")
    body = c.greet() + line + diag + " " + cta
    c.levers += ["loss aversion", "effort externalization", "binary CTA"]
    fu = {"offer": fix_s,
          "fulfill_en": "Starting now:\n" + "\n".join(f"{i+1}. {f[0].upper() + f[1:]}" for i, f in enumerate(fixes[:3]))
                        + "\nI'll share drafts here within the hour — reply CONFIRM on each to publish.",
          "done_en": "Live. I'll send you a before/after on calls next week.",
          "details_en": f"{line}{diag} Fixes: {fix_s}."}
    return _result(c, "perf_dip", body, "binary_yes_no", [c.greet().strip(" ,."), line + diag, cta], fu)


def k_perf_spike(c: Ctx) -> dict:
    metric, delta, base = c.p.get("metric"), _num(c.p.get("delta_pct")), c.p.get("vs_baseline")
    if not metric or delta is None:
        got = _delta_pick(c, +1)
        if got:
            metric, delta = got
    driver = c.p.get("likely_driver")
    offers = active_offers(c.m)
    if metric and delta is not None:
        c.anchor(f"{metric} {pct(delta, True)} 7d")
        line = f"your {metric} are up {pct(delta)} this week" + (f" (baseline {base})" if base else "")
        line += f" — it lines up with your {human(driver)}." if driver else "."
    else:
        g = best_gap(c.cat, c.m)
        line = (g["text"] + " — you're ahead of peers.") if g else f"your listing pulled {group(c.pf.get('views', 0))} views in the last 30 days."
        if g:
            c.anchor(f"{g['metric']} above peer")
    hero = offers[0] if offers else _hero_offer(c)
    pin = (f"pin '{hero}' to the top of your profile" if offers else
           f"put '{hero}' live on your profile" if hero else "add a clear offer to your profile")
    action = f"draft a follow-up post{' on the same theme' if driver else ''} + {pin} while interest is high"
    cta = c.yes(f"Want me to {action}?", f"Interest high hai — follow-up post + {(chr(39) + hero + chr(39)) if hero else 'offer'} live kar doon?")
    body = c.greet() + line + " Good moment to double down. " + cta
    c.levers += ["momentum / positive reinforcement", "effort externalization"]
    fu = {"offer": action,
          "fulfill_en": f"On it — follow-up post draft:\n\"{c.name}: thank you for the response this week! "
                        + (f"{hero} — " if hero else "") + "message us to book.\"\n"
                        + (f"And '{hero}' goes to the top of your profile. " if hero else "")
                        + "Reply CONFIRM to publish both.",
          "done_en": "Published. I'll track whether the lift holds next week.",
          "details_en": line}
    return _result(c, "perf_spike", body, "binary_yes_no", [c.greet().strip(" ,."), line, cta], fu)


def k_seasonal_dip(c: Ctx) -> dict:
    metric, delta = c.p.get("metric") or "views", _num(c.p.get("delta_pct"))
    note = str(c.p.get("season_note") or "")
    beat = seasonal_beat(c.cat, c.now.month if c.now else None, keywords=["retention", "lowest", "lull", "slow"])
    if not beat and note:
        beat = seasonal_beat(c.cat, keywords=[w for w in note.split("_") if len(w) > 3])
    line = f"your {metric} are down {pct(abs(delta))} this week" if delta is not None else f"your {metric} have dipped"
    c.anchor(f"{metric} {pct(delta, True) if delta is not None else ''}")
    season = (f" — expected: {beat_sentence(beat, c.slug)}, not something you did."
              if beat else " — this is a seasonal pattern, not something you did.")
    if beat:
        c.anchor(f"seasonal beat {beat['month_range']}")
    dg = digest_of_kind(c.cat, ["seasonal"])
    if dg and dg.get("actionable") and re.search(r"spend|ads?\b", dg["actionable"], re.I):
        season += f" {src_fmt(dg.get('source'))} says {lc_first(strip_period(dg['actionable']))}."
        c.anchor(f"digest {dg.get('id')}")
    churn, peer_churn = _num(c.agg.get("monthly_churn_pct")), _num((c.cat.get("peer_stats") or {}).get("monthly_churn_pct"))
    members = _num(c.agg.get("total_active_members"))
    ret = ""
    if churn is not None and members:
        lost = round(members * churn)
        ret = (f" What matters now is retention: monthly churn at {c.name} is {pct(churn)}"
               + (f" vs {pct(peer_churn)} for {peer_scope(c.cat)}" if peer_churn else "")
               + f" — about {lost} of your {group(members)} {c.people} a month.")
        c.anchor(f"churn {churn}", f"members {members}")
    action = "draft a 4-week summer attendance challenge to keep current members coming" if c.slug == "gyms" else "draft a retention message for your regulars"
    cta = c.yes(f"Want me to {action}?", "Members ke liye 4-week attendance challenge draft kar doon?")
    body = c.greet() + line + season + ret + " " + cta
    c.levers += ["anxiety pre-emption (reframe)", "specificity", "loss aversion on members"]
    fu = {"offer": action,
          "fulfill_en": ("Here's the challenge:\n• '28 days, 16 sessions' — members who hit 16 check-ins get a free PT session\n"
                         "• Weekly leaderboard post on Google + WhatsApp\n• Nudge on day 7 to anyone with < 3 check-ins\n"
                         "Reply CONFIRM and I'll announce it to your members.") if c.slug == "gyms" else
                        "Draft ready: a thank-you note + a small loyalty perk for your regulars. Reply CONFIRM to send.",
          "done_en": "Announced. I'll share check-in numbers weekly.",
          "details_en": line + season + ret}
    return _result(c, "seasonal_dip", body, "binary_yes_no", [c.greet().strip(" ,."), line + season, cta], fu)


def k_milestone(c: Ctx) -> dict:
    metric, now_v, target = c.p.get("metric"), _num(c.p.get("value_now")), _num(c.p.get("milestone_value"))
    peer_rev = _num((c.cat.get("peer_stats") or {}).get("avg_review_count"))
    if metric and now_v is not None and target:
        lbl = human(metric).replace("review count", "Google reviews")
        gap = int(target - now_v)
        c.anchor(f"{metric} {int(now_v)} -> {int(target)}")
        if gap > 0:
            line = f"you're at {int(now_v)} {lbl} — {gap} away from {int(target)}"
        else:
            line = f"you just crossed {int(target)} {lbl}"
        if "review" in metric and peer_rev:
            line += f" (avg for {peer_scope(c.cat)} is {int(peer_rev)})"
        line += "."
    else:
        line = (f"quick win to share: your listing got {group(c.pf.get('views', 0))} views and "
                f"{group(c.pf.get('calls', 0))} calls in the last 30 days.")
        c.anchor("30d views/calls")
        g = best_gap(c.cat, c.m)
        if g and g["metric"] in ("views", "calls"):
            line = line[:-1] + f" — {float(g['mine']) / float(g['peer']):.1f}x the {group(g['peer'])} average for {peer_scope(c.cat)}."
        elif g:
            line = line[:-1] + f" — {g['text']}."
    cta = c.yes("Want me to draft a 2-line review request you can send to regulars today?",
                "Regulars ko bhejne ke liye 2-line review request draft kar doon?")
    body = c.greet() + line + " " + cta
    c.levers += ["goal gradient (close to milestone)", "effort externalization"]
    fu = {"offer": "draft a review request",
          "fulfill_en": f"Draft:\n\"Thank you for choosing {c.name}! If you liked your visit, a quick Google review helps us a lot — takes 30 seconds.\"\nReply CONFIRM and I'll send it to your recent {c.people}.",
          "done_en": "Sent. I'll ping you when you cross the milestone.",
          "details_en": line}
    return _result(c, "milestone", body, "binary_yes_no", [c.greet().strip(" ,."), line, cta], fu)


def k_review_theme(c: Ctx) -> dict:
    theme, occ, trend, quote = c.p.get("theme"), c.p.get("occurrences_30d"), c.p.get("trend"), c.p.get("common_quote")
    if not theme:
        neg = [r for r in c.m.get("review_themes") or [] if r.get("sentiment") == "neg"]
        if neg:
            theme, occ, quote = neg[0].get("theme"), neg[0].get("occurrences_30d"), neg[0].get("common_quote")
    if theme:
        c.anchor(f"theme {theme}", f"{occ} reviews/30d")
        line = f"{occ} reviews in the last 30 days mention {human(theme).replace('delivery late', 'late delivery')}"
        line += f", and it's {trend}" if trend else ""
        line += f" — one says \"{quote}\"." if quote else "."
        d, di = _num(c.agg.get("delivery_orders_30d")), _num(c.agg.get("dine_in_orders_30d"))
        if "deliver" in str(theme) and d and di:
            line += f" With delivery at {int(d)} of your last {int(d + di)} orders (~{round(100 * d / (d + di))}%), this one matters."
            c.anchor("delivery share")
        action = f"draft polite replies to all {occ} reviews + a short fix note for your team" if occ else "draft replies + a fix note"
    else:
        line = "a pattern is forming in your recent Google reviews."
        action = "pull your last 30 days of reviews and group the recurring themes"
    cta = c.yes(f"Want me to {action}?", "Sabhi reviews ke polite replies draft kar doon?")
    body = c.greet() + line + " " + cta
    c.levers += ["loss aversion (reputation)", "specificity (quote)", "effort externalization"]
    fu = {"offer": action,
          "fulfill_en": (f"Reply draft for those reviews:\n\"Thanks for the honest feedback — {human(theme or 'this')} isn't the experience we want for you. "
                         f"We've fixed it with our team; please give us another try and message us directly if anything's off.\"\n"
                         "Reply CONFIRM and I'll post it on each review."),
          "done_en": "Posted on all of them. I'll watch for new mentions.",
          "details_en": line}
    return _result(c, "review_theme", body, "binary_yes_no", [c.greet().strip(" ,."), line, cta], fu)


def k_dormant(c: Ctx) -> dict:
    days, topic = c.p.get("days_since_last_merchant_message"), c.p.get("last_topic")
    opener = (f"it's been {days} days since we last spoke{f' (about your {human(topic)})' if topic else ''}. No pitch — "
              if days else "it's been a while since we spoke. No pitch — ")
    if days:
        c.anchor(f"{days}d dormant")
    g = worst_gap(c.cat, c.m)
    if g:
        fact = "one number I thought you'd want: " + _gap_sentence(c, g) + "."
        action = "send the 3 fixes that close most of that gap"
    else:
        fact = (f"your listing got {group(c.pf.get('views', 0))} views and {group(c.pf.get('calls', 0))} calls in the last 30 days — "
                "there's room to convert more of those views.")
        action = "send the 3 quickest wins for your profile"
        c.anchor("30d perf")
    cta = c.yes(f"Want me to {action}?", "3 quick fixes bhej doon?")
    body = c.greet() + opener + fact + " " + cta
    c.levers += ["reciprocity", "curiosity", "low-commitment ask"]
    probs, fixes = _fixes_from_signals(c)
    fixes = (fixes + ["publish 2 fresh Google posts this week", "add 5 recent photos", "reply to your latest reviews"])[:3]
    fu = {"offer": action,
          "fulfill_en": "Here they are:\n" + "\n".join(f"{i+1}. {f[0].upper() + f[1:]}" for i, f in enumerate(fixes))
                        + "\nReply CONFIRM and I'll start on #1 today.",
          "done_en": "Started. I'll share progress here.",
          "details_en": fact}
    return _result(c, "dormant", body, "binary_yes_no", [c.greet().strip(" ,."), fact, cta], fu)


def k_winback(c: Ctx) -> dict:
    days, dip, lapsed = c.p.get("days_since_expiry"), _num(c.p.get("perf_dip_pct")), c.p.get("lapsed_customers_added_since_expiry")
    days = days or (c.m.get("subscription") or {}).get("days_since_expiry")
    parts = []
    if dip is not None:
        parts.append(f"views are down {pct(abs(dip))}")
    if lapsed:
        parts.append(f"{lapsed} more of your {c.people} have gone quiet")
    c.anchor(f"{days}d since expiry" if days else "", f"dip {dip}" if dip is not None else "", f"{lapsed} lapsed" if lapsed else "")
    since = f"since your magicpin plan lapsed {days} days ago" if days else "since your magicpin plan lapsed"
    line = f"{since}: {_join(parts)}." if parts else f"{since}, your listing has been running without support."
    hook = active_offers(c.m)[:1] or [catalog_pick(c.cat)]
    hook_s = f" — e.g. '{hook[0]}' as the hook" if hook and hook[0] else ""
    plan = f"a win-back message to those {lapsed}{hook_s}" if lapsed else f"a win-back message to your lapsed {c.people}{hook_s}"
    hook_hi = f" ('{hook[0]}' ke saath)" if hook and hook[0] else ""
    who_hi = f"un {lapsed} {c.people}" if lapsed else f"lapsed {c.people}"
    cta = c.yes(f"Restart and the first thing I'll do is {plan}. Want me to set it up?",
                f"Restart karein toh sabse pehle {who_hi} ko win-back message bhejungi{hook_hi}. Set up kar doon?")
    body = c.greet() + line + " " + cta
    c.levers += ["loss aversion", "specificity", "effort externalization"]
    fu = {"offer": "restart + win-back campaign",
          "fulfill_en": (f"Great — reactivating your plan and preparing the win-back:\n\"We miss you at {c.name}! "
                         + (f"{hook[0]} this month — " if hook and hook[0] else "")
                         + "reply to book your slot.\"\nReply CONFIRM to send it once the plan is active."),
          "done_en": "Queued. I'll report bookings from it here.",
          "details_en": line}
    return _result(c, "winback", body, "binary_yes_no", [c.greet().strip(" ,."), line, cta], fu)


def k_renewal(c: Ctx) -> dict:
    sub = c.m.get("subscription") or {}
    days = c.p.get("days_remaining") or sub.get("days_remaining")
    plan = c.p.get("plan") or sub.get("plan")
    amt = c.p.get("renewal_amount")
    c.anchor(f"{days}d left", f"plan {plan}", f"amount {amt}" if amt else "")
    if days and int(days) > 45:
        line = f"quick check-in on your {plan + ' ' if plan else ''}plan ({days} days left)."
        c.notes.append("renewal far out — framed as a value check-in, not a renewal push")
    else:
        line = f"your {plan + ' ' if plan else ''}plan is up for renewal in {days} days{f' ({inr(amt)})' if amt else ''}."
    recap = (f" Straight numbers, last 30 days: {group(c.pf.get('views', 0))} profile views, {group(c.pf.get('calls', 0))} calls"
             + (f", {group(c.pf['leads'])} leads" if c.pf.get("leads") is not None else "") + ".")
    dip = _delta_pick(c, -1)
    probs, fixes = _fixes_from_signals(c)
    if dip and dip[1] <= -0.15:
        recap += f" {dip[0].capitalize()} dipped {pct(abs(dip[1]))} this week."
    if probs:
        fix_s = _join(fixes[:2])
        tail = (f" Before you decide, I'd fix what's holding you back — {_join(probs[:2])}." if not (days and int(days) > 45)
                else f" One thing holding you back: {probs[0]}.")
        cta = c.yes(f"Want me to {fix_s} this week?", f"Fix: {fix_s}. Is hafte kar doon?")
        offer = fix_s
    else:
        tail = ""
        cta = c.yes("Want me to process the renewal so there's no gap in visibility?", "Renewal process kar doon taaki visibility mein gap na aaye?")
        offer = "process the renewal"
    body = c.greet() + line + recap + tail + " " + cta
    c.levers += ["transparency (value recap)", "loss aversion"]
    fu = {"offer": offer,
          "fulfill_en": ("Starting now:\n" + "\n".join(f"{i+1}. {f[0].upper() + f[1:]}" for i, f in enumerate(fixes[:3]))
                         + "\nI'll report the impact before your renewal date.") if probs else
                        f"Done — renewal request raised for your {plan or ''} plan{f' ({inr(amt)})' if amt else ''}. You'll get the payment confirmation on this number.",
          "done_en": "Done. Thanks for staying with us.",
          "details_en": line + recap}
    return _result(c, "renewal", body, "binary_yes_no", [c.greet().strip(" ,."), line, cta], fu)


FESTIVE_OFFER_KWS = {"salons": ["bridal", "spa"], "restaurants": ["family", "party", "brunch"],
                     "gyms": ["first month", "couple"], "pharmacies": ["diabetic", "health card"],
                     "dentists": ["whitening", "cleaning"]}


def k_festival(c: Ctx) -> dict:
    fest, date_s, days = c.p.get("festival"), c.p.get("date"), c.p.get("days_until")
    if days is None and date_s and c.now:
        days = days_between(c.now, date_s)
    beat = seasonal_beat(c.cat, keywords=([fest.lower()] if fest else []) + ["festival", "wedding"])
    offer = None
    for kw in FESTIVE_OFFER_KWS.get(c.slug, []):  # merchant's own offer first, then catalog, per keyword
        offer = _offer_matching(active_offers(c.m), [kw]) or catalog_pick(c.cat, [kw], avoid_discount=True)
        if offer and kw.lower() in offer.lower():
            break
        offer = None
    offer = offer or _hero_offer(c)
    season = (f" {beat_sentence(beat, c.slug)}." if beat else "")
    if beat:
        c.anchor(f"seasonal beat {beat['month_range']}")
    if fest:
        c.anchor(f"{fest} {date_s}", f"{days} days")
        if days is not None and days > 45:
            line = f"{fest} is on {nice_date(date_s)} — {days} days out, so it's too early for a discount." + season
            c.notes.append("too early for a promo — planning frame instead")
            act_en = f"set up an early-bird '{offer}' post now to start collecting {fest}-season enquiries" if offer else f"plan your {fest} calendar now"
            act_hi = f"abhi se early-bird '{offer}' post daal doon" if offer else f"{fest} plan abhi bana loon"
        else:
            line = f"{fest} is {('in ' + str(days) + ' days') if days is not None else 'coming up'} ({nice_date(date_s)})." + season
            act_en = f"push '{offer}' as a festive post today" if offer else "draft a festive post today"
            act_hi = f"aaj hi '{offer}' festive post daal doon" if offer else "aaj festive post daal doon"
    else:
        line = ("festival season is coming up —" + season) if season else "festival season is coming up."
        act_en = f"draft a 'before the festivals' message for your lapsed {c.people} with '{offer}' as the hook" if offer else "draft a festive message for your regulars"
        act_hi = f"lapsed {c.people} ke liye '{offer}' wala festive message draft kar doon" if offer else "festive message draft kar doon"
    cta = c.yes(f"Want me to {act_en}?", f"{act_hi[0].upper() + act_hi[1:]}?")
    body = c.greet() + line + " " + cta
    c.levers += ["timing / why-now", "specificity", "effort externalization"]
    fu = {"offer": act_en,
          "fulfill_en": f"Draft post:\n\"{(fest + ' at ' + c.name) if fest else 'Festive season at ' + c.name} — {offer or 'book early'}. Limited slots, message us to reserve.\"\nReply CONFIRM to publish on Google + WhatsApp.",
          "done_en": "Published. I'll share enquiry counts weekly.",
          "details_en": line}
    return _result(c, "festival", body, "binary_yes_no", [c.greet().strip(" ,."), line, cta], fu)


def k_ipl(c: Ctx) -> dict:
    match, venue, iso, weeknight = c.p.get("match"), c.p.get("venue"), c.p.get("match_time_iso"), c.p.get("is_weeknight")
    t = _clock(iso)
    c.anchor(f"{match} {venue} {t}")
    line = f"{match} tonight{' at ' + venue if venue else ''}{', ' + t if t else ''}."
    dg = next((d for d in c.cat.get("digest") or [] if "ipl" in str(d.get("title", "")).lower()), None)
    offers = active_offers(c.m)
    d_, di = _num(c.agg.get("delivery_orders_30d")), _num(c.agg.get("dine_in_orders_30d"))
    combo = catalog_pick(c.cat, ["match-night", "match"])
    if weeknight is False:
        c.notes.append("weekend match — contrarian: skip dine-in promo, push delivery")
        caution = " One caution: it's a weekend game"
        nums = re.search(r"covers down (\d+%)[^.]*\.\s*Weeknight matches drive \+?(\d+%)", (dg or {}).get("summary", ""))
        if dg and nums:
            caution += (f", and per {src_fmt(dg.get('source'))}, Saturday IPL games cut restaurant covers ~{nums.group(1)} "
                        f"while weeknight games add {nums.group(2)} — so I'd skip a dine-in match promo.")
        elif dg:
            caution += f", and {src_fmt(dg.get('source'))} shows weekend IPL matches underperform weeknight ones — so I'd skip a dine-in match promo."
        else:
            caution += " — weekend matches usually pull people home, so I'd skip a dine-in promo."
        if dg:
            c.anchor(f"digest {dg.get('id')}")
        tue = next((o for o in offers if re.search(r"tue|wed|thu", o, re.I)), None)
        extra = f" Your '{tue}' doesn't run tonight" if tue else ""
        if d_ and di:
            extra += (" and" if extra else " Also,") + f" delivery is already {int(d_)} of your last {int(d_ + di)} orders"
            c.anchor("delivery vs dine-in")
        extra += " — so tonight, push delivery." if extra else " Tonight, push delivery."
        act_en = f"put up a delivery-only '{combo}' post before 6pm" if combo else "put up a delivery-only match-night post before 6pm"
        act_hi = f"6 baje se pehle delivery-only '{combo}' post daal doon" if combo else "6 baje se pehle delivery post daal doon"
        line += caution + extra
    else:
        act_en = f"push a '{combo}' match-night post now" if combo else "push a match-night post now"
        act_hi = f"abhi '{combo}' match-night post daal doon" if combo else "abhi match-night post daal doon"
        line += " Weeknight matches are your best window for a match-night promo."
    cta = c.yes(f"Want me to {act_en}?", f"{act_hi[0].upper() + act_hi[1:]}?")
    body = c.greet() + line + " " + cta
    c.levers += ["contrarian judgement (saves a bad promo)", "specificity", "time-bound effort"]
    fu = {"offer": act_en,
          "fulfill_en": f"Post draft:\n\"{match} tonight 🏏 Don't miss a ball — {combo or 'match-night combo'} delivered hot to your door. Order before {t or 'the toss'}!\"\nReply CONFIRM to publish on Google + your delivery listings.",
          "done_en": "Live. I'll share tonight's order count tomorrow morning.",
          "details_en": line}
    return _result(c, "ipl", body, "binary_yes_no", [c.greet().strip(" ,."), line, cta], fu)


def _planning_draft(c: Ctx, topic: str) -> tuple[str, str]:
    """(draft text, short label) for an active planning intent, built from contexts."""
    tl = topic.lower()
    offers = active_offers(c.m)
    if any(k in tl for k in ("thali", "corporate", "bulk", "catering")):
        base_offer = _offer_matching(offers, ["thali", "lunch"]) or (offers[0] if offers else None)
        base = _price_in(base_offer)
        bv = _num(base.replace("₹", "").replace(",", "")) if base else None
        per_day = _hist_number(c.m, r"(\d+)\s*orders?\s*/\s*day")
        lbl = "corporate thali package"
        if bv:
            t1, t2, t3 = (int(round(bv * f / 5.0) * 5) for f in (0.9, 0.84, 0.8))
            c.anchor(f"base offer {base_offer}")
            draft = (f"• 10–24 thalis: ₹{t1} each\n• 25–49: ₹{t2} each\n• 50+: ₹{t3} each + free delivery\n"
                     f"• Order by 11am, delivered 12:30–1:30pm")
            intro = f"here's a first cut of the {lbl} — built off your {base} {base_offer.split('@')[0].strip().lower()}"
            if per_day:
                intro += f" ({per_day} orders/day now)"
                c.anchor(f"{per_day} orders/day")
            return intro + ":\n" + draft, lbl
        return (f"here's a first cut of the {lbl}:\n• 3 tiers by daily volume (10+, 25+, 50+), lower price per plate at each tier\n"
                f"• Order cut-off 11am, delivery 12:30–1:30pm"), lbl
    if any(k in tl for k in ("kid", "yoga", "camp", "program", "class", "batch")):
        hist_txt = " ".join(h.get("body", "") for h in c.m.get("conversation_history") or [] if h.get("from") == "vera")
        weeks = (re.search(r"(\d+)[- ]week", hist_txt) or [None, "4"])[1]
        per_wk = (re.search(r"(\d+)\s*classes?/week", hist_txt) or [None, "3"])[1]
        ages = (re.search(r"age[sd]?\s*(\d+\s*[-–]\s*\d+)", hist_txt) or [None, "7–12"])[1].replace("-", "–")
        price = _price_in(hist_txt)
        if price:
            c.anchor("plan numbers from earlier Vera suggestion")
        lbl = human(topic).replace("summer camp", "summer camp")
        draft = (f"• Ages {ages} · {weeks} weeks · {per_wk} classes/week, 45 min each\n"
                 f"• Fee: {price + ' for the full camp' if price else 'your call — I can suggest a price'}\n"
                 f"• Free trial class on the first Saturday morning\n• Small batches so each child gets attention")
        return f"here's the {lbl}, ready to publish:\n" + draft, lbl
    lbl = human(topic)
    hook = offers[0] if offers else catalog_pick(c.cat)
    return (f"here's a starter plan for the {lbl}:\n• What's included + who it's for\n"
            f"• Price anchored to your existing {('`' + hook + '`') if hook else 'menu'}\n"
            f"• Launch: Google post + WhatsApp to your regular {c.people}"), lbl


def k_planning(c: Ctx) -> dict:
    topic = str(c.p.get("intent_topic") or "plan")
    last = c.p.get("merchant_last_message")
    draft, lbl = _planning_draft(c, topic)
    c.anchor(f"merchant asked: {last}" if last else "planning intent")
    c.notes.append("merchant already asked — delivering the draft, not qualifying")
    cta = c.yes("Edit anything you like. Want me to publish it as a Google post + a WhatsApp you can forward?",
                "Jo badalna ho bata dijiye. Google post + forwardable WhatsApp bana doon?")
    body = c.greet() + draft + "\n" + cta
    c.levers += ["effort externalization (complete draft)", "momentum on stated intent"]
    fu = {"offer": f"publish the {lbl}",
          "fulfill_en": f"Done — the {lbl} is drafted as a Google post + a forwardable WhatsApp. Reply CONFIRM and it goes live; I'll track enquiries for you.",
          "done_en": "Live. I'll send enquiry counts every Friday.",
          "details_en": draft}
    return _result(c, "planning", body, "binary_yes_no", [c.greet().strip(" ,."), draft, cta], fu)


def k_curious_ask(c: Ctx) -> dict:
    offers = active_offers(c.m)
    if offers:
        opts = " or ".join(offers[:2])
        c.anchor("active offers")
        q_en = f"what's been the most-asked {('service' if c.slug in ('salons', 'dentists', 'gyms') else 'item')} at {c.name} this week? {opts}, or something else?"
    else:
        q_en = f"what's been the most-asked {('service' if c.slug in ('salons', 'dentists', 'gyms') else 'item')} at {c.name} this week?"
    payoff = "Tell me and I'll turn it into a Google post + a ready reply for price enquiries on WhatsApp — 30 seconds from you."
    if c.lang == "hi-en":
        payoff = "Bata dijiye — main usse Google post + WhatsApp price-reply bana dungi. Aapke bas 30 second."
    body = c.greet() + "quick one — " + q_en + " " + payoff
    c.levers += ["asking the merchant", "reciprocity", "low effort"]
    fu = {"offer": "turn the top service into a Google post + price reply",
          "fulfill_en": "Got it — drafting a Google post on that + a 4-line WhatsApp reply for price questions. I'll send both here in a few minutes; reply CONFIRM to publish.",
          "done_en": "Published. I'll ask again next week.",
          "details_en": q_en}
    return _result(c, "curious_ask", body, "open_ended", [c.greet().strip(" ,."), q_en, payoff], fu)


def k_category_seasonal(c: Ctx) -> dict:
    trends = c.p.get("trends") or []
    parsed = []
    for t in trends:
        m = re.match(r"(.+?)_demand_([+-]\d+)", str(t))
        if m:
            parsed.append(f"{human(m.group(1))} {m.group(2)}%".replace("+", "+").replace("cold cough", "cold & cough"))
    dg = digest_of_kind(c.cat, ["seasonal"])
    src = f" ({dg.get('source')})" if dg else ""
    season = human(c.p.get("season") or "seasonal").replace(" 2026", "")
    if parsed:
        c.anchor("trend list")
        line = f"the {season} demand shift is showing up across {c.slug}{src}: {', '.join(parsed)}."
    else:
        beat = seasonal_beat(c.cat, c.now.month if c.now else None)
        line = (f"{beat['month_range']}: {beat['note']}." if beat else "seasonal demand is shifting.")
    ups = [p.split(" +")[0] for p in parsed if "+" in p]
    downs = [p.split(" -")[0] for p in parsed if " -" in p]
    shelf = ""
    if c.p.get("shelf_action_recommended") and ups:
        shelf = f" Worth moving {_join(ups[:2])} to the counter" + (f" and trimming {downs[0]} reorders" if downs else "") + " this month."
    tot = c.agg.get("total_unique_ytd")
    deliv = _offer_matching(active_offers(c.m), ["delivery"])
    act = f"draft a '{season.split()[0].capitalize()} essentials' Google post + WhatsApp for your {group(tot) + ' ' if tot else ''}{c.people}"
    if deliv:
        act += f" (with '{deliv}')"
    cta = c.yes(f"Want me to {act}?", f"{season.split()[0].capitalize()} essentials post + WhatsApp draft kar doon?")
    body = c.greet() + line + shelf + " " + cta
    c.levers += ["specificity (demand deltas)", "effort externalization"]
    fu = {"offer": act,
          "fulfill_en": f"Draft:\n\"{season.split()[0].capitalize()} essentials at {c.name}: {', '.join(ups[:3]) or 'seasonal care'} in stock" + (f" — {deliv}" if deliv else "") + ". Reply to order.\"\nReply CONFIRM to publish + send.",
          "done_en": "Sent. I'll share orders from it next week.",
          "details_en": line + shelf}
    return _result(c, "category_seasonal", body, "binary_yes_no", [c.greet().strip(" ,."), line, cta], fu)


def k_gbp_unverified(c: Ctx) -> dict:
    path, upl = c.p.get("verification_path"), _num(c.p.get("estimated_uplift_pct"))
    c.anchor("unverified GBP", f"uplift est {upl}" if upl is not None else "")
    line = f"{c.name} is still unverified on Google"
    line += f" — verifying is estimated to lift your visibility ~{pct(upl)}." if upl else "."
    how = f" Verification is by {human(path).replace(' or ', ' or ')}; I'll start it and walk you through — about 5 minutes from your side." if path else " I'll start verification and walk you through it — about 5 minutes from your side."
    cta = pick(c.lang, "Shall I start? Reply YES.", "Shuru karein? Bas YES reply karein.")
    body = c.greet() + line + how + " " + cta
    c.levers += ["loss aversion", "effort externalization", "binary CTA"]
    fu = {"offer": "start Google verification",
          "fulfill_en": f"Started. Step 1: confirm the business address + phone on your listing (reply with any correction). Step 2: Google sends a {('code by ' + human(path)) if path else 'verification code'} — share it here and I'll finish the rest.",
          "done_en": "Verification submitted. I'll tell you the moment it's approved.",
          "details_en": line + how}
    return _result(c, "gbp_unverified", body, "binary_yes_no", [c.greet().strip(" ,."), line, cta], fu)


def k_competitor(c: Ctx) -> dict:
    name, dist, their, opened = c.p.get("competitor_name"), c.p.get("distance_km"), c.p.get("their_offer"), c.p.get("opened_date")
    offers = active_offers(c.m)
    if name:
        c.anchor(f"competitor {name} {dist}km", their or "")
        line = f"a new {c.place_noun} — {name} — opened {dist} km away" + (f" on {nice_date(opened)}" if opened else "")
        if their:
            mine = _offer_matching(offers, [w for w in re.split(r"\W+", their.split("@")[0]) if len(w) > 3])
            line += f", advertising {their}" + (f" vs your {_price_in(mine)}" if mine and _price_in(mine) else "") + "."
            if mine:
                c.notes.append("don't race on price — defend on quality")
        else:
            line += "."
        pos = next((r for r in c.m.get("review_themes") or [] if r.get("sentiment") == "pos"), None)
        if pos:
            defend = f" Don't race them on price: your reviews already praise {human(pos['theme']).replace('doctor manner', 'how you explain things')} ({pos.get('occurrences_30d')} mentions this month)."
            c.anchor(f"pos review theme {pos['theme']}")
        else:
            defend = " Price-matching rarely wins; showing what's included in yours does."
        act = f"post a 'what's included in our {_price_in(mine) if their and mine and _price_in(mine) else ''} {(mine or 'service').split('@')[0].strip().lower()}' explainer to defend your price" if their else "post a profile refresh that shows your strengths"
        act = re.sub(r"\s{2,}", " ", act)
    else:
        line = f"a new competitor has opened near you{(' in ' + c.loc) if c.loc else ''}."
        defend = (f" You have the head start — {group(c.pf.get('views', 0))} views and {group(c.pf.get('calls', 0))} calls on your listing in the last 30 days.")
        act = "pull their listing + offers so you can compare side by side"
        c.anchor("30d perf")
    cta = c.yes(f"Want me to {act}?", "Explainer post draft kar doon?" if name else "Unki listing + offers nikaal ke compare kar doon?")
    body = c.greet() + line + defend + " " + cta
    c.levers += ["loss aversion", "specificity", "judgement (don't discount)"]
    fu = {"offer": act,
          "fulfill_en": f"Draft:\n\"At {c.name}, every {('cleaning' if c.slug == 'dentists' else 'visit')} includes a full check by the {('dentist' if c.slug == 'dentists' else 'team')} and clear advice on what you actually need — no upselling.\"\nReply CONFIRM to publish it on Google.",
          "done_en": "Published. I'll watch your calls vs last month.",
          "details_en": line + defend}
    return _result(c, "competitor", body, "binary_yes_no", [c.greet().strip(" ,."), line, cta], fu)


def k_trend(c: Ctx) -> dict:
    q = c.p.get("query")
    tr = next((t for t in c.cat.get("trend_signals") or [] if t.get("query") == q), None) if q else None
    tr = tr or top_trend(c.cat)
    if not tr:
        return k_generic(c)
    c.anchor(f"trend {tr['query']} {tr.get('delta_yoy')}")
    line = f"'{tr['query']}' searches are up {pct(tr.get('delta_yoy'))} YoY" + (f" (mostly {human(tr['segment_age'])})" if tr.get("segment_age") else "") + "."
    off = catalog_pick(c.cat, [w for w in tr["query"].split() if len(w) > 4])
    act = f"put '{off}' on your profile to catch that demand" if off else "post about it this week"
    cta = c.yes(f"Want me to {act}?", "Is demand ke liye post daal doon?")
    body = c.greet() + line + " " + cta
    c.levers += ["curiosity", "specificity"]
    fu = {"offer": act, "fulfill_en": f"Draft ready for '{off or tr['query']}'. Reply CONFIRM to publish.",
          "done_en": "Live.", "details_en": line}
    return _result(c, "trend", body, "binary_yes_no", [c.greet().strip(" ,."), line, cta], fu)


def k_generic(c: Ctx) -> dict:
    facts = []
    for k, v in (c.p or {}).items():
        if k in ("placeholder", "metric_or_topic", "category") or isinstance(v, (dict, list)) or v in (None, ""):
            continue
        facts.append(f"{human(k)}: {v}")
    head = f"{human(c.kind)}" + (f" — {'; '.join(facts[:3])}" if facts else "")
    g = worst_gap(c.cat, c.m) or best_gap(c.cat, c.m)
    line = f"flagging {head}."
    if g:
        line += " Also, " + _gap_sentence(c, g) + "."
    probs, fixes = _fixes_from_signals(c)
    act = fixes[0] if fixes else "draft a Google post to act on this"
    cta = c.yes(f"Want me to {act}?", "Main shuru kar doon?")
    body = c.greet() + line + " " + cta
    c.levers += ["specificity", "effort externalization"]
    fu = {"offer": act, "fulfill_en": f"On it — {act}. I'll share the draft here; reply CONFIRM to publish.",
          "done_en": "Done.", "details_en": line}
    return _result(c, "generic", body, "binary_yes_no", [c.greet().strip(" ,."), line, cta], fu)


# =============================================================================
# customer-facing composers (send_as = merchant_on_behalf)
# =============================================================================

def _cpick(c: Ctx, en: str, hi_en: str, hi: Optional[str] = None) -> str:
    if c.clang == "hi":
        return hi or hi_en
    if c.clang == "hi-en":
        return hi_en
    return en


def _slots(c: Ctx) -> list[str]:
    out = []
    for s in c.p.get("available_slots") or c.p.get("next_session_options") or []:
        lbl = s.get("label") if isinstance(s, dict) else str(s)
        if not lbl and isinstance(s, dict):
            d = parse_dt(s.get("iso"))
            lbl = d.strftime("%-d %b, ") + _clock(s.get("iso")) if d else None
        if lbl:
            out.append(lbl)
    return out


def _slot_cta(c: Ctx, slots: list[str]) -> tuple[str, str]:
    if len(slots) >= 2:
        return (_cpick(c, "Reply 1 or 2 — or tell us a time that works.",
                       "1 ya 2 reply karein — ya jo time suit kare, batayein."), "multi_choice_slot")
    if len(slots) == 1:
        return (_cpick(c, f"Reply YES to book {slots[0]}, or tell us another time.",
                       f"{slots[0]} book karne ke liye YES reply karein, ya koi aur time batayein."), "binary_yes_no")
    return (_cpick(c, "Reply YES and we'll hold a slot for you this week.",
                   "YES reply karein, hum is hafte aapke liye slot rakh denge."), "binary_yes_no")


def _cust_fu(c: Ctx, offer: str, slots: list[str]) -> dict:
    conf = ("Booked ✅ {slot} — see you then! Reply here if you need to change anything." if slots else
            _cpick(c, "Great — we'll keep a slot for you this week and confirm the exact time here shortly. 🙏",
                   "Badhiya — is hafte aapke liye slot rakhte hain, exact time yahin jaldi confirm karenge. 🙏"))
    return {"offer": offer, "slots": slots,
            "fulfill_en": conf,
            "done_en": "Thank you! See you soon.",
            "details_en": offer}


CUST_NUDGE = {  # (en, hindi) low-pressure line when we have no offer/slot to anchor on
    "dentists": ("A quick check-up now keeps small issues small.", "Ek quick check-up se chhoti problem chhoti hi rehti hai."),
    "salons": ("Due for a refresh?", "Refresh ka time ho gaya?"),
    "gyms": ("Your spot's still here whenever you're ready.", "Aapki jagah abhi bhi yahin hai, jab chahein."),
    "pharmacies": ("Need a refill or anything for home?", "Koi dawai ya refill chahiye?"),
    "restaurants": ("Your favourites are still on the menu.", "Aapki favourite dishes abhi bhi menu par hain."),
}
CUST_CTA = {
    "pharmacies": ("Reply here and we'll keep it ready for you.", "Bas reply karein, hum ready rakh denge."),
    "restaurants": ("Reply YES and we'll reserve a table for you.", "YES reply karein, hum table rakh denge."),
}


def kc_recall(c: Ctx) -> dict:
    cu = c.cu or {}
    rel = cu.get("relationship") or {}
    service = c.p.get("service_due")
    last = c.p.get("last_service_date") or rel.get("last_visit")
    due = c.p.get("due_date")
    slots = _slots(c)
    if service:
        svc = re.sub(r"(\d+)\s*month", r"\1-month", human(service))
    else:
        svc = {"dentists": "check-up", "salons": "next appointment", "gyms": "next session",
               "pharmacies": "follow-up", "restaurants": "next visit"}.get(c.slug, "next visit")
    c.anchor(f"service {svc}", f"last {last}", f"due {due}" if due else "", f"{len(slots)} slots" if slots else "")
    offers = active_offers(c.m)
    offer = _offer_matching(offers, [w for w in (service or "").split("_") if len(w) > 3]) or (offers[:1] or [None])[0]
    pref = str((cu.get("preferences") or {}).get("preferred_slots") or "")
    evening = slots and all(re.search(r"\b([5-9]|1[01])\s*(:\d\d)?\s*pm", s, re.I) for s in slots)
    emoji = {"dentists": " 🦷", "gyms": " 🙏"}.get(c.slug, "")
    if service or c.slug == "dentists":
        lead = _cpick(c, f"your {svc} is due" + (f" — your last visit was on {nice_date(last)}" if last else "") + ".",
                      f"aapki {svc} due hai" + (f" — last visit {nice_date(last)} ko tha" if last else "") + ".")
    else:
        lead = _cpick(c, f"it's been a while since your last visit{(' on ' + nice_date(last)) if last else ''} — time for your {svc}.",
                      f"last visit{(' (' + nice_date(last) + ')') if last else ''} ke baad kaafi time ho gaya — {svc} ka time aa gaya hai.")
    slot_s = ""
    if slots:
        listed = _cpick(c, " or ", " ya ").join(f"{i + 1}) {s}" for i, s in enumerate(slots[:2])) if len(slots) > 1 else slots[0]
        if "evening" in pref and evening:
            c.anchor("pref weekday_evening honoured")
            slot_s = _cpick(c, f" We've kept weekday-evening slots for you: {listed}.",
                            f" Aapke liye weekday evening slots rakhe hain: {listed}.")
        else:
            slot_s = _cpick(c, f" Slots we've kept for you: {listed}.", f" Aapke liye slots: {listed}.")
    price = ""
    if offer:
        price = _cpick(c, f" {offer} — same as always." if service else f" {offer} is running right now.",
                       f" {offer} wala offer chal raha hai.")
    elif not slots:
        price = " " + _cpick(c, *CUST_NUDGE.get(c.slug, ("", "")))
    cta, ctype = _slot_cta(c, slots)
    if not slots and c.slug in CUST_CTA:
        cta = _cpick(c, *CUST_CTA[c.slug])
    body = c.cgreet() + c.csign() + (emoji.strip() + " " if emoji else "") + lead[0].upper() + lead[1:] + slot_s + price + " " + cta
    c.levers += ["personal timing", "specific slots" if slots else "low-pressure nudge", "low-friction reply"]
    return _result(c, "c_recall", re.sub(r"\s{2,}(?!\d\))", " ", body), ctype,
                   [customer_first(cu), c.name, lead, " / ".join(slots), offer or ""],
                   _cust_fu(c, svc, slots), send_as="merchant_on_behalf")


def kc_appointment(c: Ctx) -> dict:
    t = c.p.get("appointment_time") or c.p.get("time") or c.p.get("slot")
    svc = c.p.get("service")
    when = f"tomorrow{(' at ' + (_clock(t) or str(t))) if t else ''}"
    noun = {"restaurants": "table reservation", "gyms": "session", "pharmacies": "pickup"}.get(c.slug, "appointment")
    c.anchor("appointment tomorrow", f"time {t}" if t else "no time in payload — not invented")
    lead = _cpick(c, f"a reminder about your {human(svc) + ' ' if svc else ''}{noun} {when}.",
                  f"kal aapka {human(svc) + ' ' if svc else ''}{noun} hai{(' — ' + (_clock(t) or str(t))) if t else ''}.")
    cta = _cpick(c, "Reply YES to confirm, or tell us if you need a different time.",
                 "Confirm karne ke liye YES reply karein, ya time badalna ho toh batayein.")
    body = c.cgreet() + c.csign() + lead[0].upper() + lead[1:] + " " + cta
    c.levers += ["reminder", "binary confirm"]
    fu = _cust_fu(c, "appointment", [])
    fu["fulfill_en"] = _cpick(c, "Confirmed ✅ See you tomorrow! Reply here if anything changes.",
                              "Confirmed ✅ Kal milte hain! Kuch badle toh yahin bata dijiye.")
    return _result(c, "c_appointment", body, "binary_yes_no", [customer_first(c.cu or {}), c.name, lead, cta],
                   fu, send_as="merchant_on_behalf")


def kc_refill(c: Ctx) -> dict:
    mols = c.p.get("molecule_list") or []
    if c.slug != "pharmacies" or not mols:
        c.notes.append(f"refill trigger on a {c.slug} merchant without molecules — sent as a neutral follow-up")
        return kc_recall(c)
    cu = c.cu or {}
    runs_out = c.p.get("stock_runs_out_iso")
    raw = customer_first(cu)
    surname = re.sub(r"^(mr|mrs|ms|shri|smt|dr)\.?\s+", "", raw, flags=re.I)
    via = "via_" in str((cu.get("preferences") or {}).get("channel") or "")
    c.anchor(f"molecules {', '.join(mols)}", f"runs out {str(runs_out)[:10]}")
    offers = active_offers(c.m)
    senior = _offer_matching(offers, ["senior"]) if (cu.get("identity") or {}).get("senior_citizen") else None
    deliv = _offer_matching(offers, ["delivery"])
    addr = c.p.get("delivery_address_saved") or (cu.get("preferences") or {}).get("delivery_address") == "saved"
    recall = next((d for d in c.cat.get("digest") or []
                   if (d.get("kind") == "alert" or "recall" in str(d.get("title", "")).lower())
                   and any(m.lower() in str(d.get("title", "")).lower() for m in mols)), None)
    rmol = next((m for m in mols if recall and m.lower() in recall.get("title", "").lower()), None)
    mol_s = ", ".join(mols)
    dmin = re.search(r"₹[\d,]+", deliv or "")
    if c.clang in ("hi", "hi-en"):
        who = f"{surname} ji ki" if (via or surname != raw) else "aapki"
        lead = f"{who} {len(mols)} monthly medicines ({mol_s}) {nice_date(runs_out)} tak khatam ho jayengi. Same dose ka pack ready kar sakte hain."
        perks = []
        if senior:
            perks.append("senior citizen 15% off lagega" if "15%" in senior else f"{senior} lagega")
        if deliv:
            perks.append((f"{dmin.group(0)} se upar free home delivery" if dmin else "free home delivery") + (" saved address par" if addr else ""))
        perk_s = (" " + ", aur ".join(perks).capitalize() + ".") if perks else ""
        rc = f" {rmol.capitalize()} ke recall wale batches is pack mein nahi honge." if rmol else ""
        cta = "Dispatch ke liye CONFIRM reply karein; dose badli ho toh batayein."
    else:
        who = f"{raw}'s" if via else "your"
        lead = f"{who} {len(mols)} monthly medicines ({mol_s}) run out on {nice_date(runs_out)}. We can keep the same-dose pack ready."
        perks = [p for p in [senior, (deliv + (' to your saved address' if addr else '')) if deliv else None] if p]
        perk_s = (" " + " · ".join(perks) + ".") if perks else ""
        rc = f" We'll make sure no {rmol} from the batches under the current recall goes into the pack." if rmol else ""
        cta = "Reply CONFIRM to dispatch, or tell us if the dose has changed."
    if rmol:
        c.notes.append(f"cross-checked the {rmol} recall in the category digest")
    body = c.cgreet() + c.csign() + lead + perk_s + rc + " " + cta
    c.levers += ["precision (molecules + date)", "convenience", "single CONFIRM"]
    fu = _cust_fu(c, "refill", [])
    fu["fulfill_en"] = "Confirmed ✅ Same-dose pack is being packed" + (" — delivery to your saved address by tomorrow." if addr else " — ready for pickup tomorrow.")
    return _result(c, "c_refill", body, "binary_confirm_cancel", [raw, c.name, mol_s, nice_date(runs_out) or ""], fu,
                   send_as="merchant_on_behalf")


def kc_lapsed(c: Ctx) -> dict:
    cu = c.cu or {}
    rel, prefs = cu.get("relationship") or {}, cu.get("preferences") or {}
    days = c.p.get("days_since_last_visit")
    if days is None and c.now and rel.get("last_visit"):
        d = days_between(rel.get("last_visit"), c.now)
        days = d if d and d > 0 else None
    focus = c.p.get("previous_focus") or prefs.get("training_focus")
    offers = active_offers(c.m)
    off = offers[0] if offers else None
    last = rel.get("last_visit")
    if days and days < 120:
        since_en = f"it's been about {max(1, round(days / 7))} weeks since your last visit"
        since_hi = f"last visit ko lagbhag {max(1, round(days / 7))} hafte ho gaye"
    else:
        since_en = f"it's been a while since your last visit{(' on ' + nice_date(last)) if last else ''}"
        since_hi = f"last visit{(' (' + nice_date(last) + ')') if last else ''} ke baad kaafi time ho gaya"
    c.anchor(f"{days}d since visit" if days else f"last visit {last}", f"focus {focus}" if focus else "",
             f"offer {off}" if off else "")
    warm = c.slug in ("gyms", "salons")
    nudge_en, nudge_hi = CUST_NUDGE.get(c.slug, ("", ""))
    if c.clang in ("hi", "hi-en"):
        mid = (" — koi baat nahi, aisa sabke saath hota hai." if warm else ".") + (f" {off} chal raha hai." if off else f" {nudge_hi}")
        cta = (CUST_CTA.get(c.slug) or (None, "YES reply karein, hum is hafte aapke liye slot rakh denge."))[1]
        body = c.cgreet() + c.csign() + since_hi[0].upper() + since_hi[1:] + mid + " " + cta
    else:
        mid = " — happens to everyone, no judgment." if warm else "."
        hook = ""
        if focus and off:
            hook = f" Since {human(focus)} was your focus, an easy restart: {off}"
        elif off:
            hook = f" An easy way back: {off}"
        if hook:
            hook += " — on weekday evenings, your usual time." if "evening" in str(prefs.get("preferred_slots") or "") else "."
        else:
            hook = f" {nudge_en}" if nudge_en else ""
        free = bool(off and "free" in off.lower())
        if off:
            cta = "Want me to block the first one this week? Reply YES" + (" — no charge." if free else ".")
        else:
            cta = (CUST_CTA.get(c.slug) or ("Reply YES and we'll hold a slot for you this week.",))[0]
        emoji = " 👋" if warm else ""
        body = c.cgreet() + c.csign().strip() + emoji + " " + since_en[0].upper() + since_en[1:] + mid + hook + " " + cta
    c.levers += ["no-shame warmth" if warm else "low-pressure check-in", "goal recall" if focus else "relationship continuity",
                 "free restart" if off and "free" in off.lower() else "single reply"]
    return _result(c, "c_lapsed", body, "binary_yes_no", [customer_first(cu), c.sender(), since_en, off or ""],
                   _cust_fu(c, off or "a slot this week", []), send_as="merchant_on_behalf")


def kc_trial(c: Ctx) -> dict:
    cu = c.cu or {}
    trial = c.p.get("trial_date") or (cu.get("relationship") or {}).get("last_visit")
    slots = _slots(c)
    child = customer_subject(cu)
    default_svc = {"gyms": "trial class", "salons": "first visit", "dentists": "consultation",
                   "pharmacies": "first order", "restaurants": "first visit"}.get(c.slug, "trial")
    svc = human(((cu.get("relationship") or {}).get("services_received") or [default_svc])[-1])
    c.anchor(f"trial {trial}", f"next {slots[:1]}")
    who = f"{child} enjoyed his {svc}" if child else f"you enjoyed your {svc}"
    who = who.replace("his kids yoga trial", "his kids yoga trial class")
    lead = f"hope {who}{(' on ' + nice_date(trial)) if trial else ''}."
    pref = str((cu.get("preferences") or {}).get("preferred_slots") or "")
    nxt = ""
    if slots:
        nxt = f" Next session: {slots[0]}"
        if "saturday" in pref and "sat" in slots[0].lower():
            nxt += " — your preferred Saturday-morning slot" if "morning" in pref else " — your preferred Saturday"
        nxt += "."
    cta = (f"Shall we book {'him' if child else 'you'} in? Reply YES." if slots else
           (CUST_CTA.get(c.slug) or ("Reply YES and we'll book the next session.",))[0])
    body = c.cgreet() + c.csign().strip() + " 🙏 " + lead[0].upper() + lead[1:] + nxt + " " + cta
    c.levers += ["continuity from trial", "specific next slot", "binary CTA"]
    return _result(c, "c_trial", body, "binary_yes_no", [customer_first(cu), c.name, lead, slots[0] if slots else ""],
                   _cust_fu(c, "next session", slots), send_as="merchant_on_behalf")


def kc_bridal(c: Ctx) -> dict:
    from datetime import timedelta
    cu = c.cu or {}
    wd = c.p.get("wedding_date") or (cu.get("preferences") or {}).get("wedding_date")
    days = c.p.get("days_to_wedding")
    trial = c.p.get("trial_completed")
    step = human(c.p.get("next_step_window_open") or "skin prep program").replace("program 30day", "program").replace("30day", "30-day")
    if "30" in str(c.p.get("next_step_window_open")) and "30-day" not in step:
        step = "30-day " + step.replace(" 30day", "")
    c.anchor(f"wedding {wd}", f"{days} days", f"trial {trial}")
    start = None
    d = parse_dt(wd)
    if d and "30" in str(c.p.get("next_step_window_open", "")):
        start = (d - timedelta(days=37)).strftime("%-d %b")
    beat = seasonal_beat(c.cat, keywords=["wedding", "bridal"])
    pref = str((cu.get("preferences") or {}).get("preferred_slots") or "")
    lead = f"{days} days to your big day ({nice_date(wd)})!" if days else f"your big day is on {nice_date(wd)}!"
    since = f" Since your bridal trial on {nice_date(trial)}, the next step is the {step}" if trial else f" Next step: the {step}"
    since += f" — best started around {start} so you finish a week before the wedding." if start else "."
    lock = ""
    if beat and "oct" in beat.get("month_range", "").lower():
        lock = f" {beat['month_range']} is peak bridal season (bookings run {re.search(r'(\d+x)', beat['note']).group(1) if re.search(r'(\d+x)', beat['note']) else 'well above'} normal)"
        lock += f", so {pref.replace('_', ' ').title()}s fill fast." if pref else ", so slots fill fast."
        c.anchor(f"seasonal beat {beat['month_range']}")
    cta = f"Want us to reserve your {pref.replace('_', ' ').title() + ' ' if pref else ''}slots for it now? Reply YES."
    body = c.cgreet().rstrip(", ") + " 💍 " + c.csign() + lead + since + lock + " " + cta
    c.levers += ["date specificity", "loss aversion (peak season)", "preference honoured"]
    return _result(c, "c_bridal", body, "binary_yes_no", [customer_first(cu), c.sender(), lead, step],
                   _cust_fu(c, step, []), send_as="merchant_on_behalf")


def kc_generic(c: Ctx) -> dict:
    return kc_recall(c) if c.p.get("available_slots") else kc_lapsed(c)


COMPOSERS = {
    "research": k_research, "compliance": k_compliance, "cde": k_cde, "supply_alert": k_supply_alert,
    "perf_dip": k_perf_dip, "perf_spike": k_perf_spike, "seasonal_dip": k_seasonal_dip,
    "milestone": k_milestone, "review_theme": k_review_theme, "dormant": k_dormant,
    "winback": k_winback, "renewal": k_renewal, "festival": k_festival, "ipl": k_ipl,
    "planning": k_planning, "curious_ask": k_curious_ask, "category_seasonal": k_category_seasonal,
    "gbp_unverified": k_gbp_unverified, "competitor": k_competitor, "trend": k_trend,
    "c_recall": kc_recall, "c_appointment": kc_appointment, "c_refill": kc_refill,
    "c_lapsed": kc_lapsed, "c_trial": kc_trial, "c_bridal": kc_bridal, "c_slot": kc_recall,
    "c_generic": kc_generic, "generic": k_generic,
}


def family_of(trigger: dict) -> str:
    kind = str(trigger.get("kind") or "")
    if kind in FAMILY:
        return FAMILY[kind]
    k = kind.lower()
    customer = trigger.get("scope") == "customer" or bool(trigger.get("customer_id"))
    if customer:
        for key, fam in (("lapse", "c_lapsed"), ("winback", "c_lapsed"), ("recall", "c_recall"),
                         ("refill", "c_refill"), ("appointment", "c_appointment"), ("trial", "c_trial"),
                         ("bridal", "c_bridal"), ("wedding", "c_bridal"), ("slot", "c_slot")):
            if key in k:
                return fam
        return "c_generic"
    for key, fam in (("research", "research"), ("digest", "research"), ("regulat", "compliance"),
                     ("compliance", "compliance"), ("cde", "cde"), ("webinar", "cde"), ("recall", "supply_alert"),
                     ("supply", "supply_alert"), ("dip", "perf_dip"), ("spike", "perf_spike"),
                     ("milestone", "milestone"), ("review", "review_theme"), ("dormant", "dormant"),
                     ("winback", "winback"), ("renewal", "renewal"), ("festival", "festival"),
                     ("ipl", "ipl"), ("match", "ipl"), ("planning", "planning"), ("curious", "curious_ask"),
                     ("seasonal", "category_seasonal"), ("unverified", "gbp_unverified"),
                     ("competitor", "competitor"), ("trend", "trend")):
        if key in k:
            return fam
    return "generic"


# =============================================================================
# public API
# =============================================================================

def compose_full(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None,
                 now: Any = None, prior_contact: bool = False) -> dict:
    c = Ctx(category, merchant, trigger, customer, now=now, prior_contact=prior_contact)
    fam = family_of(c.t)
    if fam.startswith("c_") and not customer:
        c.notes.append("customer context missing — drafted without personal details")
    try:
        r = COMPOSERS.get(fam, k_generic)(c)
    except Exception as e:  # never fail a send because one composer hit odd data
        c.notes.append(f"{fam} composer fallback ({type(e).__name__})")
        r = (kc_lapsed if fam.startswith("c_") else k_generic)(c)
    taboos = (c.cat.get("voice") or {}).get("vocab_taboo") or (c.cat.get("voice") or {}).get("taboos") or []
    body = scrub(r["body"], taboos)
    # composers write clauses lower-case after "Hi X, "; capitalise anything that follows a full stop
    body = re.sub(r"([.!?] |\n)(?!magicpin)([a-z])", lambda m: m.group(1) + m.group(2).upper(), body)
    anchors = [a for a in c.anchors if a]
    head = ", ".join(x for x in (c.t.get("source"), f"urgency {c.t['urgency']}" if c.t.get("urgency") is not None else None) if x)
    rationale = (f"{c.kind}{f' ({head})' if head else ''}"
                 f"{' [placeholder payload — anchored on merchant/category data]' if c.placeholder else ''}. "
                 + (f"Anchors: {'; '.join(dict.fromkeys(anchors))[:220]}. " if anchors else "")
                 + f"Levers: {', '.join(dict.fromkeys(c.levers))}. "
                 + (f"Judgement: {'; '.join(c.notes)}. " if c.notes else "")
                 + f"Lang: {c.clang if r['send_as'] == 'merchant_on_behalf' else c.lang}.")
    supp = c.t.get("suppression_key") or f"{c.kind}:{c.m.get('merchant_id')}:{(customer or {}).get('customer_id') or ''}"
    prefix = "merchant" if r["send_as"] == "merchant_on_behalf" else "vera"
    return {
        "body": body,
        "cta": r["cta"],
        "send_as": r["send_as"],
        "suppression_key": supp,
        "rationale": rationale,
        "template_name": f"{prefix}_{r['family']}_v1",
        "template_params": [str(p) for p in r["params"] if p is not None],
        "family": r["family"],
        "lang": c.clang if r["send_as"] == "merchant_on_behalf" else c.lang,
        "followup": r["followup"],
        "composer_version": COMPOSER_VERSION,
    }


def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None, **kw) -> dict:
    """Brief §7.1 contract. Returns body, cta, send_as, suppression_key, rationale
    (+ template_name/template_params for the first-touch WhatsApp template)."""
    full = compose_full(category, merchant, trigger, customer, **kw)
    return {k: full[k] for k in ("body", "cta", "send_as", "suppression_key", "rationale",
                                 "template_name", "template_params")}
