from cli import parse_cli_arguments
from merge_bot import commit_go_mod_updates

import unittest
import os
from git import Repo

valid_args = [
    "--source-repo",
    "https://opendev.org/openstack/kuryr-kubernetes",
    "--source-branch",
    "master",
    "--dest-repo",
    "https://github.com/openshift/kuryr-kubernetes",
    "--dest-branch",
    "master",
    "--merge-branch",
    "merge_branch",
    "--bot-name",
    "test",
    "--bot-email",
    "test@email.com",
    "--working-dir",
    "tmp",
    "--github-key",
    "/credentials/gh-key",
    "--slack-webhook",
    "/credentials/slack-webhook",
    "--update-go-modules",
]

invalid_url_args = [
    "--source-repo",
    "opendev.org/openstack/kuryr-kubernetes",
    "--source-branch",
    "master",
    "--dest-repo",
    "https://github/openshift/kuryr-kubernetes",
    "--dest-branch",
    "master",
    "--merge-branch",
    "merge_branch",
    "--bot-name",
    "test",
    "--bot-email",
    "test@email.com",
    "--working-dir",
    "tmp",
    "--github-key",
    "/credentials/gh-token",
    "--slack-webhook",
    "/credentials/slack-webhook",
]

working_dir = os.getcwd()


def make_golang_repo(tmp_dir):
    test_file = os.path.join(tmp_dir, "test.go")
    script = """
package main
import (
	"k8s.io/klog/v2"
)
func main() {
	klog.Errorln("This is a test")
	return
}
"""

    # Create testing directory and files
    os.mkdir(tmp_dir)
    f = open(test_file, "x")
    f.write(script)
    f.close()
    return Repo.init(tmp_dir)


class test_cli(unittest.TestCase):
    def test_valid_cli_argmuents(self):
        args, errors = parse_cli_arguments(valid_args)

        # sanity checks
        self.assertEqual(
            args.source_repo, "https://opendev.org/openstack/kuryr-kubernetes"
        )
        self.assertEqual(args.source_branch, "master")
        self.assertEqual(
            args.dest_repo, "https://github.com/openshift/kuryr-kubernetes"
        )
        self.assertEqual(args.dest_branch, "master")
        self.assertEqual(args.bot_email, "test@email.com")
        self.assertEqual(args.working_dir, "tmp")
        self.assertEqual(args.github_key, "/credentials/gh-key")
        self.assertEqual(args.slack_webhook, "/credentials/slack-webhook")
        self.assertEqual(args.update_go_modules, True)

        # error checks
        self.assertEqual(errors, [])

    def test_invalid_url(self):
        _, errors = parse_cli_arguments(invalid_url_args)
        self.assertEqual(
            errors,
            [
                "the value for `--source-repo`, opendev.org/openstack/kuryr-kubernetes, is not a valid URL",
                "the value for `--dest-repo`, https://github/openshift/kuryr-kubernetes, is not a valid URL",
            ],
        )


class test_go_mod(unittest.TestCase):
    def test_update_and_commit(self):
        tmp_dir = os.path.join(os.getcwd(), "tmp")
        repo = make_golang_repo(tmp_dir)

        os.chdir(tmp_dir)
        os.system("go mod init example.com/foo")
        repo.git.add(all=True)
        repo.git.commit("-m", "Initial commit")

        try:
            commit_go_mod_updates(repo)
        except Exception as err:
            self.assertEqual(str(err), "")
        else:
            self.assertTrue(
                repo.active_branch.is_valid(),
                "A commit was not made to add the changes to the repo.",
            )
            commits = list(repo.iter_commits())
            self.assertEqual(len(commits), 2)
            self.assertEqual(
                commits[0].message,
                "Updating and vendoring go modules after an upstream merge.\n",
            )
        finally:
            # clean up
            os.chdir(working_dir)
            os.system("rm -rf " + str(tmp_dir))

    # Test how the function handles an empty commit. This should not error out and exit if working properly.
    def test_update_and_commit_empty(self):
        tmp_dir = os.path.join(os.getcwd(), "tmp")
        repo = make_golang_repo(tmp_dir)

        os.chdir(tmp_dir)
        os.system("go mod init example.com/foo")
        os.system("go mod tidy")
        os.system("go mod vendor")
        repo.git.add(all=True)
        repo.git.commit("-m", "Initial commit")

        try:
            commit_go_mod_updates(repo)
        except Exception as err:
            self.assertEqual(str(err), "")
        else:
            commits = list(repo.iter_commits())
            self.assertEqual(len(commits), 1)
            self.assertEqual(commits[0].message, "Initial commit\n")
        finally:
            # clean up
            os.chdir(working_dir)
            os.system("rm -rf " + str(tmp_dir))


if __name__ == "__main__":
    unittest.main()
