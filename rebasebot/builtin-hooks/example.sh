#!/bin/bash

set -e  # Exit immediately if a command exits with a non-zero status
set -o pipefail  # Return the exit status of the last command in the pipe that failed

# This is a example script that can be set up as a hook to inject custom logic into the rebasebot process.
# To run this script add `--post-init-hook _BUILTIN_/example.sh` to the rebasebot command.
echo "Hello, from a lifecyclehook!"

echo "Available rebasebot environment variables"
echo "REBASEBOT_SOURCE: ${REBASEBOT_SOURCE}"
echo "REBASEBOT_DEST: ${REBASEBOT_DEST}"
echo "REBASEBOT_REBASE: ${REBASEBOT_REBASE}"
echo "REBASEBOT_WORKING_DIR: ${REBASEBOT_WORKING_DIR}"
echo "REBASEBOT_GIT_USERNAME: ${REBASEBOT_GIT_USERNAME}"
echo "REBASEBOT_GIT_EMAIL: ${REBASEBOT_GIT_EMAIL}"
