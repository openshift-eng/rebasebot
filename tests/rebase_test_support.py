from __future__ import annotations

import os
from dataclasses import dataclass
from unittest.mock import MagicMock

from git import Repo

from rebasebot.github import GitHubBranch

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


def make_rebasebot_args(
    *,
    source,
    dest,
    rebase,
    working_dir,
    **overrides,
):
    defaults = {
        "source": source,
        "source_repo": None,
        "dest": dest,
        "rebase": rebase,
        "working_dir": working_dir,
        "git_username": "test_rebasebot",
        "git_email": "test@rebasebot.ocp",
        "tag_policy": "soft",
        "bot_emails": [],
        "exclude_commits": [],
        "update_go_modules": False,
        "conflict_policy": "auto",
        "ignore_manual_label": False,
        "dry_run": False,
        "pause_on_conflict": False,
        "continue_run": False,
        "retry_failed_step": False,
        "always_run_hooks": False,
        "title_prefix": "",
        "pre_rebase_hook": None,
        "post_rebase_hook": None,
        "pre_carry_commit_hook": None,
        "pre_push_rebase_branch_hook": None,
        "pre_create_pr_hook": None,
    }
    defaults.update(overrides)
    args = MagicMock()
    for key, value in defaults.items():
        setattr(args, key, value)
    return args


class FakeArtCommit:
    def __init__(self, sha: str):
        self.sha = sha


class FakeArtPullRequest:
    def __init__(self, sha: str, art_repo_dir: str, branch: str):
        self.title = "update image consistent with ART"
        self.user = MagicMock()
        self.user.login = "openshift-bot"
        repository = MagicMock()
        repository.name = "art-remote"
        repository.html_url = art_repo_dir
        self.head = MagicMock()
        self.head.repository = repository
        self.head.ref = branch
        self.labels = []
        self._sha = sha

    def commits(self):
        return [FakeArtCommit(self._sha)]


def setup_fake_art_pr(fake_github_provider, source, dest, rebase, art_repo_dir):
    dest_repo = MagicMock()
    dest_repo.clone_url = dest.url
    source_repo = MagicMock()
    source_repo.clone_url = source.url
    rebase_repo = MagicMock()
    rebase_repo.clone_url = rebase.url

    Repo.init(art_repo_dir)
    art_branch = GitHubBranch(url=art_repo_dir, ns="art", name="art", branch="art-branch")
    art_base_branch = GitHubBranch(url=art_repo_dir, ns="art", name="art", branch="master")
    CommitBuilder(art_base_branch).add_file("art-shared.txt", "base art version\n").commit("ART base")
    art_commit = CommitBuilder(art_branch).move_file("art-shared.txt", "art-side.txt").commit("ART conflicting commit")
    art_pr = FakeArtPullRequest(art_commit.hexsha, art_repo_dir, art_branch.branch)

    def pull_requests(*args, **kwargs):
        if kwargs.get("state") == "open" and kwargs.get("base") == dest.branch:
            return [art_pr]
        return []

    dest_repo.pull_requests.side_effect = pull_requests
    fake_github_provider.github_app.repository.side_effect = lambda ns, name: {
        dest.name: dest_repo,
        source.name: source_repo,
    }[name]
    fake_github_provider.github_cloner_app.repository.side_effect = lambda ns, name: {rebase.name: rebase_repo}[name]
    return dest_repo, art_commit


def write_hook_script(script_dir: str, name: str, content: str) -> str:
    path = os.path.join(script_dir, name)
    with open(path, "w", encoding="utf-8") as hook_file:
        hook_file.write(content)
    os.chmod(path, 0o700)
    return path
