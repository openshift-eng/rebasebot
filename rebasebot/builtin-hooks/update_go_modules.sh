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
        git commit "${author_flag[@]}" -q -m "UPSTREAM: <carry>: Updating and vendoring go modules after an upstream rebase"
    fi
}

process_go_mod_updates() {
    echo "Performing go modules update"

    find . -name 'go.mod' -print0 | while IFS= read -r -d '' go_mod_file; do
        local module_base_path
        module_base_path=$(dirname "$go_mod_file")

        # Reset go.mod and go.sum to make sure they are the same as in the source
        for filename in "go.mod" "go.sum"; do
            local full_path="$module_base_path/$filename"
            if [[ ! -f "$full_path" ]]; then
                continue
            fi
            if ! git checkout "source/$REBASEBOT_SOURCE" -- "$full_path"; then
                echo "go module at $module_base_path is downstream only, skip its resetting"
                break
            fi
        done

        pushd "$module_base_path"

        echo "go mod tidy output for $module_base_path"
        if ! go mod tidy; then
            echo "Unable to run 'go mod tidy' in $module_base_path" >&2
            exit 1
        fi

        echo "go mod vendor output for $module_base_path"
        if ! go mod vendor; then
            echo "Unable to run 'go mod vendor' in $module_base_path" >&2
            exit 1
        fi

        popd
    done

    stage_and_commit
}

# Check if the source branch environment variable is set
if [[ -z "$REBASEBOT_SOURCE" ]]; then
    echo "The environment variable REBASEBOT_SOURCE is not set." >&2
    exit 1
fi

process_go_mod_updates
