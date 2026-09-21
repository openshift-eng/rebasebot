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
import os
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


# Shared blob content used to provoke false rename/delete conflicts: a large
# downstream delete set plus a new upstream file with near-identical content.
_VENDOR_BLOB = 'package labels\n\nconst LabelKey = "app"\n' * 3
_VENDOR_FILE_COUNT = 20
_E2E_LABELS_FILE = "e2e_labels.go"
_E2E_LABELS_CONTENT = _VENDOR_BLOB + "// upstream e2e labels\n"


def _vendor_filename(index: int) -> str:
    return f"vendor_{index:02d}.go"


def _vendor_content(index: int) -> str:
    return _VENDOR_BLOB + f"// vendor file {index}\n"


def _prepare_working_repo(source, rebase, dest, fake_github_provider, tmpdir):
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
    return gitwd


def _setup_vendor_files_on_source_and_dest(source, dest):
    source_builder = CommitBuilder(source)
    dest_builder = CommitBuilder(dest)
    for i in range(_VENDOR_FILE_COUNT):
        source_builder.add_file(_vendor_filename(i), _vendor_content(i))
        dest_builder.add_file(_vendor_filename(i), _vendor_content(i))
    source_builder.commit("add vendor stand-ins")
    dest_builder.commit("UPSTREAM: <carry>: add vendor stand-ins")


class TestFalseRenameDeleteResolution:
    """Regression tests for false rename/delete keep-versus-delete handling."""

    def test_false_rename_delete_keeps_head_file(self, init_test_repositories, fake_github_provider, tmpdir):
        """HEAD file not listed in the picked commit survives a false rename/delete conflict."""
        source, rebase, dest = init_test_repositories
        _setup_vendor_files_on_source_and_dest(source, dest)

        source_drop = CommitBuilder(source)
        for i in range(_VENDOR_FILE_COUNT):
            source_drop.remove_file(_vendor_filename(i))
        source_drop.add_file(_E2E_LABELS_FILE, _E2E_LABELS_CONTENT).commit(
            "upstream: drop vendor stand-ins, add e2e labels"
        )

        dest_pick = CommitBuilder(dest)
        for i in range(_VENDOR_FILE_COUNT):
            dest_pick.remove_file(_vendor_filename(i))
        # Extra path so the pick still has a real change after keeping the false rename.
        carry = dest_pick.add_file("carry_marker.txt", "marker\n").commit("UPSTREAM: <carry>: remove vendor stand-ins")

        gitwd = _prepare_working_repo(source, rebase, dest, fake_github_provider, tmpdir)
        assert _E2E_LABELS_FILE in gitwd.git.ls_files().splitlines()

        result = _safe_cherry_pick(
            gitwd=gitwd,
            sha=carry.hexsha,
            source_branch=source.branch,
            conflict_policy="auto",
            commit_description=f"{carry.hexsha} - UPSTREAM: <carry>: remove vendor stand-ins",
        )

        assert result.created_commit is True
        assert _E2E_LABELS_FILE in gitwd.git.ls_files().splitlines()
        assert "carry_marker.txt" in gitwd.git.ls_files().splitlines()
        with open(f"{gitwd.working_dir}/{_E2E_LABELS_FILE}", encoding="utf8") as f:
            assert f.read() == _E2E_LABELS_CONTENT

    def test_legitimate_delete_removes_path(self, init_test_repositories, fake_github_provider, tmpdir):
        """A path listed in the picked commit is still removed on modify/delete conflict."""
        source, rebase, dest = init_test_repositories

        CommitBuilder(source).update_file("test.go", "upstream modified content\n").commit("modify test.go")
        carry = CommitBuilder(dest).remove_file("test.go").commit("UPSTREAM: <carry>: remove test.go")

        gitwd = _prepare_working_repo(source, rebase, dest, fake_github_provider, tmpdir)
        assert "test.go" in gitwd.git.ls_files().splitlines()

        result = _safe_cherry_pick(
            gitwd=gitwd,
            sha=carry.hexsha,
            source_branch=source.branch,
            conflict_policy="auto",
            commit_description=f"{carry.hexsha} - UPSTREAM: <carry>: remove test.go",
        )

        assert result.created_commit is True
        assert "test.go" not in gitwd.git.ls_files().splitlines()

    def test_legitimate_delete_removes_non_ascii_path(self, init_test_repositories, fake_github_provider, tmpdir):
        """Quoted non-ASCII paths in the picked commit still delete (path unescape must match)."""
        source, rebase, dest = init_test_repositories
        non_ascii_name = "café.txt"

        CommitBuilder(source).add_file(non_ascii_name, "upstream café\n").commit("add non-ascii file")
        CommitBuilder(dest).add_file(non_ascii_name, "downstream café\n").commit(
            "UPSTREAM: <carry>: add non-ascii file"
        )
        CommitBuilder(source).update_file(non_ascii_name, "upstream modified café\n").commit(
            "modify non-ascii file upstream"
        )
        carry = CommitBuilder(dest).remove_file(non_ascii_name).commit("UPSTREAM: <carry>: remove non-ascii file")

        gitwd = _prepare_working_repo(source, rebase, dest, fake_github_provider, tmpdir)
        non_ascii_path = os.path.join(gitwd.working_dir, non_ascii_name)
        assert os.path.exists(non_ascii_path)

        result = _safe_cherry_pick(
            gitwd=gitwd,
            sha=carry.hexsha,
            source_branch=source.branch,
            conflict_policy="auto",
            commit_description=f"{carry.hexsha} - UPSTREAM: <carry>: remove non-ascii file",
        )

        assert result.created_commit is True
        assert not os.path.exists(non_ascii_path)

    def test_empty_after_resolution_skips_pick(self, init_test_repositories, fake_github_provider, tmpdir):
        """Keeping false renames with no remaining changes skips instead of failing or empty-committing."""
        source, rebase, dest = init_test_repositories
        _setup_vendor_files_on_source_and_dest(source, dest)

        source_drop = CommitBuilder(source)
        for i in range(_VENDOR_FILE_COUNT):
            source_drop.remove_file(_vendor_filename(i))
        source_drop.add_file(_E2E_LABELS_FILE, _E2E_LABELS_CONTENT).commit(
            "upstream: drop vendor stand-ins, add e2e labels"
        )

        dest_pick = CommitBuilder(dest)
        for i in range(_VENDOR_FILE_COUNT):
            dest_pick.remove_file(_vendor_filename(i))
        carry = dest_pick.commit("UPSTREAM: <carry>: remove vendor stand-ins")

        gitwd = _prepare_working_repo(source, rebase, dest, fake_github_provider, tmpdir)
        head_before = gitwd.head.commit.hexsha

        result = _safe_cherry_pick(
            gitwd=gitwd,
            sha=carry.hexsha,
            source_branch=source.branch,
            conflict_policy="auto",
            commit_description=f"{carry.hexsha} - UPSTREAM: <carry>: remove vendor stand-ins",
        )

        assert result.created_commit is False
        assert gitwd.head.commit.hexsha == head_before
        assert _E2E_LABELS_FILE in gitwd.git.ls_files().splitlines()
