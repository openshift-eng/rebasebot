#!/bin/bash
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

set -o nounset
set -o pipefail


REPO_ROOT=$(dirname "${BASH_SOURCE}")/..

OPENSHIFT_CI=${OPENSHIFT_CI:-""}
ARTIFACT_DIR=${ARTIFACT_DIR:-""}

PYTEST_ARGS=${PYTEST_ARGS:-"-vv --cov=rebasebot"}


if [ "$OPENSHIFT_CI" == "true" ] && [ -n "$ARTIFACT_DIR" ] && [ -d "$ARTIFACT_DIR" ]; then # detect ci environment there
  # Set up coverage.py/pytest-cov related env variables, since source root is not writable in OCP CI
  export COV_CORE_DATAFILE=/tmp/.rebasebot_unit_coverage
  export COVERAGE_FILE=/tmp/.rebasebot_unit_coverage

  # point gopath to /tmp since go mod and go tidy is using during tests
  export GOPATH=/tmp/temp_gopath

  PYTEST_ARGS="${PYTEST_ARGS} --cov-report=term --cov-report=html:${ARTIFACT_DIR}/cov-report --junitxml=${ARTIFACT_DIR}/junit_rebasebot_tests.xml"
fi

set -x
pytest $PYTEST_ARGS "$REPO_ROOT"
