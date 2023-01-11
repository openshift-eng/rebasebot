import os
from unittest.mock import patch

import pytest

from rebasebot.github import GitHubBranch
from rebasebot.bot import (
    _commit_go_mod_updates,
    _add_to_rebase
)


class TestGoMod:

    def test_update_and_commit(self, tmp_go_app_repo):
        repo_dir, repo = tmp_go_app_repo

        os.chdir(repo_dir)
        os.system("go mod init example.com/foo")
        repo.git.add(all=True)
        repo.git.commit("-m", "Init go module")

        source = GitHubBranch(repo_dir, "example", "foo", repo.active_branch.name)
        repo.create_remote("source", source.url)
        repo.remotes.source.fetch(source.branch)

        _commit_go_mod_updates(repo, source)

        commits = list(repo.iter_commits())

        assert len(commits) == 3
        assert commits[0].message == "UPSTREAM: <carry>: Updating and vendoring go modules after an upstream rebase\n"

    # Test how the function handles an empty commit.
    # This should not error out and exit if working properly.
    def test_update_and_commit_empty(self, tmp_go_app_repo):
        repo_dir, repo = tmp_go_app_repo

        os.chdir(repo_dir)
        os.system("go mod init example.com/foo")
        os.system("go mod tidy")
        os.system("go mod vendor")
        repo.git.add(all=True)
        repo.git.commit("-m", "tidy and vendor go stuff")

        source = GitHubBranch(repo_dir, "example", "foo", repo.active_branch.name)
        repo.create_remote("source", source.url)
        repo.remotes.source.fetch(source.branch)

        _commit_go_mod_updates(repo, source)

        commits = list(repo.iter_commits())

        assert len(commits) == 2  # first commit came from the fixture
        assert commits[0].message == "tidy and vendor go stuff\n"


class TestCommitMessageTags:

    @pytest.mark.parametrize(
        'pr_is_merged,commit_message,tag_strategy,expected',
        (
                (False, "UPSTREAM: <carry>: something", "soft", True),
                (False, "UPSTREAM: <drop>: something", "soft", False),  # Drop commit with drop tag
                (True, "UPSTREAM: 100: something", "soft", False),  # Drop commit if upstream pr was merged
                (False, "UPSTREAM: 100: something", "soft", True),
                (False, "NO TAG: <carry>: something", "soft", True),
                (False, "NO TAG: something", "soft", True),

                # always keep commits with none policy
                (False, "NO TAG: something", "none", True),
                (True, "UPSTREAM: 100: something", "none", True),
                (False, "foo", "none", True),

                # With "strict" tag policy intagged commits are discarded
                (False, "NO TAG: <carry>: something", "strict", False),
                (False, "NO TAG: something", "strict", False),
                (False, "fooo fooo fooo", "strict", False),

                # random strategy behaves like "strict"
                # TODO add strategy check, throw error if random stuff was passed
                (False, "NO TAG: <carry>: something", "asdkjqwe", False),
                (False, "NO TAG: something", "123123", False),
                (False, "fooo fooo fooo", "fufufu", False),
        )
    )
    @patch('rebasebot.bot._is_pr_merged')
    def test_commit_messages_tags(self, mocked_is_pr_merged, pr_is_merged, commit_message, tag_strategy, expected):
        mocked_is_pr_merged.return_value = pr_is_merged
        assert _add_to_rebase(commit_message, None, tag_strategy) is expected
