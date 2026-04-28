from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable

import git
from github3.repos.repo import Repository

from rebasebot import lifecycle_hooks, resume_state
from rebasebot.github import GitHubBranch
from rebasebot.lifecycle_hooks import LifecycleHookScriptException


class PausedRebaseException(Exception):
    """Raised when rebasebot intentionally pauses for manual resolution."""


class ResumeFlowException(Exception):
    """Raised when a paused run cannot be safely resumed."""


class PauseRebaseTaskException(Exception):
    """Raised when a task should pause and persist resume state."""

    def __init__(
        self,
        message: str,
        *,
        pause_reason: str | None = None,
        resolution_instructions: str | None = None,
    ) -> None:
        super().__init__(message)
        self.pause_reason = pause_reason
        self.resolution_instructions = resolution_instructions


@dataclass
class FlowResult:
    """Carries flow results needed by later publish steps."""

    needs_rebase: bool
    skip_pre_push_rebase_branch_hook: bool = False
    skip_pre_create_pr_hook: bool = False


@dataclass
class FlowActions:
    """Operations that the flow engine delegates back to the main bot flow."""

    needs_rebase: Callable[[git.Repo, GitHubBranch, GitHubBranch], bool]
    prepare_rebase_branch: Callable[[git.Repo, GitHubBranch, GitHubBranch], None]
    build_rebase_tasks: Callable[..., list[resume_state.ResumeTask]]
    build_art_pr_tasks: Callable[[Repository, GitHubBranch, git.Repo], list[resume_state.ResumeTask]]
    execute_rebase_tasks: Callable[..., None]


@dataclass
class FlowContext:
    """Shared immutable inputs and computed state for a flow run."""

    gitwd: git.Repo
    source: GitHubBranch
    dest: GitHubBranch
    rebase: GitHubBranch
    working_dir: str
    source_repo: Repository
    dest_repo: Repository
    hooks: lifecycle_hooks.LifecycleHooks
    tag_policy: str
    conflict_policy: str
    bot_emails: list
    exclude_commits: list
    update_go_modules: bool
    always_run_hooks: bool
    pause_on_conflict: bool
    retry_failed_step: bool
    resume: resume_state.ResumeState | None
    needs_rebase: bool
    runtime_art_tasks: list[resume_state.ResumeTask] | None = None

    def is_resume(self) -> bool:
        return self.resume is not None

    def resume_phase(self) -> resume_state.ResumePhase | None:
        if self.resume is None:
            return None
        return self.resume.phase

    def effective_pause_on_conflict(self) -> bool:
        return True if self.is_resume() else self.pause_on_conflict

    def flow_args(self) -> dict[str, Any]:
        return {
            "gitwd": self.gitwd,
            "source": self.source,
            "dest": self.dest,
            "rebase": self.rebase,
            "working_dir": self.working_dir,
        }

    def current_art_tasks(self) -> list[resume_state.ResumeTask]:
        if self.runtime_art_tasks is not None:
            return self.runtime_art_tasks
        if self.resume is not None:
            return self.resume.art_tasks
        return []

    def set_runtime_art_tasks(self, art_tasks: list[resume_state.ResumeTask]) -> None:
        self.runtime_art_tasks = art_tasks


@dataclass(frozen=True)
class StepSpec:
    """Describes one persisted phase in the unified step runner."""

    phase: resume_state.ResumePhase
    fresh_when: Callable[[FlowContext], bool]
    run_fresh: Callable[[FlowContext, FlowActions], dict[str, bool] | None]
    run_resume: Callable[[FlowContext, FlowActions, resume_state.ResumeState], dict[str, bool] | None]
    terminal_on_resume: bool = False


_NO_REBASE_CONTINUE_MESSAGE = (
    "Cannot continue paused run because no rebase is needed and lifecycle hooks are not configured to run."
)


