#    Copyright 2026 Red Hat, Inc.
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

"""Tests for --conflict-policy behavior."""

from __future__ import annotations
import logging
from unittest.mock import MagicMock, patch

from rebasebot import cli
from .conftest import CommitBuilder

# File content simulating upstream with original formatting
_ORIGINAL_CODE = """\
package main

const (
\tregionKey    = "region"
\tebsCSIDriver = "ebs.csi.aws.com"
)

type Snapshotter struct {
\tlog string
\tec2 string
}
"""

# Upstream adds a new field/constant (between existing lines)
_UPSTREAM_ADDED_CODE = """\
package main

const (
\tregionKey      = "region"
\tebsKmsKeyIDKey = "ebsKmsKeyId"
\tebsCSIDriver   = "ebs.csi.aws.com"
)

type Snapshotter struct {
\tlog         string
\tec2         string
\tebsKmsKeyId string
}
"""

# Downstream carry patch reformats and adds its own field/constant
# (conflicts with upstream because it modifies the same lines)
_DOWNSTREAM_CARRY_CODE = """\
package main

const (
\tregionKey                      = "region"
\tebsCSIDriver                   = "ebs.csi.aws.com"
\tsnapshotCreationTimeoutKey     = "snapshotCreationTimeout"
)

type Snapshotter struct {
\tlog                     string
\tec2                     string
\tsnapshotCreationTimeout string
}
"""


