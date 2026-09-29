from pathlib import Path
from unittest.mock import MagicMock

import git
import pytest

from rebasebot import bot, merge_sync
from rebasebot.lifecycle_hooks import LifecycleHook, LifecycleHookScriptException
from tests.conftest import CommitBuilder


@pytest.fixture
def merge_run(init_test_repositories, fake_github_provider, tmp_path, monkeypatch):
    source, rebase, dest = init_test_repositories
    git.Repo(rebase.url).git.config("receive.denyCurrentBranch", "updateInstead")
    create_pr = MagicMock(return_value="https://example.com/pr/1")
    monkeypatch.setattr(bot, "_create_pr", create_pr)
    monkeypatch.setattr(bot, "_is_pr_available", lambda *args: (None, False))
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
        "ignore_manual_label": True,
        "sync_mode": "merge",
        "dry_run": True,
    }
    return options, create_pr


def work_repo(options):
    return git.Repo(options["working_dir"])


def test_merge_preserves_carries_and_downstream_merges(merge_run):
    options, _ = merge_run
    source, dest = options["source"], options["dest"]
    downstream_repo = git.Repo(dest.url)
    downstream_repo.git.checkout("-b", "feature")
    Path(dest.url, "feature.txt").write_text("feature")
    downstream_repo.git.add("feature.txt")
    downstream_repo.git.commit("-m", "feature commit")
    feature = downstream_repo.head.commit
    downstream_repo.git.checkout(dest.branch)
    downstream_repo.git.merge("--no-ff", "-m", "merge feature", "feature")
    downstream = downstream_repo.head.commit
    upstream = CommitBuilder(source).add_file("upstream.txt", "upstream").commit("upstream change")
    assert bot.run(**options)
    repo = work_repo(options)
    assert repo.head.commit.parents == (downstream, upstream)
    assert repo.is_ancestor(feature, "HEAD")
    assert repo.git.show("HEAD:another_file.go")
    assert repo.git.show("HEAD:upstream.txt") == "upstream"
    assert upstream.hexsha in repo.head.commit.message
    assert merge_sync.MODE_MARKER in repo.head.commit.message
    assert repo.active_branch.name == "rebase"
    assert not repo.is_dirty(untracked_files=True)
    assert [call.kwargs["hook"] for call in options["hooks"].execute_scripts_for_hook.call_args_list] == [
        LifecycleHook.PRE_REBASE,
        LifecycleHook.POST_REBASE,
    ]


def test_unchanged_tree_new_ancestry_pushes_and_creates_pr(merge_run):
    options, create_pr = merge_run
    upstream = CommitBuilder(options["source"]).commit("empty upstream commit")
    options["dry_run"] = False
    assert bot.run(**options)
    repo = work_repo(options)
    downstream = repo.commit(f"dest/{options['dest'].branch}")
    assert repo.head.commit.tree == downstream.tree
    assert repo.is_ancestor(upstream, "HEAD")
    remote_head = git.Repo(options["rebase"].url).head.commit
    assert remote_head == repo.head.commit
    create_pr.assert_called_once()
    assert bot.run(**options)
    assert git.Repo(options["rebase"].url).head.commit == remote_head
    downstream_repo = git.Repo(options["dest"].url)
    downstream_repo.git.fetch(options["rebase"].url, options["rebase"].branch)
    downstream_repo.git.merge("--ff-only", "FETCH_HEAD")
    create_pr.reset_mock()
    options["hooks"].reset_mock()
    assert bot.run(**options)
    create_pr.assert_not_called()
    options["hooks"].execute_scripts_for_hook.assert_not_called()
    newer = CommitBuilder(options["source"]).commit("another empty upstream commit")
    assert bot.run(**options)
    assert git.Repo(options["rebase"].url).head.commit.parents == (remote_head, newer)
    create_pr.assert_called_once()


@pytest.mark.parametrize("conflict", [True, False])
def test_failed_merge_restores_clean_downstream(merge_run, conflict):
    options, create_pr = merge_run
    source, dest = options["source"], options["dest"]
    if conflict:
        CommitBuilder(source).update_file("test.go", "upstream version").commit("upstream conflict")
        CommitBuilder(dest).update_file("test.go", "downstream version").commit("downstream conflict")
    else:
        upstream_repo = git.Repo(source.url)
        upstream_repo.git.checkout("--orphan", "unrelated")
        upstream_repo.git.commit("-m", "unrelated root")
        source.branch = "unrelated"
    downstream = git.Repo(dest.url).head.commit
    options["dry_run"] = False
    assert not bot.run(**options)
    repo = work_repo(options)
    assert repo.head.commit == downstream
    assert not repo.is_dirty(untracked_files=True)
    assert not Path(repo.git_dir, "MERGE_HEAD").exists()
    create_pr.assert_not_called()


