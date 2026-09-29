"""Ancestry-preserving synchronization, isolated from carry replay."""

import logging
from pathlib import Path

import git

from rebasebot.github import GitHubBranch
from rebasebot.lifecycle_hooks import LifecycleHook, LifecycleHooks

MODE_MARKER = "Rebasebot sync mode: merge"
REPLAY_OPTIONS = (
    "--tag-policy",
    "--conflict-policy",
    "--bot-emails",
    "--exclude-commits",
    "--pre-carry-commit-hook",
)
OPERATION_STATE = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer")


class MergeSyncError(Exception):
    """A merge requiring manual intervention before publication."""


def has_merge_sync(gitwd: git.Repo, downstream: str, upstream: str) -> bool:
    return bool(
        gitwd.git.log(f"{upstream}..{downstream}", "-1", "--format=%H", "--fixed-strings", f"--grep={MODE_MARKER}")
    )


def restore(gitwd: git.Repo, downstream: str) -> None:
    state = Path(gitwd.git_dir)
    if (state / "rebase-apply" / "applying").exists():
        gitwd.git.am("--quit")
    elif any((state / name).exists() for name in ("rebase-merge", "rebase-apply")):
        gitwd.git.rebase("--quit")
    if any((state / name).exists() for name in ("sequencer", "CHERRY_PICK_HEAD", "REVERT_HEAD")):
        gitwd.git.cherry_pick("--quit")
    gitwd.git.reset("--hard", downstream)
    gitwd.git.checkout("-f", "-B", "rebase", downstream)
    gitwd.git.clean("-ffd", "--", ".")
    check_result(gitwd, downstream)


def _restore_after_failure(gitwd: git.Repo, downstream: str) -> None:
    try:
        restore(gitwd, downstream)
    except Exception:
        logging.exception("Failed to restore disposable merge workspace to %s; original failure follows.", downstream)


def check_result(gitwd: git.Repo, *parents: str) -> None:
    if any(Path(gitwd.git_dir, name).exists() for name in OPERATION_STATE):
        raise MergeSyncError("Merge hooks must finish all Git operations before returning.")
    if gitwd.is_dirty(untracked_files=True):
        raise MergeSyncError("Merge hooks must commit their changes and leave a clean working tree.")
    if any(not gitwd.is_ancestor(parent, "HEAD") for parent in parents):
        raise MergeSyncError(
            "Merge hooks must preserve downstream and pinned upstream ancestry; do not reset or rebase."
        )
    if gitwd.head.is_detached or gitwd.active_branch.name != "rebase":
        raise MergeSyncError("Merge hooks must remain on the local rebase branch.")


def execute_hook(
    gitwd: git.Repo,
    hooks: LifecycleHooks,
    hook: LifecycleHook,
    downstream: str,
    upstream: str | None = None,
) -> None:
    before = gitwd.head.commit.hexsha
    try:
        hooks.execute_scripts_for_hook(hook=hook)
        check_result(gitwd, downstream, *([upstream] if upstream else []))
        if hook == LifecycleHook.PRE_CREATE_PR and gitwd.head.commit.hexsha != before:
            raise MergeSyncError("PRE_CREATE_PR must not change commits after the branch has been pushed.")
    except Exception:
        _restore_after_failure(gitwd, downstream)
        raise


def prepare(
    gitwd: git.Repo,
    hooks: LifecycleHooks,
    downstream: str,
    upstream: str,
    source: GitHubBranch,
    dest: GitHubBranch,
    needs_merge: bool,
) -> None:
    gitwd.git.checkout("-B", "rebase", downstream)
    try:
        execute_hook(gitwd, hooks, LifecycleHook.PRE_REBASE, downstream)
        if needs_merge:
            message = f"Merge {source.url}:{source.branch} ({upstream}) into {dest.branch}\n\n{MODE_MARKER}"
            try:
                gitwd.git.execute(
                    [
                        git.Git.GIT_PYTHON_GIT_EXECUTABLE,
                        "-c",
                        "rerere.enabled=false",
                        "-c",
                        "branch.rebase.mergeOptions=",
                        "merge",
                        "--strategy=ort",
                        "--no-ff",
                        "--no-edit",
                        "--no-autostash",
                        "-m",
                        message,
                        upstream,
                    ]
                )
            except git.GitCommandError as exc:
                conflicts = gitwd.git.diff("--name-only", "--diff-filter=U")
                raise MergeSyncError(
                    f"Cannot merge pinned upstream {upstream} into downstream {downstream}. "
                    f"Conflicting paths: {conflicts or '(none; inspect Git error)'}. "
                    "Resolve and merge upstream into the downstream branch manually, then rerun. "
                    f"Git error: {exc}"
                ) from exc
        execute_hook(gitwd, hooks, LifecycleHook.POST_REBASE, downstream, upstream)
    except Exception:
        _restore_after_failure(gitwd, downstream)
        raise


def _candidate_history(gitwd: git.Repo, ref: str, downstream: str) -> list[tuple[str, str, tuple[str, ...]]] | None:
    """Compare first-parent hook/merge provenance, ignoring recreated commit timestamps."""
    history = []
    commit = gitwd.commit(ref)
    while commit.hexsha != downstream:
        if not commit.parents:
            return None
        history.append((commit.tree.hexsha, commit.message, tuple(parent.hexsha for parent in commit.parents[1:])))
        commit = commit.parents[0]
    return history


def push_required(gitwd: git.Repo, rebase: GitHubBranch, downstream: str, upstream: str) -> bool:
    if rebase.branch not in gitwd.remotes.rebase.refs:
        return True
    remote = f"rebase/{rebase.branch}"
    same_tree = gitwd.commit(remote).tree == gitwd.head.commit.tree
    contains_parents = all(gitwd.is_ancestor(parent, remote) for parent in (downstream, upstream))
    if same_tree and contains_parents:
        local_history = _candidate_history(gitwd, "HEAD", downstream)
        if local_history is not None and local_history == _candidate_history(gitwd, remote, downstream):
            logging.info("Existing merge branch has equivalent pinned merge and hook provenance.")
            return False
    return True
