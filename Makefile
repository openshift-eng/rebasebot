PYTHON=python3

.PHONY: deps
deps:
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install --upgrade -r requirements-hacking.txt

.PHONY: unittests
unittests:
	hack/tests.sh

.PHONY: lint
lint:
	$(PYTHON) -m flake8 --max-line-length=120 rebasebot tests
	$(PYTHON) -m pylint rebasebot tests
	$(PYTHON) -m mypy rebasebot tests --no-strict-optional --ignore-missing-imports

.PHONY: venv
venv:
	$(PYTHON) -m venv env

install:
	$(PYTHON) -m pip install --user .
