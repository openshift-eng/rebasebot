from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import git
import pytest

from rebasebot import bot
from rebasebot.lifecycle_hooks import LifecycleHooks
from tests.conftest import CommitBuilder


@pytest.fixture
def rebase_run(init_test_repositories, fake_github_provider, tmp_path):
    source, publication, destination = init_test_repositories
    destination_repo = fake_github_provider.github_destination_app.repository.return_value
    destination_repo.pull_requests.return_value = []
    return {
        "source": source,
        "destination": destination,
        "publication": publication,
        "working_dir": str(tmp_path / "work"),
        "git_username": "test",
        "git_email": "test@example.com",
        "github_app_provider": fake_github_provider,
        "slack_webhook": None,
        "tag_policy": "soft",
        "bot_emails": [],
        "exclude_commits": [],
        "dry_run": True,
        "hooks": MagicMock(hooks={}),
    }


@pytest.mark.parametrize("source_ref", ["branch", "tag", "commit"])
@pytest.mark.parametrize("legacy_alias", [False, True])
def test_rebase_source_refs_in_reused_workdir(rebase_run, source_ref, legacy_alias):
    source = rebase_run["source"]
    upstream = CommitBuilder(source).add_file("release.txt", "release\n").commit("upstream release")
    git.Repo(source.url).git.tag("-a", "v1.14.3", "-m", "release", upstream.hexsha)
    source.ref = {"branch": "main", "tag": "v1.14.3", "commit": upstream.hexsha}[source_ref]
    if legacy_alias:
        work = bot._init_working_dir(
            **{
                k: rebase_run[k]
                for k in ("source", "destination", "publication", "github_app_provider", "git_username", "git_email")
            },
            workdir=rebase_run["working_dir"],
        )
        work.create_head(f"source/{source.ref}", work.commit(f"dest/{rebase_run['destination'].ref}"))
    for _ in range(2):
        assert bot.run(**rebase_run)
        work = git.Repo(rebase_run["working_dir"])
        assert Path(work.working_dir, "release.txt").read_text() == "release\n"
        assert Path(work.working_dir, "another_file.go").exists()
        assert work.is_ancestor(upstream, work.head.commit)


@pytest.fixture
def reverted_release(rebase_run, tmp_path):
    source, destination = rebase_run["source"], rebase_run["destination"]
    upstream = CommitBuilder(source).add_file("release.txt", "restored upstream\n").commit("upstream release")
    assert bot.run(**rebase_run)
    work = git.Repo(rebase_run["working_dir"])
    downstream = git.Repo(destination.url)
    downstream.git.fetch(work.working_dir, "rebase")
    downstream.git.merge("--no-ff", "FETCH_HEAD", "-m", "Merge release PR 308")
    release_merge = downstream.head.commit
    downstream.git.revert("-m", "1", release_merge.hexsha, no_commit=True)
    downstream.git.commit("-m", "UPSTREAM: <drop>: revert release PR 308")
    CommitBuilder(destination).add_file("later-carry.txt", "later carry\n").commit("UPSTREAM: <carry>: later carry")
    git.Repo(source.url).git.tag("-a", "v1.14.3", "-m", "release", upstream.hexsha)
    source.ref = "v1.14.3"
    assert downstream.is_ancestor(upstream, downstream.head.commit)
    assert not Path(destination.url, "release.txt").exists()
    rebase_run["working_dir"] = str(tmp_path / "recreate")
    return rebase_run


