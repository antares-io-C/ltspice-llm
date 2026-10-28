# Contributing

Issues and pull requests are welcome in English or Turkish. The code comments and most of the documentation are in
Turkish.

## Setup

```bash
python -m pip install -e ".[dev]"
```

LTspice (26.x, Windows) is only needed for the tests that run the simulator; everything else runs without it.

## Before you open a pull request

```bash
python scripts/check.py          # .asc integrity, ruff, mypy, tests without LTspice, use case coverage
python scripts/check.py --full   # also the LTspice tests and the examples (Windows + LTspice)
```

The project's rules for tests and simulations are in [GEMINI.md](GEMINI.md) (Turkish; Claude Code reads it through
[CLAUDE.md](CLAUDE.md)). The short version:

- An expected value comes from a source (a standard, a datasheet page, the ADI LTspice reference, or an LTspice
  measurement made by a script in `examples/`), never from running the code and pasting its output.
- Don't change a test to make it pass: no deleted tests, no added skip/xfail, no loosened asserts or tolerances.
- Every number in a report or document comes from `.meas` or `.raw`.

If you use Claude Code, the hooks in `.claude/` run the quality gate at the end of a turn that changed Python files
and ask before an edit removes tests. To turn them off for yourself, put `"disableAllHooks": true` in `.claude/settings.local.json`.

The hooks and the quality gate run Python from whatever branch is checked out, and an agent follows the instructions
in `CLAUDE.md` and `GEMINI.md`. Before you check out someone else's branch, read its diff on GitHub or with
`gh pr diff <number>`, especially changes to `.claude/`, `CLAUDE.md`, `GEMINI.md`, `scripts/`, `tests/conftest.py`,
`pyproject.toml` and `.github/`. Open a branch you don't trust in a separate clone with hooks turned off.

Report security problems privately; see [SECURITY.md](SECURITY.md).

## License

By contributing, you agree that your contribution is licensed under GPL-3.0-or-later, like the rest of the project.
