// Test document for pdf-skill (original content, MIT licensed with the repo).
#set page(paper: "a4", margin: 2.2cm,
  header: align(right)[_Flat Libraries Working Note_],
  footer: context align(center)[#counter(page).display()])
#set text(size: 11pt, font: ("New Computer Modern", "PingFang SC", "Songti SC"))
#set heading(numbering: "1.1")
#set math.equation(numbering: "(1)")
#set par(justify: true)

#align(center)[
  #text(size: 18pt, weight: "bold")[Flat Document Libraries for Agents]
  #v(4pt)
  Ada Example · Lin Placeholder
]

*Abstract.* We describe a flat, git-friendly layout for storing parsed documents. Every document is converted into hierarchical Markdown, while page-level geometry is kept in JSON. Retrieval uses plain BM25 over text chunks, so the whole library can be versioned, diffed and merged like source code.

= Introduction

Agents increasingly need to read large collections of papers, manuals and scanned books. Storing each document as a directory tree quickly becomes hard to navigate, and binary vector indexes do not merge cleanly in version control. We therefore keep one Markdown file and one JSON file per document, both named by a content hash. #lorem(170)

== Notation

Let $D = {d_1, dots, d_n}$ be the set of documents and let $q$ be a query. The BM25 score of a document is

$ "score"(d, q) = sum_(t in q) "IDF"(t) dot frac(f(t, d) dot (k_1 + 1), f(t, d) + k_1 dot (1 - b + b dot (|d|) / "avgdl")) $ <bm25>

where $f(t, d)$ is the term frequency and $k_1 = 1.5$, $b = 0.75$ are the usual constants. Equation @bm25 is evaluated over chunks rather than whole documents.

== Design goals

The layout must satisfy three goals: it must be flat, it must be text-first, and it must preserve page numbers for citation.

= Method

== Storage layout

#figure(
  table(
    columns: 3,
    table.header[*Path*][*Content*][*In git*],
    [`docs/<id>.md`], [Hierarchical Markdown], [yes],
    [`docs/<id>.json`], [Page geometry and anchors], [yes],
    [`assets/<sha>.png`], [Extracted images], [yes],
  ),
  caption: [Files written for each document.],
)

== Retrieval pipeline

#figure(
  box(width: 70%, height: 3.2cm, stroke: 0.8pt, inset: 8pt)[
    #grid(columns: 3, gutter: 12pt,
      rect(width: 100%, height: 2cm, fill: rgb("#dbeafe"))[#align(center + horizon)[BM25]],
      rect(width: 100%, height: 2cm, fill: rgb("#dcfce7"))[#align(center + horizon)[JSON]],
      rect(width: 100%, height: 2cm, fill: rgb("#fef9c3"))[#align(center + horizon)[Markdown]],
    )
  ],
  caption: [Coarse-to-fine retrieval: BM25, then JSON anchors, then Markdown.],
)

A query first retrieves candidate chunks with BM25, then resolves page numbers through the JSON file, and finally reads the exact section from Markdown.

= 中文说明

本节用于测试中文内容的解析与检索。扁平化存储意味着所有文档都放在同一层目录中，依靠索引文件进行查询。对于公式 $E = m c^2$，解析结果应当保留为行内公式。检索时先用 BM25 泛查，再用 JSON 定位页码，最后从 Markdown 中读取细节。

= Conclusion

Keeping text and geometry separate makes both easy to maintain. #lorem(60)

#heading(numbering: none)[References]

+ S. Robertson and H. Zaragoza. The probabilistic relevance framework: BM25 and beyond. 2009.
+ Example Authors. Parsing documents with layout models. 2024.
