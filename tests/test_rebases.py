from __future__ import annotations
from dataclasses import dataclass
import os
from unittest.mock import MagicMock, patch, ANY

import pytest

from git import Repo

from rebasebot import cli
from rebasebot.github import GitHubBranch, parse_github_branch
from rebasebot.bot import (
    _init_working_dir,
    _needs_rebase,
    _prepare_rebase_branch,
)

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


class TestBotInternalHelpers:

    @pytest.fixture
    def working_repo_context(self, init_test_repositories,
                             fake_github_provider, tmpdir) -> WorkingRepoContext:
        source, rebase, dest = init_test_repositories
        working_repo = _init_working_dir(
            source=source,
            dest=dest,
            rebase=rebase,
            github_app_provider=fake_github_provider,
            git_username="foo",
            git_email="foo@example.com",
            workdir=tmpdir
        )
        return WorkingRepoContext(
            source, rebase, dest, working_repo, tmpdir
        )

    def test_workdir_init(self, working_repo_context):
        working_repo = working_repo_context.working_repo
        working_repo_path = working_repo_context.working_repo_path

        assert working_repo
        assert len(working_repo.remotes)
        assert working_repo.working_dir == working_repo_path

        commits = list(working_repo.iter_commits())
        assert len(commits) == 1
        assert commits[0].message == "Upstream commit\n"

        working_repo_dir_content = {
            i.name for i in os.scandir(working_repo_path)}
        assert working_repo_dir_content == {'test.go', '.git'}

    def test_needs_rebase(self, working_repo_context):
        r_ctx = working_repo_context
        gitwd, source, dest = r_ctx.working_repo, r_ctx.source, r_ctx.dest
        assert not _needs_rebase(gitwd, source, dest)

        CommitBuilder(dest).add_file("bar.txt", "foo").commit(
            "UPSTREAM: <carry>: carry patch")
        working_repo_context.fetch_remotes()
        assert not _needs_rebase(gitwd, source, dest)

        CommitBuilder(source).add_file("baz.txt", "fiz").commit(
            "some other upstream commit")
        working_repo_context.fetch_remotes()
        assert _needs_rebase(gitwd, source, dest)

    def test_prepare_rebase_branch(self, working_repo_context):
        r_ctx = working_repo_context
        _prepare_rebase_branch(r_ctx.working_repo, r_ctx.source, r_ctx.dest)

        commits = list(r_ctx.working_repo.iter_commits())
        assert len(commits[0].parents)
        commit_msgs = [c.summary for c in commits]
        assert commit_msgs == [
            'merge upstream/main into main',  # merge commit
            'UPSTREAM: <carry>: our cool addition',
            'Upstream commit'
        ]


