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
from rebasebot.bot import _init_working_dir, _prepare_rebase_branch, _safe_cherry_pick
from rebasebot.pr_body import build_pr_body
from rebasebot.prow import ProwJobContext

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
    @patch("rebasebot.slack.requests.post")
    def test_auto_policy_silent_on_conflict(
        self,
        mocked_post,
        mocked_is_pr_available,
        mocked_push_rebase_branch,
        mocked_create_pr,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
    ):
        """With auto policy, -Xtheirs conflicts resolve silently."""
        source, rebase, dest = init_test_repositories
        mocked_is_pr_available.return_value = None, False
        mocked_push_rebase_branch.return_value = True

        # Replace test.go with structured code in source (initial state)
        CommitBuilder(source).update_file("test.go", _ORIGINAL_CODE).commit("set up base code")

        # Sync dest to have the same base
        CommitBuilder(dest).update_file("test.go", _ORIGINAL_CODE).commit("UPSTREAM: <carry>: sync base")

        # Upstream adds new fields
        CommitBuilder(source).update_file("test.go", _UPSTREAM_ADDED_CODE).commit("Add KMS key support")

        # Downstream carry patch reformats and adds timeout
        CommitBuilder(dest).update_file("test.go", _DOWNSTREAM_CARRY_CODE).commit(
            "UPSTREAM: <carry>: add snapshot timeout"
        )

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

        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)
        # Should succeed silently
        assert result is True

    @patch("rebasebot.bot._create_pr")
    @patch("rebasebot.bot._push_rebase_branch")
    @patch("rebasebot.bot._is_pr_available")
    @patch("rebasebot.slack.requests.post")
    def test_warn_policy_logs_warning_on_content_loss(
        self,
        mocked_post,
        mocked_is_pr_available,
        mocked_push_rebase_branch,
        mocked_create_pr,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
        caplog,
    ):
        """With warn policy, warnings are logged but rebase succeeds."""
        source, rebase, dest = init_test_repositories
        mocked_is_pr_available.return_value = None, False
        mocked_push_rebase_branch.return_value = True

        CommitBuilder(source).update_file("test.go", _ORIGINAL_CODE).commit("set up base code")

        CommitBuilder(dest).update_file("test.go", _ORIGINAL_CODE).commit("UPSTREAM: <carry>: sync base")

        CommitBuilder(source).update_file("test.go", _UPSTREAM_ADDED_CODE).commit("Add KMS key support")

        CommitBuilder(dest).update_file("test.go", _DOWNSTREAM_CARRY_CODE).commit(
            "UPSTREAM: <carry>: add snapshot timeout"
        )

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
            result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

        assert result is True
        warning_messages = [r.message.lower() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("upstream content may have been dropped" in m for m in warning_messages), (
            f"Expected warning about dropped content, got: {warning_messages}"
        )
        mocked_create_pr.assert_not_called()

    @patch("rebasebot.bot._create_pr")
    @patch("rebasebot.bot._push_rebase_branch")
    @patch("rebasebot.bot._is_pr_available")
    @patch("rebasebot.slack.requests.post")
    def test_warn_policy_includes_content_loss_in_pr_body(
        self,
        mocked_post,
        mocked_is_pr_available,
        mocked_push_rebase_branch,
        mocked_create_pr,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
    ):
        """With warn policy, content loss warnings appear in the PR body."""
        source, rebase, dest = init_test_repositories
        mocked_is_pr_available.return_value = None, False
        mocked_push_rebase_branch.return_value = True
        mocked_create_pr.return_value = "https://github.com/downstream/repo/pull/1"

        CommitBuilder(source).update_file("test.go", _ORIGINAL_CODE).commit("set up base code")
        CommitBuilder(dest).update_file("test.go", _ORIGINAL_CODE).commit("UPSTREAM: <carry>: sync base")
        CommitBuilder(source).update_file("test.go", _UPSTREAM_ADDED_CODE).commit("Add KMS key support")
        CommitBuilder(dest).update_file("test.go", _DOWNSTREAM_CARRY_CODE).commit(
            "UPSTREAM: <carry>: add snapshot timeout"
        )

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
        args.dry_run = False

        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

        assert result is True
        mocked_create_pr.assert_called_once()
        summary = mocked_create_pr.call_args.kwargs["summary"]
        assert len(summary.content_loss_warnings) == 1
        assert summary.content_loss_warnings[0].file == "test.go"
        assert any("ebsKmsKeyId" in line for line in summary.content_loss_warnings[0].lost_lines)
        pr_body = build_pr_body(summary, source, dest, ProwJobContext.from_env())
        assert "## ⚠️ Possible upstream content loss" in pr_body
        assert "<details>" in pr_body
        assert "<summary>" in pr_body
        assert "test.go" in pr_body
        assert "ebsKmsKeyId" in pr_body

    @patch("rebasebot.bot._create_pr")
    @patch("rebasebot.bot._push_rebase_branch")
    @patch("rebasebot.bot._is_pr_available")
    @patch("rebasebot.slack.requests.post")
    def test_strict_policy_fails_on_content_loss(
        self,
        mocked_post,
        mocked_is_pr_available,
        mocked_push_rebase_branch,
        mocked_create_pr,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
    ):
        """With strict policy, upstream content loss causes failure."""
        source, rebase, dest = init_test_repositories
        mocked_is_pr_available.return_value = None, False
        mocked_push_rebase_branch.return_value = True

        CommitBuilder(source).update_file("test.go", _ORIGINAL_CODE).commit("set up base code")

        CommitBuilder(dest).update_file("test.go", _ORIGINAL_CODE).commit("UPSTREAM: <carry>: sync base")

        CommitBuilder(source).update_file("test.go", _UPSTREAM_ADDED_CODE).commit("Add KMS key support")

        CommitBuilder(dest).update_file("test.go", _DOWNSTREAM_CARRY_CODE).commit(
            "UPSTREAM: <carry>: add snapshot timeout"
        )

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

        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)
        # Should fail — upstream content was lost
        assert result is False

    @patch("rebasebot.bot._create_pr")
    @patch("rebasebot.bot._push_rebase_branch")
    @patch("rebasebot.bot._is_pr_available")
    @patch("rebasebot.slack.requests.post")
    def test_strict_policy_succeeds_when_no_conflict(
        self,
        mocked_post,
        mocked_is_pr_available,
        mocked_push_rebase_branch,
        mocked_create_pr,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
    ):
        """With strict policy, clean cherry-picks succeed (no false positives)."""
        source, rebase, dest = init_test_repositories
        mocked_is_pr_available.return_value = None, False
        mocked_push_rebase_branch.return_value = True

        # Source adds an unrelated new file
        CommitBuilder(source).add_file("new_upstream_file.go", "package main\nfunc upstream() {}\n").commit(
            "Add new upstream file"
        )

        # Dest adds a different unrelated file (no conflict)
        CommitBuilder(dest).add_file("downstream_only.go", "package main\nfunc downstream() {}\n").commit(
            "UPSTREAM: <carry>: downstream only file"
        )

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

        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)
        # Should succeed — no conflict, no content loss
        assert result is True


