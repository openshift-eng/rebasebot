#!/bin/bash
set -e  # Exit immediately if a command exits with a non-zero status
set -o pipefail  # Return the exit status of the last command in the pipe that failed

stage_and_commit(){
    # If commiter email and name is passed as environment variable then use it.
    if [[ -z "$REBASEBOT_GIT_USERNAME" || -z "$REBASEBOT_GIT_EMAIL" ]]; then
        author_flag=()
    else
        author_flag=(--author="$REBASEBOT_GIT_USERNAME <$REBASEBOT_GIT_EMAIL>")
    fi

    if [[ -n $(git status --porcelain) ]]; then
        git add -A
        git commit "${author_flag[@]}" -q -m "UPSTREAM: <drop>: Generate OpenShift manifests"
    fi
}

# $REBASEBOT_SOURCE is version tag in format v0.0.0
if [[ -z "$REBASEBOT_SOURCE" ]]; then
    echo "The environment variable REBASEBOT_SOURCE is not set." >&2
    exit 1
fi

# Remove the leading 'v' from REBASEBOT_SOURCE
export PROVIDER_VERSION="${REBASEBOT_SOURCE#v}"

pushd openshift
make ocp-manifests
popd
stage_and_commit
