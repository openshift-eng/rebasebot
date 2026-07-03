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
"""Prow job context for Rebase Bot runs in OpenShift CI."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class ProwJobContext:
    """Captures Prow job metadata injected into the Rebase Bot runtime environment."""

    job_name: str | None
    job_type: str | None
    build_id: str | None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ProwJobContext:
        if env is None:
            env = os.environ
        return cls(
            job_name=env.get("JOB_NAME"),
            job_type=env.get("JOB_TYPE"),
            build_id=env.get("BUILD_ID"),
        )

    @property
    def is_rehearsal(self) -> bool:
        if self.job_name is None and self.job_type is None:
            return False
        if self.job_name is not None and self.job_name.startswith("rehearse-"):
            return True
        if self.job_type is not None and self.job_type != "periodic":
            return True
        return False

    @property
    def log_url(self) -> str | None:
        if self.job_name is not None and self.build_id is not None:
            return f"https://prow.ci.openshift.org/view/gs/origin-ci-test/logs/{self.job_name}/{self.build_id}"
        return None
