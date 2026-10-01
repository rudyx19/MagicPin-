"""In-memory state: versioned contexts, conversations, per-merchant flags.

Thread-safe (one RLock). Optional JSON snapshot to disk (STATE_FILE env) so a
process restart mid-test doesn't lose the pushed contexts.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Optional

SCOPES = ("category", "merchant", "customer", "trigger")


class Store:
    def __init__(self, state_file: Optional[str] = None):
        self.lock = threading.RLock()
        self.state_file = state_file
        self.started = time.time()
        self.reset()
        if state_file and os.path.exists(state_file):
            try:
                with open(state_file) as f:
                    data = json.load(f)
                self.contexts = {tuple(k.split("|", 1)): v for k, v in data.get("contexts", {}).items()}
                self.conversations = data.get("conversations", {})
                self.merchants = data.get("merchants", {})
                self.sent_suppression = set(data.get("sent_suppression", []))
            except Exception:
                self.reset()
        self._dirty = False
        if state_file:
            threading.Thread(target=self._flusher, daemon=True).start()

    def reset(self):
        with getattr(self, "lock", threading.RLock()):
            self.contexts: dict[tuple[str, str], dict] = {}     # (scope, id) -> {version, payload, delivered_at}
            self.conversations: dict[str, dict] = {}            # conversation_id -> state
            self.merchants: dict[str, dict] = {}                # merchant_id -> flags (opt-out, auto-reply, cooldown)
            self.sent_suppression: set[str] = set()
            self._dirty = True

    # ---------------------------------------------------------------- contexts
    def put_context(self, scope: str, cid: str, version: int, payload: dict, delivered_at: Any = None):
        """Returns (status_code, body)."""
        with self.lock:
            cur = self.contexts.get((scope, cid))
            if cur and int(cur["version"]) >= int(version):
                return 409, {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}
            self.contexts[(scope, cid)] = {"version": int(version), "payload": payload, "delivered_at": delivered_at}
            if scope == "trigger" and isinstance(payload, dict) and payload.get("id") and payload["id"] != cid:
                self.contexts[("trigger_alias", payload["id"])] = {"version": int(version), "payload": cid}
            self._dirty = True
            return 200, None

    def get(self, scope: str, cid: Optional[str]) -> Optional[dict]:
        if not cid:
            return None
        with self.lock:
            v = self.contexts.get((scope, cid))
            if v is None and scope == "trigger":
                alias = self.contexts.get(("trigger_alias", cid))
                if alias:
                    v = self.contexts.get(("trigger", alias["payload"]))
            return v["payload"] if v else None

    def version(self, scope: str, cid: Optional[str]) -> Optional[int]:
        with self.lock:
            v = self.contexts.get((scope, cid or ""))
            return v["version"] if v else None

    def counts(self) -> dict:
        out = {s: 0 for s in SCOPES}
        with self.lock:
            for (scope, _), _v in self.contexts.items():
                if scope in out:
                    out[scope] += 1
        return out

    def category_for(self, merchant: Optional[dict], trigger: Optional[dict] = None) -> Optional[dict]:
        slug = (merchant or {}).get("category_slug") or ((trigger or {}).get("payload") or {}).get("category")
        return self.get("category", slug) if slug else None

    # ---------------------------------------------------------------- merchants / conversations
    def merchant_state(self, mid: str) -> dict:
        with self.lock:
            st = self.merchants.setdefault(mid or "_unknown", {
                "opted_out": False, "opted_out_at": None, "auto_reply_count": 0, "auto_reply_texts": [],
                "last_vera_send": None, "conv_ids": [], "ended_reason": None, "backoff_until": None,
            })
            return st

    def conv(self, conv_id: str) -> Optional[dict]:
        with self.lock:
            return self.conversations.get(conv_id)

    def new_conv(self, conv_id: str, **fields) -> dict:
        with self.lock:
            st = {"conversation_id": conv_id, "status": "active", "turns": [], "bot_bodies": [],
                  "stage": "pitched", "auto_reply_count": 0, "nudges_without_reply": 0, "created": time.time()}
            st.update(fields)
            self.conversations[conv_id] = st
            mid = fields.get("merchant_id")
            if mid:
                self.merchant_state(mid)["conv_ids"].append(conv_id)
            self._dirty = True
            return st

    def touch(self):
        self._dirty = True

    # ---------------------------------------------------------------- persistence
    def _flusher(self):
        while True:
            time.sleep(3)
            if self._dirty:
                self.flush()

    def flush(self):
        if not self.state_file:
            return
        with self.lock:
            data = {"contexts": {f"{k[0]}|{k[1]}": v for k, v in self.contexts.items()},
                    "conversations": self.conversations, "merchants": self.merchants,
                    "sent_suppression": sorted(self.sent_suppression)}
            self._dirty = False
        tmp = self.state_file + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(data, f, ensure_ascii=False, default=str)
            os.replace(tmp, self.state_file)
        except Exception:
            self._dirty = True

    def wipe(self):
        with self.lock:
            self.reset()
            if self.state_file and os.path.exists(self.state_file):
                try:
                    os.remove(self.state_file)
                except OSError:
                    pass
