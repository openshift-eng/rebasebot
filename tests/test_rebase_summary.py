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

"""Tests for RebaseSummary metadata collected during rebase."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest
from git import Repo
from github3.pulls import ShortPullRequest

from rebasebot.bot import _cherrypick_art_pull_request, _do_rebase, _init_working_dir, _prepare_rebase_branch
from rebasebot.github import GitHubBranch
from rebasebot.rebase_summary import ArtPrInfo

from .conftest import CommitBuilder
from .test_conflict_policy import _DOWNSTREAM_CARRY_CODE, _ORIGINAL_CODE, _UPSTREAM_ADDED_CODE

_GO_MODULES_CARRY_COMMIT_MESSAGE = "UPSTREAM: <carry>: Updating and vendoring go modules after an upstream rebase"


@dataclass
class WorkingRepoContext:
    source: GitHubBranch
    rebase: GitHubBranch
    dest: GitHubBranch
    working_repo: Repo

    def fetch_remotes(self) -> None:
        self.working_repo.git.fetch("--all")


@pytest.fixture
def working_repo_context(init_test_repositories, fake_github_provider, tmpdir) -> WorkingRepoContext:
    source, rebase, dest = init_test_repositories
    working_repo = _init_working_dir(
        source=source,
        dest=dest,
        rebase=rebase,
        github_app_provider=fake_github_provider,
        git_username="foo",
        git_email="foo@example.com",
        workdir=tmpdir,
    )
    return WorkingRepoContext(source, rebase, dest, working_repo)


class TestDoRebaseDroppedCommits:
    def test_excluded_commit(self, working_repo_context, fake_github_provider):
        ctx = working_repo_context
        CommitBuilder(ctx.source).add_file("upstream.txt", "x").commit("upstream change")
        excluded = CommitBuilder(ctx.dest).add_file("drop.txt", "x").commit("UPSTREAM: <carry>: excluded")
        CommitBuilder(ctx.dest).add_file("keep.txt", "x").commit("UPSTREAM: <carry>: keep")
        ctx.fetch_remotes()
        _prepare_rebase_branch(ctx.working_repo, ctx.source, ctx.dest)

        dropped, _ = _do_rebase(
            gitwd=ctx.working_repo,
            source=ctx.source,
            dest=ctx.dest,
            source_repo=fake_github_provider.github_app.repository.return_value,
            tag_policy="soft",
            bot_emails=[],
            exclude_commits=[excluded.hexsha],
            update_go_modules=False,
        )

        assert len(dropped) == 1
        assert dropped[0].sha == excluded.hexsha
        assert dropped[0].message == "UPSTREAM: <carry>: excluded"
        assert dropped[0].reason == "explicitly excluded via --exclude-commits"

    def test_tag_policy_drop(self, working_repo_context, fake_github_provider):
        ctx = working_repo_context
        CommitBuilder(ctx.source).add_file("upstream.txt", "x").commit("upstream change")
        CommitBuilder(ctx.dest).add_file("drop.txt", "x").commit("untagged commit")
        CommitBuilder(ctx.dest).add_file("keep.txt", "x").commit("UPSTREAM: <carry>: keep")
        ctx.fetch_remotes()
        _prepare_rebase_branch(ctx.working_repo, ctx.source, ctx.dest)

        dropped, _ = _do_rebase(
            gitwd=ctx.working_repo,
            source=ctx.source,
            dest=ctx.dest,
            source_repo=fake_github_provider.github_app.repository.return_value,
            tag_policy="strict",
            bot_emails=[],
            exclude_commits=[],
            update_go_modules=False,
        )

        assert len(dropped) == 1
        assert dropped[0].message == "untagged commit"
        assert dropped[0].reason == "dropped by tag policy"

    def test_go_modules_drop(self, working_repo_context, fake_github_provider):
        ctx = working_repo_context
        CommitBuilder(ctx.source).add_file("upstream.txt", "x").commit("upstream change")
        CommitBuilder(ctx.dest).add_file("vendor.txt", "x").commit(_GO_MODULES_CARRY_COMMIT_MESSAGE)
        CommitBuilder(ctx.dest).add_file("keep.txt", "x").commit("UPSTREAM: <carry>: keep")
        ctx.fetch_remotes()
        _prepare_rebase_branch(ctx.working_repo, ctx.source, ctx.dest)

        dropped, _ = _do_rebase(
            gitwd=ctx.working_repo,
            source=ctx.source,
            dest=ctx.dest,
            source_repo=fake_github_provider.github_app.repository.return_value,
            tag_policy="soft",
            bot_emails=[],
            exclude_commits=[],
            update_go_modules=True,
        )

        assert len(dropped) == 1
        assert dropped[0].message == _GO_MODULES_CARRY_COMMIT_MESSAGE
        assert dropped[0].reason == "superseded by Go module regeneration"


class TestDoRebaseContentLossWarnings:
    def test_warn_policy_populates_content_loss_warnings(self, working_repo_context, fake_github_provider):
        ctx = working_repo_context
        CommitBuilder(ctx.source).update_file("test.go", _ORIGINAL_CODE).commit("set up base code")
        CommitBuilder(ctx.dest).update_file("test.go", _ORIGINAL_CODE).commit("UPSTREAM: <carry>: sync base")
        CommitBuilder(ctx.source).update_file("test.go", _UPSTREAM_ADDED_CODE).commit("Add KMS key support")
        carry = (
            CommitBuilder(ctx.dest)
            .update_file("test.go", _DOWNSTREAM_CARRY_CODE)
            .commit("UPSTREAM: <carry>: add snapshot timeout")
        )
        ctx.fetch_remotes()
        _prepare_rebase_branch(ctx.working_repo, ctx.source, ctx.dest)

        dropped, content_loss_warnings = _do_rebase(
            gitwd=ctx.working_repo,
            source=ctx.source,
            dest=ctx.dest,
            source_repo=fake_github_provider.github_app.repository.return_value,
            tag_policy="soft",
            conflict_policy="warn",
            bot_emails=[],
            exclude_commits=[],
            update_go_modules=False,
        )

        assert dropped == []
        assert len(content_loss_warnings) >= 1
        kms_warnings = [
            warning
            for warning in content_loss_warnings
            if warning.file == "test.go" and any("ebsKmsKeyId" in line for line in warning.lost_lines)
        ]
        assert kms_warnings
        carry_warnings = [
            warning for warning in kms_warnings if warning.message == "UPSTREAM: <carry>: add snapshot timeout"
        ]
        if carry_warnings:
            assert carry_warnings[0].sha == carry.hexsha

    def test_auto_policy_leaves_content_loss_warnings_empty(self, working_repo_context, fake_github_provider):
        ctx = working_repo_context
        CommitBuilder(ctx.source).update_file("test.go", _ORIGINAL_CODE).commit("set up base code")
        CommitBuilder(ctx.dest).update_file("test.go", _ORIGINAL_CODE).commit("UPSTREAM: <carry>: sync base")
        CommitBuilder(ctx.source).update_file("test.go", _UPSTREAM_ADDED_CODE).commit("Add KMS key support")
        CommitBuilder(ctx.dest).update_file("test.go", _DOWNSTREAM_CARRY_CODE).commit(
            "UPSTREAM: <carry>: add snapshot timeout"
        )
        ctx.fetch_remotes()
        _prepare_rebase_branch(ctx.working_repo, ctx.source, ctx.dest)

        _, content_loss_warnings = _do_rebase(
            gitwd=ctx.working_repo,
            source=ctx.source,
            dest=ctx.dest,
            source_repo=fake_github_provider.github_app.repository.return_value,
            tag_policy="soft",
            conflict_policy="auto",
            bot_emails=[],
            exclude_commits=[],
            update_go_modules=False,
        )

        assert content_loss_warnings == []


class TestCherrypickArtPullRequest:
    @patch("rebasebot.bot._safe_cherry_pick")
    def test_returns_art_pr_info_when_matching_pr_exists(self, _mock_safe_cherry_pick, working_repo_context):
        ctx = working_repo_context
        art_pr = MagicMock(spec=ShortPullRequest)
        art_pr.title = "Update build image to be consistent with ART"
        art_pr.user = MagicMock(login="openshift-bot")
        art_pr.number = 42
        art_pr.html_url = "https://github.com/downstream/repo/pull/42"
        repository = MagicMock()
        repository.name = "fork"
        repository.html_url = "https://github.com/downstream/fork"
        art_pr.head = MagicMock(repository=repository, ref="art-update")
        art_pr.commits.return_value = []

        dest_repo = MagicMock()
        dest_repo.pull_requests.return_value = [art_pr]

        if "fork" not in [remote.name for remote in ctx.working_repo.remotes]:
            ctx.working_repo.create_remote("fork", ctx.dest.url)

        with patch("git.remote.Remote.fetch"):
            result, content_loss = _cherrypick_art_pull_request(ctx.working_repo, dest_repo, ctx.dest)

        assert result == ArtPrInfo(
            number=42,
            title="Update build image to be consistent with ART",
            url="https://github.com/downstream/repo/pull/42",
        )
        assert content_loss == []

    def test_returns_none_when_no_matching_pr_exists(self, working_repo_context):
        ctx = working_repo_context
        other_pr = MagicMock(spec=ShortPullRequest)
        other_pr.title = "Unrelated change"
        other_pr.user = MagicMock(login="openshift-bot")

        dest_repo = MagicMock()
        dest_repo.pull_requests.return_value = [other_pr]

        result, content_loss = _cherrypick_art_pull_request(ctx.working_repo, dest_repo, ctx.dest)

        assert result is None
        assert content_loss == []