def persist_resume_state(
    *,
    gitwd: git.Repo,
    working_dir: str,
    source: GitHubBranch,
    dest: GitHubBranch,
    rebase: GitHubBranch,
    phase: resume_state.ResumePhase,
    remaining_tasks: list[resume_state.ResumeTask],
    art_tasks: list[resume_state.ResumeTask] | None = None,
    current_task: resume_state.ResumeTask | None = None,
    head_before_task: str | None = None,
    head_at_pause: str | None = None,
    allowed_untracked_files: list[str] | None = None,
    next_hook_script_index: int | None = None,
    hook_script_locations: list[str] | None = None,
) -> str:
    state = resume_state.ResumeState(
        source=resume_state.BranchState.from_github_branch(source),
        dest=resume_state.BranchState.from_github_branch(dest),
        rebase=resume_state.BranchState.from_github_branch(rebase),
        source_head_sha=gitwd.commit(f"source/{source.branch}").hexsha,
        dest_head_sha=gitwd.commit(f"dest/{dest.branch}").hexsha,
        phase=phase,
        remaining_tasks=remaining_tasks,
        art_tasks=art_tasks or [],
        current_task=current_task,
        head_before_task=head_before_task,
        head_at_pause=head_at_pause,
        allowed_untracked_files=allowed_untracked_files or [],
        next_hook_script_index=next_hook_script_index,
        hook_script_locations=hook_script_locations,
    )
    return resume_state.write_resume_state(working_dir, state)


def pause_rebase_for_resolution(
    *,
    gitwd: git.Repo,
    working_dir: str,
    source: GitHubBranch,
    dest: GitHubBranch,
    rebase: GitHubBranch,
    phase: resume_state.ResumePhase,
    current_task: resume_state.ResumeTask,
    remaining_tasks: list[resume_state.ResumeTask],
    art_tasks: list[resume_state.ResumeTask],
    head_before_task: str,
    pause_reason: str | None = None,
    resolution_instructions: str | None = None,
) -> None:
    allowed_untracked_files = sorted(path for path in gitwd.untracked_files if path != resume_state.STATE_FILENAME)
    state_path = persist_resume_state(
        gitwd=gitwd,
        working_dir=working_dir,
        source=source,
        dest=dest,
        rebase=rebase,
        phase=phase,
        remaining_tasks=remaining_tasks,
        art_tasks=art_tasks,
        current_task=current_task,
        head_before_task=head_before_task,
        head_at_pause=gitwd.head.commit.hexsha,
        allowed_untracked_files=allowed_untracked_files,
    )
    default_resolution_instructions = (
        f"Resolve the conflict in {working_dir}, finish the cherry-pick with a commit or "
        "'git cherry-pick --continue', then rerun rebasebot with --continue."
    )
    message_parts = [f"Paused during {phase.display_name} while applying '{current_task.commit_description}'."]
    if pause_reason is not None:
        message_parts.append(pause_reason)
    message_parts.append(resolution_instructions or default_resolution_instructions)
    message_parts.append(f"Resume state saved to {state_path}.")
    raise PausedRebaseException(" ".join(message_parts))


def execute_hook_with_resume(
    *,
    hook: lifecycle_hooks.LifecycleHook,
    hooks: lifecycle_hooks.LifecycleHooks,
    gitwd: git.Repo,
    working_dir: str,
    source: GitHubBranch,
    dest: GitHubBranch,
    rebase: GitHubBranch,
    phase: resume_state.ResumePhase,
    remaining_tasks: list[resume_state.ResumeTask] | None = None,
    art_tasks: list[resume_state.ResumeTask] | None = None,
    start_script_index: int = 0,
) -> None:
    try:
        hooks.execute_scripts_for_hook(hook=hook, start_index=start_script_index)
    except LifecycleHookScriptException as ex:
        state_path = persist_resume_state(
            gitwd=gitwd,
            working_dir=working_dir,
            source=source,
            dest=dest,
            rebase=rebase,
            phase=phase,
            remaining_tasks=remaining_tasks or [],
            art_tasks=art_tasks,
            next_hook_script_index=ex.script_index + 1 if ex.script_index is not None else None,
            hook_script_locations=hooks.get_script_locations_for_hook(hook),
        )
        logging.warning(
            "Saved resume state to %s after %s failed at %s. Continue with --continue to skip the failed script "
            "and continue from the next saved step, or rerun with --continue --retry-failed-step after fixing "
            "the issue to retry it.",
            state_path,
            phase.display_name,
            ex.script_location or hook,
        )
        raise


