from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from unittest.mock import ANY, MagicMock, patch

import git
import pytest
from git import Repo

from rebasebot import cli
from rebasebot.bot import (
    RepoException,
    _apply_merge_only_delta,
    _init_working_dir,
    _needs_rebase,
    _prepare_rebase_branch,
    _tree_entry_for_path,
)
from rebasebot.github import GitHubBranch, parse_github_branch

from .conftest import CommitBuilder


@dataclass
class WorkingRepoContext:
    source: GitHubBranch
    rebase: GitHubBranch
    dest: GitHubBranch

    working_repo: Repo
    working_repo_path: str

    def fetch_remotes(self):
        self.working_repo.git.fetch("--all")


def _make_rebase_args(
    source,
    rebase,
    dest,
    working_dir: str,
    *,
    tag_policy: str = "soft",
    bot_emails: list[str] | None = None,
    exclude_commits: list[str] | None = None,
    always_run_hooks: bool = False,
):
    args = MagicMock()
    args.source = source
    args.source_repo = None
    args.dest = dest
    args.rebase = rebase
    args.working_dir = working_dir
    args.git_username = "test_rebasebot"
    args.git_email = "test@rebasebot.ocp"
    args.tag_policy = tag_policy
    args.bot_emails = bot_emails or []
    args.exclude_commits = exclude_commits or []
    args.update_go_modules = False
    args.conflict_policy = "auto"
    args.ignore_manual_label = False
    args.dry_run = True
    args.always_run_hooks = always_run_hooks
    args.title_prefix = ""
    args.pre_rebase_hook = None
    args.post_rebase_hook = None
    args.pre_carry_commit_hook = None
    args.pre_push_rebase_branch_hook = None
    args.pre_create_pr_hook = None
    args.source_ref_hook = None
    return args


def _merge_rebase_branch_into_dest(
    dest: GitHubBranch,
    rebase_worktree: str,
    remote_name: str,
    message: str = "Merge rebase PR into main",
) -> Repo:
    dest_repo = Repo(dest.url)
    dest_repo.git.checkout(dest.branch)
    if remote_name not in [remote.name for remote in dest_repo.remotes]:
        dest_repo.create_remote(remote_name, rebase_worktree)
    dest_repo.remotes[remote_name].fetch("rebase")
    dest_repo.git.merge(f"{remote_name}/rebase", "--no-ff", "-m", message)
    return dest_repo


class TestBotInternalHelpers:
    @pytest.fixture
    def working_repo_context(self, init_test_repositories, fake_github_provider, tmpdir) -> WorkingRepoContext:
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
        return WorkingRepoContext(source, rebase, dest, working_repo, tmpdir)

    def test_workdir_init(self, working_repo_context):
        working_repo = working_repo_context.working_repo
        working_repo_path = working_repo_context.working_repo_path

        assert working_repo
        assert len(working_repo.remotes)
        assert working_repo.working_dir == working_repo_path

        commits = list(working_repo.iter_commits())
        assert len(commits) == 1
        assert commits[0].message == "Upstream commit\n"

        working_repo_dir_content = {i.name for i in os.scandir(working_repo_path)}
        assert working_repo_dir_content == {"test.go", ".git"}

    def test_needs_rebase(self, working_repo_context):
        r_ctx = working_repo_context
        gitwd, source, dest = r_ctx.working_repo, r_ctx.source, r_ctx.dest
        assert not _needs_rebase(gitwd, source, dest)

        CommitBuilder(dest).add_file("bar.txt", "foo").commit("UPSTREAM: <carry>: carry patch")
        working_repo_context.fetch_remotes()
        assert not _needs_rebase(gitwd, source, dest)

        CommitBuilder(source).add_file("baz.txt", "fiz").commit("some other upstream commit")
        working_repo_context.fetch_remotes()
        assert _needs_rebase(gitwd, source, dest)

    def test_prepare_rebase_branch(self, working_repo_context):
        r_ctx = working_repo_context
        _prepare_rebase_branch(r_ctx.working_repo, r_ctx.source, r_ctx.dest)

        commits = list(r_ctx.working_repo.iter_commits())
        assert len(commits[0].parents)
        commit_msgs = [c.summary for c in commits]
        assert commit_msgs == [
            "merge upstream/main into main",  # merge commit
            "UPSTREAM: <carry>: our cool addition",
            "Upstream commit",
        ]

    def test_tree_entry_for_path_propagates_real_lookup_failures(self):
        gitwd = MagicMock()
        gitwd.git.ls_tree.side_effect = git.GitCommandError("ls-tree", 128, stderr="fatal: bad tree object")

        with pytest.raises(git.GitCommandError, match="ls-tree"):
            _tree_entry_for_path(gitwd, "missing-ref", "test.go")


