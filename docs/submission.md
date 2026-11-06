# Publish and submit

The requested handoff is a **public GitHub repository link by Friday, November 6, 2026, EOD**. The assessment email does not define a deadline timezone. Keep time for a final repository visibility check.

## Before publishing

```bash
uv sync --locked
make check
make test
uv run --locked quilr-demo
git status --short
```

The repository should contain source, tests, documentation, `.env.example`, `pyproject.toml`, `uv.lock`, the workflow, and the license. Virtual environments, actual `.env` files, SQLite/WAL files, coverage scratch files, and local credentials are excluded by `.gitignore`. Public synthetic fixture tokens and synthetic PII examples are intentional test data.

## Create the public repository

From the repository root, with GitHub CLI authenticated to your intended account:

```bash
gh auth status
```

If this copy does not yet have Git history:

```bash
git init -b main
git add .
git commit -m "Implement QuilrAI assessment gateway lab"
```

Create and push:

```bash
gh repo create quilrai-gateway-lab --public --source=. --remote=origin --push \
  --description "QuilrAI assessment: MCP validation, authorization, streaming PII guardrails, and SQLite token routing"
```

This command publishes the code publicly. Use it only when you are ready to publish. If that name is already taken in your account, choose another name and use the returned URL. If an `origin` remote already exists, inspect it with `git remote -v` and push to that reviewed destination instead of recreating the repository.

Verify:

```bash
gh repo view --json url,visibility
gh run list --limit 3
```

Check the returned URL in a signed-out browser. It must be publicly readable, with the README and source visible. Wait for the workflow to finish and inspect any failure before representing CI as passed. The local verification record does not claim a hosted CI run.

## Share the result

Reply in the existing QuilrAI assessment email thread with the actual public repository URL. A compact submission message:

```text
Hi QuilrAI team,

Here is my completed AI Solutions Engineer assessment:
[paste the verified public repository URL]

The README maps all four tasks to the implementation and tests. A single local demo exercises the MCP server, tool authorization, streamed PII redaction, and model fallback without external API keys. I also included the accounting decisions and security trade-offs so the review can go straight to the interesting parts.

Best,
Shiva Reddy
```

Replace the bracketed URL before sending. This repository does not send any email or contact the assessment team.