def test_force_reapplies_reverted_release_and_runs_generation(reverted_release, tmp_path):
    options = reverted_release
    assert bot.run(**options)
    work = git.Repo(options["working_dir"])
    assert not bot._needs_rebase(work, options["source"], options["destination"])
    assert work.head.commit == git.Repo(options["source"].url).commit("v1.14.3")

    marker = tmp_path / "hook-result"
    hook = tmp_path / "generate.sh"
    hook.write_text(
        "#!/bin/sh\n"
        f'if test -f release.txt; then cat release.txt > "{marker}"; '
        f'else echo missing > "{marker}"; fi\n'
        "if test -f release.txt; then cp release.txt generated.txt; git add generated.txt; "
        'git commit -m "UPSTREAM: <drop>: regenerate"; fi\n'
    )
    hook.chmod(0o755)
    args = SimpleNamespace(**options)
    args.pre_rebase_hook = None
    args.pre_carry_commit_hook = None
    args.post_rebase_hook = [str(hook)]
    args.pre_publication_hook = None
    args.pre_create_pr_hook = None
    args.update_go_modules = False
    options["hooks"] = LifecycleHooks(tmp_script_dir=str(tmp_path / "scripts"), args=args)
    assert bot.run(**options, always_run_hooks=True)
    assert marker.read_text() == "missing\n"
    assert not Path(work.working_dir, "release.txt").exists()

    with patch.object(bot, "_push_publication_branch") as push, patch.object(bot, "_create_pr") as create_pr:
        assert bot.run(**options, force_rebase=True)
    push.assert_not_called()
    create_pr.assert_not_called()
    assert marker.read_text() == "restored upstream\n"
    assert Path(work.working_dir, "release.txt").read_text() == "restored upstream\n"
    assert Path(work.working_dir, "generated.txt").read_text() == "restored upstream\n"
    assert Path(work.working_dir, "another_file.go").exists()
    assert Path(work.working_dir, "later-carry.txt").read_text() == "later carry\n"
    reapplied = []
    for commit in work.iter_commits("HEAD", first_parent=True):
        if len(commit.parents) > 1:
            break
        reapplied.append(commit.summary)
    assert reapplied == [
        "UPSTREAM: <drop>: regenerate",
        "UPSTREAM: <carry>: later carry",
        "UPSTREAM: <carry>: our cool addition",
    ]
    assert not git.Repo(options["publication"].url).heads


def test_force_uses_normal_push_and_pr_path(reverted_release):
    options = reverted_release
    options["dry_run"] = False
    with patch.object(bot, "_push_publication_branch") as push, patch.object(bot, "_create_pr") as create_pr:
        assert bot.run(**options, force_rebase=True)
    push.assert_called_once()
    create_pr.assert_called_once()
    upstream = git.Repo(options["source"].url).commit("v1.14.3")
    assert f"({upstream.hexsha[:7]})" in create_pr.call_args.kwargs["title"]
    assert "UPSTREAM: <drop>: revert release PR 308" in create_pr.call_args.kwargs["body"]


def test_force_keeps_strict_conflict_policy(reverted_release, caplog):
    options = reverted_release
    CommitBuilder(options["destination"]).add_file("release.txt", "conflicting downstream\n").commit(
        "UPSTREAM: <carry>: conflict with restored release"
    )
    assert not bot.run(**options, force_rebase=True, conflict_policy="strict")
    assert "Upstream content may have been dropped from 'release.txt'" in caplog.text
    assert "lost line: restored upstream" in caplog.text


def test_force_requires_operator_filtering_of_reverts(reverted_release):
    options = reverted_release
    options["tag_policy"] = "none"
    assert bot.run(**options, force_rebase=True)
    assert not Path(options["working_dir"], "release.txt").exists()
    assert Path(options["working_dir"], "later-carry.txt").read_text() == "later carry\n"


def test_force_keeps_automation_pause_gate(rebase_run):
    options = rebase_run
    destination_repo = options["github_app_provider"].github_destination_app.repository.return_value
    pr = MagicMock(labels=[{"name": "rebase/manual"}])
    destination_repo.pull_requests.return_value = [pr]
    assert bot.run(**options, force_rebase=True)
    assert not Path(options["working_dir"]).exists()


def test_merge_api_rejects_force(rebase_run):
    rebase_run.pop("hooks")
    assert not bot.run(**rebase_run, sync_mode="merge", force_rebase=True)
    assert not Path(rebase_run["working_dir"]).exists()
