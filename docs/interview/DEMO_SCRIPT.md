# Five-minute demo script

The demo runs the **real API, database, nginx configs and web build** with stand-in model providers (an extractive answerer, deterministic embeddings, loopback Kafka, in-memory object store). Say this out loud at the start: answers are echoes of the document text, because no real model is attached. The demo proves wiring, permissions and UX, not answer quality.

## Setup (once, on a machine with Postgres+pgvector, Redis, Node 22, nginx)
1. Create a disposable database whose name contains `test`, enable `vector`, `alembic upgrade head` (see `tests/README.md`).
2. `cd apps/web && npm ci && npm run build`.
3. Run the browser tests once to confirm the stack works: `pytest tests/e2e` (they start the whole stack, seed users with random passwords, and tear everything down).
For an interactive session, start `python -m tests.e2e.harness --port 8766 --state-file state.json` (test database only: it refuses anything else), read the seeded credentials from `state.json`, and put a gateway nginx in front as `tests/e2e/conftest.py` does. Stop the harness before running any other tests.

## Script
1. **Sign in** as the tenant-A analyst. Point out the password is random per run and never in the repo.
2. **Upload** a short policy document; watch it go from uploaded to Ready (relay, consumer, parse, chunk, embed, index).
3. **Ask** "What is the refund policy for customers?" and show the answer, its citation and the routing explanation.
4. **Isolation:** sign out, sign in as tenant B, ask the same question: it abstains and shows no tenant-A content.
5. **Roles:** as admin show Usage (set a daily limit, reload, it persists) and Audit (verify the chain); as analyst show those menus are absent and the API returns 403.
6. **Honesty slide:** open `docs/operations/PHASE_43_PRODUCTION_READINESS.md` and read the verdict (no-go) and the top three blockers.

## If something fails live
Say what failed and why you believe it failed; check `docs/troubleshooting/TROUBLESHOOTING.md`. Do not retry silently.
