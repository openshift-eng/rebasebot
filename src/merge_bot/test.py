import unittest
import os
from git import Repo

from . import cli
from . import merge_bot


valid_args = {
    # XXX(mdbooth): By assuming everything is a GitHub branch I've broken the
    # original Kuryr use case where upstream is:
    #   https://opendev.org/openstack/kuryr-kubernetes:master
    # We don't use any GitHub-specific features on the source, so this should
    # be easy to fix.
    "source": "openstack/kuryr-kubernetes:master",
    "dest": "openshift/kuryr-kubernetes:master",
    "merge": "shiftstack/kuryr-kubernetes:merge-bot-master",
    "bot-name": "test",
    "bot-email": "test@email.com",
    "working-dir": "tmp",
    "github-app-key": "/credentials/gh-app-key",
    "github-oauth-token": "/credentials/gh-oauth-token",
    "slack-webhook": "/credentials/slack-webhook",
    "update-go-modules": None,
}


def args_dict_to_list(args_dict):
    args = []
    for k, v in args_dict.items():
        args.append(f"--{k}")
        if v is not None:
            args.append(v)
    return args


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
        args = cli.parse_cli_arguments(args_dict_to_list(valid_args))

        # sanity checks
        self.assertEqual(args.source.ns, "openstack")
        self.assertEqual(args.source.name, "kuryr-kubernetes")
        self.assertEqual(args.source.branch, "master")
        self.assertEqual(args.dest.ns, "openshift")
        self.assertEqual(args.dest.name, "kuryr-kubernetes")
        self.assertEqual(args.dest.branch, "master")
        self.assertEqual(args.merge.ns, "shiftstack")
        self.assertEqual(args.merge.name, "kuryr-kubernetes")
        self.assertEqual(args.merge.branch, "merge-bot-master")
        self.assertEqual(args.bot_email, "test@email.com")
        self.assertEqual(args.working_dir, "tmp")
        self.assertEqual(args.github_app_key, "/credentials/gh-app-key")
        self.assertEqual(args.github_oauth_token, "/credentials/gh-oauth-token")
        self.assertEqual(args.slack_webhook, "/credentials/slack-webhook")
        self.assertEqual(args.update_go_modules, True)

    def test_invalid_branch(self):
        for branch in ("dest", "source", "merge"):
            invalid_args = valid_args.copy()
            invalid_args[branch] = "invalid"

            with self.assertRaises(SystemExit):
                cli.parse_cli_arguments(args_dict_to_list(invalid_args))


class test_go_mod(unittest.TestCase):
    def test_update_and_commit(self):
        tmp_dir = os.path.join(os.getcwd(), "tmp")
        repo = make_golang_repo(tmp_dir)

        os.chdir(tmp_dir)
        os.system("go mod init example.com/foo")
        repo.git.add(all=True)
        repo.git.commit("-m", "Initial commit")

        try:
            merge_bot.commit_go_mod_updates(repo)
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
                "Updating and vendoring go modules after an upstream merge\n",
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
            merge_bot.commit_go_mod_updates(repo)
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
