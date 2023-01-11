#!/bin/sh

wd=$(dirname $0)/..

pip install --user -r "${wd}"/requirements-hacking.txt
ln -sf ../../hacking/lint "${wd}"/.git/hooks/pre-commit