def resolve_hook_resume_index(
    *,
    hook: lifecycle_hooks.LifecycleHook,
    hooks: lifecycle_hooks.LifecycleHooks,
    state: resume_state.ResumeState,
    retry_failed_step: bool = False,
) -> int:
    current_hook_scripts = hooks.get_script_locations_for_hook(hook)
    if state.hook_script_locations is None or state.next_hook_script_index is None:
        if retry_failed_step:
            raise ResumeFlowException(
                f"Cannot retry failed step for {hook} because the saved hook position is unavailable."
            )
        return len(current_hook_scripts)

    if current_hook_scripts != state.hook_script_locations:
        raise ResumeFlowException(
            f"Cannot continue paused run because configured {hook} scripts changed after the pause."
        )

    if not 0 <= state.next_hook_script_index <= len(current_hook_scripts):
        raise ResumeFlowException(
            f"Cannot continue paused run because saved {hook} script position is invalid."
        )

    if retry_failed_step:
        if state.next_hook_script_index == 0:
            raise ResumeFlowException(
                f"Cannot retry failed step for {hook} because the saved hook position is invalid."
            )
        return state.next_hook_script_index - 1

    return state.next_hook_script_index


def execute_rebase_tasks(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    *,
    gitwd: git.Repo,
    source: GitHubBranch,
    dest: GitHubBranch,
    rebase: GitHubBranch,
    working_dir: str,
    tasks: list[resume_state.ResumeTask],
    phase: resume_state.ResumePhase,
    conflict_policy: str,
    pause_on_conflict: bool,
    safe_cherry_pick: Callable[..., None],
    pause_exception_cls: type[Exception],
    future_art_tasks: list[resume_state.ResumeTask] | None = None,
) -> None:
    for index, task in enumerate(tasks):
        if task.kind == "squash":
            logging.info("Squashing commits for bot: %s", task.author)
            gitwd.git.reset("--soft", f"HEAD~{task.reset_count}")
            gitwd.git.commit("-m", task.commit_message, "--author", task.author)
            continue

        logging.info("Picking commit: %s", task.commit_description)
        head_before_task = gitwd.head.commit.hexsha
        try:
            safe_cherry_pick(
                gitwd=gitwd,
                sha=task.sha,
                source_branch=task.source_branch,
                conflict_policy=conflict_policy,
                commit_description=task.commit_description,
                pause_on_conflict=pause_on_conflict,
            )
        except pause_exception_cls as ex:
            pause_rebase_for_resolution(
                gitwd=gitwd,
                working_dir=working_dir,
                source=source,
                dest=dest,
                rebase=rebase,
                phase=phase,
                current_task=task,
                remaining_tasks=tasks[index + 1 :],
                art_tasks=future_art_tasks or [],
                head_before_task=head_before_task,
                pause_reason=getattr(ex, "pause_reason", None),
                resolution_instructions=getattr(ex, "resolution_instructions", None),
            )


def validate_resume_request(
    *,
    state: resume_state.ResumeState,
    source: GitHubBranch,
    dest: GitHubBranch,
    rebase: GitHubBranch,
) -> None:
    expected = {
        "source": state.source.to_github_branch(),
        "dest": state.dest.to_github_branch(),
        "rebase": state.rebase.to_github_branch(),
    }
    actual = {"source": source, "dest": dest, "rebase": rebase}
    for key, expected_branch in expected.items():
        if actual[key] != expected_branch:
            raise ResumeFlowException(
                f"Cannot continue paused run: {key} does not match resume state. "
                f"Expected {expected_branch.ns}/{expected_branch.name}:{expected_branch.branch}."
            )


