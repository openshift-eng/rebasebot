from pathlib import Path
from unittest.mock import MagicMock

import git
import pytest
import requests

from rebasebot import bot
from rebasebot.lifecycle_hooks import LifecycleHook
from rebasebot.pr_body import build_merge_pr_body
from rebasebot.prow import ProwJobContext
from tests.conftest import CommitBuilder


@pytest.fixture
def pr_creation_patch(monkeypatch):
    with monkeypatch.context() as patch:
        yield patch


@pytest.fixture
def merge_run(init_test_repositories, fake_github_provider, tmp_path, pr_creation_patch):
    source, rebase, dest = init_test_repositories
    git.Repo(rebase.url).git.config("receive.denyCurrentBranch", "updateInstead")
    create_pr = MagicMock()
    pr_creation_patch.setattr(bot, "_create_pr", create_pr)
    dest_repo = fake_github_provider.github_app.repository.return_value
    dest_repo.pull_requests.return_value = []
    hooks = MagicMock()
    hooks.hooks = {}
    options = {
        "source": source,
        "dest": dest,
        "rebase": rebase,
        "working_dir": str(tmp_path / "work"),
        "git_username": "test",
        "git_email": "test@example.com",
        "github_app_provider": fake_github_provider,
        "slack_webhook": None,
        "tag_policy": "none",
        "bot_emails": [],
        "exclude_commits": [],
        "hooks": hooks,
        "sync_mode": "merge",
    }
    create_pr.return_value = sync_pr(options)
    return options, create_pr, dest_repo


def sync_pr(options, *, manual=False, has_conflicts=False, number=1):
    pr = MagicMock()
    rebase = options["rebase"]
    pr.as_dict.return_value = {"head": {"repo": {"full_name": f"{rebase.ns}/{rebase.name}"}}}
    pr.head.ref = rebase.branch
    pr.labels = [{"name": "rebase/manual"}] if manual else []
    pr.html_url = "https://example.com/pr/1"
    pr.number = number
    pr.body = build_merge_pr_body(
        options["source"],
        options["dest"],
        rebase,
        ProwJobContext(None, None, None),
        conflicted_pr_number=pr.number if has_conflicts else None,
    )

    def update(*, body):
        pr.body = body
        return True

    pr.update.side_effect = update
    return pr


@pytest.mark.parametrize("conflicts", [False, True])
def test_publishes_exact_upstream_and_reuses_pr_even_with_conflicts(merge_run, conflicts):
    options, create_pr, dest_repo = merge_run
    source, dest, rebase = (options[name] for name in ("source", "dest", "rebase"))
    if conflicts:
        CommitBuilder(dest).update_file("test.go", "downstream").commit("downstream conflict")
    upstream = CommitBuilder(source).update_file("test.go", "upstream").commit("upstream update")
    assert bot.run(**options)
    assert git.Repo(rebase.url).head.commit == upstream
    repo = git.Repo(options["working_dir"])
    assert not repo.heads
    assert not Path(repo.git_dir, "MERGE_HEAD").exists()
    assert not repo.is_dirty(untracked_files=True)
    options["hooks"].fetch_hook_scripts.assert_not_called()
    options["hooks"].execute_scripts_for_hook.assert_not_called()
    assert "summary" not in create_pr.call_args.kwargs
    assert create_pr.return_value.update.called is conflicts

    pr = sync_pr(options, has_conflicts=conflicts)
    dest_repo.pull_requests.return_value = [pr]
    repo.git.checkout("-b", "local-work", upstream.hexsha)
    local_file = Path(repo.working_dir, "test.go")
    local_file.write_text("local changes")
    untracked = Path(repo.working_dir, "untracked.txt")
    untracked.write_text("local notes")
    newer = CommitBuilder(source).commit("next upstream update")
    assert bot.run(**options)
    assert git.Repo(rebase.url).head.commit == newer
    assert repo.head.commit == upstream
    assert repo.active_branch.name == "local-work"
    assert local_file.read_text() == "local changes"
    assert untracked.read_text() == "local notes"
    create_pr.assert_called_once()
    pr.update.assert_not_called()


