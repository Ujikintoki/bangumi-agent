# Project instructions

This is the only maintained agent-instruction file for this repository. Keep it short; put product,
status, architecture, and operational detail in their dedicated documents instead of duplicating them here.

## Read before changing

- Product or prompt work: read [`PRODUCT.md`](PRODUCT.md).
- Current priorities and verified status: read [`ROADMAP.md`](ROADMAP.md). Historical Phase documents are not a backlog.
- Cross-layer work: read the four pre-work questions in [`docs/architecture-blueprint.md`](docs/architecture-blueprint.md).
- The July vision is historical context and a long-term north star, not an implementation checklist.

## Working rules

- Treat this repository's `main` checkout as the source of truth. Nested or sanitized repositories are reference material only.
- Preserve uncommitted user work. Inspect the working tree before edits and never apply candidate patches wholesale.
- Keep changes tied to one product problem and one verification path. Do not add capabilities merely because the architecture can support them.
- Maintain the dependency direction: orchestration may depend on persona, memory, and data layers; lower layers must not import Agent state or orchestration code.
- Tools and HTTP clients return structured error values at their boundary. HTTP endpoints may translate them into documented status responses.
- Keep `SystemMessage` outside conversation truncation. When changing persona behavior, inspect both reasoning prompts and final rendering.
- Follow existing Pydantic v2 and structured tool-output conventions. Avoid reviving Critic, L3 memory, or `BangumiChunk` without a new reviewed decision.

## Safety and verification

- Do not read or modify `eval/data`, `eval/results`, `rag/corpus`, `scripts/data`, or `docs/Test/test_output`.
- Do not connect to the real PostgreSQL service on port 5432. Database work requires an explicitly isolated environment.
- Use `scripts/test_offline.py` for the reviewed unit-test set. Excluding one test file does not guarantee database isolation.
- Never print secrets or read `.env` for inspection. Do not push or rebase remote `main` unless the user explicitly requests it.

## Documentation and comments

- `PRODUCT.md` owns product scope; `ROADMAP.md` owns current work; `README.md` owns setup and use; the architecture blueprint owns system boundaries.
- Do not add Phase ledgers, hard-coded test counts, duplicated roadmaps, or completed-task histories to live documentation.
- Comments explain current invariants and non-obvious reasons. Git history, not comments or docstrings, stores implementation history.
- Before deleting code or documents, prove current references, compatibility responsibilities, and test coverage; handle deletion in a focused change.