def validate_resume_git_state(
    *,
    gitwd: git.Repo,
    state: resume_state.ResumeState,
) -> None:
    current_head = gitwd.head.commit.hexsha

    if gitwd.head.is_detached:
        raise ResumeFlowException(
            "Cannot continue paused run while HEAD is detached. Check out the local rebase branch first."
        )

    if state.phase in {
        resume_state.ResumePhase.CARRY_COMMITS,
        resume_state.ResumePhase.POST_REBASE,
        resume_state.ResumePhase.ART_PR,
        resume_state.ResumePhase.PRE_PUSH_REBASE_BRANCH,
        resume_state.ResumePhase.PRE_CREATE_PR,
    } and gitwd.active_branch.name != "rebase":
        raise ResumeFlowException(
            f"Cannot continue paused run from branch '{gitwd.active_branch.name}'. "
            "Check out the local rebase branch first."
        )

    cherry_pick_head = os.path.join(gitwd.git_dir, "CHERRY_PICK_HEAD")
    if os.path.exists(cherry_pick_head):
        raise ResumeFlowException(
            "Conflict resolution is still in progress. Finish it with a commit or "
            "'git cherry-pick --continue' before rerunning rebasebot --continue."
        )

    dirty_status = gitwd.git.status("--porcelain", "--untracked-files=no")
    if dirty_status:
        raise ResumeFlowException(
            "Cannot continue paused run with staged or modified tracked files. "
            "Commit, stash, or discard those changes first."
        )

    unexpected_untracked = sorted(
        set(gitwd.untracked_files) - set(state.allowed_untracked_files or []) - {resume_state.STATE_FILENAME}
    )
    if unexpected_untracked:
        raise ResumeFlowException(
            f"Cannot continue paused run with unexpected untracked files present: {', '.join(unexpected_untracked)}."
        )

    current_source_head = gitwd.commit(f"source/{state.source.branch}").hexsha
    if current_source_head != state.source_head_sha:
        raise ResumeFlowException(
            "Cannot continue paused run because the source branch advanced after the pause. "
            "Restart the rebase with the new upstream state."
        )

    current_dest_head = gitwd.commit(f"dest/{state.dest.branch}").hexsha
    if current_dest_head != state.dest_head_sha:
        raise ResumeFlowException(
            "Cannot continue paused run because the destination branch advanced after the pause. "
            "Restart the rebase with the new downstream state."
        )

    if (
        state.phase in {resume_state.ResumePhase.CARRY_COMMITS, resume_state.ResumePhase.ART_PR}
        and state.current_task is not None
        and state.head_before_task is not None
    ):
        paused_head = state.head_at_pause or state.head_before_task
        if paused_head != state.head_before_task and current_head == paused_head:
            raise ResumeFlowException(
                "Cannot continue paused run because the paused commit was not changed after the pause. "
                "Amend it, replace it, or drop it before rerunning rebasebot --continue."
            )

        if current_head == state.head_before_task:
            logging.info(
                "Paused task '%s' was skipped or resolved without a new commit; continuing with remaining tasks.",
                state.current_task.commit_description,
            )

        return

def load_and_validate_resume_state(
    *,
    gitwd: git.Repo,
    working_dir: str,
    source: GitHubBranch,
    dest: GitHubBranch,
    rebase: GitHubBranch,
) -> resume_state.ResumeState:
    try:
        state = resume_state.read_resume_state(working_dir)
    except resume_state.ResumeStateError as err:
        raise ResumeFlowException(str(err)) from err
    validate_resume_request(
        state=state,
        source=source,
        dest=dest,
        rebase=rebase,
    )
    validate_resume_git_state(
        gitwd=gitwd,
        state=state,
    )
    return state


def build_flow_context(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    *,
    gitwd: git.Repo,
    source: GitHubBranch,
    dest: GitHubBranch,
    rebase: GitHubBranch,
    working_dir: str,
    source_repo: Repository,
    dest_repo: Repository,
    hooks: lifecycle_hooks.LifecycleHooks,
    tag_policy: str,
    conflict_policy: str,
    bot_emails: list,
    exclude_commits: list,
    update_go_modules: bool,
    always_run_hooks: bool,
    pause_on_conflict: bool,
    retry_failed_step: bool,
    actions: FlowActions,
    state: resume_state.ResumeState | None,
) -> FlowContext:
    return FlowContext(
        gitwd=gitwd,
        source=source,
        dest=dest,
        rebase=rebase,
        working_dir=working_dir,
        source_repo=source_repo,
        dest_repo=dest_repo,
        hooks=hooks,
        tag_policy=tag_policy,
        conflict_policy=conflict_policy,
        bot_emails=bot_emails,
        exclude_commits=exclude_commits,
        update_go_modules=update_go_modules,
        always_run_hooks=always_run_hooks,
        pause_on_conflict=pause_on_conflict,
        retry_failed_step=retry_failed_step,
        resume=state,
        needs_rebase=actions.needs_rebase(gitwd, source, dest),
    )


def apply_flow_result_patch(result: FlowResult, patch: dict[str, bool] | None) -> None:
    if patch is None:
        return

    for key, value in patch.items():
        setattr(result, key, value)