class TestRebases:

    def test_simple_dry_run(self, init_test_repositories,
                            fake_github_provider, tmpdir):
        source, rebase, dest = init_test_repositories
        CommitBuilder(source).add_file(
            "baz.txt", "fiz").commit("other upstream commit")

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
        args.ignore_manual_label = False
        args.dry_run = True
        result = cli.rebasebot_run(
            args, slack_webhook=None, github_app_wrapper=fake_github_provider)
        assert (result)

        working_repo = Repo.init(tmpdir)
        assert working_repo.head.ref.name == "rebase"
        log_graph = working_repo.git.log(
            "--graph", "--oneline", "--pretty='<%an>, %s'")
        assert log_graph == """ 
* '<dest_author>, UPSTREAM: <carry>: our cool addition'
*   '<test_rebasebot>, merge upstream/main into main'
|\\  
| * '<source_author>, other upstream commit'
* | '<dest_author>, UPSTREAM: <carry>: our cool addition'
|/  
* '<source_author>, Upstream commit'
""".strip()  # noqa: W291

    # Tests that all commits from bots are squashed into one for each bot

    def test_squash_bot_dry_run(
            self, init_test_repositories, fake_github_provider, tmpdir):
        source, rebase, dest = init_test_repositories
        with CommitBuilder(source) as cb:
            cb.add_file("baz.txt", "fiz")
            cb.commit("other upstream commit")
        with CommitBuilder(dest) as cb:
            cb.add_file("generated-test", "content")
            cb.commit("commit #1 from genbot",
                      committer_email="genbot@example.com")
        with CommitBuilder(dest) as cb:
            cb.add_file("generated-test2", "content")
            cb.commit("commit #2 from genbot",
                      committer_email="genbot@example.com")
        with CommitBuilder(dest) as cb:
            cb.add_file("generated-test3", "content")
            cb.commit("commit #1 from anotherbot",
                      committer_email="anotherbot@example.com")

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
        args.ignore_manual_label = False
        args.dry_run = True
        result = cli.rebasebot_run(
            args, slack_webhook=None, github_app_wrapper=fake_github_provider)
        assert (result)

        working_repo = Repo.init(tmpdir)
        assert working_repo.head.ref.name == "rebase"
        log_graph = working_repo.git.log(
            "--graph", "--oneline", "--pretty='<%an>, %s'")

        assert log_graph == r"""
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

    def test_first_run_dest_has_merges_dry_run(
            self, init_test_repositories, fake_github_provider, tmpdir):
        source, rebase, dest = init_test_repositories
        with CommitBuilder(source) as cb:
            cb.add_file("baz.txt", "fiz")
            cb.commit("other upstream commit")
        with CommitBuilder(dest) as cb:
            cb.add_file("generated-test", "content")
            cb.commit("commit #1 from genbot",
                      committer_email="genbot@example.com")
        # make branch
        dest_feature_branch = GitHubBranch(
            url=dest.url, ns="dest", name="dest", branch="feature")
        with CommitBuilder(dest_feature_branch) as cb:
            cb.add_file("feature-file", "feature content")
            cb.commit("commit on dest feature branch")
        with CommitBuilder(dest) as cb:
            cb.add_file("generated-test2", "content")
            cb.commit("commit #2 from genbot",
                      committer_email="genbot@example.com")
        # merge feature branch to dest
        repo = Repo(dest.url)
        repo.git.checkout(dest.branch)
        repo.git.merge(repo.heads.feature)
        with CommitBuilder(dest) as cb:
            cb.add_file("generated-test3", "content")
            cb.commit("commit #1 from anotherbot",
                      committer_email="anotherbot@example.com")

        args = MagicMock()
        args.source = source
        args.source_repo = None
        args.dest = dest
        args.rebase = rebase
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "soft"
        args.bot_emails = [],
        args.exclude_commits = []
        args.update_go_modules = False
        args.ignore_manual_label = False
        args.dry_run = True
        result = cli.rebasebot_run(
            args, slack_webhook=None, github_app_wrapper=fake_github_provider)
        assert (result)

        working_repo = Repo.init(tmpdir)
        assert working_repo.head.ref.name == "rebase"
        log_graph = working_repo.git.log(
            "--graph", "--oneline", "--pretty='<%an>, %s'")

        assert log_graph == r"""
