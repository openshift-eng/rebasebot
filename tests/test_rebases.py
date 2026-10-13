from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest
from git import Repo

from rebasebot import cli, lifecycle_hooks
from rebasebot.bot import (
    _init_working_dir,
    _is_pr_required,
    _needs_rebase,
    _prepare_publication_branch,
    _prepare_rebase_workdir,
    _push_publication_branch,
)
from rebasebot.github import GitHubReference, parse_github_reference
from rebasebot.prow import ProwJobContext

from .conftest import CommitBuilder


@dataclass
class WorkingRepoContext:
    source: GitHubReference
    publication: GitHubReference
    destination: GitHubReference

    working_repo: Repo
    working_repo_path: str

    def fetch_remotes(self):
        self.working_repo.git.fetch("--all")


class TestBotInternalHelpers:
    @pytest.fixture
    def working_repo_context(self, init_test_repositories, fake_github_provider, tmpdir) -> WorkingRepoContext:
        source, publication, destination = init_test_repositories
        working_repo = _init_working_dir(
            source=source,
            destination=destination,
            publication=publication,
            github_app_provider=fake_github_provider,
            git_username="foo",
            git_email="foo@example.com",
            workdir=tmpdir,
        )
        _prepare_rebase_workdir(working_repo, source, publication)
        return WorkingRepoContext(source, publication, destination, working_repo, tmpdir)

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

    def test_hook_remote_names_unchanged(self, working_repo_context):
        context = working_repo_context
        working_repo = context.working_repo
        publication_commit = CommitBuilder(context.publication).add_file("published.txt", "content").commit("published")
        _prepare_rebase_workdir(working_repo, context.source, context.publication)
        assert {remote.name for remote in working_repo.remotes} == {"source", "dest", "rebase"}
        assert working_repo.remotes.dest.url == context.destination.url
        assert working_repo.remotes.rebase.url == context.publication.url
        assert working_repo.commit(f"rebase/{context.publication.ref}") == publication_commit
        assert working_repo.head.ref.name == "rebase"

    @pytest.mark.parametrize("existing_publication", [False, True])
    def test_legacy_pre_create_pr_hook_reads_published_commit(
        self, working_repo_context, tmp_path, existing_publication
    ):
        context = working_repo_context
        publication_repo = Repo(context.publication.url)
        publication_repo.git.config("receive.denyCurrentBranch", "updateInstead")
        if existing_publication:
            CommitBuilder(context.publication).add_file("published.txt", "old content").commit("previous publication")
        _prepare_rebase_workdir(context.working_repo, context.source, context.publication)
        context.working_repo.git.commit("--allow-empty", "-m", "new publication")
        published_sha = context.working_repo.head.commit.hexsha
        _push_publication_branch(context.working_repo, context.publication)

        script = tmp_path / "legacy-pre-create-pr.sh"
        script.write_text('#!/bin/sh\ngit rev-parse --verify "refs/remotes/rebase/$REBASEBOT_REBASE"\n')
        script.chmod(0o755)
        with patch.dict(os.environ, {"REBASEBOT_REBASE": context.publication.ref}):
            result = lifecycle_hooks.LifecycleHookScript(str(script))(cwd=context.working_repo_path)
        assert result.return_code == 0
        assert "".join(result.stdout).strip() == published_sha
        assert publication_repo.head.commit.hexsha == published_sha

    def test_pre_create_pr_hook_push_updates_pr_decision(self, working_repo_context, tmp_path):
        context = working_repo_context
        publication_repo = Repo(context.publication.url)
        publication_repo.git.config("receive.denyCurrentBranch", "updateInstead")
        published_file = Path(context.working_repo_path) / "published.txt"
        published_file.write_text("bot result")
        context.working_repo.git.add("published.txt")
        context.working_repo.git.commit("-m", "new publication")
        _push_publication_branch(context.working_repo, context.publication)
        assert _is_pr_required(context.working_repo, context.publication, context.destination)

        script = tmp_path / "pre-create-pr.sh"
        script.write_text(
            f"#!/bin/sh\nset -e\ngit reset --hard dest/{context.destination.ref}\n"
            f"git push --force rebase HEAD:refs/heads/{context.publication.ref}\n"
        )
        script.chmod(0o755)
        result = lifecycle_hooks.LifecycleHookScript(str(script))(cwd=context.working_repo_path)
        assert result.return_code == 0
        assert publication_repo.head.commit == context.working_repo.commit(f"dest/{context.destination.ref}")
        assert not _is_pr_required(context.working_repo, context.publication, context.destination)

    def test_needs_rebase(self, working_repo_context):
        r_ctx = working_repo_context
        gitwd, source, destination = r_ctx.working_repo, r_ctx.source, r_ctx.destination
        assert not _needs_rebase(gitwd, source, destination)

        CommitBuilder(destination).add_file("bar.txt", "foo").commit("UPSTREAM: <carry>: carry patch")
        working_repo_context.fetch_remotes()
        assert not _needs_rebase(gitwd, source, destination)

        CommitBuilder(source).add_file("baz.txt", "fiz").commit("some other upstream commit")
        working_repo_context.fetch_remotes()
        assert _needs_rebase(gitwd, source, destination)

    def test_prepare_rebase_branch(self, working_repo_context):
        r_ctx = working_repo_context
        _prepare_publication_branch(r_ctx.working_repo, r_ctx.source, r_ctx.destination)

        commits = list(r_ctx.working_repo.iter_commits())
        assert len(commits[0].parents)
        commit_msgs = [c.summary for c in commits]
        assert commit_msgs == [
            "merge upstream/main into main",  # merge commit
            "UPSTREAM: <carry>: our cool addition",
            "Upstream commit",
        ]