@pytest.mark.parametrize("failure", ["dirty", "exception", "rewrite"])
@pytest.mark.parametrize(
    "stage",
    [
        LifecycleHook.PRE_REBASE,
        LifecycleHook.POST_REBASE,
        LifecycleHook.PRE_PUSH_REBASE_BRANCH,
        LifecycleHook.PRE_CREATE_PR,
    ],
)
def test_hook_failure_cleanup(merge_run, failure, stage):
    options, _ = merge_run
    options["dry_run"] = False
    CommitBuilder(options["source"]).commit("upstream advance")

    def hook(*, hook):
        if hook == stage:
            repo = work_repo(options)
            if failure == "rewrite":
                repo.git.reset("--hard", f"source/{options['source'].branch}")
            else:
                Path(repo.working_dir, "untracked").write_text("unfinished")
                if failure == "exception":
                    raise LifecycleHookScriptException("failed")

    options["hooks"].execute_scripts_for_hook.side_effect = hook
    assert not bot.run(**options)
    repo = work_repo(options)
    assert repo.head.commit == git.Repo(options["dest"].url).head.commit
    assert not repo.is_dirty(untracked_files=True)


def test_pre_create_cannot_add_unpublished_commit(merge_run):
    options, create_pr = merge_run
    options["dry_run"] = False
    CommitBuilder(options["source"]).commit("upstream advance")

    def hook(*, hook):
        if hook == LifecycleHook.PRE_CREATE_PR:
            work_repo(options).git.commit("--allow-empty", "-m", "too late")

    options["hooks"].execute_scripts_for_hook.side_effect = hook
    assert not bot.run(**options)
    create_pr.assert_not_called()
    assert work_repo(options).head.commit == git.Repo(options["dest"].url).head.commit


def test_merge_forces_two_parents_when_fast_forward_possible(merge_run):
    options, _ = merge_run
    downstream_repo = git.Repo(options["dest"].url)
    downstream_repo.git.reset("--hard", "HEAD^")
    upstream = CommitBuilder(options["source"]).commit("upstream advance")
    assert bot.run(**options)
    assert work_repo(options).head.commit.parents == (downstream_repo.head.commit, upstream)


def test_idle_hooks_do_not_publish(merge_run):
    options, create_pr = merge_run
    options.update(always_run_hooks=True, dry_run=False)
    assert bot.run(**options)
    create_pr.assert_not_called()
    assert not git.Repo(options["rebase"].url).heads


@pytest.mark.parametrize(
    "option,value",
    [
        ("tag_policy", "strict"),
        ("conflict_policy", "strict"),
        ("bot_emails", ["bot@example.com"]),
        ("exclude_commits", ["abc123"]),
    ],
)
def test_programmatic_merge_rejects_replay_options(merge_run, option, value):
    options, _ = merge_run
    options[option] = value
    assert not bot.run(**options)
    assert not Path(options["working_dir"]).exists()


def test_programmatic_merge_rejects_carry_hook(merge_run):
    options, _ = merge_run
    options["hooks"].hooks = {LifecycleHook.PRE_CARRY_COMMIT: [MagicMock()]}
    assert not bot.run(**options)
    assert not Path(options["working_dir"]).exists()


def test_failed_merge_can_be_retried(merge_run):
    options, _ = merge_run
    CommitBuilder(options["source"]).update_file("test.go", "upstream").commit("upstream conflict")
    CommitBuilder(options["dest"]).update_file("test.go", "downstream").commit("downstream conflict")
    assert not bot.run(**options)
    CommitBuilder(options["dest"]).update_file("test.go", "upstream").commit("manual resolution")
    assert bot.run(**options)
    assert not work_repo(options).is_dirty(untracked_files=True)


def test_hooks_only_preserve_downstream(merge_run):
    options, create_pr = merge_run
    options.update(always_run_hooks=True, dry_run=False)

    def hook(*, hook):
        if hook == LifecycleHook.POST_REBASE:
            repo = work_repo(options)
            assert Path(repo.working_dir, "another_file.go").exists()
            Path(repo.working_dir, "generated").write_text("generated")
            repo.git.add("generated")
            repo.git.commit("-m", "generated change")

    options["hooks"].execute_scripts_for_hook.side_effect = hook
    assert bot.run(**options)
    assert work_repo(options).head.commit.parents == (git.Repo(options["dest"].url).head.commit,)
    create_pr.assert_called_once()


def test_source_is_pinned_before_hooks(merge_run):
    options, _ = merge_run
    upstream = CommitBuilder(options["source"]).commit("pinned upstream")

    def hook(*, hook):
        if hook == LifecycleHook.PRE_REBASE:
            repo = work_repo(options)
            repo.git.update_ref(f"refs/remotes/source/{options['source'].branch}", upstream.parents[0].hexsha)

    options["hooks"].execute_scripts_for_hook.side_effect = hook
    assert bot.run(**options)
    assert work_repo(options).head.commit.parents[1] == upstream


