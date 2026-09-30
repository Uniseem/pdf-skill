# Contributor notes for agents

- Python package in `src/pdfskill/`; CLI entry `pdfskill.cli:main`; tests in `tests/` (offline: httpx MockTransport for MinerU, `tests/mock_llm.py` for LLMs).
- `src/pdfskill/_vendor/` is third-party code (retain-pdf pipeline, KaTeX). Do not edit it except for documented packaging patches (`VENDORED.md`).
- `skills/pdf-skill/SKILL.md` = frontmatter + the exact text of `src/pdfskill/guide.md` (a test enforces this). Edit the guide, then regenerate the SKILL body.
- Before committing: `uv run ruff check src tests`, `uv run pytest`, `uvx --from skills-ref==0.1.1 agentskills validate skills/pdf-skill`.
- Keep versions in sync: `pyproject.toml`, `src/pdfskill/__init__.py`, SKILL.md `metadata.version`, `.claude-plugin/plugin.json`, `gemini-extension.json`.
- Never write secrets into the library or the repo; keys come from env vars or `~/.config/pdfskill/config.toml`.
