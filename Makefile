.PHONY: help test lint type mdlint check check-oo update-oo check-coupling update-coupling check-suppressions update-suppressions check-layers update-layers require-full-ratchet-source report format build install clean depot lock-clean fuzz prob prob-session prfaq clean-tex

ifneq ($(filter environment%,$(origin SRC)),)
override undefine SRC
endif
.DEFAULT_GOAL := help
SRC ?= src/biff/
# Plain "=" so an environment value such as LAYERS_BASE=--allow-config-change
# cannot waive a gate; only the command line or the CI step sets these.
OO_BASE =
COUPLING_BASE =
SUPPRESSION_BASE =
LAYERS_BASE =
DESIGN_BASE ?= origin/main
override export DESIGN_BASE := $(value DESIGN_BASE)

.PHONY: design-shadow

design-shadow: ## Report advisory design changes; never part of check
	uv run oo-ratchet design "$(SRC)" --shadow-check "$${DESIGN_BASE}"

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-12s %s\n", $$1, $$2}'

test: ## Run the default suite (unit + integration)
	uv run pytest

hooks: ## Install the repository's pre-commit and pre-push test hooks
	@if path=$$(git config --get core.hooksPath) && [ "$$path" != .githooks ]; then \
		echo "core.hooksPath already set to $$path; chain .githooks manually" >&2; exit 1; \
	fi
	git config --local core.hooksPath .githooks

test-changed: ## Run default tests affected by staged paths (pre-commit)
	uv run python tools/test_changed.py

test-push: test ## Run the pre-push gate

test-unit: ## Run the default suite without the integration directory
	uv run pytest --ignore=tests/test_integration

test-integration: ## Run in-process MCP integration tests
	uv run pytest tests/test_integration

test-subprocess: ## Run real biff subprocesses over stdio
	uv run pytest -m 'subprocess and not quarantine'

test-cli-multi-user: ## Run CLI multi-user tests over a local nats-server
	uv run pytest -m 'cli_multi_user and not quarantine'

test-nats-local: ## Run MCP and relay tests over a local nats-server
	uv run pytest -m 'nats_local and not quarantine'

test-docker: ## Build the relay image and run tests against it
	uv run pytest -m 'nats_docker and not quarantine'

test-e2e: test-wheel test-cli-multi-user test-nats-local ## Run installed-wheel and local NATS end-to-end tests

test-nightly: ## Run broader credential-free suites, reporting every suite
	@status=0; for suite in test-subprocess test-cli-multi-user test-nats-local; do \
		$(MAKE) --no-print-directory $$suite || status=1; \
	done; exit $$status

test-quarantine: ## List quarantines; an empty selection is expected
	@uv run pytest --collect-only -m quarantine || [ "$$?" = 5 ]

test-live: ## Run hosted NATS tests (requires BIFF_TEST_NATS_URL and auth)
	@test -n "$${BIFF_TEST_NATS_URL:-}" -a -n "$${BIFF_TEST_NATS_TOKEN:-}$${BIFF_TEST_NATS_NKEYS_SEED:-}$${BIFF_TEST_NATS_CREDS:-}" || { echo "Set BIFF_TEST_NATS_URL and one BIFF_TEST_NATS_* auth variable" >&2; exit 1; }
	uv run pytest -m 'nats_hosted and not stress and not quarantine'

test-stress: ## Run hosted NATS scale tests (same prerequisites as test-live)
	@test -n "$${BIFF_TEST_NATS_URL:-}" -a -n "$${BIFF_TEST_NATS_TOKEN:-}$${BIFF_TEST_NATS_NKEYS_SEED:-}$${BIFF_TEST_NATS_CREDS:-}" || { echo "Set BIFF_TEST_NATS_URL and one BIFF_TEST_NATS_* auth variable" >&2; exit 1; }
	uv run pytest -m 'stress and not quarantine'

test-agent: ## Drive real Claude sessions through biff (requires ANTHROPIC_API_KEY)
	@test -n "$${ANTHROPIC_API_KEY:-}" || { echo "Set a scoped ANTHROPIC_API_KEY" >&2; exit 1; }
	uv run pytest -m 'sdk and not quarantine'

