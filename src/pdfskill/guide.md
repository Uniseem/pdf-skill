# pdf-skill

One CLI, `pdfskill`, runs three jobs. It keeps a local **document library**, which is a flat git repository:

1. **Ingest**: turn any file (PDF, scans, images, Word, PowerPoint, Excel, HTML) into strict, hierarchical Markdown.
   - The MinerU cloud API parses the layout into JSON, and an LLM cleans the text into Markdown.
   - Page numbers and geometry stay in JSON. Text lives in Markdown.
   - Images are saved locally.
   - The original file is **not** stored.
2. **Translate**: produce a layout-preserving Chinese translation of a PDF (retain-pdf). The translated PDF and a translated Markdown are added to the library as well.
3. **Query**, coarse to fine:
   - BM25 full-text search over all documents.
   - A JSON lookup for pages, headings and captions.
   - Exact Markdown ranges for reading.

## Setup (once per machine)

1. Check the tool: `pdfskill --version`.
   - If it is missing, install it: `uv tool install git+https://github.com/Uniseem/pdf-skill`. This needs uv: https://docs.astral.sh/uv/.
   - For a one-off run without installing: `uvx --from git+https://github.com/Uniseem/pdf-skill pdfskill ...`.
2. Run `pdfskill doctor`. It reports what is missing. Tell the user which environment variable to set, and never ask them to paste a secret into the chat.
   - **MinerU**: `MINERU_TOKEN` (from https://mineru.net/apiManage/token). Without it, the anonymous, rate-limited MinerU API is used.
   - **LLM**: any OpenAI-compatible provider.
     - Set a provider key such as `DEEPSEEK_API_KEY`, `DASHSCOPE_API_KEY`, `ZHIPUAI_API_KEY`, `MOONSHOT_API_KEY`, `SILICONFLOW_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`. `pdfskill providers` lists them all.
     - Optional overrides: `PDFSKILL_LLM_PROVIDER`, `PDFSKILL_LLM_MODEL`, and `PDFSKILL_LLM_BASE_URL` for any other endpoint.
     - Keys can also go in `~/.config/pdfskill/config.toml`; `pdfskill config --init` writes a template.
     - Without an LLM, ingest still works and produces heuristic Markdown.
3. Create the library: `pdfskill init`.
   - The default location is `~/pdfskill-library`. Choose another with `--library PATH` or `PDFSKILL_LIBRARY`.
   - A directory that contains `.pdfskill/` is found automatically from inside it.
4. Translation only: `pdfskill setup translate`. It installs the retain-pdf environment, typst and CJK fonts, about 200 MB.

## Ingest

```bash
pdfskill ingest paper.pdf                      # one file
pdfskill ingest ~/Downloads/papers/            # every supported file in a directory
pdfskill ingest scan.pdf --ocr --lang ch       # scanned document, Chinese OCR hint
pdfskill ingest book.pdf --pages 1-50          # MinerU accepts at most 200 pages per file
pdfskill ingest paper.pdf --translate          # ingest + Chinese translation (PDF only)
```

- Output: one line per file with the `doc_id` (16 hex characters), title, page count and whether the Markdown passed strict validation. Add `--json` for details.
- Each ingest commits to the library's git repository (`--no-commit` to skip).
- Re-ingesting the same file is a no-op. The file is identified by its content hash.
- `--force` re-converts using the cached MinerU result. `--reparse` calls MinerU again.
- Long documents take minutes because the LLM refines them chunk by chunk. For large batches, run the command in the background and check its output.

## Translate

```bash
pdfskill translate paper.pdf                   # ingests first if needed; target is Simplified Chinese
pdfskill translate paper.pdf --render-mode dual  # bilingual side-by-side PDF
```

- Writes `docs/<id>.zh.pdf` and `docs/<id>.zh.md`, plus search chunks for the translation.
- Translation reuses the MinerU result from ingest, so MinerU runs only once per document.
- It uses the same LLM settings as ingest. Typical papers take 1–5 minutes.
- The original PDF must be supplied again to translate a document that was ingested earlier, because originals are never stored.

## Query: search, then locate, then read

1. **Broad search (BM25)** across every document and translation:
   `pdfskill search "attention mechanism 注意力" -k 8`.
   - Each hit shows a chunk id, the page range, the Markdown line range, the section path and a «highlighted» snippet.
   - Filters: `--doc ID`; `--in orig` or `--in zh` to choose original text or translations.
   - If `unknown_terms` is reported, rephrase the query or try the other language.
2. **Coarse lookup (layout JSON)**, to find where something is without reading text:
   - `pdfskill locate "Table 3"`
   - `pdfskill locate "loss function" --type heading`
   - `pdfskill outline <doc>` shows the heading tree with pages and lines.
3. **Read exact text (Markdown)**, only the part you need:
   - `pdfskill get <chunk_id>`
   - `pdfskill get <doc> --pages 4-5`
   - `pdfskill get <doc> --heading "Method"`
   - `pdfskill get <doc> --lines 120-180`
   - Add `--in zh` to read the translation instead.

A document can be referred to by its full id, a unique id prefix (4 or more characters), its original file name or its title. When citing, give the title and page number(s). `pdfskill list` shows the catalog. `pdfskill show <doc>` shows conversion and validation details.

## Library layout

The library is plain files, so you can also read them directly. It is flat: there are no per-document folders.

```text
catalog.jsonl              one line per document (id, title, source name, pages, translations)
docs/<id>.md               hierarchical Markdown (one H1, no level jumps, KaTeX-valid math, GFM tables)
docs/<id>.json             pages, outline, blocks: type, page, bbox (0-1000), preview, Markdown line range "md"
docs/<id>.zh.md|.zh.pdf    translations ("md_zh" line ranges in the JSON)
assets/<hash>.<ext>        images, referenced from Markdown as ../assets/...
chunks/<id>[.zh].jsonl     search chunks (committed); the BM25 index is rebuilt into .cache/ (ignored)
```

## Rules for agents

- Use `--json` whenever you parse output. Progress is printed on stderr, data on stdout.
- Do not read whole documents into context. Search, then `get` the relevant pages or sections. `get` truncates at 20 000 characters; adjust with `--max-chars`.
- If ingest reports validation issues, the Markdown is still usable. `pdfskill show <doc> --json` lists them.
- MinerU calls and LLM tokens cost the user money or quota. Do not use `--reparse` or `--force` without a reason.
- If the user edits Markdown by hand, run `pdfskill reindex --rechunk --doc <id>`. After a `git pull` of the library, run `pdfskill reindex`.
- Remove a document with `pdfskill remove <doc>`. This deletes its files and any assets no other document uses.
