PYTHON ?= python
REVIEWER_BATCH ?= var/arxiv-review.jsonl
REVIEWER_DATABASE ?= var/reviewer.sqlite3
REVIEWER_LIMIT ?= 250
REVIEWER_REQUESTS ?= 500
OPTIMIZER_CALLS ?= 10
OPTIMIZE_EVERY ?= 10
OPTIMIZER_MODEL ?= gpt-6-luna
DECISIONS_PROVIDER ?= jev
DECISIONS_MODEL ?= jev-1.13.0
EVALUATION_WEIGHTING ?= equal_class
TRAINING_CLASS_WEIGHTING ?= natural
MIN_EVALUATION_PER_CLASS ?= 2
OPTIMIZATION_STAGE ?= rubric
RETROSPECTIVE_LIMIT ?= 200
STAGE_MIN_EVALUATION_PER_CLASS ?= 20
WEB_PORT ?= 8782
WEB_DATABASE ?= var/web/workspace.sqlite3

.PHONY: web web-live
web:
	$(PYTHON) -m decision_flywheel.web_server serve --database "$(WEB_DATABASE)" --articles "$(REVIEWER_BATCH)" --port "$(WEB_PORT)"

web-live:
	$(PYTHON) -m decision_flywheel.web_server serve --database "$(WEB_DATABASE)" --articles "$(REVIEWER_BATCH)" --port "$(WEB_PORT)" --allow-live

.PHONY: test demo review review-local review-arxiv run-flywheel review-live install-tools release

test:
	$(PYTHON) -m pytest -q

.PHONY: build-trace-ui test-trace-ui
build-trace-ui:
	cd trace-ui && npm run build

test-trace-ui:
	cd trace-ui && npm test

.PHONY: diagrams check-diagrams
docs/diagrams/node_modules: docs/diagrams/package-lock.json
	cd docs/diagrams && npm ci
	touch $@

diagrams: docs/diagrams/node_modules
	cd docs/diagrams && node render.mjs

check-diagrams: docs/diagrams/node_modules
	cd docs/diagrams && node render.mjs --check

demo:
	$(PYTHON) -m decision_flywheel.demo --output demo-output

review:
	$(PYTHON) -m decision_flywheel.reviewer --database "$(REVIEWER_DATABASE)" --live-flywheel --confirm-live --max-live-requests "$(REVIEWER_REQUESTS)" --max-optimizer-calls "$(OPTIMIZER_CALLS)" --optimize-every "$(OPTIMIZE_EVERY)" --optimizer-model "$(OPTIMIZER_MODEL)" --decisions-provider "$(DECISIONS_PROVIDER)" --decisions-model "$(DECISIONS_MODEL)" --evaluation-weighting "$(EVALUATION_WEIGHTING)" --training-class-weighting "$(TRAINING_CLASS_WEIGHTING)" --min-evaluation-per-class "$(MIN_EVALUATION_PER_CLASS)" --optimization-stage "$(OPTIMIZATION_STAGE)" --retrospective-limit "$(RETROSPECTIVE_LIMIT)" --stage-min-evaluation-per-class "$(STAGE_MIN_EVALUATION_PER_CLASS)"

review-local:
	$(PYTHON) -m decision_flywheel.reviewer --database "$(REVIEWER_DATABASE)"

review-arxiv:
	$(PYTHON) scripts/seed_arxiv_reviewer.py --output "$(REVIEWER_BATCH)" --limit "$(REVIEWER_LIMIT)"
	$(PYTHON) -m decision_flywheel.reviewer --database "$(REVIEWER_DATABASE)" --articles "$(REVIEWER_BATCH)" --live-flywheel --confirm-live --max-live-requests "$(REVIEWER_REQUESTS)" --max-optimizer-calls "$(OPTIMIZER_CALLS)" --optimize-every "$(OPTIMIZE_EVERY)" --optimizer-model "$(OPTIMIZER_MODEL)" --decisions-provider "$(DECISIONS_PROVIDER)" --decisions-model "$(DECISIONS_MODEL)" --evaluation-weighting "$(EVALUATION_WEIGHTING)" --training-class-weighting "$(TRAINING_CLASS_WEIGHTING)" --min-evaluation-per-class "$(MIN_EVALUATION_PER_CLASS)" --optimization-stage "$(OPTIMIZATION_STAGE)" --retrospective-limit "$(RETROSPECTIVE_LIMIT)" --stage-min-evaluation-per-class "$(STAGE_MIN_EVALUATION_PER_CLASS)"

run-flywheel:
	$(PYTHON) scripts/run_reviewer_flywheel.py --database "$(REVIEWER_DATABASE)" --confirm-live

review-live:
	$(PYTHON) -m decision_flywheel.reviewer --database "$(REVIEWER_DATABASE)" --live-jev --confirm-live

install-tools:
	$(PYTHON) -m pip install -e '.[tools]'

release:
	semantic-release version

.PHONY: pack-ui
# The web package: dist/cyclotron-<version>.tgz, attached to a GitHub release
# as ui-v<version> so applications pin an immutable tarball.
pack-ui:
	mkdir -p dist && npm pack --pack-destination dist
