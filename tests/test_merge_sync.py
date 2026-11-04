import shlex
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock

import git
import pytest
import requests

from rebasebot import bot
from rebasebot.lifecycle_hooks import LifecycleHook, LifecycleHooks
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
def test_publishes_merged_tree_and_reuses_pr_even_with_conflicts(merge_run, conflicts):
    options, create_pr, dest_repo = merge_run
    source, dest, rebase = (options[name] for name in ("source", "dest", "rebase"))
    if conflicts:
        CommitBuilder(dest).update_file("test.go", "downstream").commit("downstream conflict")
    upstream = CommitBuilder(source).update_file("test.go", "upstream").commit("upstream update")
    assert bot.run(**options)
    published = git.Repo(rebase.url).head.commit
    downstream = git.Repo(dest.url).head.commit
    if conflicts:
        assert published == upstream
    else:
        assert published.parents == (downstream, upstream)
        assert published.tree / "another_file.go"
        assert (published.tree / "test.go").data_stream.read() == b"upstream"
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
    published = git.Repo(rebase.url).head.commit
    if conflicts:
        assert published == newer
    else:
        assert published.parents == (downstream, newer)
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
    assert git.Repo(options["rebase"].url).head.commit.parents == (git.Repo(options["dest"].url).head.commit, upstream)
    assert not git.Repo(options["working_dir"]).heads


def test_existing_pr_follows_conflicts_when_downstream_changes(merge_run):
    options, create_pr, dest_repo = merge_run
    upstream = CommitBuilder(options["source"]).update_file("test.go", "upstream").commit("upstream update")
    assert bot.run(**options)
    pr = sync_pr(options)
    dest_repo.pull_requests.return_value = [pr]
    downstream = CommitBuilder(options["dest"])
    downstream.update_file("test.go", "downstream").commit("downstream conflict")
    assert bot.run(**options)
    assert git.Repo(options["rebase"].url).head.commit == upstream
    assert "## Resolve conflicts" in pr.body
    pr.update.assert_called_once()
    assert bot.run(**options)
    pr.update.assert_called_once()

    resolved = downstream.update_file("test.go", "upstream").commit("downstream resolves conflict")
    assert bot.run(**options)
    assert "## Resolve conflicts" not in pr.body
    assert pr.update.call_count == 2
    published = git.Repo(options["rebase"].url).head.commit
    assert published.parents == (resolved, upstream)
    assert published.tree / "another_file.go"
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


@pytest.mark.parametrize("conflicts", [False, True])
def test_repeat_does_not_push(merge_run, monkeypatch, caplog, conflicts):
    options, create_pr, dest_repo = merge_run
    CommitBuilder(options["source"]).update_file("test.go", "upstream").commit("upstream advance")
    if conflicts:
        CommitBuilder(options["dest"]).update_file("test.go", "downstream").commit("downstream conflict")
    assert bot.run(**options)
    dest_repo.pull_requests.return_value = [sync_pr(options, has_conflicts=conflicts)]
    execute = git.Git.execute
    commands = []

    def record(self, command, *args, **kwargs):
        commands.append(command)
        return execute(self, command, *args, **kwargs)

    monkeypatch.setattr(git.Git, "execute", record)
    caplog.set_level("INFO")
    assert bot.run(**options)
    assert not any(command[1] == "push" for command in commands)
    assert not any(command[1] == "commit-tree" for command in commands)
    assert "no push required" in caplog.text
    create_pr.assert_called_once()


def test_replaces_upstream_only_branch_with_merge_commit(merge_run):
    options, create_pr, dest_repo = merge_run
    upstream = CommitBuilder(options["source"]).add_file("upstream.txt", "upstream").commit("upstream update")
    remote = git.Repo(options["rebase"].url)
    remote.git.fetch(options["source"].url, options["source"].branch)
    remote.git.checkout("-B", options["rebase"].branch, "FETCH_HEAD")
    dest_repo.pull_requests.return_value = [sync_pr(options)]

    assert bot.run(**options)

    published = remote.head.commit
    assert published.parents == (git.Repo(options["dest"].url).head.commit, upstream)
    assert published.tree / "another_file.go"
    assert published.tree / "upstream.txt"
    create_pr.assert_not_called()