.PHONY: hooks test-changed test-push test-unit test-integration test-subprocess test-cli-multi-user test-nats-local test-docker test-e2e test-nightly test-quarantine test-live test-stress test-agent

smoke-release: ## Build a wheel and smoke-test it installed in a clean environment
	rm -rf .tmp/smoke-dist
	uv build --wheel --out-dir .tmp/smoke-dist
	bash scripts/smoke-release.sh candidate "$$(uv version --short)" .tmp/smoke-dist/*.whl

.PHONY: smoke-release

test-wheel: smoke-release ## Run the installed CLI and MCP round-trip against a freshly built wheel

# pytest-cov conflicts with beartype's import hooks, so drive coverage directly.
coverage: ## Test line and branch coverage with an HTML report
	uv run coverage run -m pytest
	uv run coverage html
	uv run coverage report

coverage-ci: ## Test line and branch coverage with a CI XML report
	uv run coverage run -m pytest
	uv run coverage xml
	uv run coverage report

.PHONY: test-wheel coverage coverage-ci

lint: check-comments check-docstrings ## Lint and format check (ruff + shellcheck)
	uv run ruff check .
	uv run ruff format --check .
	shellcheck plugin/hooks/*.sh install.sh scripts/*.sh .githooks/*
	uv run python -c 'import json, subprocess; hook = json.load(open("devin-plugin/hooks.json"))["SessionStart"][0]["hooks"][0]["command"]; subprocess.run(["shellcheck", "--shell=sh", "-"], input=hook, text=True, check=True)'

check-comments: ## Reject commented-out code and tracker references in Python prose
	uv run ruff check --select ERA001 src tests tools scripts
	uv run python tools/check_python_docs.py comments src tests tools scripts

check-docstrings: ## Check public Python docstrings and documented argument names
	uv run ruff check --select D,D401 --ignore D105,D107,D417 src/
	uv run python tools/check_python_docs.py docstrings src/

build-api-docs: ## Build and verify the generated Python API reference
	uv run --group docs mkdocs build --strict
	uv run python tools/check_python_docs.py api .tmp/api-site/objects.inv

.PHONY: check-comments check-docstrings build-api-docs

type: ## Type check with mypy and pyright
	uv run mypy src/ tests/ tools/
	uv run pyright

mdlint: ## Lint markdown files (matches .github/workflows/docs.yml)
	npx --yes markdownlint-cli2 "**/*.md" "#node_modules"

check: lint type mdlint test check-oo check-coupling check-suppressions check-layers ## Run all quality gates

check-oo: ## Check the OO must-improve ratchet
	uv run oo-ratchet oo "$(SRC)" --check $(OO_BASE)

update-oo: ## Update touched OO baseline entries in SRC
	uv run oo-ratchet oo "$(SRC)" --update $(OO_BASE)

check-coupling: ## Check for coupling and cohesion regressions
	uv run oo-ratchet coupling "$(SRC)" --check $(COUPLING_BASE)

update-coupling: require-full-ratchet-source ## Update the coupling baseline with the full import graph
	uv run oo-ratchet coupling "$(SRC)" --update $(COUPLING_BASE)

check-suppressions: ## Check for new lint and type suppressions
	uv run oo-ratchet suppressions "$(SRC)" --check $(SUPPRESSION_BASE)

update-suppressions: require-full-ratchet-source ## Update the suppression baseline for the full source tree
	uv run oo-ratchet suppressions "$(SRC)" --update

# The layer contract covers the biff package only, whatever SRC is.
check-layers: ## Check package imports against the DES-088 layers
	uv run oo-ratchet layers src/biff --check $(LAYERS_BASE)

update-layers: ## Shrink the layer baseline (adopts it on first run)
	uv run oo-ratchet layers src/biff --update

require-full-ratchet-source:
	@test "$(abspath $(SRC))" = "$(abspath src/biff/)" || { \
		echo "Baseline update requires SRC=src/biff/" >&2; exit 1; }

report: ## Advisory design diagnostics (non-failing)
	-uv run oo-ratchet design "$(SRC)"

