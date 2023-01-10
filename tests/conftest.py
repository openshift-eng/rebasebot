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
