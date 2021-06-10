PYTHON=python3

.PHONY: install-all-deps
install-all-deps:
	$(PYTHON) -m pip install -r requirements-hacking.txt
	$(PYTHON) -m pip install .

.PHONY: unittests
unittests:
	$(PYTHON) -c 'import unittest; \
		      suite=unittest.TestLoader().discover("./src"); \
		      unittest.TextTestRunner().run(suite)'

.PHONY: lint
lint:
	./hacking/lint


.PHONY: reformat
reformat:
	./hacking/lint --reformat
