#!/bin/bash

set -e  # Exit immediately if a command exits with a non-zero status
set -o pipefail  # Return the exit status of the last command in the pipe that failed

stage_and_commit(){
    # If committer email and name is passed as environment variable then use it.
    if [[ -z "$REBASEBOT_GIT_USERNAME" || -z "$REBASEBOT_GIT_EMAIL" ]]; then
        author_flag=()
    else
        author_flag=(--author="$REBASEBOT_GIT_USERNAME <$REBASEBOT_GIT_EMAIL>")
    fi

    if [[ -n $(git status --porcelain) ]]; then
        git add -A
        git commit "${author_flag[@]}" -q -m "UPSTREAM: <drop>: Updating go modules after an upstream rebase"
    fi
}

reset_go_mod_files() {
    while IFS= read -r -d '' go_mod_file; do
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
    done < <(find . -name 'go.mod' -print0)
}

process_go_workspace_updates() {
    echo "Performing go workspace modules update"

    for filename in "go.work" "go.work.sum"; do
        if [[ ! -f "$filename" ]]; then
            continue
        fi
        if ! git checkout "source/$REBASEBOT_SOURCE" -- "$filename"; then
            echo "go.work is downstream only, which is not supported" >&2
            exit 1
        fi
    done

    reset_go_mod_files

    echo "Running go work sync"
    if ! go work sync; then
        echo "Unable to run 'go work sync'" >&2
        exit 1
    fi

    stage_and_commit
}

process_go_mod_updates() {
    echo "Performing go modules update"

    reset_go_mod_files

    while IFS= read -r -d '' go_mod_file; do
        local module_base_path
        module_base_path=$(dirname "$go_mod_file")

        pushd "$module_base_path" > /dev/null || { echo "Failed to cd to $module_base_path" >&2; exit 1; }

        echo "Running go mod tidy for $module_base_path"
        if ! go mod tidy; then
            echo "Unable to run 'go mod tidy' in $module_base_path" >&2
            exit 1
        fi

        popd > /dev/null
    done < <(find . -name 'go.mod' -print0)

    stage_and_commit
}

# Check if the source branch environment variable is set
if [[ -z "$REBASEBOT_SOURCE" ]]; then
    echo "The environment variable REBASEBOT_SOURCE is not set." >&2
    exit 1
fi

if [[ -f "go.work" ]]; then
    process_go_workspace_updates
else
    process_go_mod_updates
fi