def test_replay_to_merge_and_back_requires_manual_baseline(merge_run):
    options, _ = merge_run
    CommitBuilder(options["source"]).commit("upstream advance")
    assert bot.run(**{**options, "sync_mode": "replay"})
    downstream_repo = git.Repo(options["dest"].url)
    downstream_repo.git.fetch(options["working_dir"], "rebase")
    downstream_repo.git.merge("--ff-only", "FETCH_HEAD")
    replay_tip = downstream_repo.head.commit
    CommitBuilder(options["source"]).commit("upstream advances again")
    assert bot.run(**options)
    assert work_repo(options).head.commit.parents[0] == replay_tip
    downstream_repo.git.fetch(options["working_dir"], "rebase")
    downstream_repo.git.merge("--ff-only", "FETCH_HEAD")
    assert not bot.run(**{**options, "sync_mode": "replay"})


@pytest.mark.parametrize("setting,value", [("branch.rebase.mergeOptions", "-Xours"), ("pull.twohead", "ours")])
def test_inherited_merge_settings_cannot_discard_conflicts(merge_run, setting, value):
    options, create_pr = merge_run
    options["dry_run"] = False
    upstream = CommitBuilder(options["source"]).update_file("test.go", "upstream").commit("upstream conflict")
    downstream = CommitBuilder(options["dest"]).update_file("test.go", "downstream").commit("downstream conflict")

    def hook(*, hook):
        if hook == LifecycleHook.PRE_REBASE:
            work_repo(options).git.config(setting, value)

    options["hooks"].execute_scripts_for_hook.side_effect = hook
    assert not bot.run(**options)
    repo = work_repo(options)
    assert repo.head.commit == downstream
    assert not repo.is_ancestor(upstream, "HEAD")
    assert not repo.is_dirty(untracked_files=True)
    assert not Path(repo.git_dir, "MERGE_HEAD").exists()
    assert not git.Repo(options["rebase"].url).heads
    create_pr.assert_not_called()


@pytest.mark.parametrize("hook_commits", [False, True])
def test_upstream_rollback_replaces_same_tree_pending_merge(merge_run, hook_commits, monkeypatch):
    options, _ = merge_run
    options["dry_run"] = False
    selected = CommitBuilder(options["source"]).commit("selected upstream")
    withdrawn = CommitBuilder(options["source"]).commit("withdrawn upstream")

    def hook(*, hook):
        if hook_commits and hook in (LifecycleHook.PRE_REBASE, LifecycleHook.POST_REBASE):
            work_repo(options).git.commit("--allow-empty", "-m", f"hook {hook}")

    options["hooks"].execute_scripts_for_hook.side_effect = hook
    assert bot.run(**options)
    remote = git.Repo(options["rebase"].url)
    old_head = remote.head.commit
    git.Repo(options["source"].url).git.reset("--hard", selected.hexsha)
    options["working_dir"] += "-rollback"
    assert bot.run(**options)
    assert remote.head.commit.tree == old_head.tree
    assert remote.is_ancestor(selected, "HEAD")
    assert not remote.is_ancestor(withdrawn, "HEAD")
    replacement = remote.head.commit
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2030-01-01T00:00:00+0000")
    assert bot.run(**options)
    assert remote.head.commit == replacement
    assert work_repo(options).head.commit != replacement


@pytest.mark.parametrize("failure", ["pending_merge", "nested_repo", "rename", "delete"])
def test_hook_state_failure_restores_workspace_and_allows_retry(merge_run, failure, caplog):
    options, create_pr = merge_run
    options["dry_run"] = False
    CommitBuilder(options["source"]).commit("upstream advance")
    downstream = git.Repo(options["dest"].url).head.commit
    outside = Path(options["working_dir"]).parent / "outside-repo"
    git.Repo.init(outside)
    (outside / "file").write_text("preserve")

    def hook(*, hook):
        if hook != LifecycleHook.POST_REBASE:
            return
        repo = work_repo(options)
        if failure == "pending_merge":
            repo.git.checkout("-b", "hook-side", downstream.hexsha)
            repo.git.commit("--allow-empty", "-m", "hook side")
            repo.git.checkout("rebase")
            repo.git.merge("--no-commit", "--no-ff", "hook-side")
            assert not repo.is_dirty(untracked_files=True)
            assert Path(repo.git_dir, "MERGE_HEAD").exists()
        elif failure == "nested_repo":
            nested = Path(repo.working_dir, "generated-repo")
            git.Repo.init(nested)
            (nested / "file").write_text("unfinished")
            Path(repo.working_dir, "outside-link").symlink_to(outside, target_is_directory=True)
        else:
            if failure == "rename":
                repo.git.branch("-m", "hook-branch")
            else:
                repo.git.checkout("--detach")
                repo.git.branch("-D", "rebase")
            repo.git.commit("--allow-empty", "-m", "hook change")
            raise LifecycleHookScriptException("original hook failure")

    options["hooks"].execute_scripts_for_hook.side_effect = hook
    assert not bot.run(**options)
    repo = work_repo(options)
    assert repo.head.commit == downstream
    assert repo.active_branch.name == "rebase"
    assert not repo.is_dirty(untracked_files=True)
    assert not Path(repo.git_dir, "MERGE_HEAD").exists()
    assert not Path(repo.working_dir, "generated-repo").exists()
    assert (outside / ".git").is_dir()
    assert (outside / "file").read_text() == "preserve"
    if failure in ("rename", "delete"):
        assert "original hook failure" in caplog.text
    assert not git.Repo(options["rebase"].url).heads
    create_pr.assert_not_called()
    options["hooks"].execute_scripts_for_hook.side_effect = None
    assert bot.run(**options)
    assert not repo.is_dirty(untracked_files=True)
    create_pr.assert_called_once()


