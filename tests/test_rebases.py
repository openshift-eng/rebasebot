from __future__ import annotations
from dataclasses import dataclass
import os
from unittest.mock import MagicMock, patch

import pytest

from git import Repo

from rebasebot.github import GitHubBranch
from rebasebot.bot import (
    _init_working_dir,
    _needs_rebase,
    _prepare_rebase_branch,

    run as rebasebot_run
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
    def working_repo_context(self, init_test_repositories, fake_github_provider, tmpdir) -> WorkingRepoContext:
        source, rebase, dest = init_test_repositories
        working_repo = _init_working_dir(
            source, dest, rebase, fake_github_provider, "foo", "foo@example.com", workdir=tmpdir
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

        working_repo_dir_content = {i.name for i in os.scandir(working_repo_path)}
        assert working_repo_dir_content == {'test.go', '.git'}

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
            'merge upstream/main into main',  # merge commit
            'UPSTREAM: <carry>: our cool addition',
            'Upstream commit'
        ]


class TestRebases:

    def test_simple_dry_run(self, init_test_repositories, fake_github_provider, tmpdir):
        source, rebase, dest = init_test_repositories
        CommitBuilder(source).add_file("baz.txt", "fiz").commit("other upstream commit")

        result = rebasebot_run(
            source=source,
            dest=dest,
            rebase=rebase,
            working_dir=tmpdir,
            git_username="test_rebasebot",
            git_email="test@rebasebot.ocp",
            github_app_provider=fake_github_provider,
            slack_webhook=None,
            tag_policy="soft",
            bot_emails=[],
            exclude_commits=[],
            update_go_modules=False,
            dry_run=True,
        )
        assert result

        working_repo = Repo.init(tmpdir)
        assert working_repo.head.ref.name == "rebase"
        log_graph = working_repo.git.log("--graph", "--oneline", "--pretty='<%an>, %s'")
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

        result = rebasebot_run(
            source=source,
            dest=dest,
            rebase=rebase,
            working_dir=tmpdir,
            git_username="test_rebasebot",
            git_email="test@rebasebot.ocp",
            github_app_provider=fake_github_provider,
            slack_webhook=None,
            tag_policy="soft",
            bot_emails=["genbot@example.com", "anotherbot@example.com"],
            exclude_commits=[],
            update_go_modules=False,
            dry_run=True,
        )
        assert(result)
        
        working_repo = Repo.init(tmpdir)
        assert working_repo.head.ref.name == "rebase"
        log_graph = working_repo.git.log("--graph", "--oneline", "--pretty='<%an>, %s'")
        
        assert log_graph == """
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
""".strip()

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

        result = rebasebot_run(
            source=source,
            dest=dest,
            rebase=rebase,
            working_dir=tmpdir,
            git_username="test_rebasebot",
            git_email="test@rebasebot.ocp",
            github_app_provider=fake_github_provider,
            slack_webhook=None,
            tag_policy="soft",
            bot_emails=["genbot@example.com", "anotherbot@example.com"],
            exclude_commits=[],
            update_go_modules=False,
            dry_run=True,
        )
        assert(result)
        
        working_repo = Repo.init(tmpdir)
        assert working_repo.head.ref.name == "rebase"
        log_graph = working_repo.git.log("--graph", "--oneline", "--pretty='<%an>, %s'")
        
        assert log_graph == """
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
""".strip()

    @patch("rebasebot.bot._push_rebase_branch")
    @patch("rebasebot.bot._is_pr_available")
    @patch("rebasebot.bot._message_slack")
    def test_conflict(self, mocked_message_slack, mocked_is_pr_available, mocked_push_rebase_branch,init_test_repositories, fake_github_provider, tmpdir):
        source, rebase, dest = init_test_repositories
        mocked_is_pr_available.return_value = None, False
        mocked_push_rebase_branch.return_value = True
        CommitBuilder(source).update_file("test.go", "new content").commit("update test.go")
        CommitBuilder(dest).remove_file("test.go").commit("remove test.go")
        with CommitBuilder(dest) as cb:
            cb.commit("Empty commit")

        result = rebasebot_run(
            source=source,
            dest=dest,
            rebase=rebase,
            working_dir=tmpdir,
            git_username="test_rebasebot",
            git_email="test@rebasebot.ocp",
            github_app_provider=fake_github_provider,
            slack_webhook="test://webhook",
            tag_policy="soft",
            bot_emails=["genbot@example.com", "anotherbot@example.com"],
            exclude_commits=[],
            update_go_modules=False,
            dry_run=False,
        )
        assert(result)

        working_repo = Repo.init(tmpdir)
        assert working_repo.head.ref.name == "rebase"
        log_graph = working_repo.git.log("--graph", "--oneline", "--pretty='<%an>, %s'")

        assert mocked_message_slack.call_args.args[0] == "test://webhook"
        assert mocked_message_slack.call_args.args[1].startswith("I created a new rebase PR:")

        assert log_graph == """
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
""".strip()

    @patch("rebasebot.bot._message_slack")
    def test_has_manual_rebase_pr(self, mocked_message_slack, init_test_repositories, fake_github_provider, tmpdir):
        source, rebase, _ = init_test_repositories
        dest = MagicMock()
        pr = MagicMock()
        pr.labels = [{'name': 'rebase/manual'}]
        pr.html_url = "https://github.com/rg/test/pull/1"
        dest.pull_requests.return_value = [pr]
        fake_github_provider.github_app.repository.return_value = dest

        result = rebasebot_run(
            source=source,
            dest=dest,
            rebase=rebase,
            working_dir=tmpdir,
            git_username="test_rebasebot",
            git_email="test@rebasebot.ocp",
            github_app_provider=fake_github_provider,
            slack_webhook=None,
            tag_policy="soft",
            bot_emails=[],
            exclude_commits=[],
            update_go_modules=False,
            dry_run=False,
        )

        mocked_message_slack.assert_called_once_with(None, f"Repo {dest.clone_url} has PR {pr.html_url} with 'rebase/manual' label, aborting")
        assert(result)

    def test_strict_and_excluded_commits(self, init_test_repositories, fake_github_provider, tmpdir):
        source, rebase, dest = init_test_repositories
        with CommitBuilder(source) as cb:
            cb.add_file("baz.txt", "fiz")
            cb.commit("other upstream commit")
        with CommitBuilder(dest) as cb:# this commit will be dropped by strict policy
            cb.add_file("carry-file0", "content")
            cb.commit("untagged commit")
        with CommitBuilder(dest) as cb:
            cb.add_file("carry-file1", "content")
            cb.commit("UPSTREAM: <carry>: carry commit #1")
        with CommitBuilder(dest) as cb:
            cb.add_file("carry-file2", "content")
            drop_commit = cb.commit("UPSTREAM: <carry>: dropped by exclude_commits")

        result = rebasebot_run(
            source=source,
            dest=dest,
            rebase=rebase,
            working_dir=tmpdir,
            git_username="test_rebasebot",
            git_email="test@rebasebot.ocp",
            github_app_provider=fake_github_provider,
            slack_webhook=None,
            tag_policy="strict",
            bot_emails=["genbot@example.com", "anotherbot@example.com"],
            exclude_commits=[drop_commit.hexsha],
            update_go_modules=False,
            dry_run=True,
        )
        assert(result)
        
        working_repo = Repo.init(tmpdir)
        assert working_repo.head.ref.name == "rebase"
        log_graph = working_repo.git.log("--graph", "--oneline", "--pretty='<%an>, %s'")

        assert log_graph == """
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
""".strip()