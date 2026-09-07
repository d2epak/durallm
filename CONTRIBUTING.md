# Contributing to LLM Circuit Breaker

Thank you for your interest in contributing to **LLM Circuit Breaker**! We welcome bug fixes, performance improvements, new provider adapters, and documentation updates.

---

## Development Setup

### Prerequisites
- Python 3.10+
- `uv` (recommended) or standard `pip` / `venv`

### Setup Instructions
1. Clone the repository:
   ```bash
   git clone https://github.com/d2epak/llm-circuit-breaker.git
   cd llm-circuit-breaker
   ```

2. Create and activate a virtual environment:
   ```bash
   uv venv
   source .venv/bin/activate
   ```

3. Install dependencies including test and development packages:
   ```bash
   uv pip install -e ".[dev]"
   ```

---

## Code Quality & Testing

All pull requests must pass the test suite, linting, and type checking before being merged.

### Run Tests
```bash
pytest
```
To run tests with coverage reporting:
```bash
pytest --cov=llm_circuit_breaker --cov-report=term-missing
```

### Code Formatting and Linting
We use [ruff](https://astral.sh/ruff) for linting and formatting:
```bash
ruff check .
ruff format . --check
```

### Type Checking
We enforce strict typing with `mypy`:
```bash
mypy src/llm_circuit_breaker
```

---

## Submitting Pull Requests

1. Create a descriptive feature branch:
   ```bash
   git checkout -b feat/your-feature-name
   ```
2. Commit your changes following conventional commit conventions (`feat:`, `fix:`, `docs:`, `chore:`).
3. Ensure all tests and lint checks pass.
4. Open a pull request against the `main` branch with a clear description of the changes and motivation.
