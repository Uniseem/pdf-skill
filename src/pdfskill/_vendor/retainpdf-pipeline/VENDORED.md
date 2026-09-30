# Vendored: retain-pdf pipeline

- Upstream: https://github.com/wxyhgk/retain-pdf (MIT, see `LICENSE`)
- Path: `backend/pipeline`
- Commit: `857868407aeace94ccf5d8cf0bcf90aecae3b7bf` (2026-09-21, v4.2.5 + 32 commits)

pdf-skill installs this package into its own virtual environment
(`pdfskill setup translate`) and drives it as a subprocess per stage
(`normalize-ocr`, `translate-only`, `render-only`), feeding it the MinerU result
that ingest already downloaded, so MinerU runs only once per document.

## Local patches (packaging only, no behaviour changes)

1. `requires-python = ">=3.11"` (upstream pins `<3.12`; 3.12 and 3.13 run the full pipeline).
2. `pikepdf>=8,<10` (upstream pins `==7.2.0`, which has no macOS arm64 wheel).
3. Ship `retainpdf_pipeline/foundation/shared/*.json` as package data
   (upstream omits `latex_commands.json`, so every translate import crashes).

Fonts (`SourceHanSerifSC-*.otf`, SIL OFL 1.1, see `FONTS-LICENSE-OFL-1.1.txt`) are
downloaded by `pdfskill setup translate` instead of being vendored.
