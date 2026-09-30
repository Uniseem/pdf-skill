# 设计说明与调研记录

本文记录 pdf-skill 的设计决策，以及为每个部件调研过的开源方案和最终取舍。调研时间为 2026-09。

## 需求

- 两条工作流合并成一个 skill：**文件入库**和 **PDF 原版翻译**。
- 入库要求：
  - 支持 PDF、图片等各种文件，统一走 MinerU 云端 API，产出 JSON。
  - LLM 逐块把内容转成**严格、分层**的 Markdown，并且支持多家便宜的 API。
  - 页码留在 JSON 里。查询先查 JSON 定位，再到 Markdown 取细节。
  - 扁平 RAG，必须对 git 友好，只用纯文本 BM25。
  - 图片转存到本地，所有文件扁平存放，靠索引查找。
  - 二进制文件（图片、译文 PDF）也进 git，但原件不保存。
- 翻译要求：
  - 直接基于 retain-pdf 改成 skill，模型与入库共用同一份配置。
  - 译文 PDF 和译文 Markdown 同样入库、进索引，并且直接用翻译阶段的产物，不重新 OCR。
- skill 要能被各家 agent 使用。

## 数据流

```text
file ──sha256──► doc_id ──► MinerU（v4 带 token / v1 匿名）──► .cache/mineru/<id>/（zip 解压后缓存）
                                   │
                                   ├─► normalize：content_list.json + layout.json → 块模型
                                   │     （页码从 1 开始，bbox 归一化到 0-1000，页眉页脚记为 furniture）
                                   │       ├─► assets/：图片按内容哈希命名；CDN/http 链接会下载
                                   │       └─► convert：标题层级 → 正文整理 → 公式修复 → 规范化 → 严格校验
                                   │             └─► docs/<id>.md + docs/<id>.json（块 → Markdown 行号）
                                   │                   └─► chunks/<id>.jsonl（按标题分块，带页码和行号）
                                   │
                                   └─► retain-pdf（独立 venv，每个阶段一个子进程）：
                                         normalize-ocr（读同一份 layout.json）→ translate-only → render-only
                                           ├─► docs/<id>.zh.pdf
                                           └─► 译文按页码、bbox 和原文相似度映射回块模型
                                                 → docs/<id>.zh.md + chunks/<id>.zh.jsonl
```

所有写操作结束后自动 `git commit`。BM25 索引放在 `.cache/bm25/`，不进 git；每次查询前比对 chunk 文件的指纹，有变化就自动重建。

## 部件与取舍

### MinerU 客户端（自己写，约 300 行）

- **官方 `mineru-open-sdk`**（Apache-2.0，Alpha）有几个问题：
  - 整个文件和 zip 都读进内存；
  - 401 在映射成错误码之前就被 `raise_for_status` 吞掉；
  - 在 zip 里找 content_list 时可能错拿成 `_model.json`。
- **`mineru` 主包**太重（依赖 torch、gradio 等），许可证也有附加条款。
- 最终参照 retain-pdf 的 `mineru_provider` 和官方文档自己实现：
  - **上传**：用预签名 PUT 上传，**不带** Authorization 和 Content-Type，带上会 403 签名不匹配。
  - **重试**：遇到 408、429、5xx 时退避重试，并遵守 `Retry-After`。
  - **zip 解压安全**：检查路径穿越、总大小和文件数上限。
  - **找结果文件**：按文件名后缀定位，因为前缀是服务端生成的 UUID。
  - **token 过期提醒**：解析 JWT 的 `exp` 字段。
- **v4 与 v1 两个版本**：
  - v4 是带 token 的正式接口。v4 的图片在 zip 包的 `images/` 里，并不是 CDN 链接；仍保留把 http 图片下载到本地的逻辑，用来处理 HTML 输入和表格里的外链。
  - v1 支持匿名调用（每分钟 5 次，单文件不超过 200 页），没有 token 时自动用它。
  - v1 在缓存命中时，会对完整结果报 `file_conversion_failed`。客户端遇到这种情况会校验 zip 内容，完整就接受。
- **页码偏移**：指定 `page_ranges` 后，MinerU 的 `page_idx` 会从 0 重新计数，需要按起始页补偏移。

### 严格 Markdown（复用校验工具，协议自己写）

