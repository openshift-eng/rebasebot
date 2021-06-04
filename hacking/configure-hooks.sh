#!/bin/sh

wd=$(dirname $0)/..

pip install --user -r "${wd}"/requirements-hacking.txt
ln -sf ../../hacking/pre-commit "${wd}"/.git/hooks/pre-commit
