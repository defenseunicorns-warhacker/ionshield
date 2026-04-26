# Contributing to IonShield

Thank you for your interest in improving IonShield.  
This guide covers the development workflow, coding standards, and how to submit changes.

---

## Table of contents

1. [Development setup](#1-development-setup)
2. [Project structure](#2-project-structure)
3. [Running tests](#3-running-tests)
4. [Coding standards](#4-coding-standards)
5. [Submitting a pull request](#5-submitting-a-pull-request)
6. [Commit message format](#6-commit-message-format)
7. [Reporting bugs](#7-reporting-bugs)
8. [Security vulnerabilities](#8-security-vulnerabilities)

---

## 1. Development setup

### Prerequisites

| Tool | Version | Purpose |
|------|---------|---------|
| Python | 3.11+ | Backend |
| Node.js | 20+ | Frontend build & tests |
| npm | 10+ | Frontend package management |
| Git | any | Version control |

### Clone and install

```bash
git clone https://github.com/your-org/ionshield-backend.git
cd ionshield-backend

# Python backend
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Frontend
cd frontend && npm install && cd ..

# Environment variables (copy and edit)
cp .env.example .env
```

### Start the dev servers

```bash
# Terminal 1 — FastAPI backend (http://localhost:8000)
uvicorn app.main:app --reload

# Terminal 2 — Vite frontend dev server (http://localhost:5173)
cd frontend && npm run dev
```

The Vite dev server proxies `/api/*` requests to the backend, so you only
need to work in one browser tab at `http://localhost:5173`.

---

## 2. Project structure

```
ionshield-backend/
├── app/                    # FastAPI application
│   ├── api/                # Route handlers (routes.py = v1, routes_v2.py = v2)
│   ├── data/               # Business logic (noaa.py, decision.py, archiver.py)
│   ├── outputs/            # Export formatters (KML, GeoJSON, CoT)
│   ├── pages/              # Marketing HTML pages
│   └── static/             # Built frontend assets (gitignored except index.html)
├── frontend/               # React + CesiumJS SPA
│   ├── src/
│   │   ├── components/     # Globe, Panel, Header, TimelineSlider, …
│   │   ├── hooks/          # useStatus, useDecision, useLayers, …
│   │   ├── utils/          # api.js, cesiumHelpers.js, riskColors.js
│   │   └── __tests__/      # Vitest unit tests
│   └── e2e/                # Playwright end-to-end tests
├── tests/                  # pytest backend tests
├── docs/                   # Documentation
├── .github/workflows/      # CI/CD (ci.yml)
└── Dockerfile              # Multi-stage Docker build
```

---

## 3. Running tests

### Backend (pytest)

```bash
# All backend tests
pytest tests/ -v

# With coverage
pytest tests/ --cov=app --cov-report=term-missing
```

### Frontend unit tests (Vitest)

```bash
cd frontend

# Run once
npm test

# Watch mode
npm run test:watch

# Coverage
npm run coverage
```

### End-to-end tests (Playwright)

```bash
# Start the backend first, then:
cd frontend

# Install browsers (first time)
npx playwright install --with-deps

# Run all E2E tests
npx playwright test

# Run with UI (headed)
npx playwright test --headed

# Run a specific file
npx playwright test e2e/dashboard.spec.js
```

### Lint (ruff)

```bash
# Check
ruff check .

# Auto-fix
ruff check --fix .

# Format
ruff format .
```

### All checks in one go (mirrors CI)

```bash
ruff check . && ruff format --check . && \
pytest tests/ -v && \
cd frontend && npm test && npm run build
```

---

## 4. Coding standards

### Python

- **Formatter / linter:** [ruff](https://github.com/astral-sh/ruff) (config in `pyproject.toml`)
- **Type hints:** use them on all public function signatures
- **Async:** prefer `async def` for I/O-bound routes; use `asyncio.run` only at the top level
- **Error handling:** raise `HTTPException` with a descriptive `detail` string; never return `None` for errors
- **Tests:** every new endpoint gets at least one happy-path and one error-path pytest test

### JavaScript / React

- **Formatter / linter:** ESLint + Prettier (configured in `frontend/.eslintrc.cjs`)
- **React hooks:** follow the [Rules of Hooks](https://react.dev/warnings/invalid-hook-call-warning) — never call hooks conditionally or inside loops; never place an early `return` between two hook calls
- **Components:** functional components only; no class components
- **State:** keep state as close to the consumer as possible; lift only when multiple siblings need it
- **Tests:** Vitest + React Testing Library for unit/component tests; Playwright for E2E
- **CesiumJS:** all Cesium API calls must be guarded with `viewerRef.current?.` — the viewer may not be mounted yet

### CSS

- Custom properties (CSS variables) for all colours — defined in `frontend/src/index.css`
- No inline `style` colour values; use `var(--risk-caution)` etc.
- BEM-style class names for new components

### Commit hygiene

- Keep commits focused — one logical change per commit
- All tests must pass before opening a PR
- Do not commit `frontend/node_modules/`, `.env`, `*.key`, `ionshield.db`, or built assets

---

## 5. Submitting a pull request

1. **Fork** the repo (external contributors) or create a **feature branch** (team members):
   ```bash
   git checkout -b feat/my-feature
   ```

2. Make your changes, following the coding standards above.

3. **Add or update tests** for any changed behaviour. CI will fail without them.

4. Run the full local check suite (see §3) and fix any failures.

5. **Push** and open a pull request against `main`:
   - Fill in the PR template (summary, test plan, screenshots if UI change)
   - Link any related issue with `Closes #NNN`

6. A maintainer will review within 2 business days. Expect at least one round of
   feedback before merge.

7. Squash-merge is preferred for feature branches; rebase-merge for fixes.

### PR checklist

- [ ] Tests added / updated
- [ ] `ruff check .` passes with no errors
- [ ] `npm test` passes (Vitest)
- [ ] `npm run build` succeeds (no Vite build errors)
- [ ] Docs updated if behaviour changed (check `docs/`)
- [ ] No secrets or generated files committed

---

## 6. Commit message format

We follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <short summary>

<optional body — wrap at 72 chars>

<optional footer — Closes #NNN, Co-Authored-By: …>
```

**Types:**

| Type | Use for |
|------|---------|
| `feat` | New feature |
| `fix` | Bug fix |
| `docs` | Documentation only |
| `test` | Adding or fixing tests |
| `refactor` | Code restructuring without behaviour change |
| `perf` | Performance improvement |
| `ci` | CI/CD pipeline changes |
| `chore` | Dependency updates, tooling |

**Examples:**

```
feat(globe): auto-fly camera to newly placed waypoint
fix(elevation-profile): move early-return after all useMemo hooks
docs(dashboard): rewrite guide for React/CesiumJS SPA
test(elevation-profile): add regression tests for hooks ordering
```

---

## 7. Reporting bugs

1. Search [existing issues](https://github.com/your-org/ionshield-backend/issues) first
2. Open a new issue with the **Bug report** template
3. Include:
   - **Steps to reproduce** (exact sequence)
   - **Expected behaviour**
   - **Actual behaviour** (include any console errors)
   - **Environment** (OS, browser, Node/Python version)
   - Screenshots or screen recordings if the bug is visual

---

## 8. Security vulnerabilities

**Do not open a public issue for security vulnerabilities.**

Email `security@ionshield.io` with:
- A description of the vulnerability
- Steps to reproduce
- Potential impact

We aim to acknowledge reports within 48 hours and patch critical issues within 7 days.