参考过的项目：MinerU `llm_aided` 标题分级（v4 分组打分；v2 允许用 0 降级）、marker `--use_llm`（ID 集合相等校验、长度比例保护）、olmOCR（`finish_reason` 检查、重试时升温）、mineru-refine（一致性闸门、出错时退回原文）、docling（先用确定性信号定标题层级）、llm_aided_ocr（反例：文本重叠后让 LLM 去重，会同时导致重复和丢失）。

最终做法：

1. **LLM 只做决策和整理，所有字符由代码产出并校验。** 表格、公式、图片、代码块从不发给 LLM。
2. **标题层级**：
   - 先用编号正则打先验，覆盖 `1.2.3`、`第X章/节`、`一、`、`（一）`、`Chapter`、`Appendix` 等写法。
   - MinerU 漏掉的短编号行也列为候选标题。
   - 再让 LLM 分批输出 `{id: level}`，其中 0 表示降为正文；返回的 key 集合必须与候选完全一致。
   - 最后由代码保证只有一个 H1、标题不跳级，并去掉标题末尾的标点。
3. **正文**：
   - 按字数分块，尽量在标题处切开；块与块之间只给只读上下文，不重叠。
   - LLM 输出 `[[b0012]]` / `[[b0012+b0013]]` 锚点。合并只允许相邻的文本块，中间可以隔着浮动体（图、表）。
   - 每段都要过闸门：
     - ID 顺序正确，且是连续的文本块；
     - 归一化后的字符相似度 ≥ 88%（rapidfuzz）；
     - 长度比在 0.8–1.2 之间；
     - 不含标题、表格或 HTML；
     - 公式都能通过 KaTeX。
   - 不合格的段落带着具体问题重试一次，仍不合格就按段回退到原文。
4. **公式**：
   - 用 KaTeX 0.18.9 在 dukpy 里校验，每条约 0.7 ms。`latex2mathml` 和 `pylatexenc` 会放过非法 LaTeX，没有采用。
   - 不能渲染的公式按顺序处理：
     1. 先做廉价修正（`\label`、`\hdots`、`\bm` 等）；
     2. 再交给 LLM 单条修复；
     3. 最后降级为代码。
5. **规范化与检查**：
   - mdformat 配 GFM 和一个自写的严格 `$` 数学插件。`mdformat-dollarmath` 会把 `\$` 改坏，并且锁死了 mdformat<0.8，所以不用。
   - 校验规范化前后渲染出的 HTML 一致、规范化结果幂等。
   - pymarkdownlnt 检查的是**把公式遮蔽掉的副本**，否则 `$x*y$` 会被误报成强调语法。
   - 表格列数单独检查，因为 pymarkdownlnt 没有 MD056。
6. **MinerU 文本清理**：
   - `<sup>`/`<sub>` 转成 Unicode 上下标，转不了的写成 `$^{...}$`；
   - 其他常见内联 HTML 标签去掉或转成对应的 Markdown；
   - 裸 URL 用尖括号包成 autolink；
   - 行内公式内侧多余的空格去掉。
7. **表格**：HTML 转 GFM，合并单元格展开并重复内容；单元格内公式里的 `|` 改写成 `\vert`，否则会把单元格切开。

### LLM 接入（复用 openai SDK）

- **LiteLLM**：依赖重，锁死 `openai<3`，而且 2026-03 在 PyPI 上发生过供应链投毒，放弃。
- **any-llm**：能用，但多了一层间接调用。
- **最终方案**：`openai` SDK 设置 `base_url`，外加一张预设表，记录各家的差异：
  - 温度范围，例如 GLM 的温度必须在 (0, 1) 之间；
  - 是否支持 JSON mode；
  - 如何关闭思考模式（DashScope 用 `enable_thinking`，GLM 用 `thinking`）；
  - OpenAI 使用 `max_completion_tokens` 参数。
- 回复开头的 `<think>` 段会被剥掉；JSON 用 `json-repair` 修复后再做校验，失败时把错误反馈给模型重试。
- 响应缓存到 `.cache/llm-cache.sqlite`，中断后重跑不会重复计费。

### 检索（复用 bm25s，分词器和分块自己写）

实测对比过五个方案：