class TestSafeCherryPickReturnValue:
    def test_warn_policy_returns_structured_content_loss(self, init_test_repositories, fake_github_provider, tmpdir):
        source, rebase, dest = init_test_repositories

        CommitBuilder(source).update_file("test.go", _ORIGINAL_CODE).commit("set up base code")
        CommitBuilder(dest).update_file("test.go", _ORIGINAL_CODE).commit("UPSTREAM: <carry>: sync base")
        CommitBuilder(source).update_file("test.go", _UPSTREAM_ADDED_CODE).commit("Add KMS key support")
        carry = (
            CommitBuilder(dest)
            .update_file("test.go", _DOWNSTREAM_CARRY_CODE)
            .commit("UPSTREAM: <carry>: add snapshot timeout")
        )

        gitwd = _init_working_dir(
            source=source,
            dest=dest,
            rebase=rebase,
            github_app_provider=fake_github_provider,
            git_username="test_rebasebot",
            git_email="test@rebasebot.ocp",
            workdir=tmpdir,
        )
        gitwd.remotes.source.fetch(source.branch)
        gitwd.remotes.dest.fetch(dest.branch)
        _prepare_rebase_branch(gitwd, source, dest)

        result = _safe_cherry_pick(
            gitwd=gitwd,
            sha=carry.hexsha,
            source_branch=source.branch,
            conflict_policy="warn",
            commit_description=f"{carry.hexsha} - UPSTREAM: <carry>: add snapshot timeout",
        )

        assert result.created_commit is True
        assert len(result.content_loss) == 1
        filename, lost_lines = result.content_loss[0]
        assert filename == "test.go"
        assert any("ebsKmsKeyId" in line for line in lost_lines)

    def test_auto_policy_returns_empty_content_loss(self, init_test_repositories, fake_github_provider, tmpdir):
        source, rebase, dest = init_test_repositories

        CommitBuilder(source).update_file("test.go", _ORIGINAL_CODE).commit("set up base code")
        CommitBuilder(dest).update_file("test.go", _ORIGINAL_CODE).commit("UPSTREAM: <carry>: sync base")
        CommitBuilder(source).update_file("test.go", _UPSTREAM_ADDED_CODE).commit("Add KMS key support")
        carry = (
            CommitBuilder(dest)
            .update_file("test.go", _DOWNSTREAM_CARRY_CODE)
            .commit("UPSTREAM: <carry>: add snapshot timeout")
        )

        gitwd = _init_working_dir(
            source=source,
            dest=dest,
            rebase=rebase,
            github_app_provider=fake_github_provider,
            git_username="test_rebasebot",
            git_email="test@rebasebot.ocp",
            workdir=tmpdir,
        )
        gitwd.remotes.source.fetch(source.branch)
        gitwd.remotes.dest.fetch(dest.branch)
        _prepare_rebase_branch(gitwd, source, dest)

        result = _safe_cherry_pick(
            gitwd=gitwd,
            sha=carry.hexsha,
            source_branch=source.branch,
            conflict_policy="auto",
            commit_description=f"{carry.hexsha} - UPSTREAM: <carry>: add snapshot timeout",
        )

        assert result.created_commit is True
        assert result.content_loss == []
