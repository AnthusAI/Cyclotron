.PHONY: test install-tools release

test:
	python -m pytest -q

install-tools:
	python -m pip install -e '.[tools]'

release:
	semantic-release version
