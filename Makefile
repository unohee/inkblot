PY ?= python3
RUN = PYTHONPATH=src $(PY) -m harness_eval.cli

.PHONY: help lint codex-audit codex-skills drift test selftest codex-selftest eval codex-eval report clean

help:
	@echo "make lint      - static no-op detection over the live harness (no API calls, free)"
	@echo "make codex-audit - audit every installed Codex skill and setup surface (free)"
	@echo "make drift     - detect harness files that changed since case extraction (free)"
	@echo "make test      - unit tests for checkers/stats/case loading (no API calls)"
	@echo "make selftest  - validate the ablation mechanism itself (few small API calls)"
	@echo "make codex-selftest - validate Codex AGENTS.md and skill isolation"
	@echo "make codex-skills - activation-check every installed Codex skill"
	@echo "make eval      - run every enabled case (real API calls; honours CASES=, N=, MODELS=)"
	@echo "make report    - re-render the report for the latest run"
	@echo ""
	@echo "  make eval CASES=commit-language N=6 MODELS=opus,sonnet"

lint:
	@$(RUN) lint

codex-audit:
	@$(RUN) audit

drift:
	@$(RUN) drift

test:
	@$(PY) -m pytest

selftest:
	@$(RUN) selftest

codex-selftest:
	@$(RUN) --suite codex selftest

codex-skills:
	@$(RUN) skills $(if $(CASES),--cases $(CASES),) $(if $(N),-n $(N),) $(if $(MODELS),--models $(MODELS),) $(if $(RESUME),--resume $(RESUME),)

eval:
	@$(RUN) run $(if $(CASES),--cases $(CASES),) $(if $(N),-n $(N),) $(if $(MODELS),--models $(MODELS),)

codex-eval:
	@$(RUN) --suite codex run $(if $(CASES),--cases $(CASES),) $(if $(N),-n $(N),) $(if $(MODELS),--models $(MODELS),)

report:
	@$(RUN) report

clean:
	@rm -rf .pytest_cache .ruff_cache src/*.egg-info
	@find . -name __pycache__ -type d -prune -exec rm -rf {} +
