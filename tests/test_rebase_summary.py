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

import pytest
from git import Repo

from rebasebot.bot import _do_rebase, _init_working_dir, _prepare_rebase_branch
from rebasebot.github import GitHubBranch

from .conftest import CommitBuilder

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

        dropped = _do_rebase(
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

        dropped = _do_rebase(
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

        dropped = _do_rebase(
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
