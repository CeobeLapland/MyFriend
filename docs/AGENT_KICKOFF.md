# Agent Kickoff Prompt

You are the coding agent for the Astra AI Companion project.

First, read the entire `ai_partner_spec/` directory (or the copied `docs/` directory) and treat it as the project specification.

Do not implement the entire system in one pass.

Start with Phase 0 from `08_IMPLEMENTATION_PLAN.md`:

1. Initialize the Python project.
2. Create the package structure described in `02_ARCHITECTURE.md`.
3. Add SQLite initialization/migrations based on `03_DATA_MODEL.md`.
4. Add `/health`.
5. Add pytest, linting, formatting, and type checking.
6. Add unit tests for configuration loading and database initialization.
7. Do not add Unity, STT, TTS, or complex Agent behavior yet.

After Phase 0 passes, stop and report:

- files created
- commands to run the project
- test results
- known limitations
- the exact next milestone you recommend

Then continue milestone by milestone only after the previous milestone is in a runnable and tested state.

For every implementation task:

- Read the relevant specification first.
- Keep provider-specific code inside adapters.
- Add tests with the implementation.
- Validate all structured model output.
- Never allow arbitrary LLM text to execute OS actions.
- Do not silently change the public event schema.
- Do not add unnecessary dependencies.
- Preserve restart/persistence behavior.
- Prefer a minimal implementation over speculative abstractions.

When a requirement is ambiguous, choose the smallest behavior consistent with the specification and document the decision in `docs/decisions/` rather than redesigning the whole system.