def test_default_replay_accepts_upstream_inherited_merge_marker(merge_run):
    options, _ = merge_run
    source = git.Repo(options["source"].url)
    source.git.checkout("-b", "upstream-side")
    source.git.commit("--allow-empty", "-m", "upstream side")
    source.git.checkout(options["source"].branch)
    source.git.merge("--no-ff", "-m", merge_sync.MODE_MARKER, "upstream-side")
    downstream = git.Repo(options["dest"].url)
    downstream.git.fetch(options["source"].url, options["source"].branch)
    downstream.git.reset("--hard", "FETCH_HEAD")
    CommitBuilder(options["dest"]).add_file("carry", "carry").commit("downstream carry")
    CommitBuilder(options["source"]).commit("new upstream")
    del options["sync_mode"]
    assert bot.run(**options)
    assert work_repo(options).git.show("HEAD:carry") == "carry"


@pytest.mark.parametrize("operation", ["cherry-pick", "rebase"])
def test_tree_clean_sequencer_failure_and_retry(tmp_go_app_repo, operation):
    _, repo = tmp_go_app_repo
    downstream = repo.head.commit.hexsha
    repo.git.checkout("-b", "rebase")
    hooks = MagicMock()

    def hook(*, hook):
        if operation == "cherry-pick":
            repo.git.checkout("-b", "side")
            repo.git.commit("--allow-empty", "-m", "empty one")
            first = repo.head.commit.hexsha
            repo.git.commit("--allow-empty", "-m", "empty two")
            second = repo.head.commit.hexsha
            repo.git.checkout("rebase")
            with pytest.raises(git.GitCommandError):
                repo.git.cherry_pick(first, second)
            assert Path(repo.git_dir, "sequencer").is_dir()
            assert Path(repo.git_dir, "CHERRY_PICK_HEAD").exists()
        else:
            repo.git.commit("--allow-empty", "-m", "edit me")
            with repo.git.custom_environment(GIT_SEQUENCE_EDITOR="sed -i s/^pick/edit/"):
                repo.git.rebase("-i", "HEAD^")
            assert Path(repo.git_dir, "rebase-merge").is_dir()
        assert not repo.is_dirty(untracked_files=True)

    hooks.execute_scripts_for_hook.side_effect = hook
    with pytest.raises(merge_sync.MergeSyncError, match="finish all Git operations"):
        merge_sync.execute_hook(repo, hooks, LifecycleHook.POST_REBASE, downstream)
    assert repo.head.commit.hexsha == downstream
    assert repo.active_branch.name == "rebase"
    assert not any(Path(repo.git_dir, name).exists() for name in merge_sync.OPERATION_STATE)
    assert not repo.is_dirty(untracked_files=True)
    hooks.execute_scripts_for_hook.side_effect = None
    merge_sync.execute_hook(repo, hooks, LifecycleHook.POST_REBASE, downstream)


def test_cleanup_error_does_not_mask_original_hook_failure(tmp_go_app_repo, monkeypatch, caplog):
    _, repo = tmp_go_app_repo
    hooks = MagicMock()
    hooks.execute_scripts_for_hook.side_effect = LifecycleHookScriptException("original hook failure")
    monkeypatch.setattr(merge_sync, "restore", MagicMock(side_effect=RuntimeError("cleanup failure")))
    with pytest.raises(LifecycleHookScriptException, match="original hook failure"):
        merge_sync.execute_hook(repo, hooks, LifecycleHook.PRE_REBASE, repo.head.commit.hexsha)
    assert "Failed to restore disposable merge workspace" in caplog.text
    assert "cleanup failure" in caplog.text
