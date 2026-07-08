#    Copyright 2023 Red Hat, Inc.
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
from unittest.mock import patch

import pytest
import requests

from rebasebot.prow import ProwJobContext
from rebasebot.slack import SlackNotifier, _build_slack_blocks


class TestBuildSlackBlocks:
    @pytest.mark.parametrize(
        "message, emoji, log_url, expected_block_count",
        [
            ("All good", "✅", None, 1),
            ("Something broke", "❌", None, 1),
            ("Please help", "🖐️", None, 1),
            (
                "All good",
                "✅",
                "https://prow.ci.openshift.org/view/gs/test-platform-results/logs/job/123",
                2,
            ),
        ],
    )
    def test_build_slack_blocks(self, message, emoji, log_url, expected_block_count):
        blocks = _build_slack_blocks(message, emoji, log_url)

        assert len(blocks) == expected_block_count
        assert blocks[0]["type"] == "section"
        assert blocks[0]["text"]["type"] == "mrkdwn"
        assert blocks[0]["text"]["text"] == f"{emoji} {message}"

        if log_url is not None:
            assert blocks[1]["text"]["text"] == f"<{log_url}|View job log>"

    def test_exception_text_in_fenced_code_block(self):
        message = "I got an error:\n```boom```"
        blocks = _build_slack_blocks(message, "❌", None)

        assert blocks[0]["text"]["text"] == f"❌ {message}"


class TestSlackNotifier:
    @patch("rebasebot.slack.requests.post")
    def test_rehearsal_run_skips_post(self, mocked_post):
        rehearsal = ProwJobContext(
            job_name="rehearse-1234-periodic-openshift-release-rebasebot",
            job_type="presubmit",
            build_id="12345",
        )
        notifier = SlackNotifier("test://webhook", rehearsal)

        notifier.notify("hello", "✅")

        mocked_post.assert_not_called()

    @patch("rebasebot.slack.requests.post")
    def test_no_webhook_is_no_op(self, mocked_post):
        prow_job = ProwJobContext(job_name="periodic-job", job_type="periodic", build_id="99")
        notifier = SlackNotifier(None, prow_job)

        notifier.notify("hello", "✅")

        mocked_post.assert_not_called()

    @patch("rebasebot.slack.requests.post")
    def test_posts_with_blocks_without_log_url(self, mocked_post):
        prow_job = ProwJobContext(job_name=None, job_type=None, build_id=None)
        notifier = SlackNotifier("test://webhook", prow_job)
        message = "All good"

        notifier.notify(message, "✅")

        expected_blocks = _build_slack_blocks(message, "✅", None)
        mocked_post.assert_called_once_with(
            "test://webhook",
            json={"text": message, "blocks": expected_blocks},
            timeout=5,
        )

    @patch("rebasebot.slack.requests.post")
    def test_posts_with_log_url_block(self, mocked_post):
        prow_job = ProwJobContext(job_name="periodic-job", job_type="periodic", build_id="12345")
        notifier = SlackNotifier("test://webhook", prow_job)
        message = "Something broke"

        notifier.notify(message, "❌")

        expected_blocks = _build_slack_blocks(message, "❌", prow_job.log_url)
        mocked_post.assert_called_once_with(
            "test://webhook",
            json={"text": message, "blocks": expected_blocks},
            timeout=5,
        )

    @patch("rebasebot.slack.requests.post")
    def test_webhook_failure_does_not_propagate(self, mocked_post):
        mocked_post.side_effect = requests.exceptions.ConnectionError("boom")
        prow_job = ProwJobContext(job_name="periodic-job", job_type="periodic", build_id="99")
        notifier = SlackNotifier("test://webhook", prow_job)

        notifier.notify("hello", "✅")  # must not raise

    @patch("rebasebot.slack.requests.post")
    def test_webhook_http_error_does_not_propagate(self, mocked_post):
        mocked_post.return_value.raise_for_status.side_effect = requests.exceptions.HTTPError("404")
        prow_job = ProwJobContext(job_name="periodic-job", job_type="periodic", build_id="99")
        notifier = SlackNotifier("test://webhook", prow_job)

        notifier.notify("hello", "✅")  # must not raise

    @patch("rebasebot.slack.requests.post")
    def test_webhook_failure_does_not_log_secret_url(self, mocked_post, caplog):
        # A RequestException's message embeds the request URL, which for a real
        # Slack webhook includes a secret token. Make sure that never ends up in logs.
        secret_webhook = "https://hooks.slack.com/services/SECRET/TOKEN/abc"
        mocked_post.side_effect = requests.exceptions.ConnectionError(
            f"Max retries exceeded with url: {secret_webhook}"
        )
        prow_job = ProwJobContext(job_name="periodic-job", job_type="periodic", build_id="99")
        notifier = SlackNotifier(secret_webhook, prow_job)

        notifier.notify("hello", "✅")

        assert secret_webhook not in caplog.text
        assert "SECRET" not in caplog.text
        assert "Failed to post Slack notification" in caplog.text
