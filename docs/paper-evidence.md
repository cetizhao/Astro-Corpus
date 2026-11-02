# 天体—论文关系与原文证据

这项功能回答：一篇论文怎样使用了某个天体，支持这一判断的原文在哪里？它以已有的 Gaia 源和本地全文为输入，通过 MCP 把上下文交给外部 agent 阅读，再校验并保存分类。Python 服务不调用模型，也不根据名称命中推断科学关系。第一版以 TWA（TW Hya association，TW 长蛇座星协）的文献为试点。

SIMBAD 书目仍保存在原来的 `source_papers` 中，表示书目系统关联了对象与论文，并不证明作者分析过对象。新标注单独保存。第一版提供关系说明和原文证据，不做数值单位换算、跨论文结论合并、人工审核界面或模型批量抽取。

## 分类与判断状态

关系使用多标签，而非单一的强弱等级。同一对象可以既属于工作样本，又接受本文分析，同时引用了前人测量。每个标签都有自己的说明和证据。分类契约为 `object-paper-1`；外部 agent 可使用配套的[阅读与标注指令](prompts/object-paper-1.md)。

| 标签 | 含义 | 不能据此推断 |
|---|---|---|
| `mentioned` | 正文、表格或图注明确提到对象 | 名称出现不等于分析 |
| `in_sample` | 对象被纳入本文的工作样本 | 工作样本不是星协成员认证 |
| `analysed` | 本文对对象进行测量、拟合、建模或分类 | 转述前人的分析不算本文分析 |
| `reports_result` | 本文报告对象的结果，包括上限和未探测 | 论文的报告不等于服务确认的事实 |
| `cites_prior_result` | 本文引用或采用前人对对象的结果 | 不归为本文的新测量 |
| `comparison` | 对象用于比较 | 比较对象未必只作比较 |
| `calibrator` | 对象用作定标对象或参考标准 | 普通样本对象不自动成为定标对象 |

`subject_scope` 区分 Gaia 源（`source`）、分量（`component`）和系统（`system`）。系统的综合光谱或速度不能自动赋给每个分量，星协整体结论也不能展开到各个源。没有 Gaia 身份的对象暂留在原论文中，不虚构 ID。

一次对象评估可为 `evidence_found`（提供了支持标签的证据）、`uncertain`（身份或关系不确定）或 `not_found_in_inspected_blocks`（只在列出的已检查块中未找到证据）。没有记录时返回 `not_assessed`。缺少全文、解析不完整、只读了部分内容或过滤后无结果，都不意味着“论文与对象无关”。

## 三个 MCP 接口

### 读取上下文：`get_paper_evidence_context`

先用 `lookup_library_paper` 或 `paper_corpus_status` 取得全文的 `version_id`，再作为 `paper_version_id` 传入。它是库中的不可变全文版本 ID，不是裸 arXiv ID；同一个 arXiv 版本经不同解析器或刷新产生的快照也不能混用。

```python
get_paper_evidence_context(
    paper_version_id=version_id,
    targets=[{
        "release": "dr3", "source_id": source_id,  # ID 始终是字符串
        "aliases": [{"name": paper_name, "provenance": mapping_reference}],
    }],
    context_blocks=1, limit=20, offset=0,
)
```

名称搜索覆盖正文、图注、表格单元格和表注，并可返回相邻块。别名必须说明映射来源。库中 SIMBAD 快照的主名称也作为候选返回，并标记过期状态。命中只产生待检查位置，不能证明身份；例如 `TWA 1` 不匹配 `TWA 11A` 或 `TWA 1A`。

`units` 是分页的文本片段，每片最多 4,000 个 Unicode 字符。`start`、`end` 是在**完整解析字段**中的位置，结束位置不包含在引文中，不是原文件字节偏移。字段包括段落 `text`、表格 `table_cell`、表注 `table_note`，以及不能可靠分格的 LaTeX 表格 `raw`。表格行列从 0 开始，按实际保存的单元格编号，不展开跨行/跨列；`cell_structure` 保留跨度和表头标记。