@pytest.mark.parametrize("source_ref", ["tag", "commit"])
def test_merge_accepts_tag_and_commit_sources_without_a_checkout(merge_run, source_ref):
    options, _, _ = merge_run
    source = options["source"]
    upstream = CommitBuilder(source).commit("upstream advance")
    git.Repo(source.url).git.tag("-a", "upstream-version", "-m", "upstream release", upstream.hexsha)
    source.branch = "upstream-version" if source_ref == "tag" else upstream.hexsha
    assert bot.run(**options)
    assert git.Repo(options["rebase"].url).head.commit == upstream
    assert not git.Repo(options["working_dir"]).heads


def test_existing_pr_instructions_follow_conflicts_when_downstream_changes(merge_run, monkeypatch):
    options, create_pr, dest_repo = merge_run
    upstream = CommitBuilder(options["source"]).update_file("test.go", "upstream").commit("upstream update")
    assert bot.run(**options)
    pr = sync_pr(options)
    dest_repo.pull_requests.return_value = [pr]
    execute = git.Git.execute
    pushes = []

    def record(self, command, *args, **kwargs):
        if command[1] == "push":
            pushes.append(command)
        return execute(self, command, *args, **kwargs)

    monkeypatch.setattr(git.Git, "execute", record)
    downstream = CommitBuilder(options["dest"])
    downstream.update_file("test.go", "downstream").commit("downstream conflict")
    assert bot.run(**options)
    assert "## Resolve conflicts" in pr.body
    pr.update.assert_called_once()
    assert bot.run(**options)
    pr.update.assert_called_once()

    downstream.update_file("test.go", "upstream").commit("downstream resolves conflict")
    assert bot.run(**options)
    assert "## Resolve conflicts" not in pr.body
    assert pr.update.call_count == 2
    assert git.Repo(options["rebase"].url).head.commit == upstream
    assert not pushes
    create_pr.assert_called_once()


def test_conflict_probe_error_stops_before_publication(merge_run, monkeypatch, caplog):
    options, create_pr, _ = merge_run
    CommitBuilder(options["source"]).commit("upstream update")
    execute = git.Git.execute

    def fail_probe(self, command, *args, **kwargs):
        if command[1] == "merge-tree":
            return 128, "", "merge-tree failed"
        return execute(self, command, *args, **kwargs)

    monkeypatch.setattr(git.Git, "execute", fail_probe)
    assert not bot.run(**options)
    assert "Cannot check merge conflicts" in caplog.text
    assert not git.Repo(options["rebase"].url).heads
    create_pr.assert_not_called()


def test_repeat_does_not_push(merge_run, monkeypatch, caplog):
    options, create_pr, dest_repo = merge_run
    CommitBuilder(options["source"]).commit("upstream advance")
    assert bot.run(**options)
    dest_repo.pull_requests.return_value = [sync_pr(options)]
    execute = git.Git.execute
    commands = []

    def record(self, command, *args, **kwargs):
        commands.append(command)
        return execute(self, command, *args, **kwargs)

    monkeypatch.setattr(git.Git, "execute", record)
    caplog.set_level("INFO")
    assert bot.run(**options)
    assert not any(command[1] == "push" for command in commands)
    assert "no push required" in caplog.text
    create_pr.assert_called_once()


@pytest.mark.parametrize("dry_run", [False, True])
def test_downstream_already_contains_upstream(merge_run, dry_run):
    options, create_pr, _ = merge_run
    options["dry_run"] = dry_run
    assert bot.run(**options)
    assert not git.Repo(options["rebase"].url).heads
    create_pr.assert_not_called()


def test_dry_run_with_conflict_never_publishes(merge_run):
    options, create_pr, _ = merge_run
    CommitBuilder(options["source"]).update_file("test.go", "upstream").commit("upstream conflict")
    CommitBuilder(options["dest"]).update_file("test.go", "downstream").commit("downstream conflict")
    options["dry_run"] = True
    assert bot.run(**options)
    assert not git.Repo(options["rebase"].url).heads
    create_pr.assert_not_called()


