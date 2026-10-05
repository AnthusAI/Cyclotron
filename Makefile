PYTHON ?= python
REVIEWER_BATCH ?= var/arxiv-review.jsonl
REVIEWER_DATABASE ?= var/reviewer.sqlite3
REVIEWER_LIMIT ?= 50

.PHONY: test demo review review-arxiv install-tools release

test:
	$(PYTHON) -m pytest -q

demo:
	$(PYTHON) -m decision_flywheel.demo --output demo-output

review:
	$(PYTHON) -m decision_flywheel.reviewer --database "$(REVIEWER_DATABASE)"

review-arxiv:
	$(PYTHON) scripts/seed_arxiv_reviewer.py --output "$(REVIEWER_BATCH)" --limit "$(REVIEWER_LIMIT)"
	$(PYTHON) -m decision_flywheel.reviewer --database "$(REVIEWER_DATABASE)" --articles "$(REVIEWER_BATCH)"

install-tools:
	$(PYTHON) -m pip install -e '.[tools]'

release:
	semantic-release version
