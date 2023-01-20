import os
import shutil
from typing import Tuple, Generator, TypeVar
from tempfile import TemporaryDirectory

import pytest
from unittest import mock

from git import Repo, GitCommandError

from rebasebot.github import GitHubBranch, GithubAppProvider


T = TypeVar("T")

YieldFixture = Generator[T, None, None]

_GO_CODE = """
package main
import (
    "k8s.io/klog/v2"
)

func main() {
    klog.Errorln("This is a test")
    return
}
"""

_ANOTHER_GO_CODE = """
package main
func foo() {}
"""

_GO_CODE_FILENAME = "test.go"


@pytest.fixture
def tmp_go_app_repo() -> YieldFixture[Tuple[str, Repo]]:
    with TemporaryDirectory(prefix="rebasebot_tests_") as tmpdir:
        with open(os.path.join(tmpdir, _GO_CODE_FILENAME), "x", encoding="utf8") as file:
            file.write(_GO_CODE)
        repo = Repo.init(tmpdir)
        with repo.config_writer() as config:
            config.set_value("user", "email", "test@example.com")
            config.set_value("user", "name", "test")
        repo.git.add(all=True)
        repo.git.commit("-m", "Initial commit")
        yield tmpdir, repo


@pytest.fixture
def tmpdir() -> YieldFixture[str]:
    with TemporaryDirectory(prefix="rebasebot_tests_") as tmpdir:
        yield tmpdir


def commit_file_with_content(content: str, filename: str, commit_msg: str, gh_branch: GitHubBranch) -> None:
    repo_path = gh_branch.url  # in this tests we are using temp dir as git remote
    if not os.path.exists(repo_path):
        raise NotADirectoryError("temp repo does not exists")
    with open(os.path.join(repo_path, filename), "x", encoding="utf8") as file:
        file.write(content)
    repo = Repo.init(repo_path)
    with repo.config_writer() as config:
        config.set_value("user", "email", f"{gh_branch.name}_author@{gh_branch.ns}.org")
        config.set_value("user", "name", f"{gh_branch.name}_author")
    try:
        repo.git.checkout(gh_branch.branch)
    except GitCommandError:
        repo.git.checkout("-b", gh_branch.branch)
    repo.git.add(filename)
    repo.git.commit("-m", commit_msg)


@pytest.fixture
def init_test_repositories() -> YieldFixture[Tuple[GitHubBranch, GitHubBranch, GitHubBranch]]:
    """
    Creates three repositories in own temp directories

    source:
     Represents upstream git repository. Contains one commit in 'main'
    """

    source = TemporaryDirectory(prefix="rebasebot_tests_source_repo_")
    source_gh_branch = GitHubBranch(url=source.name, ns="source", name="source", branch="main")
    commit_file_with_content(_GO_CODE, _GO_CODE_FILENAME, "Upstream commit", source_gh_branch)

    rebase = TemporaryDirectory(prefix="rebasebot_tests_rebase_repo_")
    rebase_repo = Repo.init(rebase.name)
    rebase_gh_branch = GitHubBranch(url=rebase.name, ns="rebase", name="rebase", branch=rebase_repo.head.ref.name)

    dest = TemporaryDirectory(prefix="rebasebot_tests_dest_repo_")
    shutil.copytree(source.name, dest.name, dirs_exist_ok=True)
    dest_gh_branch = GitHubBranch(url=dest.name, ns="dest", name="dest", branch="main")
    commit_file_with_content(
        _ANOTHER_GO_CODE, "another_file.go", "UPSTREAM: <carry>: our cool addition", dest_gh_branch
    )

    yield source_gh_branch, rebase_gh_branch, dest_gh_branch

    source.cleanup()
    rebase.cleanup()
    dest.cleanup()


@pytest.fixture
def fake_github_provider() -> mock.MagicMock:
    provider = mock.MagicMock(spec=GithubAppProvider)
    return provider