`page.has_more` 表示还有片段，下一页的 `offset` 增加本页 `returned`。用 `mode="all"` 浏览全文，或用 `block_ids=[...]` 回到指定方法、结果或参考文献块。`citations` 保留链接及可用的 `linked_block_id`，其中也可能有图表、章节链接，不能一律视为参考文献。`candidate_matches` 最多列出前 200 个命中，截断会标记；全文片段分页不因此丢弃后续块。

返回值包含章节、原始定位、原文件路径、校验和及可用的在线锚点。引文来自解析字段，空白和公式可能经过解析器转换，必要时打开原文件复核。HTML 表格必须结合表头和脚注阅读；PDF 只保证页定位文本，不保证可靠的表格结构。

### 保存标注：`record_paper_annotations`

一个批次对应一个全文版本，包含 1–100 个对象评估。下面以 `mentioned` 为例；`unit` 必须是实际读取的片段，`paper_name` 必须在其中完整出现。别名映射和关系判断仍须由调用者确认，不能仅凭这段代码自动确认。

```python
start = unit["start"] + unit["text"].index(paper_name)
evidence = {
    "block_id": unit["block_id"], "field": unit["field"],
    "start": start, "end": start + len(paper_name), "quote": paper_name,
}
for key in ("row", "column", "note_index"):
    if key in unit:
        evidence[key] = unit[key]

record_paper_annotations(batch={
    "batch_id": batch_id,  # 每次新评估使用新的唯一 ID
    "paper_version_id": version_id,
    "ontology_version": "object-paper-1",
    "extractor": {
        "name": "Codex", "version": model_version,
        "prompt_version": "object-paper-1",
    },
    "annotations": [{
        "target": {
            "release": "dr3", "source_id": source_id,
            "aliases": [{"name": paper_name, "provenance": mapping_reference}],
        },
        "paper_name": paper_name, "subject_scope": "source",
        "identity_status": "resolved", "identity_basis": mapping_explanation,
        "identity_evidence": [evidence], "assessment": "evidence_found",
        "inspected_block_ids": inspected_block_ids,
        "relations": [{
            "role": "mentioned", "statement": "正文明确提到该对象。",
            "evidence": [evidence],
        }],
    }],
})
```

对象必须已存在于共享库，并显式给出 Gaia release。`identity_status="resolved"` 是标注者的判断，不是服务认证。身份不确定时用 `uncertain`、`identity_status="ambiguous"` 和说明性 `note`，不提交确定的关系标签。

每条证据必须属于 `inspected_block_ids`，引文必须逐字等于对应字段的 `[start:end]`。错误的版本、块、单元格、偏移或引文会让整批失败，不留下半批评估。服务先核对文件校验和，再在一个短事务中保存原请求与定位。

所有记录都是 `review_status="pending"`、`scientifically_verified=false`。逐字核对只能证明文本存在，不能验证身份映射、科学推理或原论文结论。第一版不允许调用者直接写入“审核通过”。同一 `batch_id` 和相同规范化请求可安全重试；修改判断需用新 ID，旧评估保留。批次内重复对象的各次出现也按序号保留。

### 查询关系：`query_source_evidence`

```python
query_source_evidence(source_id, release="dr3", role="analysed", limit=20)
query_source_evidence(source_id, release="dr3", paper_version_id=version_id)
query_source_evidence(source_id, release="dr3", assessment="uncertain")
```

查询返回评估历史、逐标签引文、对象范围、已检查块、全文版本与校验和、标注工具和提示词版本。默认保留所有批次和论文版本，按创建时间倒序分页，不把最新意见当作已接受的结论。`evidence_validation` 指保存时核对成功；查询不会重新读取原文件，因此显示 `artifact_integrity="not_rechecked"`。重新读取上下文会再次核对文件。原书目继续用 `lookup_library_source` 查询。

## TWA 试点