class TestConflictPolicy:
    """Tests that --conflict-policy correctly detects upstream content loss."""

    @patch("rebasebot.bot._create_pr")
    @patch("rebasebot.bot._push_rebase_branch")
    @patch("rebasebot.bot._is_pr_available")
    @patch("rebasebot.bot._message_slack")
    def test_auto_policy_silent_on_conflict(
        self, mocked_message_slack, mocked_is_pr_available,
        mocked_push_rebase_branch, mocked_create_pr,
        init_test_repositories, fake_github_provider, tmpdir
    ):
        """With auto policy, -Xtheirs conflicts resolve silently."""
        source, rebase, dest = init_test_repositories
        mocked_is_pr_available.return_value = None, False
        mocked_push_rebase_branch.return_value = True

        # Replace test.go with structured code in source (initial state)
        CommitBuilder(source).update_file(
            "test.go", _ORIGINAL_CODE
        ).commit("set up base code")

        # Sync dest to have the same base
        CommitBuilder(dest).update_file(
            "test.go", _ORIGINAL_CODE
        ).commit("UPSTREAM: <carry>: sync base")

        # Upstream adds new fields
        CommitBuilder(source).update_file(
            "test.go", _UPSTREAM_ADDED_CODE
        ).commit("Add KMS key support")

        # Downstream carry patch reformats and adds timeout
        CommitBuilder(dest).update_file(
            "test.go", _DOWNSTREAM_CARRY_CODE
        ).commit("UPSTREAM: <carry>: add snapshot timeout")

        args = MagicMock()
        args.source = source
        args.source_repo = None
        args.dest = dest
        args.rebase = rebase
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "soft"
        args.conflict_policy = "auto"
        args.bot_emails = []
        args.exclude_commits = []
        args.update_go_modules = False
        args.ignore_manual_label = False
        args.dry_run = True

        result = cli.rebasebot_run(
            args, slack_webhook=None,
            github_app_wrapper=fake_github_provider)
        # Should succeed silently
        assert result is True

    @patch("rebasebot.bot._create_pr")
    @patch("rebasebot.bot._push_rebase_branch")
    @patch("rebasebot.bot._is_pr_available")
    @patch("rebasebot.bot._message_slack")
    def test_warn_policy_logs_warning_on_content_loss(
        self, mocked_message_slack, mocked_is_pr_available,
        mocked_push_rebase_branch, mocked_create_pr,
        init_test_repositories, fake_github_provider, tmpdir, caplog
    ):
        """With warn policy, warnings are logged but rebase succeeds."""
        source, rebase, dest = init_test_repositories
        mocked_is_pr_available.return_value = None, False
        mocked_push_rebase_branch.return_value = True

        CommitBuilder(source).update_file(
            "test.go", _ORIGINAL_CODE
        ).commit("set up base code")

        CommitBuilder(dest).update_file(
            "test.go", _ORIGINAL_CODE
        ).commit("UPSTREAM: <carry>: sync base")

        CommitBuilder(source).update_file(
            "test.go", _UPSTREAM_ADDED_CODE
        ).commit("Add KMS key support")

        CommitBuilder(dest).update_file(
            "test.go", _DOWNSTREAM_CARRY_CODE
        ).commit("UPSTREAM: <carry>: add snapshot timeout")

        args = MagicMock()
        args.source = source
        args.source_repo = None
        args.dest = dest
        args.rebase = rebase
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "soft"
        args.conflict_policy = "warn"
        args.bot_emails = []
        args.exclude_commits = []
        args.update_go_modules = False
        args.ignore_manual_label = False
        args.dry_run = True

        with caplog.at_level(logging.WARNING):
            result = cli.rebasebot_run(
                args, slack_webhook=None,
                github_app_wrapper=fake_github_provider)

        assert result is True
        warning_messages = [
            r.message.lower() for r in caplog.records
            if r.levelno >= logging.WARNING
        ]
        assert any(
            "upstream content may have been dropped" in m
            for m in warning_messages
        ), f"Expected warning about dropped content, got: {warning_messages}"

    @patch("rebasebot.bot._create_pr")
    @patch("rebasebot.bot._push_rebase_branch")
    @patch("rebasebot.bot._is_pr_available")
    @patch("rebasebot.bot._message_slack")
    def test_strict_policy_fails_on_content_loss(
        self, mocked_message_slack, mocked_is_pr_available,
        mocked_push_rebase_branch, mocked_create_pr,
        init_test_repositories, fake_github_provider, tmpdir
    ):
        """With strict policy, upstream content loss causes failure."""
        source, rebase, dest = init_test_repositories
        mocked_is_pr_available.return_value = None, False
        mocked_push_rebase_branch.return_value = True

        CommitBuilder(source).update_file(
            "test.go", _ORIGINAL_CODE
        ).commit("set up base code")

        CommitBuilder(dest).update_file(
            "test.go", _ORIGINAL_CODE
        ).commit("UPSTREAM: <carry>: sync base")

        CommitBuilder(source).update_file(
            "test.go", _UPSTREAM_ADDED_CODE
        ).commit("Add KMS key support")

        CommitBuilder(dest).update_file(
            "test.go", _DOWNSTREAM_CARRY_CODE
        ).commit("UPSTREAM: <carry>: add snapshot timeout")

        args = MagicMock()
        args.source = source
        args.source_repo = None
        args.dest = dest
        args.rebase = rebase
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "soft"
        args.conflict_policy = "strict"
        args.bot_emails = []
        args.exclude_commits = []
        args.update_go_modules = False
        args.ignore_manual_label = False
        args.dry_run = True

        result = cli.rebasebot_run(
            args, slack_webhook=None,
            github_app_wrapper=fake_github_provider)
        # Should fail — upstream content was lost
        assert result is False

    @patch("rebasebot.bot._create_pr")
    @patch("rebasebot.bot._push_rebase_branch")
    @patch("rebasebot.bot._is_pr_available")
    @patch("rebasebot.bot._message_slack")
    def test_strict_policy_succeeds_when_no_conflict(
        self, mocked_message_slack, mocked_is_pr_available,
        mocked_push_rebase_branch, mocked_create_pr,
        init_test_repositories, fake_github_provider, tmpdir
    ):
        """With strict policy, clean cherry-picks succeed (no false positives)."""
        source, rebase, dest = init_test_repositories
        mocked_is_pr_available.return_value = None, False
        mocked_push_rebase_branch.return_value = True

        # Source adds an unrelated new file
        CommitBuilder(source).add_file(
            "new_upstream_file.go",
            "package main\nfunc upstream() {}\n"
        ).commit("Add new upstream file")

        # Dest adds a different unrelated file (no conflict)
        CommitBuilder(dest).add_file(
            "downstream_only.go",
            "package main\nfunc downstream() {}\n"
        ).commit("UPSTREAM: <carry>: downstream only file")

        args = MagicMock()
        args.source = source
        args.source_repo = None
        args.dest = dest
        args.rebase = rebase
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "soft"
        args.conflict_policy = "strict"
        args.bot_emails = []
        args.exclude_commits = []
        args.update_go_modules = False
        args.ignore_manual_label = False
        args.dry_run = True

        result = cli.rebasebot_run(
            args, slack_webhook=None,
            github_app_wrapper=fake_github_provider)
        # Should succeed — no conflict, no content loss
        assert result is True
