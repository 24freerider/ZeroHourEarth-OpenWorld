# Zero Hour Earth – Open World

Read `HANDOFF\README.md` first. It covers the project, the layout and the rules. Then `HANDOFF\ARCHITECTURE.md`,
`HANDOFF\ENGINE_TOUCHPOINTS.md`, `HANDOFF\KNOWN_ISSUES.md` and `HANDOFF\WORKFLOW.md`.

Rules (the owner's):
- Answer in Slovak.
- Never start the game or `tools\run-test.ps1` without the owner's consent: they play in another window, and
  `-ClickAt` moves the real mouse.
- After every change to the world server's output, bump `GEN_VERSION` in `worldserver\chunkgen.py`.
- Commit only `engine\` (branch `openworld-074`) with `git commit -q -F <file>`; add files by name, not `git add -A`.
- Before writing code, analyse the relevant files and propose an exact plan.
- Never send the owner's personal data (e-mail, name) to external services.