- **bm25s**：MIT，只依赖 numpy。5 万个 chunk 建索引 2 秒，单次查询 0.15 ms，**采用**。
- **rank-bm25**：已停止维护，也不能保存索引。
- **SQLite FTS5**：trigram 分词对两个字的中文词直接查不到。
- **tantivy-py**：PyPI 上的 wheel 不带中文分词。
- **Pyserini**：要装 Java 和 torch，太重。

分词器不依赖词典：

- 中文：建索引时用单字加相邻两字（bigram），查询只用 bigram，所以“钛矿”能匹配“钙钛矿”。
- 英文：NFKC 归一化、去停用词、Snowball 词干。
- 这与 Lucene 的 CJKAnalyzer 以及 basic-memory 的做法一致。jieba 自 2020 年起没有新版本，而且依赖词典。

分块和检索结果：

- 按标题分块，目标约 400 token，上限 800，块与块不重叠。
- 建索引的文本是章节路径（去掉 H1）加正文；带上 H1 会让同一篇文档的每个块都命中标题里的词。
- 检索结果带上 chunk id、页码范围、Markdown 行号、章节路径和 «高亮» 片段，并列出索引里没有的查询词（`unknown_terms`），便于 agent 改写查询。

### 翻译（复用 retain-pdf）

- **源码随仓库分发**：vendoring 的是 `backend/pipeline`，commit `8578684`，只打了三处打包补丁：
  1. 把 `latex_commands.json` 列入 package-data，上游缺了它，导入时会直接崩溃；
  2. 放宽 pikepdf 的版本限制，原来钉死的版本在 arm64 macOS 上没有 wheel；
  3. 放宽 Python 版本上限，3.12 和 3.13 都已实测可用。
- **运行方式**：装在独立的 venv 里，因为上游钉死了 PyMuPDF、Pillow、pikepdf 的版本，而且 PyMuPDF 是 AGPL。每个阶段作为一个子进程运行，因为上游在导入时读环境变量，还会修改全局状态。
- **只调一次 MinerU**：`normalize-ocr` 直接读入库时缓存的 `layout.json`。
- **密钥隔离**：通过 `credential_ref=env:RETAIN_TRANSLATION_API_KEY` 传入 key，并在子进程环境里去掉 `DEEPSEEK_API_KEY`。上游在 key 为空时会退回使用它，可能把 DeepSeek 的 key 发给别家的 `base_url`。
- **译文映射回块**：
  - 按页码、bbox 重叠度（IoU）和原文相似度匹配，保证译文 Markdown 与原文 Markdown 的结构一致。
  - retain-pdf 会把跨栏、跨页的段落合成一个单元来翻译。这时整段译文放在第一个块上，其余块留空；如果单元里含有标题，就按成员各自拆分。
- **限制**：目标语言在上游代码里写死为简体中文；表格标题上游不翻译，保留原文。

### 跨 agent 分发

- **skill 目录**：遵循 agentskills.io 规范，frontmatter 只用规范定义的六个字段，已通过 `skills-ref validate`。skill 目录只放说明文件，代码作为 CLI 安装，因为 `npx skills`、`gh skill` 这类安装器只复制 skill 目录本身。
- **CLI 安装**：`uv tool install git+https://github.com/Uniseem/pdf-skill`。暂未发布到 PyPI。
- **各家入口**：
  - Claude Code 和 Codex 的插件清单：`.claude-plugin/`；
  - Gemini CLI 扩展：`gemini-extension.json`；
  - 不支持 skill 的 agent：用 AGENTS.md 片段（`docs/agents-md-snippet.md`）。
- **CLI 约定**：所有命令都不交互；`--json` 输出到 stdout，进度输出到 stderr；有明确的退出码（0/1/2/3）；`get` 的输出有长度上限。

## 已知限制和后续方向

- 目前没有真实 LLM 的端到端回归测试，CI 用 mock 服务。建议配好 key 之后先用 `pdfskill doctor --ping` 确认连通。
- 超过 200 页的文档需要手动用 `--pages` 分段入库，还没有自动切分和合并。
- 可以考虑的扩展：
  - 在 `normalize` 之前加一道可选的 `mineru-refine` 预处理；
  - 读取 PDF 自带的书签（outline），作为标题层级的权威来源；
  - 提供 MCP server 形式的封装。