def resolve_resume_index(steps: tuple[StepSpec, ...], phase: resume_state.ResumePhase) -> int:
    for index, step in enumerate(steps):
        if step.phase == phase:
            return index

    raise ResumeFlowException(f"Unsupported resume phase: {phase}")


def _should_run_hook_subset(ctx: FlowContext) -> bool:
    return ctx.needs_rebase or ctx.always_run_hooks


def _should_run_rebase_only(ctx: FlowContext) -> bool:
    return ctx.needs_rebase


def _require_hooks_when_no_rebase(ctx: FlowContext) -> None:
    if not ctx.needs_rebase and not ctx.always_run_hooks:
        raise ResumeFlowException(_NO_REBASE_CONTINUE_MESSAGE)


def run_hook_step(
    *,
    ctx: FlowContext,
    hook: lifecycle_hooks.LifecycleHook,
    phase: resume_state.ResumePhase,
    start_script_index: int = 0,
    remaining_tasks: list[resume_state.ResumeTask] | None = None,
    art_tasks: list[resume_state.ResumeTask] | None = None,
) -> None:
    execute_hook_with_resume(
        hook=hook,
        hooks=ctx.hooks,
        phase=phase,
        remaining_tasks=remaining_tasks,
        art_tasks=art_tasks,
        start_script_index=start_script_index,
        **ctx.flow_args(),
    )


def run_task_step(
    *,
    ctx: FlowContext,
    actions: FlowActions,
    tasks: list[resume_state.ResumeTask],
    phase: resume_state.ResumePhase,
    future_art_tasks: list[resume_state.ResumeTask] | None = None,
) -> None:
    actions.execute_rebase_tasks(
        tasks=tasks,
        phase=phase,
        conflict_policy=ctx.conflict_policy,
        pause_on_conflict=ctx.effective_pause_on_conflict(),
        future_art_tasks=future_art_tasks,
        **ctx.flow_args(),
    )


def _run_pre_rebase_fresh(ctx: FlowContext, _actions: FlowActions) -> dict[str, bool] | None:
    run_hook_step(
        ctx=ctx,
        hook=lifecycle_hooks.LifecycleHook.PRE_REBASE,
        phase=resume_state.ResumePhase.PRE_REBASE,
    )
    return None


def _run_pre_rebase_resume(
    ctx: FlowContext,
    _actions: FlowActions,
    state: resume_state.ResumeState,
) -> dict[str, bool] | None:
    _require_hooks_when_no_rebase(ctx)
    run_hook_step(
        ctx=ctx,
        hook=lifecycle_hooks.LifecycleHook.PRE_REBASE,
        phase=resume_state.ResumePhase.PRE_REBASE,
        start_script_index=resolve_hook_resume_index(
            hook=lifecycle_hooks.LifecycleHook.PRE_REBASE,
            hooks=ctx.hooks,
            state=state,
            retry_failed_step=ctx.retry_failed_step,
        ),
    )
    return None


def _run_pre_carry_commit_fresh(ctx: FlowContext, actions: FlowActions) -> dict[str, bool] | None:
    if ctx.needs_rebase:
        actions.prepare_rebase_branch(ctx.gitwd, ctx.source, ctx.dest)

    run_hook_step(
        ctx=ctx,
        hook=lifecycle_hooks.LifecycleHook.PRE_CARRY_COMMIT,
        phase=resume_state.ResumePhase.PRE_CARRY_COMMIT,
    )
    return None


def _run_pre_carry_commit_resume(
    ctx: FlowContext,
    actions: FlowActions,
    state: resume_state.ResumeState,
) -> dict[str, bool] | None:
    _require_hooks_when_no_rebase(ctx)
    if ctx.needs_rebase:
        actions.prepare_rebase_branch(ctx.gitwd, ctx.source, ctx.dest)

    run_hook_step(
        ctx=ctx,
        hook=lifecycle_hooks.LifecycleHook.PRE_CARRY_COMMIT,
        phase=resume_state.ResumePhase.PRE_CARRY_COMMIT,
        start_script_index=resolve_hook_resume_index(
            hook=lifecycle_hooks.LifecycleHook.PRE_CARRY_COMMIT,
            hooks=ctx.hooks,
            state=state,
            retry_failed_step=ctx.retry_failed_step,
        ),
    )
    return None