PROBCLI ?= $(HOME)/Applications/ProB/probcli
PROB_SETSIZE ?= 2
PROB_MAXINT ?= 4
PROB_TIMEOUT ?= 60000
PROB_WALL_TIMEOUT ?=
PROB_LOG_DIR ?= .tmp/prob
fuzz: ## Type-check a Z spec with fuzz (usage: make fuzz SPEC=docs/foo.tex)
	@fuzz -t "$(SPEC)" > /dev/null
	@echo "fuzz: $(SPEC) OK"

prob: ## Animate and model-check a Z spec with ProB (usage: make prob SPEC=docs/foo.tex)
	@uv run python -m biff.prob_gate --probcli "$(PROBCLI)" --spec "$(SPEC)" \
		--setsize $(PROB_SETSIZE) --maxint $(PROB_MAXINT) \
		--timeout $(PROB_TIMEOUT) --log-dir "$(PROB_LOG_DIR)" \
		$(if $(PROB_WALL_TIMEOUT),--wall-timeout "$(PROB_WALL_TIMEOUT)")
	@echo "prob: $(SPEC) OK"

prob-session: ## Validate session-model.tex (fuzz + ProB, setsize 1)
	@make fuzz SPEC=docs/session-model.tex
	@make prob SPEC=docs/session-model.tex PROB_SETSIZE=1
	@echo "prob-session: ALL CHECKS PASSED"

format: ## Auto-format code
	uv run ruff check --fix .
	uv run ruff format .

lock-clean: ## Regenerate uv.lock without local overrides (for CI/release)
	@if [ -f uv.toml ]; then \
	    trap 'mv uv.toml.bak uv.toml 2>/dev/null' EXIT INT TERM; \
	    mv uv.toml uv.toml.bak; \
	    uv lock --upgrade-package punt-oo-ratchet; status=$$?; \
	    mv uv.toml.bak uv.toml; \
	    trap - EXIT INT TERM; \
	    exit $$status; \
	else \
	    uv lock --upgrade-package punt-oo-ratchet; \
	fi

build: ## Build wheel and sdist
	rm -rf dist/
	uv build
	uvx twine check dist/*

install: ## Build wheel and install as CLI tool
	uv build --wheel
	uv tool install --force --find-links ../.depot dist/punt_biff-*.whl

clean: ## Remove build artifacts
	rm -rf dist/ .tmp/

DEPOT := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))../.depot

depot: build ## Build and copy wheel to local depot
	@mkdir -p $(DEPOT)
	@cp dist/*.whl $(DEPOT)/
	@echo "depot: $$(ls dist/*.whl | xargs -n1 basename) -> $(DEPOT)/"

# LaTeX intermediate files to remove after compilation
LATEX_ARTIFACTS = *.aux *.log *.out *.bbl *.bcf *.blg *.run.xml *.fls \
                  *.fdb_latexmk *.synctex.gz *.toc

TEX_FILES = prfaq.tex

prfaq: ## Compile .tex to .pdf and clean artifacts
	@for f in $(TEX_FILES); do \
	  echo "Compiling $$f ..."; \
	  dir=$$(dirname "$$f"); base=$$(basename "$$f" .tex); \
	  rm -f "$$dir/$$base.pdf"; \
	  pdflatex -interaction=nonstopmode -output-directory="$$dir" "$$f" > /dev/null 2>&1; \
	  if [ -f "$$dir/$$base.bib" ] && command -v biber > /dev/null 2>&1; then \
	    (cd "$$dir" && biber "$$base") > /dev/null 2>&1 || true; \
	    pdflatex -interaction=nonstopmode -output-directory="$$dir" "$$f" > /dev/null 2>&1; \
	  fi; \
	  pdflatex -interaction=nonstopmode -output-directory="$$dir" "$$f" > /dev/null 2>&1; \
	  if [ -f "$$dir/$$base.pdf" ]; then \
	    echo "  $$dir/$$base.pdf"; \
	  else \
	    echo "Error: $$f failed to compile" >&2; exit 1; \
	  fi; \
	done
	@rm -f $(LATEX_ARTIFACTS)

clean-tex: ## Remove all LaTeX artifacts (root + docs/)
	@rm -f $(LATEX_ARTIFACTS)
	@rm -f docs/*.aux docs/*.log docs/*.out docs/*.toc docs/*.fuzz \
	       docs/*.synctex.gz docs/*.pdf docs/*.mf docs/*.pk docs/*.tfm
