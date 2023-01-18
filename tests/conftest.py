#    Copyright 2023 Red Hat, Inc.
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
import os
from tempfile import TemporaryDirectory

import pytest

from git import Repo


_GO_CODE = """
package main
import (
    "k8s.io/klog/v2"
)

func main() {
    klog.Errorln("This is a test")
    return
}
"""

_GO_CODE_FILENAME = "test.go"


@pytest.fixture
def tmp_go_app_repo():
    with TemporaryDirectory(prefix="rebasebot_tests_") as tmpdir:
        with open(os.path.join(tmpdir, _GO_CODE_FILENAME), "x", encoding="utf8") as file:
            file.write(_GO_CODE)
        repo = Repo.init(tmpdir)
        with repo.config_writer() as config:
            config.set_value("user", "email", "test@example.com")
            config.set_value("user", "name", "test")
        repo.git.add(all=True)
        repo.git.commit("-m", "Initial commit")
        yield tmpdir, repo