class TestRebases:
    def test_simple_dry_run(self, init_test_repositories, fake_github_provider, tmpdir):
        source, rebase, dest = init_test_repositories
        CommitBuilder(source).add_file("baz.txt", "fiz").commit("other upstream commit")

        args = MagicMock()
        args.source = source
        args.source_repo = None
        args.dest = dest
        args.rebase = rebase
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "soft"
        args.bot_emails = []
        args.exclude_commits = []
        args.update_go_modules = False
        args.conflict_policy = "auto"
        args.ignore_manual_label = False
        args.dry_run = True
        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)
        assert result

        working_repo = Repo.init(tmpdir)
        assert working_repo.head.ref.name == "rebase"
        log_graph = working_repo.git.log("--graph", "--oneline", "--pretty='<%an>, %s'")
        assert (
            log_graph
            == """ 
* '<dest_author>, UPSTREAM: <carry>: our cool addition'
*   '<test_rebasebot>, merge upstream/main into main'
|\\  
| * '<source_author>, other upstream commit'
* | '<dest_author>, UPSTREAM: <carry>: our cool addition'
|/  
* '<source_author>, Upstream commit'
""".strip()  # noqa: W291
        )

    # Tests that all commits from bots are squashed into one for each bot

    def test_squash_bot_dry_run(self, init_test_repositories, fake_github_provider, tmpdir):
        source, rebase, dest = init_test_repositories
        with CommitBuilder(source) as cb:
            cb.add_file("baz.txt", "fiz")
            cb.commit("other upstream commit")
        with CommitBuilder(dest) as cb:
            cb.add_file("generated-test", "content")
            cb.commit("commit #1 from genbot", committer_email="genbot@example.com")
        with CommitBuilder(dest) as cb:
            cb.add_file("generated-test2", "content")
            cb.commit("commit #2 from genbot", committer_email="genbot@example.com")
        with CommitBuilder(dest) as cb:
            cb.add_file("generated-test3", "content")
            cb.commit("commit #1 from anotherbot", committer_email="anotherbot@example.com")

        args = MagicMock()
        args.source = source
        args.source_repo = None
        args.dest = dest
        args.rebase = rebase
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "soft"
        args.bot_emails = ["genbot@example.com", "anotherbot@example.com"]
        args.exclude_commits = []
        args.update_go_modules = False
        args.conflict_policy = "auto"
        args.ignore_manual_label = False
        args.dry_run = True
        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)
        assert result

        working_repo = Repo.init(tmpdir)
        assert working_repo.head.ref.name == "rebase"
        log_graph = working_repo.git.log("--graph", "--oneline", "--pretty='<%an>, %s'")

        assert (
            log_graph
            == r"""
* '<dest_anotherbot@example.com>, commit #1 from anotherbot'
* '<dest_genbot@example.com>, commit #2 from genbot'
* '<dest_author>, UPSTREAM: <carry>: our cool addition'
*   '<test_rebasebot>, merge upstream/main into main'
|\  
| * '<source_author>, other upstream commit'
* | '<dest_anotherbot@example.com>, commit #1 from anotherbot'
* | '<dest_genbot@example.com>, commit #2 from genbot'
* | '<dest_genbot@example.com>, commit #1 from genbot'
* | '<dest_author>, UPSTREAM: <carry>: our cool addition'
|/  
* '<source_author>, Upstream commit'
""".strip()  # noqa: W291
        )

    def test_first_run_dest_has_merges_dry_run(self, init_test_repositories, fake_github_provider, tmpdir):
        source, rebase, dest = init_test_repositories
        with CommitBuilder(source) as cb:
            cb.add_file("baz.txt", "fiz")
            cb.commit("other upstream commit")
        with CommitBuilder(dest) as cb:
            cb.add_file("generated-test", "content")
            cb.commit("commit #1 from genbot", committer_email="genbot@example.com")
        # make branch
        dest_feature_branch = GitHubBranch(url=dest.url, ns="dest", name="dest", branch="feature")
        with CommitBuilder(dest_feature_branch) as cb:
            cb.add_file("feature-file", "feature content")
            cb.commit("commit on dest feature branch")
        with CommitBuilder(dest) as cb:
            cb.add_file("generated-test2", "content")
            cb.commit("commit #2 from genbot", committer_email="genbot@example.com")
        # merge feature branch to dest
        repo = Repo(dest.url)
        repo.git.checkout(dest.branch)
        repo.git.merge(repo.heads.feature)
        with CommitBuilder(dest) as cb:
            cb.add_file("generated-test3", "content")
            cb.commit("commit #1 from anotherbot", committer_email="anotherbot@example.com")

        args = MagicMock()
        args.source = source
        args.source_repo = None
        args.dest = dest
        args.rebase = rebase
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "soft"
        args.bot_emails = []
        args.exclude_commits = []
        args.update_go_modules = False
        args.conflict_policy = "auto"
        args.ignore_manual_label = False
        args.dry_run = True
        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)
        assert result

        working_repo = Repo.init(tmpdir)
        assert working_repo.head.ref.name == "rebase"
        log_graph = working_repo.git.log("--graph", "--oneline", "--pretty='<%an>, %s'")

        assert (
            log_graph
            == r"""
* '<dest_anotherbot@example.com>, commit #1 from anotherbot'
* '<dest_author>, commit on dest feature branch'
* '<dest_genbot@example.com>, commit #2 from genbot'
* '<dest_genbot@example.com>, commit #1 from genbot'
* '<dest_author>, UPSTREAM: <carry>: our cool addition'
*   '<test_rebasebot>, merge upstream/main into main'
|\  
| * '<source_author>, other upstream commit'
* | '<dest_anotherbot@example.com>, commit #1 from anotherbot'
* |   '<dest_genbot@example.com>, Merge branch 'feature''
|\ \  
| * | '<dest_author>, commit on dest feature branch'
* | | '<dest_genbot@example.com>, commit #2 from genbot'
|/ /  
* | '<dest_genbot@example.com>, commit #1 from genbot'
* | '<dest_author>, UPSTREAM: <carry>: our cool addition'
|/  
* '<source_author>, Upstream commit'
""".strip()  # noqa: W291
        )

    def test_first_run_dest_merges_feature_branch_dry_run(self, init_test_repositories, fake_github_provider, tmpdir):
        source, rebase, dest = init_test_repositories

        # Ensure source/main has advanced so a rebase is required
        with CommitBuilder(source) as cb:
            cb.add_file("bar.txt", "fiz")
            cb.commit("other upstream commit")

        # Git reset to remove one commit from dest main
        repo = Repo(dest.url)
        repo.git.checkout(dest.branch)
        repo.git.reset("--hard", "HEAD~1")

        # Create feature branch in dest, make commit there, then merge into dest/main
        dest_feature_branch = GitHubBranch(url=dest.url, ns="dest", name="dest", branch="feature")
        with CommitBuilder(dest_feature_branch) as cb:
            cb.add_file("carry-commit-file", "content")
            cb.commit("UPSTREAM: <carry>: commit #1 from anotherbot", committer_email="anotherbot@example.com")
        repo.git.checkout(dest.branch)
        repo.git.merge("--no-ff", "-m", "Merge branch 'feature'", repo.heads.feature)

        args = MagicMock()
        args.source = source
        args.source_repo = None
        args.dest = dest
        args.rebase = rebase
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "soft"
        args.bot_emails = []
        args.exclude_commits = []
        args.update_go_modules = False
        args.conflict_policy = "auto"
        args.ignore_manual_label = False
        args.dry_run = True

        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)
        assert result

        working_repo = Repo.init(tmpdir)
        log_graph = working_repo.git.log("--graph", "--oneline", "--pretty='<%an>, %s'")
        assert log_graph == "\n".join(
            [
                "* '<dest_anotherbot@example.com>, UPSTREAM: <carry>: commit #1 from anotherbot'",
                "*   '<test_rebasebot>, merge upstream/main into main'",
                r"|\  ",
                "| * '<source_author>, other upstream commit'",
                "* |   '<dest_anotherbot@example.com>, Merge branch 'feature''",
                r"|\ \  ",
                r"| |/  ",
                r"|/|   ",
                "| * '<dest_anotherbot@example.com>, UPSTREAM: <carry>: commit #1 from anotherbot'",
                r"|/  ",
                "* '<source_author>, Upstream commit'",
            ]
        )

    @patch("rebasebot.bot._push_rebase_branch")
    @patch("rebasebot.bot._is_pr_available")
    @patch("rebasebot.bot._message_slack")
    def test_conflict(
        self,
        mocked_message_slack,
        mocked_is_pr_available,
        mocked_push_rebase_branch,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
    ):
        source, rebase, dest = init_test_repositories
        mocked_is_pr_available.return_value = None, False
        mocked_push_rebase_branch.return_value = True
        CommitBuilder(source).update_file("test.go", "new content").commit("update test.go")
        CommitBuilder(dest).remove_file("test.go").commit("remove test.go")
        with CommitBuilder(dest) as cb:
            cb.commit("Empty commit")

        args = MagicMock()
        args.source = source
        args.source_repo = None
        args.dest = dest
        args.rebase = rebase
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "soft"
        args.bot_emails = ["genbot@example.com", "anotherbot@example.com"]
        args.exclude_commits = []
        args.update_go_modules = False
        args.conflict_policy = "auto"
        args.ignore_manual_label = False
        args.dry_run = False

        result = cli.rebasebot_run(args, slack_webhook="test://webhook", github_app_wrapper=fake_github_provider)
        assert result

        working_repo = Repo.init(tmpdir)
        assert working_repo.head.ref.name == "rebase"
        log_graph = working_repo.git.log("--graph", "--oneline", "--pretty='<%an>, %s'")

        assert mocked_message_slack.call_args.args[0] == "test://webhook"
        assert mocked_message_slack.call_args.args[1].startswith("I created a new rebase PR:")

        assert (
            log_graph
            == r"""
* '<dest_author>, remove test.go'
* '<dest_author>, UPSTREAM: <carry>: our cool addition'
*   '<test_rebasebot>, merge upstream/main into main'
|\  
| * '<source_author>, update test.go'
* | '<dest_author>, Empty commit'
* | '<dest_author>, remove test.go'
* | '<dest_author>, UPSTREAM: <carry>: our cool addition'
|/  
* '<source_author>, Upstream commit'
""".strip()  # noqa: W291
        )

    @patch("rebasebot.bot._message_slack")
    @patch("rebasebot.bot._manual_rebase_pr_in_repo")
    def test_has_manual_rebase_pr(
        self,
        mocked_manual_rebase_pr_in_repo,
        mocked_message_slack,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
    ):
        source, rebase, dest = init_test_repositories
        dest.clone_url = "https://github.com/dest/dest"

        pr = MagicMock()
        pr.labels = [{"name": "rebase/manual"}]
        pr.html_url = "https://github.com/dest/dest/pull/1"
        mocked_manual_rebase_pr_in_repo.return_value = pr

        # Mock GitHub repository lookup function
        def fake_repository_func(namespace, name):
            repository = MagicMock()
            repository.clone_url = f"https://github.com/{namespace}/{name}"
            return repository

        fake_github_provider.github_app.repository = fake_repository_func

        args = MagicMock()
        args.source = source
        args.source_repo = None
        args.dest = dest
        args.rebase = rebase
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "soft"
        args.bot_emails = []
        args.exclude_commits = []
        args.update_go_modules = False
        args.conflict_policy = "auto"
        args.ignore_manual_label = False
        args.dry_run = False
        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)
        mocked_message_slack.assert_called_once_with(
            None, f"Repo {dest.clone_url} has PR {pr.html_url} with 'rebase/manual' label, aborting"
        )

        assert result

    def test_strict_and_excluded_commits(self, init_test_repositories, fake_github_provider, tmpdir):
        source, rebase, dest = init_test_repositories
        with CommitBuilder(source) as cb:
            cb.add_file("baz.txt", "fiz")
            cb.commit("other upstream commit")
        with CommitBuilder(dest) as cb:  # this commit will be dropped by strict policy
            cb.add_file("carry-file0", "content")
            cb.commit("untagged commit")
        with CommitBuilder(dest) as cb:
            cb.add_file("carry-file1", "content")
            cb.commit("UPSTREAM: <carry>: carry commit #1")
        with CommitBuilder(dest) as cb:
            cb.add_file("carry-file2", "content")
            drop_commit = cb.commit("UPSTREAM: <carry>: dropped by exclude_commits")

        args = MagicMock()
        args.source = source
        args.source_repo = None
        args.dest = dest
        args.rebase = rebase
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "strict"
        args.bot_emails = ["genbot@example.com", "anotherbot@example.com"]
        args.exclude_commits = [drop_commit.hexsha]
        args.update_go_modules = False
        args.conflict_policy = "auto"
        args.dry_run = True
        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)
        assert result

        working_repo = Repo.init(tmpdir)
        assert working_repo.head.ref.name == "rebase"
        log_graph = working_repo.git.log("--graph", "--oneline", "--pretty='<%an>, %s'")

        assert (
            log_graph
            == r"""
* '<dest_author>, UPSTREAM: <carry>: carry commit #1'
* '<dest_author>, UPSTREAM: <carry>: our cool addition'
*   '<test_rebasebot>, merge upstream/main into main'
|\  
| * '<source_author>, other upstream commit'
* | '<dest_author>, UPSTREAM: <carry>: dropped by exclude_commits'
* | '<dest_author>, UPSTREAM: <carry>: carry commit #1'
* | '<dest_author>, untagged commit'
* | '<dest_author>, UPSTREAM: <carry>: our cool addition'
|/  
* '<source_author>, Upstream commit'
""".strip()  # noqa: W291
        )

    @patch("rebasebot.lifecycle_hooks._fetch_file_from_github")
    def test_lifecyclehooks_remote(
        self, mock_fetch_file_from_github, init_test_repositories, fake_github_provider, tmpdir, caplog
    ):
        source, rebase, dest = init_test_repositories

        mock_fetch_file_from_github.return_value.decoded = rb"""#!/bin/bash
touch test-hook-script.success
git add test-hook-script.success
git commit -m 'UPSTREAM: <drop>: test-hook-script generated files'
"""

        with CommitBuilder(source) as cb:
            cb.add_file("baz.txt", "fiz")
            cb.commit("other upstream commit")
        with CommitBuilder(dest) as cb:
            cb.add_file("carry-file1", "content")
            cb.commit("UPSTREAM: <carry>: carry commit #1")

        args = MagicMock()
        args.source = source
        args.source_repo = None
        args.dest = dest
        args.rebase = rebase
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "strict"
        args.bot_emails = ["genbot@example.com", "anotherbot@example.com"]
        args.exclude_commits = []
        args.update_go_modules = False
        args.conflict_policy = "auto"
        args.dry_run = True
        args.post_rebase_hook = ["git:https://github.com/openshift-eng/rebasebot/main:tests/data/test-hook-script.sh"]  # noqa: E501

        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

        mock_fetch_file_from_github.assert_called_once_with(
            ANY, "openshift-eng", "rebasebot", "main", "tests/data/test-hook-script.sh"
        )
        # mock_fetch_branch.assert_called_once_with(
        # ANY, "github.com/openshift-eng/rebasebot", "main",
        # ref_filter="blob:none")
        working_repo = Repo.init(tmpdir)
        assert working_repo.head.ref.name == "rebase"
        log_graph = working_repo.git.log("--graph", "--oneline", "--pretty='<%an>, %s'")

        assert os.path.exists(os.path.join(tmpdir, "test-hook-script.success"))
        assert (
            log_graph
            == r"""* '<test_rebasebot>, UPSTREAM: <drop>: test-hook-script generated files'
* '<dest_author>, UPSTREAM: <carry>: carry commit #1'
* '<dest_author>, UPSTREAM: <carry>: our cool addition'
*   '<test_rebasebot>, merge upstream/main into main'
|\  
| * '<source_author>, other upstream commit'
* | '<dest_author>, UPSTREAM: <carry>: carry commit #1'
* | '<dest_author>, UPSTREAM: <carry>: our cool addition'
|/  
* '<source_author>, Upstream commit'"""  # noqa: W291
        )
        assert result

    def test_lifecyclehooks(self, init_test_repositories, fake_github_provider, tmpdir):
        source, rebase, dest = init_test_repositories
        with CommitBuilder(source) as cb:
            cb.add_file("baz.txt", "fiz")
            cb.commit("other upstream commit")
        with CommitBuilder(dest) as cb:
            cb.add_file("carry-file1", "content")
            cb.commit("UPSTREAM: <carry>: carry commit #1")
        with CommitBuilder(dest) as cb:
            cb.add_file(
                "test-hook-script.sh",
                r"""#!/bin/bash
                touch test-hook-script.success
                git add test-hook-script.success
                git commit -m 'UPSTREAM: <drop>: test-hook-script generated files'""",
            )
            cb.commit("UPSTREAM: <carry>: add test hook script")

        args = MagicMock()
        args.source = source
        args.dest = dest
        args.rebase = rebase
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "strict"
        args.bot_emails = ["genbot@example.com", "anotherbot@example.com"]
        args.exclude_commits = []
        args.update_go_modules = False
        args.conflict_policy = "auto"
        args.dry_run = True
        args.post_rebase_hook = [f"git:dest/{dest.branch}:test-hook-script.sh"]
        args.source_repo = None

        assert cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

        working_repo = Repo.init(tmpdir)
        assert working_repo.head.ref.name == "rebase"
        log_graph = working_repo.git.log("--graph", "--oneline", "--pretty='<%an>, %s'")
        assert os.path.exists(os.path.join(tmpdir, "test-hook-script.success"))
        assert (
            log_graph
            == r"""* '<test_rebasebot>, UPSTREAM: <drop>: test-hook-script generated files'
* '<dest_author>, UPSTREAM: <carry>: add test hook script'
* '<dest_author>, UPSTREAM: <carry>: carry commit #1'
* '<dest_author>, UPSTREAM: <carry>: our cool addition'
*   '<test_rebasebot>, merge upstream/main into main'
|\  
| * '<source_author>, other upstream commit'
* | '<dest_author>, UPSTREAM: <carry>: add test hook script'
* | '<dest_author>, UPSTREAM: <carry>: carry commit #1'
* | '<dest_author>, UPSTREAM: <carry>: our cool addition'
|/  
* '<source_author>, Upstream commit'"""  # noqa: W291
        )

    def test_lifecyclehook_fail(self, init_test_repositories, fake_github_provider, tmpdir, caplog):
        source, rebase, dest = init_test_repositories
        with CommitBuilder(source) as cb:
            cb.add_file("baz.txt", "fiz")
            cb.commit("other upstream commit")
        with CommitBuilder(dest) as cb:
            cb.add_file("carry-file1", "content")
            cb.commit("UPSTREAM: <carry>: carry commit #1")
        with CommitBuilder(dest) as cb:
            cb.add_file(
                "test-failure-hook-script.sh",
                r"""#!/bin/bash
exit 5""",
            )
            cb.commit("UPSTREAM: <carry>: add test hook script")

        args = MagicMock()
        args.source = source
        args.source_repo = None
        args.dest = dest
        args.rebase = rebase
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.pre_rebase_hook = [f"git:dest/{dest.branch}:test-failure-hook-script.sh"]
        args.tag_policy = "strict"
        args.bot_emails = ["genbot@example.com", "anotherbot@example.com"]
        args.exclude_commits = []
        args.update_go_modules = False
        args.conflict_policy = "auto"
        args.dry_run = True

        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

        assert "Script git:dest/main:test-failure-hook-script.sh failed with exit code 5" in caplog.text
        assert "Manual intervention is needed to rebase" in caplog.text

        # Rebase did not succeed
        assert result is False

    @patch("rebasebot.cli.parse_github_branch")
    @patch("rebasebot.lifecycle_hooks.parse_github_branch")
    @patch("rebasebot.lifecycle_hooks._fetch_file_from_github")
    def test_source_ref_hook(
        self,
        mock_fetch_file_from_github,
        mock_parse_github_branch_hooks,
        mock_parse_github_branch_cli,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
    ):
        source, rebase, dest = init_test_repositories
        with CommitBuilder(source) as cb:
            cb.add_file("baz.txt", "fiz")
            cb.commit("other upstream commit")
        with CommitBuilder(dest) as cb:
            cb.add_file("carry-file1", "content")
            cb.commit("UPSTREAM: <carry>: carry commit #1")

            mock_fetch_file_from_github.return_value.decoded = rb"""#!/bin/sh
echo main
"""

        def fake_parse_github_branch(location):
            branch = parse_github_branch(location)
            assert location == "source/source:main"
            branch.url = source.url
            return branch

        mock_parse_github_branch_hooks.side_effect = fake_parse_github_branch
        mock_parse_github_branch_cli.side_effect = fake_parse_github_branch

        args = MagicMock()
        args.source = None
        args.source_repo = f"{source.ns}/{source.name}"
        url = "git:https://github.com/openshift-eng/rebasebot/main:tests/data/test-source-ref-hook-script.sh"
        args.source_ref_hook = url
        args.dest = dest
        args.rebase = rebase
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "strict"
        args.bot_emails = []
        args.exclude_commits = []
        args.update_go_modules = False
        args.conflict_policy = "auto"
        args.dry_run = True

        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

        assert args.source == source
        assert result

    def test_always_run_hooks_when_no_rebase_needed(self, init_test_repositories, fake_github_provider, tmpdir):
        """Test that hooks run when --always-run-hooks is True even when no rebase is needed."""
        source, rebase, dest = init_test_repositories

        # Create separate test hooks that create different marker files
        pre_rebase_hook_script = """#!/bin/bash
touch pre-rebase-hook.success"""

        post_rebase_hook_script = """#!/bin/bash
touch post-rebase-hook.success"""

        with CommitBuilder(dest) as cb:
            cb.add_file("pre-rebase-hook-script.sh", pre_rebase_hook_script)
            cb.add_file("post-rebase-hook-script.sh", post_rebase_hook_script)
            cb.commit("UPSTREAM: <carry>: add test hook scripts")

        # Configure args with always_run_hooks=True and multiple hook types to test
        args = MagicMock()
        args.source = source
        args.source_repo = None
        args.dest = dest
        args.rebase = rebase
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "none"
        args.bot_emails = []
        args.exclude_commits = []
        args.update_go_modules = False
        args.conflict_policy = "auto"
        args.dry_run = True
        args.ignore_manual_label = True
        args.always_run_hooks = True

        # Test multiple hook types with different scripts
        args.pre_rebase_hook = [f"git:dest/{dest.branch}:pre-rebase-hook-script.sh"]
        args.post_rebase_hook = [f"git:dest/{dest.branch}:post-rebase-hook-script.sh"]
        args.pre_carry_commit_hook = None
        args.pre_push_rebase_branch_hook = None
        args.pre_create_pr_hook = None

        # Verify no rebase is needed initially (source and dest are in sync)
        # But hooks should still run due to always_run_hooks=True
        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

        assert result is True
        # Verify both hooks executed by checking for their marker files
        assert "pre-rebase-hook.success" in os.listdir(tmpdir)
        assert "post-rebase-hook.success" in os.listdir(tmpdir)

    def test_hooks_not_run_when_no_rebase_needed_and_flag_false(
        self, init_test_repositories, fake_github_provider, tmpdir
    ):
        """Test that hooks DON'T run when --always-run-hooks is False and no rebase is needed."""
        source, rebase, dest = init_test_repositories

        # Create separate test hooks that create different marker files
        pre_rebase_hook_script = """#!/bin/bash
touch pre-rebase-hook.success"""

        post_rebase_hook_script = """#!/bin/bash
touch post-rebase-hook.success"""

        with CommitBuilder(dest) as cb:
            cb.add_file("pre-rebase-hook-script.sh", pre_rebase_hook_script)
            cb.add_file("post-rebase-hook-script.sh", post_rebase_hook_script)
            cb.commit("UPSTREAM: <carry>: add test hook scripts")

        # Configure args with always_run_hooks=False (default behavior)
        args = MagicMock()
        args.source = source
        args.source_repo = None
        args.dest = dest
        args.rebase = rebase
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "none"
        args.bot_emails = []
        args.exclude_commits = []
        args.update_go_modules = False
        args.conflict_policy = "auto"
        args.dry_run = True
        args.ignore_manual_label = True
        args.always_run_hooks = False  # Key difference: hooks should NOT run

        args.pre_rebase_hook = [f"git:dest/{dest.branch}:pre-rebase-hook-script.sh"]
        args.post_rebase_hook = [f"git:dest/{dest.branch}:post-rebase-hook-script.sh"]
        args.pre_carry_commit_hook = None
        args.pre_push_rebase_branch_hook = None
        args.pre_create_pr_hook = None

        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

        assert result is True
        # Verify hooks were NOT executed - marker files should not exist
        assert "pre-rebase-hook.success" not in os.listdir(tmpdir)
        assert "post-rebase-hook.success" not in os.listdir(tmpdir)

    def test_always_run_hooks_preserves_carry_commits(self, init_test_repositories, fake_github_provider, tmpdir):
        """Test that hooks run on top of dest branch (not source) when no rebase is needed.

        This verifies the fix for a bug where --always-run-hooks would run hooks
        on top of the source/upstream branch, producing a rebase branch missing
        all downstream carry commits. The resulting PR would have merge conflicts
        because it tried to merge a branch without carries into dest which has them.
        """
        source, rebase, dest = init_test_repositories

        # Add a carry commit with a downstream-only file to dest
        with CommitBuilder(dest) as cb:
            cb.add_file("DOWNSTREAM_OWNERS", "approvers:\n- testuser\n")
            cb.commit("UPSTREAM: <carry>: add DOWNSTREAM_OWNERS")

        # Hook that verifies DOWNSTREAM_OWNERS exists (i.e. we're on dest, not source)
        # and creates a marker file to prove it ran on the right branch
        post_rebase_hook_script = """#!/bin/bash
if [ -f DOWNSTREAM_OWNERS ]; then
    echo "carry-preserved" > hook-verified-carry.txt
    git add hook-verified-carry.txt
    git commit -m "UPSTREAM: <drop>: hook verified carry commits present"
else
    echo "FAIL: DOWNSTREAM_OWNERS not found - hooks ran on source, not dest" >&2
    exit 1
fi"""

        with CommitBuilder(dest) as cb:
            cb.add_file("verify-hook.sh", post_rebase_hook_script)
            cb.commit("UPSTREAM: <carry>: add verify hook script")

        args = MagicMock()
        args.source = source
        args.source_repo = None
        args.dest = dest
        args.rebase = rebase
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "none"
        args.bot_emails = []
        args.exclude_commits = []
        args.update_go_modules = False
        args.conflict_policy = "auto"
        args.dry_run = True
        args.ignore_manual_label = True
        args.always_run_hooks = True

        args.pre_rebase_hook = None
        args.post_rebase_hook = [f"git:dest/{dest.branch}:verify-hook.sh"]
        args.pre_carry_commit_hook = None
        args.pre_push_rebase_branch_hook = None
        args.pre_create_pr_hook = None

        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

        assert result is True
        # The hook should have succeeded (exit 0), proving DOWNSTREAM_OWNERS existed
        # which means hooks ran on dest branch, not source branch
        assert "hook-verified-carry.txt" in os.listdir(tmpdir)


    def test_later_run_merge_only_delta_preserved(self, init_test_repositories, fake_github_provider, tmpdir):
        """
        Later-run path: a downstream manual merge with merge-only content must be preserved.

        Scenario:
          1. Source advances; first dry-run rebase runs, producing a rebase branch.
          2. The rebase PR is merged into dest (--no-ff, simulated locally).
          3. A downstream feature branch is merged into dest with merge-only content:
             an extra file (merge_resolution.txt) is staged before the merge commit,
             so it exists in the merge commit tree but NOT in the auto-merge baseline.
          4. Source advances again; the second (later-run) dry-run rebase runs.
          5. Assert the later run produces a synthetic UPSTREAM: <carry> commit that
             restores merge_resolution.txt from the downstream merge commit.
        """
        source, rebase, dest = init_test_repositories
        # Initial state:
        #   source: S0  (test.go)
        #   dest:   S0 → D1  (another_file.go, "UPSTREAM: <carry>: our cool addition")

        # Step 1: advance source (trigger first rebase).
        CommitBuilder(source).add_file("baz.txt", "upstream v2").commit("second upstream commit")

        # ── First dry-run rebase ────────────────────────────────────────────────
        first_run_dir = os.path.join(tmpdir, "first_run")
        os.makedirs(first_run_dir)

        def _make_args(src, rbs, dst, wdir):
            a = MagicMock()
            a.source = src
            a.source_repo = None
            a.dest = dst
            a.rebase = rbs
            a.working_dir = wdir
            a.git_username = "test_rebasebot"
            a.git_email = "test@rebasebot.ocp"
            a.tag_policy = "soft"
            a.bot_emails = []
            a.exclude_commits = []
            a.update_go_modules = False
            a.conflict_policy = "auto"
            a.ignore_manual_label = False
            a.dry_run = True
            a.always_run_hooks = False
            a.title_prefix = ""
            a.pre_rebase_hook = None
            a.post_rebase_hook = None
            a.pre_carry_commit_hook = None
            a.pre_push_rebase_branch_hook = None
            a.pre_create_pr_hook = None
            a.source_ref_hook = None
            return a

        args1 = _make_args(source, rebase, dest, first_run_dir)
        result1 = cli.rebasebot_run(args1, slack_webhook=None, github_app_wrapper=fake_github_provider)
        assert result1, "First rebase run should succeed"

        # ── Simulate merging the rebase PR into dest (--no-ff) ─────────────────
        dest_repo = Repo(dest.url)
        dest_repo.git.checkout(dest.branch)

        remote_name = "first_run_remote"
        if remote_name not in [r.name for r in dest_repo.remotes]:
            dest_repo.create_remote(remote_name, first_run_dir)
        dest_repo.remotes[remote_name].fetch("rebase")
        dest_repo.git.merge(f"{remote_name}/rebase", "--no-ff", "-m", "Merge rebase PR into main")

        # ── Create a feature branch and merge it with merge-only content ────────
        dest_repo.git.checkout("-b", "feature_branch")
        feature_file_path = os.path.join(dest.url, "feature.txt")
        with open(feature_file_path, "x", encoding="utf8") as fh:
            fh.write("feature content\n")
        dest_repo.git.add("feature.txt")
        dest_repo.git.commit("-m", "add feature file")

        dest_repo.git.checkout(dest.branch)
        # --no-commit so we can inject a merge-only file before the merge commit.
        dest_repo.git.merge("feature_branch", "--no-ff", "--no-commit")

        resolution_path = os.path.join(dest.url, "merge_resolution.txt")
        with open(resolution_path, "w", encoding="utf8") as fh:
            fh.write("manually resolved\n")
        dest_repo.git.add("merge_resolution.txt")

        dest_repo.git.commit("-m", "Manual merge: feature into main with merge-only resolution")
        manual_merge_sha = dest_repo.head.commit.hexsha

        # ── Advance source for the second (later) rebase ────────────────────────
        CommitBuilder(source).add_file("qux.txt", "upstream v3").commit("third upstream commit")

        # ── Second dry-run rebase (the "later run") ─────────────────────────────
        second_run_dir = os.path.join(tmpdir, "second_run")
        os.makedirs(second_run_dir)
        args2 = _make_args(source, rebase, dest, second_run_dir)
        result2 = cli.rebasebot_run(args2, slack_webhook=None, github_app_wrapper=fake_github_provider)
        assert result2, "Second (later) rebase run should succeed"

        # ── Assertions ───────────────────────────────────────────────────────────
        second_working = Repo(second_run_dir)
        commit_summaries = [c.summary for c in second_working.iter_commits("rebase")]

        expected_carry_msg = (
            f"UPSTREAM: <carry>: Recover merge-only delta from merge {manual_merge_sha[:12]}"
        )
        assert expected_carry_msg in commit_summaries, (
            f"Expected synthetic carry commit not found in rebase branch.\n"
            f"Expected:  {expected_carry_msg!r}\n"
            f"Got commits: {commit_summaries!r}"
        )

        # The synthetic carry commit's tree must include merge_resolution.txt.
        carry_commit = next(c for c in second_working.iter_commits("rebase") if c.summary == expected_carry_msg)
        blob_names = {b.name for b in carry_commit.tree.blobs}
        assert "merge_resolution.txt" in blob_names, (
            f"merge_resolution.txt missing from synthetic carry commit tree; found: {blob_names!r}"
        )

    def test_first_run_legacy_merge_only_delta_preserved(self, init_test_repositories, fake_github_provider, tmpdir):
        """
        First-run (onboarding) path: a legacy downstream merge with merge-only content
        must be recovered during the first automated rebase.

        Scenario:
          - Source and dest share common history; no prior RebaseBot marker exists on dest.
          - Dest has a legacy feature-branch merge committed with merge-only content
            (first_run_resolution.txt injected before the merge commit).
          - Source advances; the first automated dry-run rebase runs.
          - Assert a synthetic UPSTREAM: <carry> commit restoring first_run_resolution.txt
            is present in the rebase branch.
        """
        source, rebase, dest = init_test_repositories
        # Initial state (no prior rebasebot marker on dest):
        #   source: S0  (test.go)
        #   dest:   S0 → D1  (another_file.go, "UPSTREAM: <carry>: our cool addition")

        # Advance source to trigger the first rebase.
        CommitBuilder(source).add_file("baz.txt", "upstream v2").commit("second upstream commit")

        # Create a legacy feature branch on dest and add a feature file.
        dest_repo = Repo(dest.url)
        dest_repo.git.checkout("-b", "legacy_feature")
        with open(os.path.join(dest.url, "legacy_feature.txt"), "x", encoding="utf8") as fh:
            fh.write("legacy feature content\n")
        dest_repo.git.add("legacy_feature.txt")
        dest_repo.git.commit("-m", "add legacy feature file")

        # Merge the legacy feature branch into dest/main with --no-commit so we can
        # inject a merge-only file (first_run_resolution.txt) before the merge commit.
        dest_repo.git.checkout(dest.branch)
        dest_repo.git.merge("legacy_feature", "--no-ff", "--no-commit")

        with open(os.path.join(dest.url, "first_run_resolution.txt"), "w", encoding="utf8") as fh:
            fh.write("first-run manual resolution\n")
        dest_repo.git.add("first_run_resolution.txt")

        dest_repo.git.commit("-m", "Legacy upstream merge with merge-only resolution")
        legacy_merge_sha = dest_repo.head.commit.hexsha

        # Run the first dry-run rebase (no prior rebasebot marker on dest).
        args = MagicMock()
        args.source = source
        args.source_repo = None
        args.dest = dest
        args.rebase = rebase
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "soft"
        args.bot_emails = []
        args.exclude_commits = []
        args.update_go_modules = False
        args.conflict_policy = "auto"
        args.ignore_manual_label = False
        args.dry_run = True
        args.always_run_hooks = False
        args.title_prefix = ""
        args.pre_rebase_hook = None
        args.post_rebase_hook = None
        args.pre_carry_commit_hook = None
        args.pre_push_rebase_branch_hook = None
        args.pre_create_pr_hook = None
        args.source_ref_hook = None

        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)
        assert result, "First-run rebase should succeed"

        # Verify the synthetic carry commit was created for the first-run merge-only delta.
        working_repo = Repo(tmpdir)
        commit_summaries = [c.summary for c in working_repo.iter_commits("rebase")]

        expected_carry_msg = (
            f"UPSTREAM: <carry>: Recover merge-only delta from merge {legacy_merge_sha[:12]}"
        )
        assert expected_carry_msg in commit_summaries, (
            f"Expected synthetic carry commit not found in first-run rebase branch.\n"
            f"Expected:  {expected_carry_msg!r}\n"
            f"Got commits: {commit_summaries!r}"
        )

        # Verify first_run_resolution.txt is present in the synthetic carry commit's tree.
        carry_commit = next(c for c in working_repo.iter_commits("rebase") if c.summary == expected_carry_msg)
        blob_names = {b.name for b in carry_commit.tree.blobs}
        assert "first_run_resolution.txt" in blob_names, (
            f"first_run_resolution.txt missing from synthetic carry commit tree; found: {blob_names!r}"
        )

    def test_later_run_merge_only_delta_is_replayed_before_later_downstream_commits(
        self, init_test_repositories, fake_github_provider, tmpdir
    ):
        """
        Later-run path: a recovered merge-only carry must land before later downstream commits.

        This isolates the ordering contract: if a downstream manual merge is followed by
        another ordinary downstream commit, the synthetic carry recovered from that merge
        must appear before the later commit on the rebased branch.
        """
        source, rebase, dest = init_test_repositories
        CommitBuilder(source).add_file("baz.txt", "upstream v2").commit("second upstream commit")

        first_run_dir = os.path.join(tmpdir, "first_run")
        os.makedirs(first_run_dir)

        def _make_args(src, rbs, dst, wdir):
            a = MagicMock()
            a.source = src
            a.source_repo = None
            a.dest = dst
            a.rebase = rbs
            a.working_dir = wdir
            a.git_username = "test_rebasebot"
            a.git_email = "test@rebasebot.ocp"
            a.tag_policy = "soft"
            a.bot_emails = []
            a.exclude_commits = []
            a.update_go_modules = False
            a.conflict_policy = "auto"
            a.ignore_manual_label = False
            a.dry_run = True
            a.always_run_hooks = False
            a.title_prefix = ""
            a.pre_rebase_hook = None
            a.post_rebase_hook = None
            a.pre_carry_commit_hook = None
            a.pre_push_rebase_branch_hook = None
            a.pre_create_pr_hook = None
            a.source_ref_hook = None
            return a

        args1 = _make_args(source, rebase, dest, first_run_dir)
        assert cli.rebasebot_run(args1, slack_webhook=None, github_app_wrapper=fake_github_provider)

        dest_repo = Repo(dest.url)
        dest_repo.git.checkout(dest.branch)

        remote_name = "first_run_remote"
        if remote_name not in [r.name for r in dest_repo.remotes]:
            dest_repo.create_remote(remote_name, first_run_dir)
        dest_repo.remotes[remote_name].fetch("rebase")
        dest_repo.git.merge(f"{remote_name}/rebase", "--no-ff", "-m", "Merge rebase PR into main")

        dest_repo.git.checkout("-b", "feature_branch")
        feature_file_path = os.path.join(dest.url, "feature.txt")
        with open(feature_file_path, "x", encoding="utf8") as fh:
            fh.write("feature content\n")
        dest_repo.git.add("feature.txt")
        dest_repo.git.commit("-m", "add feature file")

        dest_repo.git.checkout(dest.branch)
        dest_repo.git.merge("feature_branch", "--no-ff", "--no-commit")

        resolution_path = os.path.join(dest.url, "merge_resolution.txt")
        with open(resolution_path, "w", encoding="utf8") as fh:
            fh.write("manually resolved\n")
        dest_repo.git.add("merge_resolution.txt")
        dest_repo.git.commit("-m", "Manual merge: feature into main with merge-only resolution")
        manual_merge_sha = dest_repo.head.commit.hexsha

        follow_up_msg = "later downstream follow-up"
        CommitBuilder(dest).add_file("post_merge_followup.txt", "follow-up content\n").commit(follow_up_msg)

        CommitBuilder(source).add_file("qux.txt", "upstream v3").commit("third upstream commit")

        second_run_dir = os.path.join(tmpdir, "second_run")
        os.makedirs(second_run_dir)
        args2 = _make_args(source, rebase, dest, second_run_dir)
        assert cli.rebasebot_run(args2, slack_webhook=None, github_app_wrapper=fake_github_provider)

        second_working = Repo(second_run_dir)
        chronological_summaries = [commit.summary for commit in reversed(list(second_working.iter_commits("rebase")))]

        expected_carry_msg = (
            f"UPSTREAM: <carry>: Recover merge-only delta from merge {manual_merge_sha[:12]}"
        )
        assert expected_carry_msg in chronological_summaries, (
            f"Expected synthetic carry commit not found in rebase branch.\n"
            f"Expected: {expected_carry_msg!r}\n"
            f"Got commits: {chronological_summaries!r}"
        )
        assert follow_up_msg in chronological_summaries, (
            f"Expected later downstream commit not found in rebase branch.\n"
            f"Expected: {follow_up_msg!r}\n"
            f"Got commits: {chronological_summaries!r}"
        )
        replayed_follow_up_index = max(
            index for index, summary in enumerate(chronological_summaries) if summary == follow_up_msg
        )
        assert chronological_summaries.index(expected_carry_msg) < replayed_follow_up_index, (
            "Recovered merge-only carry was replayed after a later downstream commit.\n"
            f"Expected {expected_carry_msg!r} before {follow_up_msg!r}.\n"
            f"Got commits: {chronological_summaries!r}"
        )

    def test_recovered_merge_only_carry_survives_strict_tag_policy_and_bot_squash(
        self, init_test_repositories, fake_github_provider, tmpdir
    ):
        source, rebase, dest = init_test_repositories
        CommitBuilder(source).add_file("baz.txt", "upstream v2").commit("second upstream commit")

        first_run_dir = os.path.join(tmpdir, "first_run")
        os.makedirs(first_run_dir)
        args1 = _make_rebase_args(source, rebase, dest, first_run_dir)
        assert cli.rebasebot_run(args1, slack_webhook=None, github_app_wrapper=fake_github_provider)

        dest_repo = _merge_rebase_branch_into_dest(dest, first_run_dir, "first_run_remote")

        dest_repo.git.checkout("-b", "feature_branch")
        with open(os.path.join(dest.url, "feature.txt"), "x", encoding="utf8") as fh:
            fh.write("feature content\n")
        dest_repo.git.add("feature.txt")
        dest_repo.git.commit("-m", "add feature file")

        dest_repo.git.checkout(dest.branch)
        dest_repo.git.merge("feature_branch", "--no-ff", "--no-commit")
        with open(os.path.join(dest.url, "merge_resolution.txt"), "w", encoding="utf8") as fh:
            fh.write("manually resolved\n")
        dest_repo.git.add("merge_resolution.txt")
        dest_repo.git.commit("-m", "Manual merge: feature into main with merge-only resolution")
        manual_merge_sha = dest_repo.head.commit.hexsha

        CommitBuilder(source).add_file("qux.txt", "upstream v3").commit("third upstream commit")

        second_run_dir = os.path.join(tmpdir, "second_run")
        os.makedirs(second_run_dir)
        args2 = _make_rebase_args(source, rebase, dest, second_run_dir)
        assert cli.rebasebot_run(args2, slack_webhook=None, github_app_wrapper=fake_github_provider)

        expected_recovery_msg = (
            f"UPSTREAM: <carry>: Recover merge-only delta from merge {manual_merge_sha[:12]}"
        )
        assert expected_recovery_msg in [commit.summary for commit in Repo(second_run_dir).iter_commits("rebase")]

        _merge_rebase_branch_into_dest(dest, second_run_dir, "second_run_remote", "Merge second rebase PR into main")

        CommitBuilder(dest).add_file("generated-metadata.txt", "bot output\n").commit(
            "UPSTREAM: <carry>: generated downstream metadata",
            committer_email="test@rebasebot.ocp",
        )

        CommitBuilder(source).add_file("quux.txt", "upstream v4").commit("fourth upstream commit")

        third_run_dir = os.path.join(tmpdir, "third_run")
        os.makedirs(third_run_dir)
        args3 = _make_rebase_args(
            source,
            rebase,
            dest,
            third_run_dir,
            tag_policy="strict",
            bot_emails=["test@rebasebot.ocp"],
        )
        assert cli.rebasebot_run(args3, slack_webhook=None, github_app_wrapper=fake_github_provider)

        replayed_summaries = Repo(third_run_dir).git.log("--first-parent", "--pretty=format:%s", "rebase").splitlines()
        assert expected_recovery_msg in replayed_summaries, (
            "Recovered merge-only carry should survive a later strict-tag replay even when "
            "bot squashing is enabled.\n"
            f"Got first-parent commits: {replayed_summaries!r}"
        )
        assert "UPSTREAM: <carry>: generated downstream metadata" in replayed_summaries, (
            "Control bot-authored carry commit should still be present in the strict-tag replay.\n"
            f"Got first-parent commits: {replayed_summaries!r}"
        )

    def test_excluding_source_merge_sha_suppresses_merge_only_delta_recovery(
        self, init_test_repositories, fake_github_provider, tmpdir
    ):
        source, rebase, dest = init_test_repositories
        CommitBuilder(source).add_file("baz.txt", "upstream v2").commit("second upstream commit")

        first_run_dir = os.path.join(tmpdir, "first_run")
        os.makedirs(first_run_dir)
        args1 = _make_rebase_args(source, rebase, dest, first_run_dir)
        assert cli.rebasebot_run(args1, slack_webhook=None, github_app_wrapper=fake_github_provider)

        dest_repo = _merge_rebase_branch_into_dest(dest, first_run_dir, "exclude_first_run_remote")

        dest_repo.git.checkout("-b", "feature_branch")
        with open(os.path.join(dest.url, "feature.txt"), "x", encoding="utf8") as fh:
            fh.write("feature content\n")
        dest_repo.git.add("feature.txt")
        dest_repo.git.commit("-m", "add feature file")

        dest_repo.git.checkout(dest.branch)
        dest_repo.git.merge("feature_branch", "--no-ff", "--no-commit")
        with open(os.path.join(dest.url, "merge_resolution.txt"), "w", encoding="utf8") as fh:
            fh.write("manually resolved\n")
        dest_repo.git.add("merge_resolution.txt")
        dest_repo.git.commit("-m", "Manual merge: feature into main with merge-only resolution")
        manual_merge_sha = dest_repo.head.commit.hexsha

        CommitBuilder(source).add_file("qux.txt", "upstream v3").commit("third upstream commit")

        second_run_dir = os.path.join(tmpdir, "second_run")
        os.makedirs(second_run_dir)
        args2 = _make_rebase_args(
            source,
            rebase,
            dest,
            second_run_dir,
            exclude_commits=[manual_merge_sha],
        )
        assert cli.rebasebot_run(args2, slack_webhook=None, github_app_wrapper=fake_github_provider)

        commit_summaries = [commit.summary for commit in Repo(second_run_dir).iter_commits("rebase")]
        expected_recovery_msg = (
            f"UPSTREAM: <carry>: Recover merge-only delta from merge {manual_merge_sha[:12]}"
        )
        assert expected_recovery_msg not in commit_summaries, (
            "Explicitly excluding the source merge SHA should suppress synthetic merge-only recovery.\n"
            f"Got commits: {commit_summaries!r}"
        )

    def test_first_run_mode_only_merge_delta_is_preserved(
        self, init_test_repositories, fake_github_provider, tmpdir
    ):
        source, rebase, dest = init_test_repositories
        CommitBuilder(source).add_file("baz.txt", "upstream v2").commit("second upstream commit")

        dest_repo = Repo(dest.url)
        dest_repo.git.checkout("-b", "mode_feature")
        script_path = os.path.join(dest.url, "mode_only.sh")
        with open(script_path, "x", encoding="utf8") as fh:
            fh.write("#!/bin/sh\necho mode-only\n")
        dest_repo.git.add("mode_only.sh")
        dest_repo.git.commit("-m", "add mode-only script")

        dest_repo.git.checkout(dest.branch)
        dest_repo.git.merge("mode_feature", "--no-ff", "--no-commit")
        os.chmod(script_path, 0o755)
        dest_repo.git.add("mode_only.sh")
        dest_repo.git.commit("-m", "Legacy merge with mode-only resolution")
        mode_merge_sha = dest_repo.head.commit.hexsha

        run_dir = os.path.join(tmpdir, "mode_run")
        os.makedirs(run_dir)
        args = _make_rebase_args(source, rebase, dest, run_dir)
        assert cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

        working_repo = Repo(run_dir)
        expected_recovery_msg = (
            f"UPSTREAM: <carry>: Recover merge-only delta from merge {mode_merge_sha[:12]}"
        )
        commit_summaries = [commit.summary for commit in working_repo.iter_commits("rebase")]
        assert expected_recovery_msg in commit_summaries, (
            "Mode-only merge deltas should be recovered as synthetic carries.\n"
            f"Got commits: {commit_summaries!r}"
        )

        carry_commit = next(commit for commit in working_repo.iter_commits("rebase") if commit.summary == expected_recovery_msg)
        assert carry_commit.tree["mode_only.sh"].mode == 0o100755

    def test_replay_time_no_op_merge_only_delta_is_skipped(
        self, init_test_repositories, fake_github_provider, tmpdir
    ):
        source, rebase, dest = init_test_repositories
        CommitBuilder(source).add_file("baz.txt", "upstream v2").commit("second upstream commit")

        first_run_dir = os.path.join(tmpdir, "first_run")
        os.makedirs(first_run_dir)
        args1 = _make_rebase_args(source, rebase, dest, first_run_dir)
        assert cli.rebasebot_run(args1, slack_webhook=None, github_app_wrapper=fake_github_provider)

        dest_repo = _merge_rebase_branch_into_dest(dest, first_run_dir, "noop_first_run_remote")

        dest_repo.git.checkout("-b", "feature_branch")
        with open(os.path.join(dest.url, "feature.txt"), "x", encoding="utf8") as fh:
            fh.write("feature content\n")
        dest_repo.git.add("feature.txt")
        dest_repo.git.commit("-m", "add feature file")

        dest_repo.git.checkout(dest.branch)
        dest_repo.git.merge("feature_branch", "--no-ff", "--no-commit")
        with open(os.path.join(dest.url, "merge_resolution.txt"), "w", encoding="utf8") as fh:
            fh.write("manually resolved\n")
        dest_repo.git.add("merge_resolution.txt")
        dest_repo.git.commit("-m", "Manual merge: feature into main with merge-only resolution")
        manual_merge_sha = dest_repo.head.commit.hexsha

        CommitBuilder(source).add_file("merge_resolution.txt", "manually resolved\n").commit(
            "upstream adopts merge resolution"
        )

        second_run_dir = os.path.join(tmpdir, "second_run")
        os.makedirs(second_run_dir)
        args2 = _make_rebase_args(source, rebase, dest, second_run_dir)
        assert cli.rebasebot_run(args2, slack_webhook=None, github_app_wrapper=fake_github_provider)

        first_parent_summaries = Repo(second_run_dir).git.log("--first-parent", "--pretty=format:%s", "rebase").splitlines()
        expected_recovery_msg = (
            f"UPSTREAM: <carry>: Recover merge-only delta from merge {manual_merge_sha[:12]}"
        )
        assert expected_recovery_msg not in first_parent_summaries, (
            "Replay-time no-op merge-only deltas should be skipped instead of materializing empty carries.\n"
            f"Got first-parent commits: {first_parent_summaries!r}"
        )
        assert os.path.exists(os.path.join(second_run_dir, "merge_resolution.txt"))

    def test_recovery_requires_manual_intervention_when_synthetic_carry_cannot_apply_cleanly(self):
        gitwd = MagicMock()
        merge_commit = MagicMock()
        merge_commit.hexsha = "1234567890abcdef1234567890abcdef12345678"

        gitwd.git.ls_tree.side_effect = [
            "100644 blob deadbeef\tmerge_resolution.txt",
            "100644 blob feedface\tmerge_resolution.txt",
        ]
        gitwd.git.checkout.side_effect = git.GitCommandError(
            "checkout",
            1,
            stderr="would overwrite conflicting path",
        )

        with pytest.raises(RepoException, match="Failed to restore merge-only delta paths from merge 1234567890ab"):
            _apply_merge_only_delta(
                gitwd,
                merge_commit,
                ":000000 100644 0000000 1111111 A\tmerge_resolution.txt",
            )

    def test_detection_and_recovery_logs_emitted(
        self, init_test_repositories, fake_github_provider, tmpdir, caplog
    ):
        """
        Operator visibility: INFO logs for detection and recovery of a merge-only delta
        must be emitted during the later-run rebase.
        """
        caplog.set_level(logging.INFO)
        source, rebase, dest = init_test_repositories
        CommitBuilder(source).add_file("baz.txt", "upstream v2").commit("second upstream commit")

        first_run_dir = os.path.join(tmpdir, "first_run")
        os.makedirs(first_run_dir)
        assert cli.rebasebot_run(
            _make_rebase_args(source, rebase, dest, first_run_dir),
            slack_webhook=None,
            github_app_wrapper=fake_github_provider,
        )
        dest_repo = _merge_rebase_branch_into_dest(dest, first_run_dir, "detect_log_remote")

        dest_repo.git.checkout("-b", "feature_branch")
        with open(os.path.join(dest.url, "feature.txt"), "x", encoding="utf8") as fh:
            fh.write("feature content\n")
        dest_repo.git.add("feature.txt")
        dest_repo.git.commit("-m", "add feature file")

        dest_repo.git.checkout(dest.branch)
        dest_repo.git.merge("feature_branch", "--no-ff", "--no-commit")
        with open(os.path.join(dest.url, "detect_resolution.txt"), "w", encoding="utf8") as fh:
            fh.write("manually resolved\n")
        dest_repo.git.add("detect_resolution.txt")
        dest_repo.git.commit("-m", "Manual merge: feature with detection log resolution")
        manual_merge_sha = dest_repo.head.commit.hexsha

        CommitBuilder(source).add_file("qux.txt", "upstream v3").commit("third upstream commit")

        second_run_dir = os.path.join(tmpdir, "second_run")
        os.makedirs(second_run_dir)
        assert cli.rebasebot_run(
            _make_rebase_args(source, rebase, dest, second_run_dir),
            slack_webhook=None,
            github_app_wrapper=fake_github_provider,
        )

        assert f"Found merge-only delta in downstream merge {manual_merge_sha[:12]}" in caplog.text, (
            "Expected detection INFO log to be captured. "
            "Ensure caplog.set_level(logging.INFO) is set."
        )
        assert (
            f"Created synthetic carry commit for merge-only delta from merge {manual_merge_sha[:12]}"
            in caplog.text
        ), "Expected recovery INFO log to be captured."

    def test_no_op_skip_log_emitted(
        self, init_test_repositories, fake_github_provider, tmpdir, caplog
    ):
        """
        Operator visibility: INFO log for skipping a merge-only delta that becomes a no-op
        after replay (because source has since adopted the same content).
        """
        caplog.set_level(logging.INFO)
        source, rebase, dest = init_test_repositories
        CommitBuilder(source).add_file("baz.txt", "upstream v2").commit("second upstream commit")

        first_run_dir = os.path.join(tmpdir, "first_run")
        os.makedirs(first_run_dir)
        assert cli.rebasebot_run(
            _make_rebase_args(source, rebase, dest, first_run_dir),
            slack_webhook=None,
            github_app_wrapper=fake_github_provider,
        )
        dest_repo = _merge_rebase_branch_into_dest(dest, first_run_dir, "noop_log_remote")

        dest_repo.git.checkout("-b", "feature_branch")
        with open(os.path.join(dest.url, "feature.txt"), "x", encoding="utf8") as fh:
            fh.write("feature content\n")
        dest_repo.git.add("feature.txt")
        dest_repo.git.commit("-m", "add feature file")

        dest_repo.git.checkout(dest.branch)
        dest_repo.git.merge("feature_branch", "--no-ff", "--no-commit")
        with open(os.path.join(dest.url, "noop_resolution.txt"), "w", encoding="utf8") as fh:
            fh.write("noop resolved\n")
        dest_repo.git.add("noop_resolution.txt")
        dest_repo.git.commit("-m", "Manual merge: feature with noop resolution")
        manual_merge_sha = dest_repo.head.commit.hexsha

        # Source adopts the same content, making the carry a no-op at replay time.
        CommitBuilder(source).add_file("noop_resolution.txt", "noop resolved\n").commit(
            "upstream adopts noop resolution"
        )

        second_run_dir = os.path.join(tmpdir, "second_run")
        os.makedirs(second_run_dir)
        assert cli.rebasebot_run(
            _make_rebase_args(source, rebase, dest, second_run_dir),
            slack_webhook=None,
            github_app_wrapper=fake_github_provider,
        )

        assert (
            f"Merge-only delta from {manual_merge_sha[:12]} is a no-op after replay; skipping"
            in caplog.text
        ), (
            "Expected no-op skip INFO log to be captured. "
            "Ensure caplog.set_level(logging.INFO) is set."
        )

    def test_excluded_skip_log_emitted(
        self, init_test_repositories, fake_github_provider, tmpdir, caplog
    ):
        """
        Operator visibility: INFO log when a merge SHA is explicitly excluded via
        --exclude-commits, suppressing merge-only delta recovery.
        """
        caplog.set_level(logging.INFO)
        source, rebase, dest = init_test_repositories
        CommitBuilder(source).add_file("baz.txt", "upstream v2").commit("second upstream commit")

        first_run_dir = os.path.join(tmpdir, "first_run")
        os.makedirs(first_run_dir)
        assert cli.rebasebot_run(
            _make_rebase_args(source, rebase, dest, first_run_dir),
            slack_webhook=None,
            github_app_wrapper=fake_github_provider,
        )
        dest_repo = _merge_rebase_branch_into_dest(dest, first_run_dir, "exclude_log_remote")

        dest_repo.git.checkout("-b", "feature_branch")
        with open(os.path.join(dest.url, "feature.txt"), "x", encoding="utf8") as fh:
            fh.write("feature content\n")
        dest_repo.git.add("feature.txt")
        dest_repo.git.commit("-m", "add feature file")

        dest_repo.git.checkout(dest.branch)
        dest_repo.git.merge("feature_branch", "--no-ff", "--no-commit")
        with open(os.path.join(dest.url, "exclude_resolution.txt"), "w", encoding="utf8") as fh:
            fh.write("excluded resolution\n")
        dest_repo.git.add("exclude_resolution.txt")
        dest_repo.git.commit("-m", "Manual merge: feature with excluded resolution")
        manual_merge_sha = dest_repo.head.commit.hexsha

        CommitBuilder(source).add_file("qux.txt", "upstream v3").commit("third upstream commit")

        second_run_dir = os.path.join(tmpdir, "second_run")
        os.makedirs(second_run_dir)
        assert cli.rebasebot_run(
            _make_rebase_args(
                source, rebase, dest, second_run_dir, exclude_commits=[manual_merge_sha]
            ),
            slack_webhook=None,
            github_app_wrapper=fake_github_provider,
        )

        assert "Explicitly skipping merge-only delta recovery from merge" in caplog.text, (
            "Expected excluded-skip INFO log to be captured. "
            "Ensure caplog.set_level(logging.INFO) is set."
        )

    def test_real_upstream_merge_resolved_to_upstream_is_not_treated_as_synthetic_marker(
        self, init_test_repositories, fake_github_provider, tmpdir
    ):
        source, rebase, dest = init_test_repositories
        CommitBuilder(source).add_file("baz.txt", "upstream v2\n").commit("second upstream commit")

        first_run_dir = os.path.join(tmpdir, "first_run")
        os.makedirs(first_run_dir)
        assert cli.rebasebot_run(
            _make_rebase_args(source, rebase, dest, first_run_dir),
            slack_webhook=None,
            github_app_wrapper=fake_github_provider,
        )

        dest_repo = _merge_rebase_branch_into_dest(dest, first_run_dir, "real_upstream_remote")
        CommitBuilder(dest).update_file(
            "test.go",
            """package main
func main() {
    println("downstream override")
}
""",
        ).commit("downstream tweak before upstream sync")
        CommitBuilder(source).update_file(
            "test.go",
            """package main
func main() {
    println("upstream sync")
}
""",
        ).commit("third upstream commit")

        remote_name = "manual_upstream_source"
        if remote_name not in [remote.name for remote in dest_repo.remotes]:
            dest_repo.create_remote(remote_name, source.url)
        dest_repo.remotes[remote_name].fetch(source.branch)
        dest_repo.git.checkout(dest.branch)
        try:
            dest_repo.git.merge(f"{remote_name}/{source.branch}", "--no-ff", "--no-commit")
        except git.GitCommandError:
            pass
        dest_repo.git.checkout("--theirs", "test.go")
        dest_repo.git.add("test.go")
        dest_repo.git.rm("--force", "another_file.go")
        dest_repo.git.commit("-m", "Manual upstream sync resolved fully to upstream")
        manual_merge_sha = dest_repo.head.commit.hexsha
        assert dest_repo.head.commit.tree.hexsha == dest_repo.head.commit.parents[1].tree.hexsha

        CommitBuilder(source).add_file("qux.txt", "upstream v3\n").commit("fourth upstream commit")

        second_run_dir = os.path.join(tmpdir, "second_run")
        os.makedirs(second_run_dir)
        assert cli.rebasebot_run(
            _make_rebase_args(source, rebase, dest, second_run_dir),
            slack_webhook=None,
            github_app_wrapper=fake_github_provider,
        )

        expected_recovery_msg = (
            f"UPSTREAM: <carry>: Recover merge-only delta from merge {manual_merge_sha[:12]}"
        )
        working_repo = Repo(second_run_dir)
        commit_summaries = [commit.summary for commit in working_repo.iter_commits("rebase")]
        assert expected_recovery_msg in commit_summaries, (
            "A real upstream merge that resolved fully to the upstream tree must still be "
            "treated as a user-authored merge and recovered on later rebases.\n"
            f"Got commits: {commit_summaries!r}"
        )
        assert "another_file.go" not in working_repo.head.commit.tree
        assert working_repo.git.show("HEAD:test.go") == Repo(source.url).git.show(f"{source.branch}:test.go")

    @patch("rebasebot.bot.subprocess.run")
    def test_merge_only_delta_detection_failure_requires_manual_intervention(
        self, mocked_subprocess_run, init_test_repositories, fake_github_provider, tmpdir
    ):
        source, rebase, dest = init_test_repositories
        CommitBuilder(source).add_file("baz.txt", "upstream v2").commit("second upstream commit")

        first_run_dir = os.path.join(tmpdir, "first_run")
        os.makedirs(first_run_dir)
        assert cli.rebasebot_run(
            _make_rebase_args(source, rebase, dest, first_run_dir),
            slack_webhook=None,
            github_app_wrapper=fake_github_provider,
        )
        dest_repo = _merge_rebase_branch_into_dest(dest, first_run_dir, "failed_detection_remote")

        dest_repo.git.checkout("-b", "feature_branch")
        with open(os.path.join(dest.url, "feature.txt"), "x", encoding="utf8") as fh:
            fh.write("feature content\n")
        dest_repo.git.add("feature.txt")
        dest_repo.git.commit("-m", "add feature file")

        dest_repo.git.checkout(dest.branch)
        dest_repo.git.merge("feature_branch", "--no-ff", "--no-commit")
        with open(os.path.join(dest.url, "merge_resolution.txt"), "w", encoding="utf8") as fh:
            fh.write("manually resolved\n")
        dest_repo.git.add("merge_resolution.txt")
        dest_repo.git.commit("-m", "Manual merge: feature into main with merge-only resolution")

        CommitBuilder(source).add_file("qux.txt", "upstream v3").commit("third upstream commit")

        mocked_subprocess_run.return_value = MagicMock(stdout="", returncode=0, stderr="")

        second_run_dir = os.path.join(tmpdir, "second_run")
        os.makedirs(second_run_dir)
        result = cli.rebasebot_run(
            _make_rebase_args(source, rebase, dest, second_run_dir),
            slack_webhook=None,
            github_app_wrapper=fake_github_provider,
        )
        assert result is False, (
            "If merge-only delta detection cannot produce a baseline for an eligible merge, "
            "the rebase must fail closed instead of warning and continuing."
        )

    def test_manual_intervention_warning_logged_on_apply_failure(self, caplog):
        """
        Operator visibility: a WARNING log must be emitted specifically for the
        merge-only delta recovery failure case before the RepoException is raised.

        This is a unit-level test using mocks. The WARNING log helps operators identify
        the precise merge that required manual intervention without needing to parse
        the generic outer error.
        """
        gitwd = MagicMock()
        merge_commit = MagicMock()
        merge_commit.hexsha = "abcdef012345abcdef012345abcdef0123456789"

        gitwd.git.ls_tree.side_effect = [
            "100644 blob deadbeef\tconflict_file.txt",
            "100644 blob feedface\tconflict_file.txt",
        ]
        gitwd.git.checkout.side_effect = git.GitCommandError(
            "checkout",
            1,
            stderr="would overwrite conflicting path",
        )

        with pytest.raises(RepoException):
            _apply_merge_only_delta(
                gitwd,
                merge_commit,
                ":000000 100644 0000000 1111111 A\tconflict_file.txt",
            )

        assert "abcdef012345" in caplog.text, (
            "Expected a WARNING log referencing the merge SHA when merge-only delta "
            "recovery cannot be applied cleanly. "
            "Add logging.warning(...) in _apply_merge_only_delta before the RepoException raise."
        )