def test_fast_forward_still_creates_merge_commit(merge_run):
    options, _, _ = merge_run
    downstream = git.Repo(options["dest"].url)
    downstream.git.reset("--hard", git.Repo(options["source"].url).head.commit.hexsha)
    original = downstream.head.commit
    upstream = CommitBuilder(options["source"]).commit("upstream advance")

    assert bot.run(**options)

    published = git.Repo(options["rebase"].url).head.commit
    assert published.parents == (original, upstream)
    assert published.tree == upstream.tree


@pytest.mark.parametrize("dry_run", [False, True])
def test_downstream_already_contains_upstream(merge_run, dry_run):
    options, create_pr, _ = merge_run
    options["dry_run"] = dry_run
    assert bot.run(**options)
    assert not git.Repo(options["rebase"].url).heads
    create_pr.assert_not_called()


def test_dry_run_with_conflict_never_publishes(merge_run, configure_preparation_hooks, caplog):
    options, create_pr, dest_repo = merge_run
    configure_preparation_hooks("exit 1\n")
    pr = sync_pr(options)
    dest_repo.pull_requests.return_value = [pr]
    caplog.set_level("INFO")
    CommitBuilder(options["source"]).update_file("test.go", "upstream").commit("upstream conflict")
    CommitBuilder(options["dest"]).update_file("test.go", "downstream").commit("downstream conflict")
    options["dry_run"] = True
    assert bot.run(**options)
    assert not git.Repo(options["rebase"].url).heads
    create_pr.assert_not_called()
    pr.update.assert_not_called()
    assert "preparation complete" not in caplog.text
    assert "No push or PR update" in caplog.text


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
    published = git.Repo(rebase.url).head.commit
    if dry_run:
        assert published.parents[1] == withdrawn
    else:
        assert published == git.Repo(options["dest"].url).head.commit
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


@pytest.mark.parametrize("stage", [hook for hook in LifecycleHook if hook != LifecycleHook.PRE_PUSH_REBASE_BRANCH])
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


@pytest.fixture
def configure_preparation_hooks(merge_run, tmp_path, monkeypatch):
    options, _, _ = merge_run
    script_dir = tmp_path / "scripts"
    script_dir.mkdir()
    args = Namespace(
        **options,
        pre_rebase_hook=None,
        pre_carry_commit_hook=None,
        post_rebase_hook=None,
        pre_create_pr_hook=None,
        pre_push_rebase_branch_hook=[],
        update_go_modules=False,
    )

    def configure(*scripts):
        paths = []
        for i, script in enumerate(scripts):
            path = tmp_path / f"hook {i}.sh"
            path.write_text("#!/bin/sh\nset -eu\n" + script)
            path.chmod(0o700)
            paths.append(str(path))
        args.pre_push_rebase_branch_hook = paths
        for name, value in (
            ("SOURCE", args.source.branch),
            ("DEST", args.dest.branch),
            ("REBASE", args.rebase.branch),
            ("WORKING_DIR", args.working_dir),
            ("GIT_USERNAME", args.git_username),
            ("GIT_EMAIL", args.git_email),
        ):
            monkeypatch.setenv(f"REBASEBOT_{name}", value)
        options["hooks"] = LifecycleHooks(str(script_dir), args)

    return configure


def test_merge_hooks_generate_commits_in_order_with_refs_and_environment(merge_run, configure_preparation_hooks):
    options, create_pr, _ = merge_run
    upstream = CommitBuilder(options["source"]).add_file("upstream.txt", "upstream").commit("upstream advance")
    downstream = git.Repo(options["dest"].url).head.commit
    configure_preparation_hooks(
        'test "$PWD" = "$REBASEBOT_WORKING_DIR"\n'
        'test "$REBASEBOT_SOURCE" = main\n'
        'test "$REBASEBOT_DEST" = main\n'
        f'test "$REBASEBOT_REBASE" = "{options["rebase"].branch}"\n'
        'test "$REBASEBOT_GIT_USERNAME" = test\n'
        'test "$REBASEBOT_GIT_EMAIL" = test@example.com\n'
        f'test "$(git rev-parse source/main)" = {upstream.hexsha}\n'
        f'test "$(git rev-parse dest/main)" = {downstream.hexsha}\n'
        "test -f another_file.go\ntest -f upstream.txt\n"
        'printf "first\\n" > generated.txt\n',
        'printf "second\\n" >> generated.txt\ngit add generated.txt\ngit commit -m generated\n',
    )

    assert bot.run(**options)

    published = git.Repo(options["rebase"].url).head.commit
    assert published.message.strip() == "generated"
    assert published.parents[0].parents == (downstream, upstream)
    assert (published.tree / "generated.txt").data_stream.read() == b"first\nsecond\n"
    assert "## Preparation hooks" in create_pr.return_value.body
    assert create_pr.return_value.body.index("hook 0.sh") < create_pr.return_value.body.index("hook 1.sh")
    assert "rebase/manual" in create_pr.return_value.body


