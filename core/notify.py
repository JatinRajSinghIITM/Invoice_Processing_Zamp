"""
Optional alert when an invoice lands in the review queue.

Off unless ALERT_WEBHOOK_URL is set (a Slack-style incoming webhook: it accepts
{"text": "..."}; Microsoft Teams and Discord have compatible ones). So a person
learns an invoice is waiting without having to watch the dashboard.

Guardrails, since the text comes from uploaded documents and the app may be public:
  - https URLs only, short timeout, and a failure never affects the pipeline
    (this function cannot raise).
  - The message is short and carries only what a reviewer needs to find the
    invoice: file name, vendor, total, the first reason and the invoice id.
  - Untrusted text is flattened to one line, truncated, and escaped for Slack
    (so "<!channel>"-style injections in a PDF cannot ping a whole workspace).
  - A daily cap stops a flood of uploads from flooding the channel.
"""
import json
import os
import threading
import urllib.request
from datetime import date

MAX_ALERTS_PER_DAY = int(os.environ.get("ALERT_MAX_PER_DAY", "50"))
TIMEOUT_SECONDS = 5

_lock = threading.Lock()
_sent_today = {"day": None, "n": 0}


def webhook_url() -> str | None:
    url = (os.environ.get("ALERT_WEBHOOK_URL") or "").strip()
    return url if url.startswith("https://") else None


def is_enabled() -> bool:
    return webhook_url() is not None


def reset_counter() -> None:
    """For tests."""
    with _lock:
        _sent_today.update(day=None, n=0)


def _plain(value, limit: int = 140) -> str:
    text = " ".join(str(value if value is not None else "—").split())
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def build_message(result) -> str:
    e = result.extracted
    total = f"₹{e.total:,.2f}" if e.total else "total not read"
    reason = result.decision.reasons[0] if result.decision.reasons else "no reason recorded"
    return (f"Invoice needs review: {_plain(result.filename)} | {_plain(e.vendor_name_raw)} | {total}\n"
            f"Why: {_plain(reason, 200)}\nId: {_plain(result.invoice_id)}")


def _post(url: str, payload: dict) -> None:
    """The only function that touches the network."""
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:   # noqa: S310 (https enforced above)
        resp.read()


def alert_needs_review(result) -> bool:
    """Send the alert if configured and under the daily cap. True if one was sent."""
    try:
        url = webhook_url()
        if not url or result.decision.status != "Needs Review":
            return False
        today = date.today().isoformat()
        with _lock:
            if _sent_today["day"] != today:
                _sent_today.update(day=today, n=0)
            if _sent_today["n"] >= MAX_ALERTS_PER_DAY:
                return False
            _sent_today["n"] += 1
        _post(url, {"text": build_message(result)})
        return True
    except Exception:
        return False