@pytest.mark.parametrize("during_fetch", [False, True])
def test_manual_label_stops_publication_before_and_after_fetch(merge_run, during_fetch):
    options, create_pr, dest_repo = merge_run
    CommitBuilder(options["source"]).commit("upstream advance")
    manual = sync_pr(options, manual=True)
    if during_fetch:
        dest_repo.pull_requests.side_effect = [[sync_pr(options)], [manual]]
    else:
        dest_repo.pull_requests.return_value = [manual]
    assert bot.run(**options)
    assert not git.Repo(options["rebase"].url).heads
    assert Path(options["working_dir"]).exists() == during_fetch
    manual.update.assert_not_called()
    create_pr.assert_not_called()


def test_other_pr_manual_label_does_not_pause_sync(merge_run):
    options, create_pr, dest_repo = merge_run
    other = sync_pr(options, manual=True)
    other.head.ref = "another-branch"
    dest_repo.pull_requests.return_value = [other]
    CommitBuilder(options["source"]).commit("upstream advance")
    assert bot.run(**options)
    create_pr.assert_called_once()


@pytest.mark.parametrize("existing_branch", [False, True])
def test_explicit_lease_preserves_concurrent_push(merge_run, monkeypatch, existing_branch):
    options, create_pr, dest_repo = merge_run
    source, rebase = options["source"], options["rebase"]
    CommitBuilder(source).commit("upstream advance")
    if existing_branch:
        assert bot.run(**options)
        dest_repo.pull_requests.return_value = [sync_pr(options)]
        CommitBuilder(source).commit("upstream advances again")
        create_pr.reset_mock()
    execute = git.Git.execute
    concurrent = []

    def push_race(self, command, *args, **kwargs):
        if command[1] == "push":
            remote = git.Repo(rebase.url)
            remote.git.fetch(source.url, source.branch)
            remote.git.checkout("-B", rebase.branch, "FETCH_HEAD")
            concurrent.append(CommitBuilder(rebase).commit("concurrent developer push"))
        return execute(self, command, *args, **kwargs)

    monkeypatch.setattr(git.Git, "execute", push_race)
    assert not bot.run(**options)
    assert git.Repo(rebase.url).head.commit == concurrent[0]
    create_pr.assert_not_called()


@pytest.mark.parametrize("dry_run", [False, True])
def test_upstream_rollback_refreshes_pending_pr(merge_run, dry_run):
    options, create_pr, dest_repo = merge_run
    source, rebase = options["source"], options["rebase"]
    original = git.Repo(source.url).head.commit
    withdrawn = CommitBuilder(source).commit("withdrawn upstream change")
    assert bot.run(**options)
    dest_repo.pull_requests.return_value = [sync_pr(options)]
    git.Repo(source.url).git.reset("--hard", original.hexsha)
    options["dry_run"] = dry_run
    assert bot.run(**options)
    assert git.Repo(rebase.url).head.commit == (withdrawn if dry_run else original)
    create_pr.assert_called_once()


def test_merge_acceptance_preserves_downstream_carries_and_stops_sync(merge_run):
    options, create_pr, dest_repo = merge_run
    upstream = CommitBuilder(options["source"]).add_file("upstream.txt", "upstream").commit("upstream update")
    assert bot.run(**options)
    downstream = git.Repo(options["dest"].url)
    downstream.git.fetch(options["rebase"].url, options["rebase"].branch)
    downstream.git.merge("--no-ff", "--no-edit", "FETCH_HEAD")
    assert downstream.is_ancestor(upstream, "HEAD")
    assert downstream.git.show("HEAD:another_file.go")
    dest_repo.pull_requests.return_value = []
    assert bot.run(**options)
    create_pr.assert_called_once()


def test_unrelated_histories_fail_before_publication(merge_run):
    options, create_pr, _ = merge_run
    upstream = git.Repo(options["source"].url)
    upstream.git.checkout("--orphan", "unrelated")
    upstream.git.commit("-m", "unrelated root")
    options["source"].branch = "unrelated"
    assert not bot.run(**options)
    assert not git.Repo(options["rebase"].url).heads
    create_pr.assert_not_called()


@pytest.mark.parametrize("stage", list(LifecycleHook))
def test_direct_merge_run_rejects_hooks(merge_run, stage):
    options, create_pr, _ = merge_run
    options["hooks"].hooks = {stage: [MagicMock()]}
    assert not bot.run(**options)
    assert not Path(options["working_dir"]).exists()
    create_pr.assert_not_called()


