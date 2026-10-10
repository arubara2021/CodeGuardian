# CodeGuardian task runner. Every recipe maps to a step in the local
# development loop or in CI. Run `just` with no arguments to list recipes.
#
# A POSIX shell is required. On Windows, install Git for Windows (which ships
# sh.exe and bash.exe) or run inside WSL2.

set shell := ["sh", "-eu", "-c"]

default:
    @just --list

install:
    python -m pip install --upgrade pip
    python -m pip install -e ".[dev,docs]"
    pre-commit install

install-hooks:
    pre-commit install

uninstall:
    python -m pip uninstall -y codeguardian

lint-python:
    ruff check python/ tests/ scripts/
    ruff format --check python/ tests/ scripts/
    mypy python/codeguardian

lint-rust:
    cargo fmt --all -- --check
    cargo clippy --workspace --all-targets -- -D warnings

lint: lint-python lint-rust

format-python:
    ruff check --fix python/ tests/ scripts/
    ruff format python/ tests/ scripts/

format-rust:
    cargo fmt --all

format: format-python format-rust

test-python:
    pytest tests/unit/ tests/contracts/ tests/integration/ -v

test-property:
    pytest tests/unit/ -v -m property

test-rust:
    cargo test --workspace --all-features

test: test-python test-rust

test-e2e:
    pytest tests/e2e/ -v

test-all: test test-e2e

coverage:
    pytest tests/ --cov=codeguardian --cov-report=term-missing --cov-report=xml

build:
    maturin develop --release

build-debug:
    maturin develop

benchmark:
    python scripts/benchmark.py

validate-schemas:
    python scripts/validate_schemas.py

doctor:
    codeguardian doctor

docs-serve:
    mkdocs serve

docs-build:
    mkdocs build --strict

clean:
    rm -rf build dist target site
    rm -rf .pytest_cache .mypy_cache .ruff_cache .hypothesis htmlcov
    rm -rf .coverage coverage.xml
    find . -type d -name __pycache__ -prune -exec rm -rf {} \;
    find . -type d -name '*.egg-info' -prune -exec rm -rf {} \;

ci: lint test test-e2e
