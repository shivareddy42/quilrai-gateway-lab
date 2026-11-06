# Execution record

Verified locally on **November 5, 2026**, using macOS on Apple Silicon. These are observed results, not expected output presented as evidence.

| Check | Observed result |
| :--- | :--- |
| Locked install, Python 3.11.18 | Passed |
| Full suite, Python 3.11.18 | 148 passed in 6.61 seconds |
| Locked install, Python 3.12.15 | Passed |
| Full suite with subprocess coverage, Python 3.12.15 | 148 passed in 7.01 seconds |
| Statement coverage, Python 3.12.15 | 723 of 748 executable statements, 96.66% |
| Locked install, Python 3.13.18 | Passed |
| Full suite, Python 3.13.18 | 148 passed in 6.31 seconds |
| Ruff lint | Passed |
| Ruff format check | All 20 Python files formatted |
| Source distribution and wheel build | Passed |
| Wheel installed in a separate environment | Passed |
| Demo launched from the installed wheel outside the source checkout | All checks passed |

Commands used for the primary verification:

```bash
uv sync --locked
uv run --locked ruff check src tests
uv run --locked ruff format --check src tests
uv run --locked pytest --cov=quilr_gateway --cov-report=term-missing --cov-fail-under=94
uv build
```

Python 3.11 and 3.13 used separate environments created from the same lockfile, then ran `python -m pytest -q`. Coverage includes subprocess execution through coverage.py's subprocess patch. The total includes the demo, mocks, stdio process, and HTTP application, rather than excluding those entry points from the denominator.

The installed-wheel demo reported:

```text
PASS  Task 1: official SDK client completed a stdio tool call.
PASS  Task 2: viewer blocked, administrator forwarded.
PASS  Task 3: Ready. Email [REDACTED]; SSN [REDACTED]; card [REDACTED]. Café done.
      Observed first safe content: 17.08 ms on this run.
PASS  Task 4: 'normal' -> primary (0.01s)
PASS  Task 4: '[429] force fallback' -> secondary (0.01s)
PASS  Task 4: '[timeout] force fallback' -> secondary (3.01s)
PASS  Provider failure returned a sanitized gateway error.
PASS  Authenticated metrics captured fallback and policy events.
```

The first-safe-content and fallback timings are single-run local observations with synthetic providers. They are not percentiles, load-test results, or measurements of a real model. They will vary with the machine and scheduler.

Hosted GitHub Actions is configured for Linux and Python 3.11, 3.12, and 3.13. No hosted workflow execution is claimed in this local record. No live paid provider, production traffic, or production deployment was tested.

The assessment's Google Doc text export was read and compared with the four-task implementation. The source document and test fixtures contain no actual customer records or real payment operations.
