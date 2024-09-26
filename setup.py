# pylint: skip-file
import setuptools

setuptools.setup(
   name='rebasebot',
   version='0.0.1',
   description='A tool to sync downstream repositories with their upstream ',
   author='Mikhail Fedosin',
   author_email='mfedosin@redhat.com',
   packages=['rebasebot'],
   install_requires=['cryptography', 'gitpython', 'github3.py', 'requests', 'validators'], #external packages as dependencies
   scripts=['rebasebot/cli.py'],
   include_package_data=True,
   package_data={'rebasebot': ['builtin-hooks/*']}
)