* '<dest_anotherbot@example.com>, commit #1 from anotherbot'
* '<dest_genbot@example.com>, commit #2 from genbot'
* '<dest_author>, commit on dest feature branch'
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

    @patch("rebasebot.bot._push_rebase_branch")
    @patch("rebasebot.bot._is_pr_available")
    @patch("rebasebot.bot._message_slack")
    def test_conflict(self, mocked_message_slack, mocked_is_pr_available, mocked_push_rebase_branch,
                      init_test_repositories, fake_github_provider, tmpdir):
        source, rebase, dest = init_test_repositories
        mocked_is_pr_available.return_value = None, False
        mocked_push_rebase_branch.return_value = True
        CommitBuilder(source).update_file(
            "test.go", "new content").commit("update test.go")
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
        args.ignore_manual_label = False
        args.dry_run = False

        result = cli.rebasebot_run(
            args, slack_webhook="test://webhook", github_app_wrapper=fake_github_provider)
        assert (result)

        working_repo = Repo.init(tmpdir)
        assert working_repo.head.ref.name == "rebase"
        log_graph = working_repo.git.log(
            "--graph", "--oneline", "--pretty='<%an>, %s'")

        assert mocked_message_slack.call_args.args[0] == "test://webhook"
        assert mocked_message_slack.call_args.args[1].startswith(
            "I created a new rebase PR:")

        assert log_graph == r"""
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

    @patch("rebasebot.bot._message_slack")
    @patch("rebasebot.bot._manual_rebase_pr_in_repo")
    def test_has_manual_rebase_pr(self, mocked_manual_rebase_pr_in_repo,
                                  mocked_message_slack, init_test_repositories, fake_github_provider, tmpdir):
        source, rebase, dest = init_test_repositories
        dest.clone_url = "https://github.com/dest/dest"

        pr = MagicMock()
        pr.labels = [{'name': 'rebase/manual'}]
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
        args.ignore_manual_label = False
        args.dry_run = False
        result = cli.rebasebot_run(
            args, slack_webhook=None, github_app_wrapper=fake_github_provider)
        mocked_message_slack.assert_called_once_with(
            None, f"Repo {dest.clone_url} has PR {pr.html_url} with 'rebase/manual' label, aborting")

        assert (result)

    def test_strict_and_excluded_commits(
            self, init_test_repositories, fake_github_provider, tmpdir):
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
            drop_commit = cb.commit(
                "UPSTREAM: <carry>: dropped by exclude_commits")

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
        args.dry_run = True
        result = cli.rebasebot_run(
            args, slack_webhook=None, github_app_wrapper=fake_github_provider)
        assert (result)

        working_repo = Repo.init(tmpdir)
        assert working_repo.head.ref.name == "rebase"
        log_graph = working_repo.git.log(
            "--graph", "--oneline", "--pretty='<%an>, %s'")

        assert log_graph == r"""
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

    @patch("rebasebot.lifecycle_hooks._fetch_file_from_github")
    def test_lifecyclehooks_remote(self, mock_fetch_file_from_github, init_test_repositories,
                                   fake_github_provider, tmpdir, caplog):
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
        args.dry_run = True
        args.post_rebase_hook = ["git:https://github.com/openshift-eng/rebasebot/main:tests/data/test-hook-script.sh"]  # noqa: E501

        result = cli.rebasebot_run(
            args, slack_webhook=None, github_app_wrapper=fake_github_provider)

        mock_fetch_file_from_github.assert_called_once_with(
            ANY, "openshift-eng", "rebasebot", "main", "tests/data/test-hook-script.sh")
        # mock_fetch_branch.assert_called_once_with(
        # ANY, "github.com/openshift-eng/rebasebot", "main",
        # ref_filter="blob:none")
        working_repo = Repo.init(tmpdir)
        assert working_repo.head.ref.name == "rebase"
        log_graph = working_repo.git.log(
            "--graph", "--oneline", "--pretty='<%an>, %s'")

        assert os.path.exists(os.path.join(tmpdir, "test-hook-script.success"))
        assert log_graph == r"""* '<test_rebasebot>, UPSTREAM: <drop>: test-hook-script generated files'
* '<dest_author>, UPSTREAM: <carry>: carry commit #1'
* '<dest_author>, UPSTREAM: <carry>: our cool addition'
*   '<test_rebasebot>, merge upstream/main into main'
|\  
| * '<source_author>, other upstream commit'
* | '<dest_author>, UPSTREAM: <carry>: carry commit #1'
* | '<dest_author>, UPSTREAM: <carry>: our cool addition'
|/  
* '<source_author>, Upstream commit'"""  # noqa: W291
        assert (result)

    def test_lifecyclehooks(self, init_test_repositories,
                            fake_github_provider, tmpdir):
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
                git commit -m 'UPSTREAM: <drop>: test-hook-script generated files'""")
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
        args.dry_run = True,
        args.post_rebase_hook = [f"git:dest/{dest.branch}:test-hook-script.sh"]
        args.source_repo = None

        assert (cli.rebasebot_run(args, slack_webhook=None,
                github_app_wrapper=fake_github_provider))

        working_repo = Repo.init(tmpdir)
        assert working_repo.head.ref.name == "rebase"
        log_graph = working_repo.git.log(
            "--graph", "--oneline", "--pretty='<%an>, %s'")
        assert os.path.exists(os.path.join(tmpdir, "test-hook-script.success"))
        assert log_graph == r"""* '<test_rebasebot>, UPSTREAM: <drop>: test-hook-script generated files'
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

    def test_lifecyclehook_fail(
            self, init_test_repositories, fake_github_provider, tmpdir, caplog):
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
exit 5""")
            cb.commit("UPSTREAM: <carry>: add test hook script")

        args = MagicMock()
        args.source = source
        args.source_repo = None
        args.dest = dest
        args.rebase = rebase
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.pre_rebase_hook = [
            f"git:dest/{dest.branch}:test-failure-hook-script.sh"]
        args.tag_policy = "strict"
        args.bot_emails = ["genbot@example.com", "anotherbot@example.com"]
        args.exclude_commits = []
        args.update_go_modules = False
        args.dry_run = True

        result = cli.rebasebot_run(
            args, slack_webhook=None, github_app_wrapper=fake_github_provider)

        assert "Script git:dest/main:test-failure-hook-script.sh failed with exit code 5" in caplog.text
        assert "Manual intervention is needed to rebase" in caplog.text

        # Rebase did not succeed
        assert result is False

    @patch('rebasebot.cli.parse_github_branch')
    @patch('rebasebot.lifecycle_hooks.parse_github_branch')
    @patch("rebasebot.lifecycle_hooks._fetch_file_from_github")
    def test_source_branch_hook(
            self, mock_fetch_file_from_github, mock_parse_github_branch_hooks, mock_parse_github_branch_cli,
            init_test_repositories, fake_github_provider, tmpdir):
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
        args.source_branch_hook = url
        args.dest = dest
        args.rebase = rebase
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "strict"
        args.bot_emails = []
        args.exclude_commits = []
        args.update_go_modules = False
        args.dry_run = True

        result = cli.rebasebot_run(
            args, slack_webhook=None, github_app_wrapper=fake_github_provider)

        assert args.source == source
        assert result
