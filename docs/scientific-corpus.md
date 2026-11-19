# 按天体的关联文献建设科研语料

这套接口让 agent 围绕指定天体整理观测、研究方法、分析过程、结果和适用条件，
保存为可查询、可修订的科研记录；也可以先建立开放的研究任务，再持续补入进展。
它适用于文献整理、跨论文比较和训练语料的积累。任务不要求预设标准答案或评分规则。
已有的 CTTS 题目与评分器作为独立的[评测示例](research-tasks.md)保留。

## 直接给 agent 的指令

接入此仓库的 MCP 服务后，可以这样说：

> 请基于 TW Hya、RU Lup、BP Tau、GM Aur 的关联文献，围绕吸积率及其时间变化建设科研语料。
> 先核对天体身份和已有记录，补齐选定论文的全文。整理观测时间、波段、测量量、单位、误差、
> 方法、关键分析步骤、结论和限制，并定位原文证据。区分论文报告、引用前人、星表记录和你的复算。
> 将有依据的内容入库；需要继续研究的问题建立开放任务，引用固定版本的输入记录。
> 已有内容通过修订更新，最后导出本次记录及观测台快照。暂不生成评测题，不调用付费模型接口。

用户负责指定对象和科学方向。Agent 负责检索、阅读、组织内容和调用工具；MCP 服务负责
检查格式、精确引文、数据行与版本并持久保存。MCP 服务自身不运行语言模型，也不执行提交的计算代码。
省略主题时，agent 可以先根据论文提出建设范围；每次入库仍需明确写出科学问题。

## 五个建设接口

| 工具 | 主要输入 | 返回与作用 |
|---|---|---|
| `prepare_research_workspace` | `targets`、`topic`，可选论文分页 | 汇总所选源的书目、已有关系标注、科研记录及全文版本，返回建设流程和内容 JSON Schema |
| `record_research_item` | `item`、`request_id` | 新建科研记录或任务，返回 `record_id` 和第一个 `revision_id` |
| `query_research_items` | 源/主题/关键词/类型/论文版本/记录 ID | 默认查询各记录的最新版本；支持精确版本、历史及分页 |
| `update_research_item` | 记录 ID、预期旧版本、完整新 `item`、请求 ID、修订说明 | 追加新版本；旧版本不变，过期写入会返回冲突 |
| `export_research_corpus` | 导出标签和明确选择范围 | 导出 JSON、JSONL、Markdown、校验清单；可附带观测台快照 |

五个接口都不联网。准备与查询只读，不创建缺失的数据库；写入要求已有天体数据。
首次有效写入会把现有共享库加法迁移到 schema v4。升级后重启 MCP 服务连接，使客户端加载
完整的 **35 个工具**。配置方式见[接入指南](integration-guide.md)。

Agent 的通常调用顺序如下：

1. 用 `resolve_name` 核对名称与 Gaia 身份，再用 `lookup_library_source` 检查本地数据。
   身份始终是 `(release, source_id)`，`source_id` 保留字符串；不要把同名系统、分量或不同 release 混在一起。
2. 调用 `prepare_research_workspace`。它接受 1–20 个明确身份；每颗星的候选论文可用
   `paper_limit`、`paper_offset` 分页。SIMBAD 关联只是阅读候选，不证明论文做过什么分析。
3. 缺少观测或书目时使用现有采集工具；缺少全文时显式调用 `download_paper_corpus`，
   然后检查 `paper_corpus_status`。准备接口不会偷偷下载，也不会把库内没有结果解释为没有相关论文。
4. 使用 `get_paper_evidence_context` 阅读选定固定版本的段落、表头、单元格和表注。
   `lookup_library_paper` 可检查全文版本与文件信息。需要天体—论文关系标签时，继续使用
   `record_paper_annotations`；关系标签与完整科研记录是两种互补的数据。
5. 组织 `item` 后入库；再次查询可避免重复建设。修改时先读取当前版本，再提交完整的新 `item`。
6. 按记录、版本、源或主题导出。记录的入库和导出不代表已经验证科学结论或提升了模型能力。

## 一条记录保存什么

`item.kind` 区分 `scientific_record` 与 `research_task`，两者使用同一套证据和版本机制。

| 字段 | 内容与用途 |
|---|---|
| `title`、`topic`、`question`、`summary` | 名称、检索主题、科学问题和内容概述 |
| `targets` | 天体名称、Gaia release/ID、别名来源、身份依据，以及单源/分量/系统范围 |
| `evidence` | 论文固定版本与精确字符区间，或某次完成采集的原始星表行与列 |
| `units` | 按观测、方法、分析、结果、限制组织的内容；每项注明归属、证据 ID 和所依赖的前序步骤 |
| `measurement` | 可选的数值、单位、原始报告文字、不确定度、上下限/未检出标记、观测时间、仪器与波段 |
| `assumptions`、`calculation` | 分析条件及可复算的公式或代码文本；服务只保存，不执行 |
| `related_revision_ids` | 引用其他科研记录或任务的不可变版本，保存本次分析实际使用的输入 |
| `task` | 开放任务的目标、步骤、预期产物及 `planned` / `in_progress` / `completed` 状态 |
| `author` | 整理者、工具/模型版本和提示词版本；未知版本如实标注 |

