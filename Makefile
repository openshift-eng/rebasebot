PYTHON=python3

.PHONY: deps
deps:
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install black coverage flake8 flit mccabe mypy pylint pytest tox tox-gh-actions
	$(PYTHON) -m pip install -r requirements.txt

.PHONY: unittests
unittests:
	$(PYTHON) -c 'import unittest; \
		      suite=unittest.TestLoader().discover("./rebasebot"); \
		      unittest.TextTestRunner().run(suite)'

.PHONY: lint
lint:
	$(PYTHON) -m flake8 --max-line-length=99 rebasebot
	$(PYTHON) -m pylint rebasebot

.PHONY: venv
venv:
	$(PYTHON) -m venv env
