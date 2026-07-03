#    Copyright 2022 Red Hat, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
"""Summary metadata collected during a Rebase Bot run."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DroppedCommit:
    """A downstream commit omitted from the rebase branch."""

    sha: str
    message: str
    reason: str


@dataclass(frozen=True)
class ArtPrInfo:
    """An ART pull request cherry-picked into the rebase branch."""

    number: int
    title: str
    url: str


@dataclass(frozen=True)
class ContentLossWarning:
    """Upstream content that may have been dropped during cherry-pick conflict resolution."""

    sha: str
    message: str
    file: str
    lost_lines: list[str]


@dataclass
class RebaseSummary:
    """Metadata about a rebase run, used when rendering the rebase PR body."""

    upstream_commit_count: int
    dropped_commits: list[DroppedCommit] = field(default_factory=list)
    art_pr: ArtPrInfo | None = None
    content_loss_warnings: list[ContentLossWarning] = field(default_factory=list)