def _run_carry_commits_fresh(ctx: FlowContext, actions: FlowActions) -> dict[str, bool] | None:
    art_tasks = actions.build_art_pr_tasks(ctx.dest_repo, ctx.dest, ctx.gitwd)
    ctx.set_runtime_art_tasks(art_tasks)
    carry_tasks = actions.build_rebase_tasks(
        gitwd=ctx.gitwd,
        source=ctx.source,
        dest=ctx.dest,
        source_repo=ctx.source_repo,
        tag_policy=ctx.tag_policy,
        bot_emails=ctx.bot_emails,
        exclude_commits=ctx.exclude_commits,
        update_go_modules=ctx.update_go_modules,
    )
    run_task_step(
        ctx=ctx,
        actions=actions,
        tasks=carry_tasks,
        phase=resume_state.ResumePhase.CARRY_COMMITS,
        future_art_tasks=art_tasks,
    )
    return None


def _run_carry_commits_resume(
    ctx: FlowContext,
    actions: FlowActions,
    state: resume_state.ResumeState,
) -> dict[str, bool] | None:
    ctx.set_runtime_art_tasks(state.art_tasks)
    run_task_step(
        ctx=ctx,
        actions=actions,
        tasks=state.remaining_tasks,
        phase=resume_state.ResumePhase.CARRY_COMMITS,
        future_art_tasks=state.art_tasks,
    )
    return None


def _run_post_rebase_fresh(ctx: FlowContext, _actions: FlowActions) -> dict[str, bool] | None:
    run_hook_step(
        ctx=ctx,
        hook=lifecycle_hooks.LifecycleHook.POST_REBASE,
        phase=resume_state.ResumePhase.POST_REBASE,
        art_tasks=ctx.current_art_tasks(),
    )
    return None


def _run_post_rebase_resume(
    ctx: FlowContext,
    _actions: FlowActions,
    state: resume_state.ResumeState,
) -> dict[str, bool] | None:
    ctx.set_runtime_art_tasks(state.art_tasks)
    run_hook_step(
        ctx=ctx,
        hook=lifecycle_hooks.LifecycleHook.POST_REBASE,
        phase=resume_state.ResumePhase.POST_REBASE,
        art_tasks=ctx.current_art_tasks(),
        start_script_index=resolve_hook_resume_index(
            hook=lifecycle_hooks.LifecycleHook.POST_REBASE,
            hooks=ctx.hooks,
            state=state,
            retry_failed_step=ctx.retry_failed_step,
        ),
    )
    return None


def _run_art_pr_fresh(ctx: FlowContext, actions: FlowActions) -> dict[str, bool] | None:
    run_task_step(
        ctx=ctx,
        actions=actions,
        tasks=ctx.current_art_tasks(),
        phase=resume_state.ResumePhase.ART_PR,
    )
    return None


def _run_art_pr_resume(
    ctx: FlowContext,
    actions: FlowActions,
    state: resume_state.ResumeState,
) -> dict[str, bool] | None:
    ctx.set_runtime_art_tasks(state.art_tasks)
    run_task_step(
        ctx=ctx,
        actions=actions,
        tasks=state.remaining_tasks,
        phase=resume_state.ResumePhase.ART_PR,
    )
    return None


def build_core_steps() -> tuple[StepSpec, ...]:
    return (
        StepSpec(
            phase=resume_state.ResumePhase.PRE_REBASE,
            fresh_when=_should_run_hook_subset,
            run_fresh=_run_pre_rebase_fresh,
            run_resume=_run_pre_rebase_resume,
        ),
        StepSpec(
            phase=resume_state.ResumePhase.PRE_CARRY_COMMIT,
            fresh_when=_should_run_hook_subset,
            run_fresh=_run_pre_carry_commit_fresh,
            run_resume=_run_pre_carry_commit_resume,
        ),
        StepSpec(
            phase=resume_state.ResumePhase.CARRY_COMMITS,
            fresh_when=_should_run_rebase_only,
            run_fresh=_run_carry_commits_fresh,
            run_resume=_run_carry_commits_resume,
        ),
        StepSpec(
            phase=resume_state.ResumePhase.POST_REBASE,
            fresh_when=_should_run_hook_subset,
            run_fresh=_run_post_rebase_fresh,
            run_resume=_run_post_rebase_resume,
        ),
        StepSpec(
            phase=resume_state.ResumePhase.ART_PR,
            fresh_when=_should_run_rebase_only,
            run_fresh=_run_art_pr_fresh,
            run_resume=_run_art_pr_resume,
            terminal_on_resume=True,
        ),
    )