每个内容单元的 `origin` 必须明确：`paper_report` 是本文报告，`prior_work` 是论文引用的既有结果，
`catalog_record` 是星表记录，`agent_calculation` 是 agent 复算，`agent_synthesis` 是综合判断，
`synthetic_example` 是明确标注条件的模拟示例。例如，论文采用 155 pc、Gaia 视差的直接倒数
约 158 pc、以及“距离平方修正不足以解释所报差异”应分成原文报告、计算和条件结论，不能合成作者结论。

论文证据的 `span` 使用 Python 字符索引 `[start:end]`，表格行列从 0 起计数。
服务验证摘录与固定解析文件完全一致，并保存原始定位、文件校验和、解析质量和许可记录。
星表证据指定 `run_id`、`member_ordinal`、目标身份和列名；数值由服务从原始行读取，
保留单位声明和行校验和，不让 agent 用另一个同星样本行替代。

这些检查不判断一段话是否足以支持某项结论。所有记录保持 `review_status=pending`、
`scientifically_verified=false`；任务完成只表示已有执行结果。论文许可和解析缺失仍须保留，
不能凭成功入库把全文视为可自由训练或再分发的数据。

## 查询、修订与导出

以下是 MCP 调用示意；ID 应来自实际工具结果，不需要用户手填：

```text
query_research_items(source_id="6010114558131195392", release="dr3")
query_research_items(kind="research_task", text="吸积", limit=20, offset=0)
query_research_items(record_id=<记录ID>, include_history=true)
query_research_items(revision_id=<固定版本ID>)
update_research_item(record_id=<记录ID>, expected_revision_id=<当前版本ID>,
                    item=<修改后的完整item>, request_id=<本次唯一请求ID>,
                    change_note="补充观测日期，保留原报告值")
export_research_corpus(label="ctts_scientific_corpus", record_ids=[<记录ID>],
                       include_observatory=true)
```

`update_research_item` 接收查询结果中的 `item` 部分，不是整个响应，也不是局部补丁。
同一写入重试时沿用相同 `request_id` 和内容；不同写入使用新的请求 ID。
若另一个 agent 已更新，旧 `expected_revision_id` 会被拒绝；读取新版本后再整合修改。

查询单页最多 100 条。`paper_version_id` 筛选直接引用该论文的记录；只引用其他科研记录的任务
不因此自动算作直接引文。导出时，`record_ids`、`revision_ids`、`source_id`、`topic` 四种范围
必须且只能选择一种。记录 ID 选择当时的最新版本，版本 ID 选择精确历史版本；源必须同时明确 release。

导出保存在 `data/research_corpora/<label>/exports/<export_id>/`，每次创建新目录。
`corpus.json` 区分所选版本与所依赖的版本；`corpus.jsonl` 每行一个完整修订，Markdown 便于阅读，
`manifest.json` 记录文件 SHA-256。被引用的旧版本会一并导出，后续修改不会改变旧任务的输入。
JSONL 目前是科研记录交换格式；训练前还需按目标筛选、划分数据并转换样本，不能直接宣称它已是训练成品。

## 在观测台查看并更新在线示例

[全库观测台](https://cetizhao.github.io/Astro-Corpus/#science-section)新增“科研语料与任务”。
可按天体、内容类型和关键词筛选，展开分析过程、原文证据、引用记录及修订历史。
开放任务显示目标、步骤和进展；既有题目单独放在“评测示例”。

当前公开选择包含 TW Hya、RU Lup、BP Tau、GM Aur 的 **4 条科研记录和 2 个开放任务**。
RU Lup 记录补入了观测日期，GM Aur 记录区分了星表观测与复算；任务引用的早期版本仍可读取。
这些是机器辅助整理的建设示例，未经科学复核，也没有实际模型训练成绩。

本机观测台直接读取共享库，刷新即可看到入库与修订。在线版读取显式导出的静态数据，
不会访问个人电脑。要发布新选择，让 agent 完成以下步骤即可：

1. 调用 `export_research_corpus(..., include_observatory=true)`，明确选择要公开的记录。
2. 将返回的 `observatory.json` 保存为仓库的 `examples/observatory/science.json`。
3. 运行 `python scripts/build_pages.py` 检查页面，再提交、推送并合并到 `main`；Pages 工作流发布更新。

导出是 MCP 能力；Git 提交和 Pages 发布由 agent 使用 Git 工具完成。公开快照只包含选定记录、
它们引用的固定版本、有限星表字段和所引摘录，不包含数据库或完整论文；自由文本也应只写准备公开的科研内容。
在线修订历史仅展示本次导出的版本，本机可查询完整历史。完整上下文未导出时，网页明确改为展示入库时保存的摘录。
