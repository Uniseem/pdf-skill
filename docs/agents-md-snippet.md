<!-- Paste into AGENTS.md for agents that do not support Agent Skills. -->
## Document library (pdf-skill)

For importing documents (PDF, scans, Office, images) into Markdown, translating PDFs with layout preserved,
or searching/citing previously ingested documents, use the `pdfskill` CLI
(install: `uv tool install git+https://github.com/Uniseem/pdf-skill`).
Run `pdfskill guide` first for the full workflow; use `--json` when parsing output;
search with `pdfskill search`, then read only what you need with `pdfskill get <doc> --pages N`.
