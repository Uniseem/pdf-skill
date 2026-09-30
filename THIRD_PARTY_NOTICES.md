# Third-party notices

## Vendored

| Component | Path | License | Notes |
| --- | --- | --- | --- |
| retain-pdf pipeline (`retainpdf_pipeline`, v4.2.5+, commit `8578684`) | `src/pdfskill/_vendor/retainpdf-pipeline/` | MIT, see `LICENSE` there | Packaging patches only, listed in `VENDORED.md` |
| KaTeX 0.18.9 (`katex.min.js`) | `src/pdfskill/_vendor/katex/` | MIT, see `LICENSE` there | Used to validate every formula |

## Downloaded on demand by `pdfskill setup translate`

| Component | Source | License |
| --- | --- | --- |
| typst 0.15.1 | github.com/typst/typst releases | Apache-2.0 |
| Source Han Serif SC (Regular, Bold) | github.com/wxyhgk/retain-pdf `resources/fonts` | SIL Open Font License 1.1 |
| Python dependencies of retain-pdf (PyMuPDF, pikepdf, Pillow, requests) | PyPI | AGPL-3.0 (PyMuPDF), MPL-2.0 (pikepdf), MIT-CMU (Pillow), Apache-2.0 (requests) |

PyMuPDF is AGPL-3.0. It is installed only into the separate translation
environment and runs as a subprocess, and pdf-skill does not link to it.
Check the AGPL terms before you redistribute that environment or offer
translation as a network service.

## Python dependencies (installed from PyPI)

| Package | License |
| --- | --- |
| httpx | BSD-3-Clause |
| openai | Apache-2.0 |
| json-repair | MIT |
| markdown-it-py, mdit-py-plugins, mdformat, mdformat-gfm | MIT |
| pymarkdownlnt | MIT |
| dukpy | MIT |
| rapidfuzz | MIT |
| bm25s | MIT |
| PyStemmer | MIT / BSD |
| numpy | BSD-3-Clause |

## Services

- The MinerU cloud API (https://mineru.net) is subject to its own terms and quotas.
- LLM providers are subject to their own terms.
