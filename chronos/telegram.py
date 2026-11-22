"""Optional fire-and-forget Telegram alerts (no trade actions or polling)."""
from __future__ import annotations
import json
from urllib import request
from .models import Assessment
from .settings import Settings


def notify(a: Assessment, cfg: Settings) -> None:
    if not ((a.actionable or a.verdict == "SETUP")
            and cfg.telegram_token and cfg.telegram_chat_id):
        return
    label = "STRICT SIGNAL" if a.actionable else "SETUP - CONFIRMATION PENDING"
    entry_kind = "Conditional trigger (not filled)" if a.metrics.get("entry_is_trigger") else "Entry reference"
    text = (f"CHRONOS | {label} | {a.symbol} {a.side}\n"
            f"Pressure score: {a.score:.1f}/100 (not a probability)\n"
            f"{entry_kind}: {a.entry:.6g}\nSL: {a.stop:.6g} | TP: {a.target:.6g}\n"
            f"Estimated net RR: {a.net_rr:.2f}\n"
            f"Research-only hypothesis; no order placed.")
    url = f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage"
    body = json.dumps({"chat_id":cfg.telegram_chat_id,"text":text}).encode()
    req = request.Request(url,data=body,headers={"Content-Type":"application/json"},method="POST")
    with request.urlopen(req,timeout=8) as response:
        answer=json.load(response)
    if answer.get("ok") is not True:
        raise RuntimeError("Telegram notification rejected")
