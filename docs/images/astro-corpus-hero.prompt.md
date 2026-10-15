# Astro-Corpus README overview cover

Asset: [astro-corpus-hero.png](astro-corpus-hero.png).
Created on 2026-10-15 with the built-in `image_gen` tool.

The cover explains four stages: source selection, two independent corpus builds,
local storage, and querying or viewing existing data. The sample route and the
explicit arXiv full-text route share a local library; each run retains its own
snapshot. CLI, Python and MCP are interfaces to these capabilities.

The illustrations are conceptual: star fields and spectra are not measured data,
paper pages are placeholders, and the viewer illustration is not a screenshot.
Functional labels and connections follow the repository's
[architecture](../architecture.md).

The earlier illustrated cover at
`8f4aa83:docs/images/astro-corpus-hero.png` was used only as a style reference.
The first prompt produced the system map; the second removed invented values
from decorative examples. These briefs record the design intent for future
revisions; generation is not deterministic.

## System map prompt

```text
Use case: infographic-diagram
Asset type: the main GitHub README cover for Astro-Corpus. This image MUST explain the project's actual main functions and structure at a glance.
Input image 1: style reference only (ivory paper, dark navy astronomical ink, refined typography, subtle spectral color). Completely redesign the composition into an informative illustrated system map.

Primary request: a polished scientific editorial infographic, landscape 16:9 at high resolution. A reader should immediately understand INPUTS -> TWO INDEPENDENT BUILD PATHS -> LOCAL STORAGE -> USE. Keep the scientific illustration and visual craft, but give the actual workflow and readable labels most of the space. Four numbered stages arranged left to right, with two horizontal lanes in the first two stages that converge at the third stage. Clear, elegant, visible arrowheads. No crossing lines. Use restrained teal for the astronomical-sample lane and muted indigo for the paper-full-text lane. Labels must be real, sharp, clean simplified Chinese; all quoted text below is verbatim. Do not add other copy.

Top masthead, about 15% of the height:
Large title "Astro-Corpus"
Subtitle "天文数据与文献语料管线"
Use beautiful editorial title typography and a clear modern sans-serif for Chinese. Keep title compact enough that the diagram dominates.

Main system map, about 70% of the height:
Stage heading "01 选择来源"
- Upper lane: illustrated celestial coordinates with a small catalog table, label "Gaia DR3 / VizieR".
- Lower lane: an elegant small stack of journal pages, label "arXiv 指定论文".
Upper input goes ONLY to the upper builder; lower input goes ONLY to the lower builder.

Stage heading "02 独立构建"
- Upper builder "样本语料"; below it three short readable lines: "星表匹配", "XP 光谱（可选）", "SIMBAD 书目". Show a selected star field and a spectral trace integrated with the labels.
- Lower builder "全文语料"; below it two readable lines: "保存原文", "JSON + Markdown". Show paper transforming into structured text.
The two builders are parallel, independent paths. Do NOT connect one builder to the other.

Stage heading "03 本地保存"
Both builders feed this region.
A compact, beautifully drawn layered archive, label "共享本地库", with two clearly labelled layers "SQLite 索引" and "不可变文件".
Below the shared-library object, a distinct small set of versioned sheets labelled "运行快照".
The whole local-storage region includes the shared library plus the per-run snapshots; do not imply snapshots are stored inside SQLite.
Small readable supporting line "来源追踪 · 去重复用".

Stage heading "04 查询与展示"
The storage region points to two clean illustrated outputs:
- "只读查看器", with an abstract miniature star plot and spectrum; supporting text "星图 · 光谱 · 文献".
- "检索与导出", with search and file symbols.
Viewer reads existing LOCAL data, it does not trigger online downloads.

Bottom integrated typography band, about 15% of the height, two readable lines:
"CLI / Python / MCP 统一调用"
"外部 AI agent 经 MCP 使用工具"
This band describes interfaces to the workflow, not a fifth processing stage. No AI brain icon.

Composition and design: precisely align the two input/build lanes, leave air between stages, use purposeful numbered headings, thin guides and restrained flow ribbons with explicit arrowheads. Avoid enclosing every element in a bland rectangular card. Make source, builder, archive, and viewer illustrations distinctive and subordinate to the information. Light ivory background and high-contrast navy text throughout; avoid dark textures behind small words. Decorative stars and coordinate marks only at margins. The architecture should be comprehensible even without decoration. Prioritize readable type over extra illustration. Every quoted label must appear exactly once.
Scientific constraints: conceptual illustrations, no actual observed data, no invented metrics or citations, no fake application screenshot. Do not claim complete sky coverage, AI training, or a built-in chatbot. Do not depict the paper route as automatically launched from SIMBAD. No watermarks, no extra slogans.
```

## Placeholder cleanup prompt

```text
Use case: precise-object-edit
Input image 1 is the edit target, an Astro-Corpus system overview cover.
Make one targeted cleanup to the illustrative placeholder content:
- In the small catalog table next to the globe, preserve the column labels RA, Dec and Gmag, but replace ALL numerical cell values with subtle gray horizontal placeholder strokes. No numerical observations.
- On the illustrated paper pages, replace journal titles and article text with subtle abstract placeholder lines. Keep the paper silhouette and small conceptual illustrations. Do not depict named journals or fake article content.
- On the run snapshot sheets, replace V1.0/V1.1/V1.2 with simple neutral tab marks, without version numbers.
Preserve EVERYTHING ELSE: the exact four-stage layout, all Chinese and English workflow labels, their positions and spelling, arrows, two independent routes, title, interfaces band, colors, paper texture, illustrations, crop and aspect ratio. Do not remove or change any functional label. This is only a cleanup of decorative examples; maintain the image's quality and sharpness.
```
