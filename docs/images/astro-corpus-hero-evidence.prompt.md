# Astro-Corpus observations and evidence cover

Asset: [astro-corpus-hero-evidence.png](astro-corpus-hero-evidence.png).
Updated on 2026-11-03 with the built-in `image_gen` tool. The final editing pass
removes the pilot-status badge at the user's request; review status remains
documented in the README and evidence guide.

This cover connects the project's independent observation and full-text builds
to object-paper relations, attributed results, original-text evidence and the
whole-library observatory. The existing [cover](astro-corpus-hero.png) supplies
the visual identity; the new composition makes evidence the central feature.

The illustrations are conceptual, not observations, scientific quotations or
application screenshots. Relationship labels can coexist; they are not a ranking.
The current structured-result schema covers membership judgments. Other paper
results can be described in relation statements, but the cover does not imply a
general numeric-result extraction system. TWA annotations remain pending review.
See [the evidence workflow](../paper-evidence.md) for the implemented contracts.

## Final editing pass (after generation)

```text
Use case: precise-object-edit.
Input image 1 is the edit target, the current Astro-Corpus README cover.
Make ONLY this targeted removal:
Remove the entire small beige outlined badge reading "TWA 试点 · 待复核" near x=59.6%, y=82.7% (lower part of the CENTER region), including its outline and the short vertical divider immediately to its left. Restore the same clean ivory paper background in that area.
Preserve the nearby open-book icon and "agent 阅读与标注" text exactly.
Preserve EVERYTHING ELSE unchanged: all remaining Chinese and English text, typography, positions, all three numbered regions, arrows, object/paper illustrations, relation chips, original-evidence highlights, observatory window, masthead, bottom library/interface band, decorative artwork, colors, crop, resolution and aspect ratio. Do not add replacement text or change any wording. This is a localized erasure only.
```

## Generation prompt

```text
Use case: infographic-diagram
Asset type: updated main GitHub README cover for Astro-Corpus, wide landscape 16:9, high resolution, crisp readable Simplified Chinese.
Input image 1: existing cover, an EDIT TARGET whose information architecture must be updated. Retain the Astro-Corpus identity and beautiful scientific editorial illustration style: warm ivory paper, navy ink, refined serif English masthead, clean sans-serif Chinese labels, muted teal and indigo flows, celestial coordinates and restrained galaxy details at the margins. Redesign the body to make the new literature/evidence capabilities central.

Main message: observations and structured full text are connected by astronomical object identity to MULTI-LABEL paper relations, attributed results and precise source evidence, all browsable in the whole-library observatory. This is an implemented corpus tool, not a chatbot or a model training service.

Composition: three clearly separated numbered regions, left to right, generous white space, distinct scientific illustrations instead of a grid of generic cards. Header 13%, main body 72%, storage/interface foundation 15%. The middle region is widest, around 42%; the others about 29% each. Clear arrowheads, no crossing lines. Decorations must not compete with functional labels.

Top exact text:
"Astro-Corpus"
"连接天体观测、论文研究与原文证据"

LEFT region heading: "01 观测与全文"
Two independent parallel inputs, NOT a chain from catalogue to automatic paper downloads:
Upper: celestial globe and a tiny catalogue with abstract placeholder rows, label "Gaia DR3 / VizieR". Flow to a selected star and small optional spectral trace, labels "天体与观测" and "天测 · 测光 · XP 光谱（可选）".
Lower: journal sheets labelled "arXiv 指定论文". Flow to structured paragraphs and table, labels "结构化全文" and "章节 · 段落 · 表格".
Both paths feed the middle region independently.

CENTER region heading: "02 关系与证据"
An elegant selected-star emblem labelled "同一天体" connects visibly to two or three paper sheets.
Readable subsection "多标签关系", with five distinct chips arranged as a cluster, NOT a sequential ranking: "提到", "纳入样本", "分析", "报告结果", "引用既有结果".
Below, a visual evidence record with two aligned semantic layers:
"结果归属" with two distinct labels "本文分析" and "前人结果", plus supporting text "方法 · 条件 · 限制".
"原文定位" with a small paragraph showing a highlighted phrase and a small table showing a highlighted cell including visible column headings and a footnote line, supporting text "段落 · 单元格 · 表注".
Show a clear connecting line from a paper to its source-evidence excerpt. Use teal for the current-analysis link and muted indigo for prior-work, with subtle gold highlighting in source text.
Small clear annotation "agent 阅读与标注".
A modest status badge "TWA 试点 · 待复核".
No fake scientific result or quotation, only neutral line placeholders in documents and tables.

RIGHT region heading: "03 全库观测台"
A tasteful conceptual browser-window illustration, clearly a diagram rather than a claimed screenshot.
Within it show a search box with "名称 / Gaia ID", an astronomical source list with a selected row, and two compact tiles for sky map and optional spectrum. Most of the viewer illustration should be a wide literature-and-evidence area with paper cards, relation chips, and highlighted paragraph/table evidence. Readable label "文献与证据" above this dominant area.
Under the viewer, two concise lines: "关系筛选 · 原文高亮" and "历史批次 · 离线导出".
Small text "只读本地已有数据".

Bottom continuous foundation band supporting all three regions, with a small archive symbol:
"共享本地库 · 固定版本 · 来源追踪 · 运行快照"
Second line:
"CLI / Python / MCP 统一调用"

Requirements: quote labels verbatim once each, make type large enough to read on a GitHub README at desktop width. No numbers for sample sizes (avoid rapid aging). No invented Gaia IDs, measurements, paper titles, journal names, citations or accuracy claims. No implication that all labels or all fields are present for every object. Do not show a progression from mentioned to analysed; relations coexist. No claim of verified membership or automatic scientific verification. No watermark or extra slogans. Preserve ivory/navy editorial aesthetic but reduce decorative galaxy area relative to reference. Deliver a polished project overview, not a slide full of tiny text.
```
