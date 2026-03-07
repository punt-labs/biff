.PHONY: help test lint type check format fuzz prob prfaq clean-tex

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-12s %s\n", $$1, $$2}'

test: ## Run tests (unit + integration)
	uv run pytest

lint: ## Lint and format check
	uv run ruff check .
	uv run ruff format --check .

type: ## Type check with mypy and pyright
	uv run mypy src/ tests/
	uv run pyright

check: lint type test ## Run all quality gates

PROBCLI ?= $(HOME)/Applications/ProB/probcli
PROB_SETSIZE ?= 2
PROB_MAXINT ?= 4
PROB_TIMEOUT ?= 60000
PROB_FLAGS = -p DEFAULT_SETSIZE $(PROB_SETSIZE) -p MAXINT $(PROB_MAXINT) -p TIME_OUT $(PROB_TIMEOUT)

fuzz: ## Type-check a Z spec with fuzz (usage: make fuzz SPEC=docs/foo.tex)
	@fuzz -t "$(SPEC)" > /dev/null
	@echo "fuzz: $(SPEC) OK"

prob: ## Animate and model-check a Z spec with ProB (usage: make prob SPEC=docs/foo.tex)
	@echo "--- init ---"
	@$(PROBCLI) "$(SPEC)" -init $(PROB_FLAGS) 2>&1 | grep -v "^Promoting\|^Z op\|^% given\|fuzz AST\|^Writing"
	@echo "--- animate ---"
	@$(PROBCLI) "$(SPEC)" -animate 20 $(PROB_FLAGS) 2>&1 | grep -E "COVERED|not_covered|Runtime"
	@echo "--- cbc assertions ---"
	@$(PROBCLI) "$(SPEC)" -cbc_assertions $(PROB_FLAGS) 2>&1 | grep -E "counter|ASSERTION"
	@echo "--- cbc deadlock ---"
	@$(PROBCLI) "$(SPEC)" -cbc_deadlock $(PROB_FLAGS) 2>&1 | grep -E "deadlock|DEADLOCK"
	@echo "--- model check ---"
	@$(PROBCLI) "$(SPEC)" -model_check $(PROB_FLAGS) \
		-p MAX_INITIALISATIONS 100 -p MAX_OPERATIONS 5000 2>&1 | \
		grep -E "states|COUNTER|No counter|COVERED|all open|not all"
	@echo "prob: $(SPEC) OK"

format: ## Auto-format code
	uv run ruff check --fix .
	uv run ruff format .

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

clean-tex: ## Remove LaTeX intermediate files
	@rm -f $(LATEX_ARTIFACTS)