class TestRebases:
    def test_simple_dry_run(self, init_test_repositories, fake_github_provider, tmpdir):
        source, publication, destination = init_test_repositories
        CommitBuilder(source).add_file("baz.txt", "fiz").commit("other upstream commit")

        args = MagicMock(sync_mode="rebase")
        args.source = source
        args.source_repo = None
        args.destination = destination
        args.publication = publication
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "soft"
        args.bot_emails = []
        args.exclude_commits = []
        args.update_go_modules = False
        args.conflict_policy = "auto"
        args.manual_override = False
        args.dry_run = True
        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)
        assert result

        working_repo = Repo.init(tmpdir)
        assert working_repo.head.ref.name == "rebase"
        log_graph = working_repo.git.log("--graph", "--oneline", "--pretty='<%an>, %s'")
        assert (
            log_graph
            == """ 
* '<destination_author>, UPSTREAM: <carry>: our cool addition'
*   '<test_rebasebot>, merge upstream/main into main'
|\\  
| * '<source_author>, other upstream commit'
* | '<destination_author>, UPSTREAM: <carry>: our cool addition'
|/  
* '<source_author>, Upstream commit'
""".strip()  # noqa: W291
        )

    # Tests that all commits from bots are squashed into one for each bot

    def test_squash_bot_dry_run(self, init_test_repositories, fake_github_provider, tmpdir):
        source, publication, destination = init_test_repositories
        with CommitBuilder(source) as cb:
            cb.add_file("baz.txt", "fiz")
            cb.commit("other upstream commit")
        with CommitBuilder(destination) as cb:
            cb.add_file("generated-test", "content")
            cb.commit("commit #1 from genbot", committer_email="genbot@example.com")
        with CommitBuilder(destination) as cb:
            cb.add_file("generated-test2", "content")
            cb.commit("commit #2 from genbot", committer_email="genbot@example.com")
        with CommitBuilder(destination) as cb:
            cb.add_file("generated-test3", "content")
            cb.commit("commit #1 from anotherbot", committer_email="anotherbot@example.com")

        args = MagicMock(sync_mode="rebase")
        args.source = source
        args.source_repo = None
        args.destination = destination
        args.publication = publication
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "soft"
        args.bot_emails = ["genbot@example.com", "anotherbot@example.com"]
        args.exclude_commits = []
        args.update_go_modules = False
        args.conflict_policy = "auto"
        args.manual_override = False
        args.dry_run = True
        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)
        assert result

        working_repo = Repo.init(tmpdir)
        assert working_repo.head.ref.name == "rebase"
        log_graph = working_repo.git.log("--graph", "--oneline", "--pretty='<%an>, %s'")

        assert (
            log_graph
            == r"""
* '<destination_anotherbot@example.com>, commit #1 from anotherbot'
* '<destination_genbot@example.com>, commit #2 from genbot'
* '<destination_author>, UPSTREAM: <carry>: our cool addition'
*   '<test_rebasebot>, merge upstream/main into main'
|\  
| * '<source_author>, other upstream commit'
* | '<destination_anotherbot@example.com>, commit #1 from anotherbot'
* | '<destination_genbot@example.com>, commit #2 from genbot'
* | '<destination_genbot@example.com>, commit #1 from genbot'
* | '<destination_author>, UPSTREAM: <carry>: our cool addition'
|/  
* '<source_author>, Upstream commit'
""".strip()  # noqa: W291
        )

    def test_squash_bot_email_normalization(self, init_test_repositories, fake_github_provider, tmpdir):
        """GitHub noreply numeric prefixes should normalize without collapsing real plus-addresses."""
        source, publication, destination = init_test_repositories
        with CommitBuilder(source) as cb:
            cb.add_file("baz.txt", "fiz")
            cb.commit("other upstream commit")
        with CommitBuilder(destination) as cb:
            cb.add_file("generated-test-genbot", "content")
            cb.commit("commit #1 from genbot", committer_email="12345+genbot@example.com")
        with CommitBuilder(destination) as cb:
            cb.add_file("generated-test-genbot-2", "content")
            cb.commit("commit #2 from genbot", committer_email="12345+genbot@example.com")
        with CommitBuilder(destination) as cb:
            cb.add_file("generated-test-ci", "content")
            cb.commit("commit #1 from ci nightly", committer_email="ci+nightly@example.com")
        with CommitBuilder(destination) as cb:
            cb.add_file("generated-test-ops", "content")
            cb.commit("commit #1 from ops nightly", committer_email="ops+nightly@example.com")

        args = MagicMock(sync_mode="rebase")
        args.source = source
        args.source_repo = None
        args.destination = destination
        args.publication = publication
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "soft"
        args.bot_emails = ["12345+genbot@example.com", "ci+nightly@example.com", "ops+nightly@example.com"]
        args.exclude_commits = []
        args.update_go_modules = False
        args.conflict_policy = "auto"
        args.manual_override = False
        args.dry_run = True
        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)
        assert result

        working_repo = Repo.init(tmpdir)
        assert working_repo.head.ref.name == "rebase"
        log_graph = working_repo.git.log("--graph", "--oneline", "--pretty='<%an>, %s'")
        assert "* '<destination_ops+nightly@example.com>, commit #1 from ops nightly'" in log_graph
        assert "* '<destination_ci+nightly@example.com>, commit #1 from ci nightly'" in log_graph
        assert (
            log_graph
            == r"""
* '<destination_ops+nightly@example.com>, commit #1 from ops nightly'
* '<destination_ci+nightly@example.com>, commit #1 from ci nightly'
* '<destination_12345+genbot@example.com>, commit #2 from genbot'
* '<destination_author>, UPSTREAM: <carry>: our cool addition'
*   '<test_rebasebot>, merge upstream/main into main'
|\  
| * '<source_author>, other upstream commit'
* | '<destination_ops+nightly@example.com>, commit #1 from ops nightly'
* | '<destination_ci+nightly@example.com>, commit #1 from ci nightly'
* | '<destination_12345+genbot@example.com>, commit #2 from genbot'
* | '<destination_12345+genbot@example.com>, commit #1 from genbot'
* | '<destination_author>, UPSTREAM: <carry>: our cool addition'
|/  
* '<source_author>, Upstream commit'
""".strip()  # noqa: W291
        )

    def test_squash_bot_skips_empty_later_pick(self, init_test_repositories, fake_github_provider, tmpdir):
        """A later empty bot pick must not rewind past earlier squashed bot commits."""
        source, publication, destination = init_test_repositories
        with CommitBuilder(source) as cb:
            cb.add_file("baz.txt", "fiz")
            cb.commit("other upstream commit")
        with CommitBuilder(destination) as cb:
            cb.add_file("generated-test", "content from first bot")
            cb.commit("commit #1 from firstbot", committer_email="firstbot@example.com")
        with CommitBuilder(destination) as cb:
            cb.update_file("generated-test", "content from second bot")
            cb.commit("commit #1 from secondbot", committer_email="secondbot@example.com")
        with CommitBuilder(destination) as cb:
            cb.update_file("generated-test", "content from second bot")
            cb.commit("commit #2 from secondbot", committer_email="secondbot@example.com")

        args = MagicMock(sync_mode="rebase")
        args.source = source
        args.source_repo = None
        args.destination = destination
        args.publication = publication
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "soft"
        args.bot_emails = ["firstbot@example.com", "secondbot@example.com"]
        args.exclude_commits = []
        args.update_go_modules = False
        args.conflict_policy = "auto"
        args.manual_override = False
        args.dry_run = True
        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)
        assert result

        working_repo = Repo.init(tmpdir)
        assert working_repo.head.ref.name == "rebase"
        log_graph = working_repo.git.log("--graph", "--oneline", "--pretty='<%an>, %s'")
        assert (
            log_graph
            == r"""
* '<destination_secondbot@example.com>, commit #1 from secondbot'
* '<destination_firstbot@example.com>, commit #1 from firstbot'
* '<destination_author>, UPSTREAM: <carry>: our cool addition'
*   '<test_rebasebot>, merge upstream/main into main'
|\  
| * '<source_author>, other upstream commit'
* | '<destination_secondbot@example.com>, commit #2 from secondbot'
* | '<destination_secondbot@example.com>, commit #1 from secondbot'
* | '<destination_firstbot@example.com>, commit #1 from firstbot'
* | '<destination_author>, UPSTREAM: <carry>: our cool addition'
|/  
* '<source_author>, Upstream commit'
""".strip()  # noqa: W291
        )

    def test_first_run_destination_has_merges_dry_run(self, init_test_repositories, fake_github_provider, tmpdir):
        source, publication, destination = init_test_repositories
        with CommitBuilder(source) as cb:
            cb.add_file("baz.txt", "fiz")
            cb.commit("other upstream commit")
        with CommitBuilder(destination) as cb:
            cb.add_file("generated-test", "content")
            cb.commit("commit #1 from genbot", committer_email="genbot@example.com")
        # make branch
        destination_feature_branch = GitHubReference(
            url=destination.url, ns="destination", name="destination", ref="feature"
        )
        with CommitBuilder(destination_feature_branch) as cb:
            cb.add_file("feature-file", "feature content")
            cb.commit("commit on destination feature branch")
        with CommitBuilder(destination) as cb:
            cb.add_file("generated-test2", "content")
            cb.commit("commit #2 from genbot", committer_email="genbot@example.com")
        # merge feature branch to destination
        repo = Repo(destination.url)
        repo.git.checkout(destination.ref)
        repo.git.merge(repo.heads.feature)
        with CommitBuilder(destination) as cb:
            cb.add_file("generated-test3", "content")
            cb.commit("commit #1 from anotherbot", committer_email="anotherbot@example.com")

        args = MagicMock(sync_mode="rebase")
        args.source = source
        args.source_repo = None
        args.destination = destination
        args.publication = publication
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "soft"
        args.bot_emails = []
        args.exclude_commits = []
        args.update_go_modules = False
        args.conflict_policy = "auto"
        args.manual_override = False
        args.dry_run = True
        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)
        assert result

        working_repo = Repo.init(tmpdir)
        assert working_repo.head.ref.name == "rebase"
        log_graph = working_repo.git.log("--graph", "--oneline", "--pretty='<%an>, %s'")

        assert (
            log_graph
            == r"""
* '<destination_anotherbot@example.com>, commit #1 from anotherbot'
* '<destination_author>, commit on destination feature branch'
* '<destination_genbot@example.com>, commit #2 from genbot'
* '<destination_genbot@example.com>, commit #1 from genbot'
* '<destination_author>, UPSTREAM: <carry>: our cool addition'
*   '<test_rebasebot>, merge upstream/main into main'
|\  
| * '<source_author>, other upstream commit'
* | '<destination_anotherbot@example.com>, commit #1 from anotherbot'
* |   '<destination_genbot@example.com>, Merge branch 'feature''
|\ \  
| * | '<destination_author>, commit on destination feature branch'
* | | '<destination_genbot@example.com>, commit #2 from genbot'
|/ /  
* | '<destination_genbot@example.com>, commit #1 from genbot'
* | '<destination_author>, UPSTREAM: <carry>: our cool addition'
|/  
* '<source_author>, Upstream commit'
""".strip()  # noqa: W291
        )

    def test_first_run_destination_merges_feature_branch_dry_run(
        self, init_test_repositories, fake_github_provider, tmpdir
    ):
        source, publication, destination = init_test_repositories

        # Ensure source/main has advanced so a rebase is required
        with CommitBuilder(source) as cb:
            cb.add_file("bar.txt", "fiz")
            cb.commit("other upstream commit")

        # Git reset to remove one commit from destination main
        repo = Repo(destination.url)
        repo.git.checkout(destination.ref)
        repo.git.reset("--hard", "HEAD~1")

        # Create feature branch in destination, make commit there, then merge into destination/main
        destination_feature_branch = GitHubReference(
            url=destination.url, ns="destination", name="destination", ref="feature"
        )
        with CommitBuilder(destination_feature_branch) as cb:
            cb.add_file("carry-commit-file", "content")
            cb.commit("UPSTREAM: <carry>: commit #1 from anotherbot", committer_email="anotherbot@example.com")
        repo.git.checkout(destination.ref)
        repo.git.merge("--no-ff", "-m", "Merge branch 'feature'", repo.heads.feature)

        args = MagicMock(sync_mode="rebase")
        args.source = source
        args.source_repo = None
        args.destination = destination
        args.publication = publication
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "soft"
        args.bot_emails = []
        args.exclude_commits = []
        args.update_go_modules = False
        args.conflict_policy = "auto"
        args.manual_override = False
        args.dry_run = True

        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)
        assert result

        working_repo = Repo.init(tmpdir)
        log_graph = working_repo.git.log("--graph", "--oneline", "--pretty='<%an>, %s'")
        assert log_graph == "\n".join(
            [
                "* '<destination_anotherbot@example.com>, UPSTREAM: <carry>: commit #1 from anotherbot'",
                "*   '<test_rebasebot>, merge upstream/main into main'",
                r"|\  ",
                "| * '<source_author>, other upstream commit'",
                "* |   '<destination_anotherbot@example.com>, Merge branch 'feature''",
                r"|\ \  ",
                r"| |/  ",
                r"|/|   ",
                "| * '<destination_anotherbot@example.com>, UPSTREAM: <carry>: commit #1 from anotherbot'",
                r"|/  ",
                "* '<source_author>, Upstream commit'",
            ]
        )

    @patch("rebasebot.bot._push_publication_branch")
    @patch("rebasebot.bot._is_pr_available")
    @patch("rebasebot.slack.requests.post")
    def test_conflict(
        self,
        mocked_post,
        mocked_is_pr_available,
        mocked_push_publication_branch,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
    ):
        source, publication, destination = init_test_repositories
        mocked_is_pr_available.return_value = None, False
        mocked_push_publication_branch.return_value = True
        CommitBuilder(source).update_file("test.go", "new content").commit("update test.go")
        CommitBuilder(destination).remove_file("test.go").commit("remove test.go")
        with CommitBuilder(destination) as cb:
            cb.commit("Empty commit")

        args = MagicMock(sync_mode="rebase")
        args.source = source
        args.source_repo = None
        args.destination = destination
        args.publication = publication
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "soft"
        args.bot_emails = ["genbot@example.com", "anotherbot@example.com"]
        args.exclude_commits = []
        args.update_go_modules = False
        args.conflict_policy = "auto"
        args.manual_override = False
        args.dry_run = False

        result = cli.rebasebot_run(args, slack_webhook="test://webhook", github_app_wrapper=fake_github_provider)
        assert result

        working_repo = Repo.init(tmpdir)
        assert working_repo.head.ref.name == "rebase"
        log_graph = working_repo.git.log("--graph", "--oneline", "--pretty='<%an>, %s'")

        assert mocked_post.call_args.args[0] == "test://webhook"
        posted_json = mocked_post.call_args.kwargs["json"]
        assert posted_json["text"].startswith(f"Created a new synchronization PR for `{destination.label}`:")
        blocks = posted_json["blocks"]
        assert blocks[0]["text"]["text"].startswith(f"✅ Created a new synchronization PR for `{destination.label}`:")

        assert (
            log_graph
            == r"""
* '<destination_author>, remove test.go'
* '<destination_author>, UPSTREAM: <carry>: our cool addition'
*   '<test_rebasebot>, merge upstream/main into main'
|\  
| * '<source_author>, update test.go'
* | '<destination_author>, Empty commit'
* | '<destination_author>, remove test.go'
* | '<destination_author>, UPSTREAM: <carry>: our cool addition'
|/  
* '<source_author>, Upstream commit'
""".strip()  # noqa: W291
        )

    @patch("rebasebot.slack.requests.post")
    @patch("rebasebot.bot._paused_pr_in_repo")
    def test_has_manual_rebase_pr(
        self,
        mocked_manual_rebase_pr_in_repo,
        mocked_post,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
    ):
        source, publication, destination = init_test_repositories
        destination.clone_url = "https://github.com/destination/destination"

        pr = MagicMock()
        pr.labels = [{"name": "rebase/manual"}]
        pr.html_url = "https://github.com/destination/destination/pull/1"
        mocked_manual_rebase_pr_in_repo.return_value = pr

        # Mock GitHub repository lookup function
        def fake_repository_func(namespace, name):
            repository = MagicMock()
            repository.clone_url = f"https://github.com/{namespace}/{name}"
            return repository

        fake_github_provider.github_destination_app.repository = fake_repository_func

        args = MagicMock(sync_mode="rebase")
        args.source = source
        args.source_repo = None
        args.destination = destination
        args.publication = publication
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "soft"
        args.bot_emails = []
        args.exclude_commits = []
        args.update_go_modules = False
        args.conflict_policy = "auto"
        args.manual_override = False
        args.dry_run = False
        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)
        mocked_post.assert_not_called()

        assert result

    def test_strict_and_excluded_commits(self, init_test_repositories, fake_github_provider, tmpdir):
        source, publication, destination = init_test_repositories
        with CommitBuilder(source) as cb:
            cb.add_file("baz.txt", "fiz")
            cb.commit("other upstream commit")
        with CommitBuilder(destination) as cb:  # this commit will be dropped by strict policy
            cb.add_file("carry-file0", "content")
            cb.commit("untagged commit")
        with CommitBuilder(destination) as cb:
            cb.add_file("carry-file1", "content")
            cb.commit("UPSTREAM: <carry>: carry commit #1")
        with CommitBuilder(destination) as cb:
            cb.add_file("carry-file2", "content")
            drop_commit = cb.commit("UPSTREAM: <carry>: dropped by exclude_commits")

        args = MagicMock(sync_mode="rebase")
        args.source = source
        args.source_repo = None
        args.destination = destination
        args.publication = publication
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
* '<destination_author>, UPSTREAM: <carry>: carry commit #1'
* '<destination_author>, UPSTREAM: <carry>: our cool addition'
*   '<test_rebasebot>, merge upstream/main into main'
|\  
| * '<source_author>, other upstream commit'
* | '<destination_author>, UPSTREAM: <carry>: dropped by exclude_commits'
* | '<destination_author>, UPSTREAM: <carry>: carry commit #1'
* | '<destination_author>, untagged commit'
* | '<destination_author>, UPSTREAM: <carry>: our cool addition'
|/  
* '<source_author>, Upstream commit'
""".strip()  # noqa: W291
        )

    @patch("rebasebot.lifecycle_hooks._fetch_file_from_github")
    def test_lifecyclehooks_remote(
        self, mock_fetch_file_from_github, init_test_repositories, fake_github_provider, tmpdir, caplog
    ):
        source, publication, destination = init_test_repositories

        mock_fetch_file_from_github.return_value.decoded = rb"""#!/bin/bash