@pytest.mark.parametrize("output", ["untracked", "tracked", "staged", "failed", "reset", "amend"])
def test_invalid_preparation_prevents_publication(merge_run, configure_preparation_hooks, tmp_path, output):
    options, create_pr, _ = merge_run
    CommitBuilder(options["source"]).commit("upstream advance")
    marker = tmp_path / "later-ran"
    scripts = {
        "untracked": "echo generated > extra.txt\n",
        "tracked": "echo modified > test.go\n",
        "staged": "echo staged > test.go\ngit add test.go\n",
        "failed": "exit 9\n",
        "reset": "git reset --hard source/main\n",
        "amend": "git commit --amend -m replaced\n",
    }
    later = f"touch '{marker}'\n" if output == "failed" else "true\n"
    configure_preparation_hooks(scripts[output], later)
    assert not bot.run(**options)
    assert not git.Repo(options["rebase"].url).heads
    assert not marker.exists()
    create_pr.assert_not_called()


@pytest.mark.parametrize("skip", ["manual", "manual-during-fetch", "synchronized", "synchronized-pr", "conflict"])
def test_preparation_skipped_for_paused_synchronized_or_conflicted_sync(
    merge_run, configure_preparation_hooks, skip, caplog
):
    options, create_pr, dest_repo = merge_run
    configure_preparation_hooks("exit 1\n")
    if skip not in ("synchronized", "synchronized-pr"):
        CommitBuilder(options["source"]).update_file("test.go", "upstream").commit("upstream advance")
    if skip == "manual":
        dest_repo.pull_requests.return_value = [sync_pr(options, manual=True)]
    elif skip == "manual-during-fetch":
        dest_repo.pull_requests.side_effect = [[sync_pr(options)], [sync_pr(options, manual=True)]]
    elif skip == "synchronized-pr":
        pr = sync_pr(options)
        dest_repo.pull_requests.return_value = [pr]
        options["dry_run"] = True
        caplog.set_level("INFO")
    elif skip == "conflict":
        CommitBuilder(options["dest"]).update_file("test.go", "downstream").commit("downstream conflict")
    assert bot.run(**options)
    if skip == "conflict":
        assert git.Repo(options["rebase"].url).head.commit == git.Repo(options["source"].url).head.commit
        body = create_pr.return_value.body
        assert "## Resolve conflicts" in body
        assert "from rebasebot.lifecycle_hooks" not in body
    elif skip == "synchronized-pr":
        pr.update.assert_not_called()
        create_pr.assert_not_called()
        assert "preparation complete" not in caplog.text
        assert "No push or PR update" in caplog.text
    else:
        assert not git.Repo(options["rebase"].url).heads
        create_pr.assert_not_called()


def test_failed_preparation_keeps_workspace_and_rerun_starts_fresh(merge_run, configure_preparation_hooks):
    options, create_pr, _ = merge_run
    CommitBuilder(options["source"]).commit("upstream advance")
    configure_preparation_hooks(
        "git cherry-pick dest/main source/main || true\n"
        "echo partial > generated.txt\necho edited > test.go\ngit init -q nested\n"
        "echo cache/ >> .git/info/exclude\nmkdir -p cache\ntouch cache/stale\nexit 1\n"
    )
    work = Path(options["working_dir"])

    def failed_state():
        repo = git.Repo(work)
        return (
            repo.head.commit.parents,
            repo.head.commit.tree,
            (work / "generated.txt").read_text(),
            repo.git.status("--porcelain"),
            Path(repo.git_dir, "sequencer").exists(),
        )

    assert not bot.run(**options)
    first = failed_state()
    assert first[2] == "partial\n"
    assert not bot.run(**options)
    assert failed_state() == first

    configure_preparation_hooks("git cherry-pick --allow-empty source/main source/main\n")
    assert bot.run(**options)
    published = git.Repo(options["rebase"].url).head.commit
    assert "generated.txt" not in published.tree
    assert (published.tree / "test.go").data_stream.read() != b"edited\n"
    assert not (work / "nested").exists()
    assert not (work / "cache").exists()
    create_pr.assert_called_once()


