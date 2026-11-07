"""Tests for the optional reviewer alert. The network call is always stubbed."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core import notify
from core.pipeline import run_pipeline
from core.workspace import new_session_workspace

SAMPLES = ROOT / "sample_invoices"


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    notify.reset_counter()
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    yield
    notify.reset_counter()


def run(filename):
    return [i for i in run_pipeline(str(SAMPLES / filename), filename, ws=new_session_workspace())
            if hasattr(i, "decision")][0]


def capture(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_post", lambda url, payload: sent.append((url, payload)))
    return sent


def test_nothing_is_sent_when_no_webhook_is_configured(monkeypatch):
    sent = capture(monkeypatch)
    run("05_edge_unknown_vendor.pdf")
    assert sent == [] and not notify.is_enabled()


def test_only_https_urls_are_accepted(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://insecure.example/hook")
    assert not notify.is_enabled()
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/abc")
    assert notify.is_enabled()


def test_needs_review_sends_one_short_alert(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/abc")
    sent = capture(monkeypatch)
    r = run("05_edge_unknown_vendor.pdf")
    assert len(sent) == 1 and sent[0][0] == "https://hooks.example/abc"
    text = sent[0][1]["text"]
    assert "05_edge_unknown_vendor.pdf" in text and r.invoice_id in text and "Why:" in text
    stage11 = next(s for s in r.stages if s.stage.startswith("11."))
    assert "alert sent" in stage11.detail


def test_approved_and_rejected_invoices_send_nothing(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/abc")
    sent = capture(monkeypatch)
    assert run("01_happy_path_nimbus.pdf").decision.status == "Approved"
    assert run("07_bonus_inactive_vendor_meridian.pdf").decision.status == "Rejected"
    assert sent == []


def test_a_failing_webhook_never_breaks_the_pipeline(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/abc")
    def boom(url, payload):
        raise OSError("network down")
    monkeypatch.setattr(notify, "_post", boom)
    r = run("05_edge_unknown_vendor.pdf")
    assert r.decision.status == "Needs Review"
    stage11 = next(s for s in r.stages if s.stage.startswith("11."))
    assert stage11.status == "ok" and "alert sent" not in stage11.detail


def test_daily_cap(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/abc")
    monkeypatch.setattr(notify, "MAX_ALERTS_PER_DAY", 2)
    sent = capture(monkeypatch)
    for _ in range(4):
        run("05_edge_unknown_vendor.pdf")
    assert len(sent) == 2


def test_untrusted_text_is_flattened_and_escaped():
    class E: vendor_name_raw = "Evil <!channel> & Co\nINJECTED LINE"; total = 10.0
    class D: status = "Needs Review"; reasons = ["x" * 500]
    class R: filename = "a<b>.pdf"; extracted = E(); decision = D(); invoice_id = "INV-1"
    msg = notify.build_message(R())
    assert "<!channel>" not in msg and "&lt;!channel&gt;" in msg
    assert "Evil &lt;!channel&gt; &amp; Co INJECTED LINE" in msg      # newline flattened
    assert len(msg.splitlines()) == 3 and max(len(l) for l in msg.splitlines()) < 260
