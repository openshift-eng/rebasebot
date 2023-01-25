from dataclasses import dataclass
import os

import pytest

from git import Repo

from rebasebot.github import GitHubBranch
from rebasebot.bot import (
    _init_working_dir,
    _needs_rebase,
    _prepare_rebase_branch,

    run as rebasebot_run
)

from .conftest import commit_file_with_content


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

        commit_file_with_content("foo", "bar.txt", "UPSTREAM: <carry>: carry patch", dest)
        working_repo_context.fetch_remotes()
        assert not _needs_rebase(gitwd, source, dest)

        commit_file_with_content("fiz", "baz.txt", "some other upstream commit", source)
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
        commit_file_with_content("fiz", "baz.txt", "other upstream commit", source)

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
