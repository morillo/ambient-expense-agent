.PHONY: install playground test lint generate-traces grade

install:
	@echo "Installing dependencies..."
	uv sync

playground:
	@echo "Launching the local web service..."
	uv run python expense_agent/fast_api_app.py

test:
	@echo "Running tests..."
	uv run pytest

lint:
	@echo "Running linter and style checks..."
	uv run agents-cli lint

generate-traces:
	@echo "Generating evaluation traces..."
	uv run python tests/eval/generate_traces.py

grade:
	@echo "Grading generated traces..."
	uv run agents-cli eval grade --traces artifacts/traces/generated_traces.json --config tests/eval/eval_config.yaml
