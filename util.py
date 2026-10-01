"""Shared helpers: formatting, names, language, fact extraction from the 4 contexts.

Everything here is pure and deterministic. Nothing in this module invents data —
helpers return None / [] when the contexts don't carry a fact.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------- formatting

def inr(n: Any) -> str:
    """4999 -> '₹4,999'; 125000 -> '₹1,25,000' (Indian grouping)."""
    try:
        v = int(round(float(str(n).replace(",", "").replace("₹", ""))))
    except (TypeError, ValueError):
        return f"₹{n}"
    return "₹" + group(v)


def group(v: int) -> str:
    s = str(abs(int(v)))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return ("-" if v < 0 else "") + s


def pct(x: Any, signed: bool = False) -> str:
    """0.021 -> '2.1%', 0.03 -> '3%', -0.5 -> '50%' (or '-50%' if signed)."""
    try:
        v = float(x) * 100
    except (TypeError, ValueError):
        return str(x)
    r = round(v, 1)
    txt = f"{abs(r):.0f}%" if abs(r - round(r)) < 0.05 else f"{abs(r):.1f}%"
    if signed:
        return ("+" if r > 0 else "-" if r < 0 else "") + txt
    return ("-" if r < 0 else "") + txt


def human(s: Any) -> str:
    """'corporate_bulk_thali_package' -> 'corporate bulk thali package'."""
    return re.sub(r"\s+", " ", str(s or "").replace("_", " ")).strip()


def parse_dt(s: Any) -> Optional[datetime]:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        try:
            d = datetime.fromisoformat(str(s)[:10])
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def nice_date(s: Any, with_year: bool = False) -> Optional[str]:
    """'2026-05-12' -> '12 May' (no weekday: dataset weekday labels are unreliable)."""
    d = parse_dt(s)
    if not d:
        return None
    return d.strftime("%-d %b %Y" if with_year else "%-d %b")


def days_between(a: Any, b: Any) -> Optional[int]:
    da, db = parse_dt(a), parse_dt(b)
    if not da or not db:
        return None
    return (db.date() - da.date()).days


def first_sentence(text: str) -> str:
    text = (text or "").strip()
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    return (m.group(1) if m else text).strip()


def strip_period(s: str) -> str:
    return (s or "").strip().rstrip(".")


def lc_first(s: str) -> str:
    """Lower-case the first letter unless it starts an acronym ('ORS', 'JIDA')."""
    if not s or (len(s) > 1 and s[:2].isupper()):
        return s
    return s[0].lower() + s[1:]


# ---------------------------------------------------------------- names

def owner_first(merchant: dict) -> Optional[str]:
    n = (merchant.get("identity") or {}).get("owner_first_name")
    if not n:
        return None
    return re.sub(r"^\s*dr\.?\s+", "", str(n), flags=re.I).strip() or None


def salutation(merchant: dict, category_slug: str) -> str:
    """How Vera addresses the merchant: 'Dr. Meera', 'Lakshmi', or the business name."""
    first = owner_first(merchant)
    raw = str((merchant.get("identity") or {}).get("owner_first_name") or "")
    if first:
        if category_slug == "dentists" or raw.lower().startswith("dr"):
            return f"Dr. {first}"
        return first
    return biz_name(merchant) + " team"


def biz_name(merchant: dict) -> str:
    return (merchant.get("identity") or {}).get("name") or "your business"


def locality(merchant: dict) -> Optional[str]:
    return (merchant.get("identity") or {}).get("locality")


def customer_first(customer: dict) -> str:
    """'Karthik (parent: Sumitra)' -> addressee 'Sumitra'; 'Mr. Sharma' stays."""
    name = str((customer.get("identity") or {}).get("name") or "").strip()
    m = re.search(r"\(parent:\s*([^)]+)\)", name)
    if m:
        return m.group(1).strip()
    return name or "there"


def customer_subject(customer: dict) -> Optional[str]:
    """For parent-mediated customers, the child's name ('Karthik')."""
    name = str((customer.get("identity") or {}).get("name") or "")
    m = re.match(r"\s*([^(]+?)\s*\(parent:", name)
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------- language

HINDI_BELT = {"delhi", "jaipur", "lucknow", "chandigarh", "mumbai", "pune", "ahmedabad",
              "noida", "gurgaon", "gurugram", "kanpur", "indore", "bhopal", "patna", "agra"}

HINGLISH_MARKERS = re.compile(
    r"\b(hai|hain|nahi|nahin|kya|karo|kar|karna|karke|haan|haanji|ji|aap|aapka|aapki|mujhe|mera|meri|"
    r"chahiye|theek|thik|bhai|kal|abhi|kaise|kitna|kitne|accha|acha|achha|bahut|bilkul|batao|"
    r"bataiye|dijiye|kijiye|samajh|lekin|aur|toh|matlab|wala|wali|hoga|hogi|raha|rahi|sakte|"
    r"chalega|chalo|karein|bhejo|bhej|mat|band)\b", re.I)
DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def text_is_hindi(text: str) -> bool:
    if not text:
        return False
    if DEVANAGARI.search(text):
        return True
    return len(HINGLISH_MARKERS.findall(text)) >= 2 or bool(
        re.search(r"\b(haan|nahi|theek hai|kar do|chahiye|bhejo|mat bhejo)\b", text, re.I))


def merchant_lang(merchant: dict) -> str:
    """'hi-en' or 'en'. Observed language in the merchant's own messages wins;
    otherwise Hindi-belt cities with 'hi' in languages get light code-mix."""
    hist = merchant.get("conversation_history") or []
    own = [h.get("body", "") for h in hist if h.get("from") == "merchant" and h.get("body")]
    if own:
        return "hi-en" if text_is_hindi(own[-1]) else "en"
    ident = merchant.get("identity") or {}
    langs = [str(l).lower() for l in (ident.get("languages") or [])]
    city = str(ident.get("city") or "").lower()
    if "hi" in langs and city in HINDI_BELT:
        return "hi-en"
    return "en"


def customer_lang(customer: Optional[dict]) -> str:
    """'hi' (Roman Hindi, respectful), 'hi-en', 'en', or regional mix 'ta-en' / 'te-en' / 'kn-en' / 'mr-en'."""
    if not customer:
        return "en"
    p = str((customer.get("identity") or {}).get("language_pref") or "en").lower().strip()
    if p in ("hi", "hindi"):
        return "hi"
    if p.startswith("hi") or "hinglish" in p:
        return "hi-en"
    for code in ("ta", "te", "kn", "mr", "bn", "gu", "ml"):
        if p.startswith(code):
            return f"{code}-en"
    return "en"


REGIONAL_GREETING = {"ta-en": "Vanakkam", "te-en": "Namaskaram", "kn-en": "Namaskara",
                     "mr-en": "Namaskar", "bn-en": "Nomoshkar", "gu-en": "Kem cho", "ml-en": "Namaskaram"}


def pick(lang: str, en: str, hi: str) -> str:
    return hi if lang in ("hi", "hi-en") else en


# ---------------------------------------------------------------- context lookups

def find_digest(category: dict, item_id: Optional[str]) -> Optional[dict]:
    if not item_id:
        return None
    for d in category.get("digest") or []:
        if d.get("id") == item_id:
            return d
    return None


def digest_of_kind(category: dict, kinds: Iterable[str]) -> Optional[dict]:
    kinds = list(kinds)
    for k in kinds:
        for d in category.get("digest") or []:
            if d.get("kind") == k:
                return d
    return None


def active_offers(merchant: dict) -> list[str]:
    return [o.get("title") for o in merchant.get("offers") or []
            if o.get("status") == "active" and o.get("title")]


def expired_offers(merchant: dict) -> list[str]:
    return [o.get("title") for o in merchant.get("offers") or []
            if o.get("status") in ("expired", "paused") and o.get("title")]


def catalog_titles(category: dict) -> list[str]:
    return [o.get("title") for o in category.get("offer_catalog") or [] if o.get("title")]


def catalog_pick(category: dict, keywords: Iterable[str] = (), avoid_discount: bool = True,
                 avoid: Iterable[str] = ()) -> Optional[str]:
    """Best catalog offer: keyword match first, else first service+price offer."""
    avoid = [a.lower() for a in avoid]
    rank = ["service_at_price", "free_service", "free_trial", "free_addon", "membership", "bogo", "percentage_discount"]
    offers = [o for o in category.get("offer_catalog") or [] if o.get("title")
              and not any(a in o["title"].lower() for a in avoid)]
    offers.sort(key=lambda o: rank.index(o.get("type")) if o.get("type") in rank else len(rank))
    titles = [o["title"] for o in offers]
    for kw in keywords:
        for t in titles:
            if kw.lower() in t.lower():
                return t
    for t in titles:
        if avoid_discount and re.search(r"%\s*off|flat", t, re.I):
            continue
        if "₹" in t or "free" in t.lower():
            return t
    return titles[0] if titles else None


def signal_map(merchant: dict) -> dict[str, Optional[str]]:
    """['stale_posts:22d', 'dormant_with_vera_14d'] -> {'stale_posts': '22d', 'dormant_with_vera': '14d'}."""
    out: dict[str, Optional[str]] = {}
    for s in merchant.get("signals") or []:
        s = str(s)
        if ":" in s:
            k, v = s.split(":", 1)
            out[k] = v
            continue
        m = re.match(r"(.+?)_(\d+d)$", s)
        if m:
            out[m.group(1)] = m.group(2)
        else:
            out[s] = None
    return out


def peer_scope(category: dict) -> str:
    sc = str((category.get("peer_stats") or {}).get("scope") or "")
    sc = re.sub(r"_?20\d\d$", "", sc)
    return human(sc) or f"{category.get('slug', 'peer')} peers"