def test_preparation_recovers_from_failed_bisect_hook(merge_run, configure_preparation_hooks):
    options, _, _ = merge_run
    CommitBuilder(options["source"]).commit("upstream advance")
    configure_preparation_hooks("git bisect start\necho partial > test.go\nexit 1\n")
    assert not bot.run(**options)
    work = Path(options["working_dir"])
    assert (work / "test.go").read_text() == "partial\n"
    assert Path(git.Repo(work).git_dir, "BISECT_START").exists()

    upstream = CommitBuilder(options["source"]).update_file("test.go", "new upstream").commit("upstream advance")
    configure_preparation_hooks("git bisect start\n")

    assert bot.run(**options)
    published = git.Repo(options["rebase"].url).head.commit
    assert published.parents == (git.Repo(options["dest"].url).head.commit, upstream)
    assert (published.tree / "test.go").data_stream.read() == b"new upstream"


def test_failed_preparation_rerun_restores_submodules(merge_run, configure_preparation_hooks, tmp_path):
    options, _, _ = merge_run
    sub = git.Repo.init(tmp_path / "dependency")
    with sub.config_writer() as config:
        config.set_value("user", "email", "test@example.com")
        config.set_value("user", "name", "test")
    Path(sub.working_dir, "data.txt").write_text("original\n")
    sub.git.add("data.txt")
    sub.git.commit("-m", "dependency")
    source = git.Repo(options["source"].url)
    source.git.execute(["git", "-c", "protocol.file.allow=always", "submodule", "add", sub.working_dir, "dependency"])
    source.git.commit("-m", "upstream adds dependency")
    observations = tmp_path / "observations"
    configure_preparation_hooks(
        "git -c protocol.file.allow=always submodule update --init\n"
        f"cat dependency/data.txt >> {shlex.quote(str(observations))}\n"
        "echo dirty > dependency/data.txt\nexit 1\n"
    )
    assert not bot.run(**options)
    assert not bot.run(**options)
    assert observations.read_text() == "original\noriginal\n"


def test_prepared_rerun_keeps_published_sha_but_changed_content_or_inputs_update(
    merge_run, configure_preparation_hooks, monkeypatch
):
    options, create_pr, dest_repo = merge_run
    source = options["source"]
    upstream = CommitBuilder(source).commit("upstream advance")
    configure_preparation_hooks("echo generated > generated.txt\ngit add generated.txt\ngit commit -m generated\n")
    assert bot.run(**options)
    remote = git.Repo(options["rebase"].url)
    original = remote.head.commit
    dest_repo.pull_requests.return_value = [create_pr.return_value]
    commands = []
    execute = git.Git.execute

    def record(self, command, *args, **kwargs):
        commands.append(command)
        return execute(self, command, *args, **kwargs)

    monkeypatch.setattr(git.Git, "execute", record)
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2030-01-01T00:00:00Z")
    assert bot.run(**options)
    assert remote.head.commit == original
    assert not any(command[1] == "push" for command in commands)
    create_pr.return_value.update.reset_mock()
    assert bot.run(**options)
    create_pr.return_value.update.assert_not_called()

    configure_preparation_hooks("echo changed > generated.txt\ngit add generated.txt\ngit commit -m generated\n")
    assert bot.run(**options)
    changed_output = remote.head.commit
    assert changed_output != original
    assert changed_output.parents[0].parents[1] == upstream
    assert (changed_output.tree / "generated.txt").data_stream.read() == b"changed\n"
    newer = CommitBuilder(source).commit("empty upstream advance")
    assert bot.run(**options)
    assert remote.head.commit != changed_output
    assert remote.head.commit.tree == changed_output.tree
    assert remote.head.commit.parents[0].parents[1] == newer
    CommitBuilder(options["dest"]).commit("empty downstream advance")
    assert bot.run(**options)
    assert remote.head.commit.parents[0].parents[0] == git.Repo(options["dest"].url).head.commit