touch test-hook-script.success
git add test-hook-script.success
git commit -m 'UPSTREAM: <drop>: test-hook-script generated files'
"""

        with CommitBuilder(source) as cb:
            cb.add_file("baz.txt", "fiz")
            cb.commit("other upstream commit")
        with CommitBuilder(destination) as cb:
            cb.add_file("carry-file1", "content")
            cb.commit("UPSTREAM: <carry>: carry commit #1")

        args = MagicMock(sync_mode="rebase")
        args.source = source
        args.source_repo = None
        args.destination = destination
        args.publication = publication
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
* '<destination_author>, UPSTREAM: <carry>: carry commit #1'
* '<destination_author>, UPSTREAM: <carry>: our cool addition'
*   '<test_rebasebot>, merge upstream/main into main'
|\  
| * '<source_author>, other upstream commit'
* | '<destination_author>, UPSTREAM: <carry>: carry commit #1'
* | '<destination_author>, UPSTREAM: <carry>: our cool addition'
|/  
* '<source_author>, Upstream commit'"""  # noqa: W291
        )
        assert result

    def test_lifecyclehooks(self, init_test_repositories, fake_github_provider, tmpdir):
        source, publication, destination = init_test_repositories
        with CommitBuilder(source) as cb:
            cb.add_file("baz.txt", "fiz")
            cb.commit("other upstream commit")
        with CommitBuilder(destination) as cb:
            cb.add_file("carry-file1", "content")
            cb.commit("UPSTREAM: <carry>: carry commit #1")
        with CommitBuilder(destination) as cb:
            cb.add_file(
                "test-hook-script.sh",
                r"""#!/bin/bash
                touch test-hook-script.success
                git add test-hook-script.success
                git commit -m 'UPSTREAM: <drop>: test-hook-script generated files'""",
            )
            cb.commit("UPSTREAM: <carry>: add test hook script")

        args = MagicMock(sync_mode="rebase")
        args.source = source
        args.destination = destination
        args.publication = publication
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "strict"
        args.bot_emails = ["genbot@example.com", "anotherbot@example.com"]
        args.exclude_commits = []
        args.update_go_modules = False
        args.conflict_policy = "auto"
        args.dry_run = True
        args.post_rebase_hook = [f"git:dest/{destination.ref}:test-hook-script.sh"]
        args.source_repo = None

        assert cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

        working_repo = Repo.init(tmpdir)
        assert working_repo.head.ref.name == "rebase"
        log_graph = working_repo.git.log("--graph", "--oneline", "--pretty='<%an>, %s'")
        assert os.path.exists(os.path.join(tmpdir, "test-hook-script.success"))
        assert (
            log_graph
            == r"""* '<test_rebasebot>, UPSTREAM: <drop>: test-hook-script generated files'
* '<destination_author>, UPSTREAM: <carry>: add test hook script'
* '<destination_author>, UPSTREAM: <carry>: carry commit #1'
* '<destination_author>, UPSTREAM: <carry>: our cool addition'
*   '<test_rebasebot>, merge upstream/main into main'
|\  
| * '<source_author>, other upstream commit'
* | '<destination_author>, UPSTREAM: <carry>: add test hook script'
* | '<destination_author>, UPSTREAM: <carry>: carry commit #1'
* | '<destination_author>, UPSTREAM: <carry>: our cool addition'
|/  
* '<source_author>, Upstream commit'"""  # noqa: W291
        )

    def test_lifecyclehook_fail(self, init_test_repositories, fake_github_provider, tmpdir, caplog):
        source, publication, destination = init_test_repositories
        with CommitBuilder(source) as cb:
            cb.add_file("baz.txt", "fiz")
            cb.commit("other upstream commit")
        with CommitBuilder(destination) as cb:
            cb.add_file("carry-file1", "content")
            cb.commit("UPSTREAM: <carry>: carry commit #1")
        with CommitBuilder(destination) as cb:
            cb.add_file(
                "test-failure-hook-script.sh",
                r"""#!/bin/bash
exit 5""",
            )
            cb.commit("UPSTREAM: <carry>: add test hook script")

        args = MagicMock(sync_mode="rebase")
        args.source = source
        args.source_repo = None
        args.destination = destination
        args.publication = publication
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.pre_rebase_hook = [f"git:dest/{destination.ref}:test-failure-hook-script.sh"]
        args.tag_policy = "strict"
        args.bot_emails = ["genbot@example.com", "anotherbot@example.com"]
        args.exclude_commits = []
        args.update_go_modules = False
        args.conflict_policy = "auto"
        args.dry_run = True

        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

        assert "Script git:dest/main:test-failure-hook-script.sh failed with exit code 5" in caplog.text
        assert "Manual intervention is needed to synchronize" in caplog.text

        # Rebase did not succeed
        assert result is False

    @patch("rebasebot.cli.parse_github_reference")
    @patch("rebasebot.lifecycle_hooks.parse_github_reference")
    @patch("rebasebot.lifecycle_hooks._fetch_file_from_github")
    def test_source_ref_hook(
        self,
        mock_fetch_file_from_github,
        mock_parse_github_reference_hooks,
        mock_parse_github_reference_cli,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
    ):
        source, publication, destination = init_test_repositories
        with CommitBuilder(source) as cb:
            cb.add_file("baz.txt", "fiz")
            cb.commit("other upstream commit")
        with CommitBuilder(destination) as cb:
            cb.add_file("carry-file1", "content")
            cb.commit("UPSTREAM: <carry>: carry commit #1")

            mock_fetch_file_from_github.return_value.decoded = rb"""#!/bin/sh
echo main
"""

        def fake_parse_github_reference(location):
            branch = parse_github_reference(location)
            assert location == "source/source:main"
            branch.url = source.url
            return branch

        mock_parse_github_reference_hooks.side_effect = fake_parse_github_reference
        mock_parse_github_reference_cli.side_effect = fake_parse_github_reference

        args = MagicMock(sync_mode="rebase")
        args.source = None
        args.source_repo = f"{source.ns}/{source.name}"
        url = "git:https://github.com/openshift-eng/rebasebot/main:tests/data/test-source-ref-hook-script.sh"
        args.source_ref_hook = url
        args.destination = destination
        args.publication = publication
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
        source, publication, destination = init_test_repositories

        # Create separate test hooks that create different marker files
        pre_rebase_hook_script = """#!/bin/bash
touch pre-rebase-hook.success"""

        post_rebase_hook_script = """#!/bin/bash
touch post-rebase-hook.success"""

        with CommitBuilder(destination) as cb:
            cb.add_file("pre-rebase-hook-script.sh", pre_rebase_hook_script)
            cb.add_file("post-rebase-hook-script.sh", post_rebase_hook_script)
            cb.commit("UPSTREAM: <carry>: add test hook scripts")

        # Configure args with always_run_hooks=True and multiple hook types to test
        args = MagicMock(sync_mode="rebase")
        args.source = source
        args.source_repo = None
        args.destination = destination
        args.publication = publication
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "none"
        args.bot_emails = []
        args.exclude_commits = []
        args.update_go_modules = False
        args.conflict_policy = "auto"
        args.dry_run = True
        args.manual_override = True
        args.always_run_hooks = True

        # Test multiple hook types with different scripts
        args.pre_rebase_hook = [f"git:dest/{destination.ref}:pre-rebase-hook-script.sh"]
        args.post_rebase_hook = [f"git:dest/{destination.ref}:post-rebase-hook-script.sh"]
        args.pre_carry_commit_hook = None
        args.pre_publication_hook = None
        args.pre_create_pr_hook = None

        # Verify no rebase is needed initially (source and destination are in sync)
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
        source, publication, destination = init_test_repositories

        # Create separate test hooks that create different marker files
        pre_rebase_hook_script = """#!/bin/bash
touch pre-rebase-hook.success"""

        post_rebase_hook_script = """#!/bin/bash
touch post-rebase-hook.success"""

        with CommitBuilder(destination) as cb:
            cb.add_file("pre-rebase-hook-script.sh", pre_rebase_hook_script)
            cb.add_file("post-rebase-hook-script.sh", post_rebase_hook_script)
            cb.commit("UPSTREAM: <carry>: add test hook scripts")

        # Configure args with always_run_hooks=False (default behavior)
        args = MagicMock(sync_mode="rebase")
        args.source = source
        args.source_repo = None
        args.destination = destination
        args.publication = publication
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "none"
        args.bot_emails = []
        args.exclude_commits = []
        args.update_go_modules = False
        args.conflict_policy = "auto"
        args.dry_run = True
        args.manual_override = True
        args.always_run_hooks = False  # Key difference: hooks should NOT run

        args.pre_rebase_hook = [f"git:dest/{destination.ref}:pre-rebase-hook-script.sh"]
        args.post_rebase_hook = [f"git:dest/{destination.ref}:post-rebase-hook-script.sh"]
        args.pre_carry_commit_hook = None
        args.pre_publication_hook = None
        args.pre_create_pr_hook = None

        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

        assert result is True
        # Verify hooks were NOT executed - marker files should not exist
        assert "pre-rebase-hook.success" not in os.listdir(tmpdir)
        assert "post-rebase-hook.success" not in os.listdir(tmpdir)

    def test_always_run_hooks_preserves_carry_commits(self, init_test_repositories, fake_github_provider, tmpdir):
        """Test that hooks run on top of destination branch (not source) when no rebase is needed.

        This verifies the fix for a bug where --always-run-hooks would run hooks
        on top of the source/upstream branch, producing a publication branch missing
        all downstream carry commits. The resulting PR would have merge conflicts
        because it tried to merge a branch without carries into destination which has them.
        """
        source, publication, destination = init_test_repositories

        # Add a carry commit with a downstream-only file to destination
        with CommitBuilder(destination) as cb:
            cb.add_file("DOWNSTREAM_OWNERS", "approvers:\n- testuser\n")
            cb.commit("UPSTREAM: <carry>: add DOWNSTREAM_OWNERS")

        # Hook that verifies DOWNSTREAM_OWNERS exists (i.e. we're on destination, not source)
        # and creates a marker file to prove it ran on the right branch
        post_rebase_hook_script = """#!/bin/bash
if [ -f DOWNSTREAM_OWNERS ]; then
    echo "carry-preserved" > hook-verified-carry.txt
    git add hook-verified-carry.txt
    git commit -m "UPSTREAM: <drop>: hook verified carry commits present"
else
    echo "FAIL: DOWNSTREAM_OWNERS not found - hooks ran on source, not destination" >&2
    exit 1
fi"""

        with CommitBuilder(destination) as cb:
            cb.add_file("verify-hook.sh", post_rebase_hook_script)
            cb.commit("UPSTREAM: <carry>: add verify hook script")

        args = MagicMock(sync_mode="rebase")
        args.source = source
        args.source_repo = None
        args.destination = destination
        args.publication = publication
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "none"
        args.bot_emails = []
        args.exclude_commits = []
        args.update_go_modules = False
        args.conflict_policy = "auto"
        args.dry_run = True
        args.manual_override = True
        args.always_run_hooks = True

        args.pre_rebase_hook = None
        args.post_rebase_hook = [f"git:dest/{destination.ref}:verify-hook.sh"]
        args.pre_carry_commit_hook = None
        args.pre_publication_hook = None
        args.pre_create_pr_hook = None

        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

        assert result is True
        # The hook should have succeeded (exit 0), proving DOWNSTREAM_OWNERS existed
        # which means hooks ran on destination branch, not source branch
        assert "hook-verified-carry.txt" in os.listdir(tmpdir)

    @staticmethod
    def _setup_github_provider(fake_github_provider):
        def fake_repository_func(namespace, name):
            repository = MagicMock()
            repository.clone_url = f"https://github.com/{namespace}/{name}"
            return repository

        fake_github_provider.github_destination_app.repository = fake_repository_func
        fake_github_provider.github_publication_app.repository = fake_repository_func

    @staticmethod
    def _make_rebase_args(source, destination, publication, tmpdir):
        args = MagicMock(sync_mode="rebase")
        args.source = source
        args.source_repo = None
        args.destination = destination
        args.publication = publication
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "soft"
        args.bot_emails = []
        args.exclude_commits = []
        args.update_go_modules = False
        args.conflict_policy = "auto"
        args.manual_override = False
        args.dry_run = False
        args.always_run_hooks = False
        args.title_prefix = ""
        args.pre_rebase_hook = None
        args.pre_carry_commit_hook = None
        args.post_rebase_hook = None
        args.pre_publication_hook = None
        args.pre_create_pr_hook = None
        return args

    @patch("rebasebot.bot._push_publication_branch")
    @patch("rebasebot.bot._is_pr_available")
    def test_pr_body_on_create(
        self,
        mocked_is_pr_available,
        mocked_push_publication_branch,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
    ):
        source, publication, destination = init_test_repositories
        CommitBuilder(source).add_file("baz.txt", "fiz").commit("other upstream commit")

        self._setup_github_provider(fake_github_provider)
        mocked_is_pr_available.return_value = None, False
        mocked_push_publication_branch.return_value = True

        captured_pr_data = {}

        def fake_post(_url, data, json=True):
            captured_pr_data.update(data)
            response = MagicMock()
            response.json.return_value = {"html_url": "https://github.com/destination/destination/pull/1"}
            return response

        fake_github_provider.github_destination_app._post = fake_post

        prow_job = ProwJobContext(
            job_name="periodic-openshift-release-rebasebot",
            job_type="periodic",
            build_id="999",
        )
        args = self._make_rebase_args(source, destination, publication, tmpdir)

        with patch("rebasebot.prow.ProwJobContext.from_env", return_value=prow_job):
            result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

        assert result
        body = captured_pr_data["body"]
        assert body
        assert "automated synchronization PR generated by RebaseBot" in body
        assert f"{source.web_url}" in body
        assert f"{destination.web_url}" in body
        assert "1 new upstream commit" in body
        assert prow_job.log_url in body
        assert "View job log" in body

    @patch("rebasebot.bot._push_publication_branch")
    @patch("rebasebot.bot._is_pr_available")
    def test_pr_body_on_update(
        self,
        mocked_is_pr_available,
        mocked_push_publication_branch,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
    ):
        source, publication, destination = init_test_repositories
        CommitBuilder(source).add_file("baz.txt", "fiz").commit("other upstream commit")

        self._setup_github_provider(fake_github_provider)
        mocked_push_publication_branch.return_value = True

        pull_req = MagicMock()
        pull_req.title = f"Merge {source.web_url} (abcdefg) into {destination.ref}"
        pull_req.update.return_value = True
        pull_req.html_url = "https://github.com/destination/destination/pull/1"
        mocked_is_pr_available.return_value = pull_req, True

        prow_job = ProwJobContext(
            job_name="periodic-openshift-release-rebasebot",
            job_type="periodic",
            build_id="888",
        )
        args = self._make_rebase_args(source, destination, publication, tmpdir)

        with patch("rebasebot.prow.ProwJobContext.from_env", return_value=prow_job):
            result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

        assert result
        body_updates = [call for call in pull_req.update.call_args_list if "body" in call.kwargs]
        assert len(body_updates) == 1
        body = body_updates[0].kwargs["body"]
        assert body
        assert "automated synchronization PR generated by RebaseBot" in body
        assert f"{source.web_url}" in body
        assert f"{destination.web_url}" in body
        assert "1 new upstream commit" in body
        assert prow_job.log_url in body

    @patch("rebasebot.bot._push_publication_branch")
    @patch("rebasebot.bot._is_pr_available")
    def test_pr_body_untouched_on_quiet_run(
        self,
        mocked_is_pr_available,
        mocked_push_publication_branch,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
    ):
        """A run with no new upstream commits must not clobber a previously-rendered,
        content-rich PR body with an empty one: rebase_summary only carries fresh data
        when needs_rebase is True.
        """
        source, publication, destination = init_test_repositories

        self._setup_github_provider(fake_github_provider)
        mocked_push_publication_branch.return_value = True

        pull_req = MagicMock()
        pull_req.title = f"Merge {source.web_url} (abcdefg) into {destination.ref}"
        pull_req.update.return_value = True
        pull_req.html_url = "https://github.com/destination/destination/pull/1"
        mocked_is_pr_available.return_value = pull_req, True

        prow_job = ProwJobContext(
            job_name="periodic-openshift-release-rebasebot",
            job_type="periodic",
            build_id="888",
        )
        args = self._make_rebase_args(source, destination, publication, tmpdir)

        with patch("rebasebot.prow.ProwJobContext.from_env", return_value=prow_job):
            result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

        assert result
        body_updates = [call for call in pull_req.update.call_args_list if "body" in call.kwargs]
        assert not body_updates

    @patch("rebasebot.bot._push_publication_branch")
    @patch("rebasebot.bot._is_pr_available")
    def test_pr_body_refreshed_on_hooks_only_run(
        self,
        mocked_is_pr_available,
        mocked_push_publication_branch,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
    ):
        """A hooks-only run (always_run_hooks=True, no rebase needed) never has real rebase
        data to lose -- _do_rebase()/_cherrypick_art_pull_request() never run on this path,
        even when the PR was first created via this same path -- so it's safe (and desirable,
        for a fresh log link) to refresh the body here, unlike the fully idle case above.
        """
        source, publication, destination = init_test_repositories

        pre_rebase_hook_script = "#!/bin/bash\ntouch pre-rebase-hook.success"
        with CommitBuilder(destination) as cb:
            cb.add_file("pre-rebase-hook-script.sh", pre_rebase_hook_script)
            cb.commit("UPSTREAM: <carry>: add test hook script")

        self._setup_github_provider(fake_github_provider)
        mocked_push_publication_branch.return_value = True

        pull_req = MagicMock()
        pull_req.title = f"Merge {source.web_url} (abcdefg) into {destination.ref}"
        pull_req.update.return_value = True
        pull_req.html_url = "https://github.com/destination/destination/pull/1"
        mocked_is_pr_available.return_value = pull_req, True

        prow_job = ProwJobContext(
            job_name="periodic-openshift-release-rebasebot",
            job_type="periodic",
            build_id="888",
        )
        args = self._make_rebase_args(source, destination, publication, tmpdir)
        args.always_run_hooks = True
        args.pre_rebase_hook = [f"git:dest/{destination.ref}:pre-rebase-hook-script.sh"]

        with patch("rebasebot.prow.ProwJobContext.from_env", return_value=prow_job):
            result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

        assert result
        body_updates = [call for call in pull_req.update.call_args_list if "body" in call.kwargs]
        assert len(body_updates) == 1
        body = body_updates[0].kwargs["body"]
        assert "0 new upstream commits" in body
        assert prow_job.log_url in body
