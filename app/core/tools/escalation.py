"""
Human Escalation module (Section 5.1 triggers implemented in orchestrator.py,
this module just delivers the notification).

Mock mode (default): appends a structured record to escalations.log in the
project root so you can see every escalation during a demo without any
external service.

If SLACK_WEBHOOK_URL is set, also POSTs a message to Slack (untested here —
no network in this sandbox). If SMTP_* env vars are set, could be extended to
send email the same way — plug-in point noted below.
"""
from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone

DEFAULT_LOG_PATH = "escalations.log"


class EscalationNotifier:
    def __init__(self, log_path: str | None = None):
        self.slack_webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
        # Resolved at call-time (not module import time) so tests / different
        # deployments can point ESCALATION_LOG_PATH at different files within
        # the same process.
        self._log_path_override = log_path

    def notify(self, conversation_id: str, reason: str, transcript_snippet: str) -> dict:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "conversation_id": conversation_id,
            "reason": reason,
            "transcript_snippet": transcript_snippet,
        }
        self._log_to_file(record)

        slack_ok = None
        if self.slack_webhook_url:
            slack_ok = self._post_to_slack(record)

        return {"logged": True, "slack_notified": slack_ok}

    def _log_to_file(self, record: dict) -> None:
        log_path = self._log_path_override or os.environ.get("ESCALATION_LOG_PATH", DEFAULT_LOG_PATH)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def _post_to_slack(self, record: dict) -> bool:
        text = (
            f":rotating_light: *Escalation* on conversation `{record['conversation_id']}`\n"
            f"*Reason:* {record['reason']}\n"
            f"*Snippet:* {record['transcript_snippet']}"
        )
        body = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(
            self.slack_webhook_url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return 200 <= resp.status < 300
        except urllib.error.URLError:
            return False
