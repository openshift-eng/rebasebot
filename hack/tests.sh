#!/bin/bash

set -o nounset
set -o pipefail


REPO_ROOT=$(dirname "${BASH_SOURCE}")/..

OPENSHIFT_CI=${OPENSHIFT_CI:-""}
ARTIFACT_DIR=${ARTIFACT_DIR:-""}

PYTEST_ARGS=${PYTEST_ARGS:-"--cov=rebasebot"}


if [ "$OPENSHIFT_CI" == "true" ] && [ -n "$ARTIFACT_DIR" ] && [ -d "$ARTIFACT_DIR" ]; then # detect ci environment there
  # Set up coverage.py/pytest-cov related env variables, since source root is not writable in OCP CI
  export COV_CORE_DATAFILE=/tmp/.rebasebot_unit_coverage
  export COVERAGE_FILE=/tmp/.rebasebot_unit_coverage

  # point gopath to /tmp since go mod and go tidy is using during tests
  export GOPATH=/tmp/temp_gopath

  PYTEST_ARGS="${PYTEST_ARGS} --junitxml=${ARTIFACT_DIR}/junit_rebasebot_tests.xml"
fi

set -x
pytest $PYTEST_ARGS "$REPO_ROOT"

