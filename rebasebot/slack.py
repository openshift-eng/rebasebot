#    Copyright 2022 Red Hat, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
"""Slack notification transport and Block Kit formatting for Rebase Bot."""

from __future__ import annotations

import requests

from rebasebot.prow import ProwJobContext


def _build_slack_blocks(message: str, emoji: str, log_url: str | None) -> list[dict]:
    """Build a Slack Block Kit blocks array for a rebasebot alert."""
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{emoji} {message}",
            },
        },
    ]
    if log_url is not None:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"<{log_url}|View job log>",
                },
            },
        )
    return blocks


def _message_slack(webhook_url: str | None, msg: str, blocks: list[dict]) -> None:
    """Send a message to Slack via a webhook if one is configured."""
    if webhook_url is None:
        return
    requests.post(webhook_url, json={"text": msg, "blocks": blocks}, timeout=5)


class SlackNotifier:
    """Posts formatted Slack messages for Rebase Bot alerts."""

    def __init__(self, webhook_url: str | None, prow_job: ProwJobContext) -> None:
        self._webhook_url = webhook_url
        self._prow_job = prow_job

    def notify(self, message: str, emoji: str) -> None:
        """Post a message to Slack, unless this is a rehearsal run or no webhook is configured."""
        if self._prow_job.is_rehearsal:
            return
        blocks = _build_slack_blocks(message, emoji, self._prow_job.log_url)
        _message_slack(self._webhook_url, message, blocks)
