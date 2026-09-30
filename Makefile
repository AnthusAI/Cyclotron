PYTHON ?= python

.PHONY: test demo install-tools release

test:
	$(PYTHON) -m pytest -q

demo:
	$(PYTHON) -m decision_flywheel.demo --output demo-output

install-tools:
	$(PYTHON) -m pip install -e '.[tools]'

release:
	semantic-release version