def _continue_publish_hook_phase(ctx: FlowContext) -> FlowResult | None:
    if ctx.resume is None:
        return None

    if ctx.resume.phase == resume_state.ResumePhase.PRE_PUSH_REBASE_BRANCH:
        run_hook_step(
            ctx=ctx,
            hook=lifecycle_hooks.LifecycleHook.PRE_PUSH_REBASE_BRANCH,
            phase=resume_state.ResumePhase.PRE_PUSH_REBASE_BRANCH,
            start_script_index=resolve_hook_resume_index(
                hook=lifecycle_hooks.LifecycleHook.PRE_PUSH_REBASE_BRANCH,
                hooks=ctx.hooks,
                state=ctx.resume,
                retry_failed_step=ctx.retry_failed_step,
            ),
        )
        return FlowResult(needs_rebase=ctx.needs_rebase, skip_pre_push_rebase_branch_hook=True)

    if ctx.resume.phase == resume_state.ResumePhase.PRE_CREATE_PR:
        run_hook_step(
            ctx=ctx,
            hook=lifecycle_hooks.LifecycleHook.PRE_CREATE_PR,
            phase=resume_state.ResumePhase.PRE_CREATE_PR,
            start_script_index=resolve_hook_resume_index(
                hook=lifecycle_hooks.LifecycleHook.PRE_CREATE_PR,
                hooks=ctx.hooks,
                state=ctx.resume,
                retry_failed_step=ctx.retry_failed_step,
            ),
        )
        return FlowResult(needs_rebase=ctx.needs_rebase, skip_pre_create_pr_hook=True)

    return None


def _execute_core_flow(ctx: FlowContext, actions: FlowActions) -> FlowResult:
    steps = build_core_steps()
    result = FlowResult(needs_rebase=ctx.needs_rebase)

    if ctx.resume is None:
        for step in steps:
            if not step.fresh_when(ctx):
                continue
            apply_flow_result_patch(result, step.run_fresh(ctx, actions))
        return result

    resume_index = resolve_resume_index(steps, ctx.resume.phase)
    for offset, step in enumerate(steps[resume_index:], start=resume_index):
        if offset == resume_index:
            patch = step.run_resume(ctx, actions, ctx.resume)
        elif step.fresh_when(ctx):
            patch = step.run_fresh(ctx, actions)
        else:
            continue

        apply_flow_result_patch(result, patch)
        if step.terminal_on_resume:
            return result

    return result


def execute_flow(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    *,
    gitwd: git.Repo,
    source: GitHubBranch,
    dest: GitHubBranch,
    rebase: GitHubBranch,
    working_dir: str,
    source_repo: Repository,
    dest_repo: Repository,
    hooks: lifecycle_hooks.LifecycleHooks,
    tag_policy: str,
    conflict_policy: str,
    bot_emails: list,
    exclude_commits: list,
    update_go_modules: bool,
    always_run_hooks: bool,
    pause_on_conflict: bool,
    retry_failed_step: bool,
    actions: FlowActions,
    state: resume_state.ResumeState | None = None,
) -> FlowResult:
    """Execute a fresh or resumed rebase flow and return publish-step metadata."""
    ctx = build_flow_context(
        gitwd=gitwd,
        source=source,
        dest=dest,
        rebase=rebase,
        working_dir=working_dir,
        source_repo=source_repo,
        dest_repo=dest_repo,
        hooks=hooks,
        tag_policy=tag_policy,
        conflict_policy=conflict_policy,
        bot_emails=bot_emails,
        exclude_commits=exclude_commits,
        update_go_modules=update_go_modules,
        always_run_hooks=always_run_hooks,
        pause_on_conflict=pause_on_conflict,
        retry_failed_step=retry_failed_step,
        actions=actions,
        state=state,
    )

    publish_result = _continue_publish_hook_phase(ctx)
    if publish_result is not None:
        return publish_result

    return _execute_core_flow(ctx, actions)
