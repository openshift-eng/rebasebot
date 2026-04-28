from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from enum import Enum

from rebasebot.github import GitHubBranch

STATE_FILENAME = ".rebasebot-resume.json"
STATE_VERSION = 4


class ResumeStateError(ValueError):
    """Raised when persisted resume state cannot be loaded or validated."""


class ResumePhase(str, Enum):
    PRE_REBASE = "pre_rebase"
    PRE_CARRY_COMMIT = "pre_carry_commit"
    CARRY_COMMITS = "carry_commits"
    POST_REBASE = "post_rebase"
    ART_PR = "art_pr"
    PRE_PUSH_REBASE_BRANCH = "pre_push_rebase_branch"
    PRE_CREATE_PR = "pre_create_pr"

    @property
    def display_name(self) -> str:
        return {
            ResumePhase.PRE_REBASE: "pre-rebase hook",
            ResumePhase.PRE_CARRY_COMMIT: "pre-carry hook",
            ResumePhase.CARRY_COMMITS: "carry commits",
            ResumePhase.POST_REBASE: "post-rebase hook",
            ResumePhase.ART_PR: "ART PR commits",
            ResumePhase.PRE_PUSH_REBASE_BRANCH: "pre-push hook",
            ResumePhase.PRE_CREATE_PR: "pre-create-PR hook",
        }[self]


@dataclass
class BranchState:
    url: str
    ns: str
    name: str
    branch: str

    @classmethod
    def from_github_branch(cls, branch: GitHubBranch) -> BranchState:
        return cls(url=branch.url, ns=branch.ns, name=branch.name, branch=branch.branch)

    def to_github_branch(self) -> GitHubBranch:
        return GitHubBranch(url=self.url, ns=self.ns, name=self.name, branch=self.branch)


@dataclass
class ResumeTask:
    kind: str
    sha: str | None = None
    source_branch: str | None = None
    commit_description: str | None = None
    commit_message: str | None = None
    author: str | None = None
    reset_count: int | None = None

    @classmethod
    def from_dict(cls, payload: dict) -> ResumeTask:
        return cls(**payload)


@dataclass
class ResumeState:
    source: BranchState
    dest: BranchState
    rebase: BranchState
    source_head_sha: str
    dest_head_sha: str
    phase: ResumePhase
    remaining_tasks: list[ResumeTask]
    art_tasks: list[ResumeTask]
    current_task: ResumeTask | None = None
    head_before_task: str | None = None
    head_at_pause: str | None = None
    allowed_untracked_files: list[str] | None = None
    next_hook_script_index: int | None = None
    hook_script_locations: list[str] | None = None
    version: int = STATE_VERSION

    @classmethod
    def from_dict(cls, payload: dict) -> ResumeState:
        if payload.get("version") != STATE_VERSION:
            raise ResumeStateError(f"Unsupported resume state version: {payload.get('version')}")

        try:
            return cls(
                source=BranchState(**payload["source"]),
                dest=BranchState(**payload["dest"]),
                rebase=BranchState(**payload["rebase"]),
                source_head_sha=payload["source_head_sha"],
                dest_head_sha=payload["dest_head_sha"],
                phase=ResumePhase(payload["phase"]),
                remaining_tasks=[ResumeTask.from_dict(task) for task in payload["remaining_tasks"]],
                art_tasks=[ResumeTask.from_dict(task) for task in payload.get("art_tasks", [])],
                current_task=ResumeTask.from_dict(payload["current_task"])
                if payload["current_task"] is not None
                else None,
                head_before_task=payload.get("head_before_task"),
                head_at_pause=payload.get("head_at_pause"),
                allowed_untracked_files=payload.get("allowed_untracked_files"),
                next_hook_script_index=payload.get("next_hook_script_index"),
                hook_script_locations=payload.get("hook_script_locations"),
                version=payload["version"],
            )
        except KeyError as err:
            raise ResumeStateError(f"Resume state is missing field: {err.args[0]}") from err
        except TypeError as err:
            raise ResumeStateError("Resume state contains invalid data") from err


def resume_state_path(workdir: str) -> str:
    return os.path.join(workdir, STATE_FILENAME)


def has_resume_state(workdir: str) -> bool:
    return os.path.exists(resume_state_path(workdir))


def write_resume_state(workdir: str, state: ResumeState) -> str:
    path = resume_state_path(workdir)
    payload = asdict(state)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as state_file:
        json.dump(payload, state_file, indent=2, sort_keys=True)
    os.replace(temp_path, path)
    return path


def read_resume_state(workdir: str) -> ResumeState:
    path = resume_state_path(workdir)
    try:
        with open(path, encoding="utf-8") as state_file:
            payload = json.load(state_file)
    except FileNotFoundError as err:
        raise ResumeStateError(f"No resume state found in {workdir}") from err
    except json.JSONDecodeError as err:
        raise ResumeStateError(f"Resume state in {path} is not valid JSON") from err

    return ResumeState.from_dict(payload)


def clear_resume_state(workdir: str) -> None:
    path = resume_state_path(workdir)
    if os.path.exists(path):
        os.remove(path)