def test_prepared_publication_keeps_explicit_lease(merge_run, configure_preparation_hooks, monkeypatch):
    options, create_pr, dest_repo = merge_run
    configure_preparation_hooks("echo generated > generated.txt\ngit add generated.txt\ngit commit -m generated\n")
    CommitBuilder(options["source"]).commit("upstream advance")
    assert bot.run(**options)
    dest_repo.pull_requests.return_value = [create_pr.return_value]
    CommitBuilder(options["source"]).commit("second upstream advance")
    create_pr.reset_mock()
    execute = git.Git.execute
    concurrent = []

    def race(self, command, *args, **kwargs):
        if command[1] == "push":
            remote = git.Repo(options["rebase"].url)
            remote.git.fetch(options["source"].url, options["source"].branch)
            remote.git.checkout("-B", options["rebase"].branch, "FETCH_HEAD")
            concurrent.append(CommitBuilder(options["rebase"]).commit("human push"))
        return execute(self, command, *args, **kwargs)

    monkeypatch.setattr(git.Git, "execute", race)
    assert not bot.run(**options)
    assert git.Repo(options["rebase"].url).head.commit == concurrent[0]
    create_pr.assert_not_called()


def test_hook_dry_run_prepares_locally_without_remote_or_pr_writes(merge_run, configure_preparation_hooks, tmp_path):
    options, create_pr, dest_repo = merge_run
    CommitBuilder(options["source"]).commit("upstream advance")
    marker = tmp_path / "hook-ran"
    configure_preparation_hooks(
        f'touch "{marker}"\necho generated > generated.txt\ngit add generated.txt\ngit commit -m generated\n'
    )
    options["dry_run"] = True
    pr = sync_pr(options)
    dest_repo.pull_requests.return_value = [pr]
    assert bot.run(**options)
    assert marker.exists()
    assert not git.Repo(options["rebase"].url).heads
    pr.update.assert_not_called()
    create_pr.assert_not_called()


def test_preparation_fetches_all_git_scripts_before_execution(merge_run, configure_preparation_hooks):
    options, create_pr, _ = merge_run
    CommitBuilder(options["source"]).commit("upstream advance")
    first = (
        "#!/bin/sh\nset -eu\n"
        "printf '%s\\n' '#!/bin/sh' 'echo NEW > result.txt' 'git add result.txt' "
        "'git commit -m replacement' > second.sh\n"
        "git add second.sh\ngit commit -m replace-second\n"
    )
    second = "#!/bin/sh\nset -eu\necho ORIGINAL > result.txt\ngit add result.txt\ngit commit -m original\n"
    CommitBuilder(options["dest"]).add_file("first.sh", first).add_file("second.sh", second).commit(
        "preparation scripts"
    )
    configure_preparation_hooks("true\n", "true\n")
    scripts = options["hooks"].hooks[LifecycleHook.PRE_PUSH_REBASE_BRANCH]
    scripts[0].script_location = "git:dest/main:first.sh"
    scripts[1].script_location = "git:HEAD:second.sh"
    assert bot.run(**options)
    published = git.Repo(options["rebase"].url).head.commit
    assert (published.tree / "result.txt").data_stream.read() == b"ORIGINAL\n"


def test_manual_label_added_during_preparation_stops_publication(merge_run, configure_preparation_hooks, tmp_path):
    options, create_pr, dest_repo = merge_run
    CommitBuilder(options["source"]).commit("upstream advance")
    marker = tmp_path / "preparation-completed"
    configure_preparation_hooks(f"touch {shlex.quote(str(marker))}\n")
    manual = sync_pr(options, manual=True)
    dest_repo.pull_requests.side_effect = lambda **kwargs: [manual] if marker.exists() else []

    assert bot.run(**options)
    assert not git.Repo(options["rebase"].url).heads
    manual.update.assert_not_called()
    create_pr.assert_not_called()


def test_equivalent_preparation_can_keep_candidate_on_a_second_parent(
    merge_run, configure_preparation_hooks, monkeypatch
):
    options, create_pr, dest_repo = merge_run
    CommitBuilder(options["source"]).commit("upstream advance")
    configure_preparation_hooks(
        "candidate=$(git rev-parse HEAD)\n"
        "git checkout --detach source/main\n"
        'git merge --no-ff "$candidate" -m "merge prepared candidate"\n'
    )
    assert bot.run(**options)
    remote = git.Repo(options["rebase"].url)
    published = remote.head.commit
    dest_repo.pull_requests.return_value = [create_pr.return_value]
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2030-01-01T00:00:00Z")
    assert bot.run(**options)
    assert remote.head.commit == published