def test_rejects_syncing_directly_into_downstream_branch(merge_run):
    options, _, _ = merge_run
    options["rebase"] = options["dest"]
    assert not bot.run(**options)
    assert not Path(options["working_dir"]).exists()


@pytest.mark.parametrize("conflicts", [False, True])
def test_generated_merge_pr_has_static_title_and_conditional_conflict_instructions(
    merge_run, pr_creation_patch, conflicts
):
    options, _, _ = merge_run
    pr_creation_patch.undo()
    gh_app = options["github_app_provider"].github_app
    gh_app._post.return_value.json.return_value = {"html_url": "https://example.com/pr/42", "number": 42}
    pr = sync_pr(options, number=42)
    gh_app._instance_or_null.return_value = pr
    CommitBuilder(options["source"]).update_file("test.go", "upstream").commit("upstream advance")
    if conflicts:
        CommitBuilder(options["dest"]).update_file("test.go", "downstream").commit("downstream conflict")
    options["title_prefix"] = "MCPLO"
    assert bot.run(**options)
    request = gh_app._post.call_args
    assert request.args == ("https://api.github.com/repos/dest/dest/pulls",)
    data = request.kwargs["data"]
    assert data["title"] == "MCPLO: Sync upstream main into main"
    assert data["head"] == options["rebase"].branch
    assert data["head_repo"] == "rebase/rebase"
    assert data["base"] == "main"
    assert data["body"] == (
        "This PR syncs upstream changes from [source/source:main](https://github.com/source/source/tree/main) "
        "into [dest/dest:main](https://github.com/dest/dest/tree/main)."
    )
    assert "## Resolve conflicts" not in data["body"]
    if not conflicts:
        assert "rebase/manual" not in data["body"]
        assert "```sh" not in data["body"]
        pr.update.assert_not_called()
        return
    pr.update.assert_called_once()
    body = pr.update.call_args.kwargs["body"]
    assert "## Resolve conflicts" in body
    assert "Conflict detected. Resolve manually." in body
    assert "Replace `123`" not in body
    assert "gh pr edit 42 --repo dest/dest --add-label rebase/manual &&" in body
    assert 'workdir=$(mktemp -d "${TMPDIR:-/tmp}/rebasebot-pr-42.XXXXXX") &&' in body
    assert "Go to your local clone of `dest/dest`, then run:" in body
    assert 'test -z "$(git status --porcelain --untracked-files=all)" &&' in body
    assert body.count("gh pr checkout 42 --repo dest/dest --detach &&") == 2
    assert "<summary>Use a temporary worktree instead</summary>" in body
    assert "git fetch --no-tags -- git@github.com:rebase/rebase.git" not in body
    assert 'git worktree add --detach "$workdir" HEAD &&' in body
    assert "git fetch --no-tags -- git@github.com:dest/dest.git refs/heads/main &&" in body
    assert "git merge --no-ff --no-edit --no-squash --no-commit FETCH_HEAD" in body
    assert "git diff --cached --check &&\ngit commit --no-edit" in body
    assert (
        f"git push --no-follow-tags -- git@github.com:rebase/rebase.git HEAD:refs/heads/{options['rebase'].branch}"
        in body
    )
    assert "Keep `rebase/manual` until this PR is merged" in body


def test_failed_initial_conflict_instructions_update_recovers_without_creating_another_pr(merge_run, pr_creation_patch):
    options, _, dest_repo = merge_run
    pr_creation_patch.undo()
    gh_app = options["github_app_provider"].github_app
    gh_app._post.return_value.json.return_value = {"html_url": "https://example.com/pr/42", "number": 42}
    created = sync_pr(options, number=42)
    created.update.side_effect = requests.HTTPError("body update failed")
    gh_app._instance_or_null.return_value = created
    CommitBuilder(options["source"]).update_file("test.go", "upstream").commit("upstream advance")
    CommitBuilder(options["dest"]).update_file("test.go", "downstream").commit("downstream conflict")
    assert not bot.run(**options)

    pr = sync_pr(options, number=42)
    dest_repo.pull_requests.return_value = [pr]
    assert bot.run(**options)
    assert "gh pr edit 42 --repo dest/dest --add-label rebase/manual" in pr.body
    pr.update.assert_called_once()
    gh_app._post.assert_called_once()
    created.update.assert_called_once()