试点论文是 Luhman (2023), *A Census of the TW Hya Association with Gaia*，[arXiv:2305.03557v1](https://arxiv.org/abs/2305.03557v1)。显式获取一篇即可开始：

```python
download_paper_corpus(
    label="twa_evidence_pilot",
    papers=[{"arxiv_id": "2305.03557v1", "bibcode": "2023AJ....165..269L"}],
    include_pdf=False,
)
```

正式成员样本应通过 `search_membership_catalogs` → `preview_membership_catalog` → `download_membership_corpus` 选择 VizieR `J/AJ/165/269` 中合适的具体表。目录表名不一定对应论文表号。保留原表、候选标志、未匹配行及重复行，不用天区搜索代替成员选择。

也可以显式选择论文里有 Gaia DR3 标识的少量研究对象，通过 `download_corpus(catalog_path=...)` 建立源输入，保存表号/段落等选择出处。这样的试点可以包含后来被排除的候选，**不是成员目录**。一篇论文集中阅读，再将多个对象一起提交；没有证据的标签留空，不为覆盖所有类型补齐标签。

离线验收使用 `tests/fixtures/twa_evidence.html` 中明确标为**虚构**的 TWA 场景，覆盖提及、分析、前人结果、系统/分量、表格与歧义。这些验证接口和存储契约，不代表模型分类准确率。真实原文件和候选标注是生成数据，保存在 `data/`，不提交 Git。未来评估模型应采用独立人工标注，并按整篇论文分割评估集；同一论文所有版本留在同一侧，分别统计身份、分类和证据归属错误。

## 存储与兼容性

### 第二版：成员资格判断

`object-paper-2` 在对象评估中增加 `results`。第一种结果是成员资格，区分
论文的判断与已获确认的事实，也区分本文分析与沿用前人结论。使用新批次
ID 添加结果，历史 v1 批次保持不变。示例中的 `evidence` 必须替换为从固定
版本读取的真实引文位置，不能直接提交占位列表：

```python
membership_result = {
    "kind": "membership",
    "association": "TWA",
    "population": None,
    "status": "candidate",  # member / candidate / non_member / uncertain
    "reported_label": "possible member",
    "origin": "current_analysis",  # 或 prior_work
    "attribution": "论文作者与年份",
    "method": "论文实际使用的运动学、年龄或其他判断依据",
    "qualification": "原文保留的候选资格、测量限制和假设",
    "evidence": [...],
}
# 放入 annotation.results，并将 batch.ontology_version 设为 object-paper-2。
query_source_evidence(
    source_id="3466327989885650176", release="dr3",
    membership_status="candidate", limit=100,
)
```

`reported_label` 必须出现在引文里。当前研究的结果需要同一评估中的
`reports_result` 标签；前人结果需要 `cites_prior_result`。身份歧义尚未
解决时，不能提交结构化结果。结果中的 `uncertain` 表示作者对成员资格
未定，与“尚不能确定论文说的是哪个天体”不同。

同一评估可保存两个不同归属的结果。例如，2019 年论文在表中沿用前人的
分类，又在正文用 Gaia DR2 数据重新讨论 TWA 31。两项陈述各有依据和
限制，不能把表中的分类误认为本文的新结论。详见
[第二版标注指引](prompts/object-paper-2.md)。

### HTML 查看与固定导出

运行 `uv run python scripts/library.py serve` 后，访问
`http://127.0.0.1:8765/`。源目录可筛选已标注/尚未标注的源，显示已标注
论文数；选中源后打开“文献与证据”。论文卡片保留每个版本和批次，并可按
关系或成员判断筛选。点击“原文”显示精确段落、表格单元格及表头/表注。
字符位置按 Unicode 码点计算，不将浏览器的 UTF-16 索引误作原文位置。

```bash
uv run python scripts/export_paper_evidence.py --source dr3:3466327989885650176 --output data/twa31_evidence.html
uv run python scripts/export_paper_evidence.py --all-annotated --output data/twa_evidence.html
```

导出只读取现有库，嵌入导出时间、批次 ID、论文版本、校验和及所引原文。
页面可离线打开；新标注不会改变已导出的文件。重新生成原有
`source_paper_viz.html` 时，也会为该页面的对象列表嵌入证据快照。

### 扩展试点（2026-11-02）

本地试点包含 **10 篇固定 v1 全文、32 个 Gaia DR3 源、59 个不同的
天体—论文配对**。54 个新增表格评估与原先 3 个配对组成第一轮扩样；
结构化成员判断再补入 TWA 31 在两篇论文中的评估。当前共有 71 条历史
评估记录，其中 14 条 v2 记录携带 15 项成员判断；重复批次中的角色标签
不作为独立研究数量。

| 固定论文 | 本次选取的证据 |
|---|---|
| [2305.03557v1](https://arxiv.org/abs/2305.03557v1) | 新光谱分类、成员讨论 |
| [2501.11716v1](https://arxiv.org/abs/2501.11716v1) | 表3中 this work 径向速度列，区别于合并后的 HQ 列 |
| [1612.02881v1](https://arxiv.org/abs/1612.02881v1) | 表10总辐射光度及距离假设 |
| [1610.01667v1](https://arxiv.org/abs/1610.01667v1) | 新视差测量与零点修正 |
| [1205.3023v1](https://arxiv.org/abs/1205.3023v1) | 重新评估的成员代码与候选限制 |
| [1909.06699v1](https://arxiv.org/abs/1909.06699v1) | 光谱拟合、前人成员代码、本文运动学讨论 |
| [1401.1935v1](https://arxiv.org/abs/1401.1935v1) | 计算的空间速度与带限制的成员拒绝 |
| [1211.2233v1](https://arxiv.org/abs/1211.2233v1) | 绝对视差结果 |
| [1411.1858v1](https://arxiv.org/abs/1411.1858v1) | 盘倾角模型估计及有效范围 |
| [astro-ph/0401318v1](https://arxiv.org/abs/astro-ph/0401318v1) | 12微米测光，区分观测通量与光球预测 |

名称映射来自经过搜索/预览流程取得的 VizieR `J/AJ/165/269/table2`：
完整保留 67 行，67 行匹配成功、无未匹配行，所得 ID 与原表逐行一致。
该次运行使用坐标匹配并保留了 12 行多候选记录。实测发现描述词
“Gaia DR3 source name”未被识别为 ID，现已修复，后续运行可使用明确的
DR3 标识直接匹配；原运行仍保留其实际匹配方式。

这些是围绕可明确定位的表格和段落选取的机器辅助候选，**没有人工复核，
不是分类准确率评估集**。10 篇全文均可用于本次所需的结构化证据读取；
解析质量仍为 partial，不代表图像、全部公式或所有附属数据均已获取。
原始全文、输入、批次和验证报告位于 gitignored `data/`。新 clone 不附带
这些本地数据。复核流程暂不启用，所有记录保持 `pending`。

### 数据库兼容性

共享库 schema v3 新增 `paper_annotation_batches`、`paper_annotations` 和 `paper_annotation_roles`。批次固定全文版本和 JSON 校验和；对象评估保存分类、引文和原定位；标签表用于过滤。它们只追加记录，不覆盖原书目或历史全文，也不发布可被旧任务覆盖的“当前标注”指针。唯一批次 ID 和内容校验处理并发重试。

v2 结构化结果保存在同一不可变标注 JSON 内，不升级数据库版本。v1
规范化请求不新增空 `results` 字段，原哈希与相同批次重试行为保持兼容。
查询响应保留旧 `ontology_version` 字段，表示未变更的七类关系定义；每条
`annotation.ontology_version` 才是该记录的 v1/v2 契约版本，支持列表见
`supported_ontology_versions`。

两个读取接口使用 `get_library(create=False)`，不创建或迁移数据库，能读取 v1/v2 的空标注状态。首次有效写入会进行追加式迁移。备份已有库应使用 `LibraryStore.backup()`。更新代码后重启 MCP 服务或 Codex，让客户端加载新工具；总工具数由 27 增至 30。
