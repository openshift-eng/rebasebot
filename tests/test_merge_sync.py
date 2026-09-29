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