def perf(merchant: dict) -> dict:
    return merchant.get("performance") or {}


def peer_gaps(category: dict, merchant: dict) -> list[dict]:
    """Merchant vs peer benchmark, strongest gap first. Each: {metric, mine, peer, rel, text}."""
    ps = category.get("peer_stats") or {}
    pf = perf(merchant)
    scope = peer_scope(category)
    out = []
    templates = {
        "ctr": "your profile CTR is {m} vs {p} for {s}",
        "calls": "you got {m} calls in the last 30 days vs a {p} average for {s}",
        "views": "your listing got {m} views in the last 30 days vs a {p} average for {s}",
        "directions": "{m} people asked for directions in the last 30 days vs a {p} average for {s}",
    }
    pairs = [("ctr", "avg_ctr", pct), ("calls", "avg_calls_30d", group),
             ("views", "avg_views_30d", group), ("directions", "avg_directions_30d", group)]
    for mk, pk, fmt in pairs:
        mine, peer = pf.get(mk), ps.get(pk)
        if mine is None or not peer:
            continue
        rel = (float(mine) - float(peer)) / float(peer)
        out.append({"metric": mk, "mine": mine, "peer": peer, "rel": rel, "label": mk,
                    "text": templates[mk].format(m=fmt(mine), p=fmt(peer), s=scope)})
    out.sort(key=lambda g: g["rel"])
    return out


def worst_gap(category: dict, merchant: dict, threshold: float = -0.12) -> Optional[dict]:
    gaps = [g for g in peer_gaps(category, merchant) if g["rel"] <= threshold]
    return gaps[0] if gaps else None


def best_gap(category: dict, merchant: dict, threshold: float = 0.12) -> Optional[dict]:
    gaps = [g for g in peer_gaps(category, merchant) if g["rel"] >= threshold]
    return gaps[-1] if gaps else None


def month_in_range(rng: str, month: int) -> bool:
    """'Nov-Feb' / 'Apr-Jun' / 'Jan' / 'Feb 14' contains month?"""
    names = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    toks = [t for t in re.findall(r"[A-Za-z]{3}", rng.lower()) if t in names]
    if not toks:
        return False
    a = names.index(toks[0]) + 1
    b = names.index(toks[-1]) + 1
    if a <= b:
        return a <= month <= b
    return month >= a or month <= b


def seasonal_beat(category: dict, month: Optional[int] = None, keywords: Iterable[str] = ()) -> Optional[dict]:
    beats = category.get("seasonal_beats") or []
    kws = [k.lower() for k in keywords]
    if kws:
        for b in beats:
            if any(k in str(b.get("note", "")).lower() for k in kws):
                return b
    if month:
        for b in beats:
            if month_in_range(str(b.get("month_range", "")), month):
                return b
    return None


def beat_sentence(beat: dict, slug: str) -> str:
    """{'month_range': 'Oct-Dec', 'note': 'primary wedding season — bridal bookings 4x baseline'}
    -> 'Oct-Dec is the primary wedding season for salons (bridal bookings 4x baseline)'."""
    note, rng = strip_period(str(beat.get("note") or "")), beat.get("month_range")
    if " — " in note:
        head, tail = note.split(" — ", 1)
        art = "" if re.match(r"(the|a|an)\b", head, re.I) else "the "
        return f"{rng} is {art}{head} for {slug} ({tail})"
    return f"{rng}: {note}"


def src_fmt(src: Optional[str]) -> str:
    """'magicpin order data, Apr 2026' -> 'magicpin order data (Apr 2026)'."""
    if not src:
        return ""
    m = re.match(r"(.+?),\s*((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+20\d\d)$", src.strip(), re.I)
    return f"{m.group(1)} ({m.group(2)})" if m else src.strip()


def top_trend(category: dict) -> Optional[dict]:
    ts = sorted(category.get("trend_signals") or [], key=lambda t: -float(t.get("delta_yoy") or 0))
    return ts[0] if ts else None


def last_merchant_turn(merchant: dict) -> Optional[dict]:
    for h in reversed(merchant.get("conversation_history") or []):
        if h.get("from") == "merchant":
            return h
    return None


def last_vera_turn(merchant: dict) -> Optional[dict]:
    for h in reversed(merchant.get("conversation_history") or []):
        if h.get("from") == "vera":
            return h
    return None


def numbers_in(text: str) -> set[str]:
    """Normalised numeric tokens, for the anti-fabrication check."""
    return {n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text or "")}


URL_RE = re.compile(r"(https?://|www\.)\S+", re.I)


def scrub(body: str, taboos: Iterable[str]) -> str:
    body = URL_RE.sub("", body)
    for t in taboos or []:
        t = re.sub(r"\s*\(.*\)\s*$", "", str(t)).strip()
        if t:
            body = re.sub(re.escape(t), "", body, flags=re.I)
    body = re.sub(r"[ \t]{2,}", " ", body)
    return body.strip()
