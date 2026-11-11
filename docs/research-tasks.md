# CTTS 科研任务试点：先检验语料是否帮助模型使用证据

这一版把 **4 颗经典 T Tauri 星（CTTS）的 Gaia DR3 数据、4 篇论文、12 个综合案例和 16 道基础题**
连接起来。综合案例要求模型将观测条件、物理关系和不同论文的结果放在一起判断，
共设置 51 个分别评分的检查点，提供原文定位、待复核参考解答和可独立运行的离线评测包。
本轮只准备评测包，不调用付费接口。**尚未运行真实模型评测，也没有模型训练提升的结果**。

## 为什么选择这些星和论文

CTTS 是仍在从星周盘吸积物质的年轻恒星。吸积率、消光、辐射诊断和时间变化
需要结合观测条件解释，适合检查模型能否正确使用科学证据。
本次选择依据是论文明确研究过这些目标，不是对 Gregory Herczeg 个人兴趣的猜测。

| 天体 | Gaia DR3 source_id | 与 Herczeg 工作的联系 |
|---|---|---|
| TW Hya | `5401795662560500352` | 2023 年长期吸积研究第一作者；ODYSSEUS I 合作者 |
| RU Lup | `6010114558131195392` | 2005 年紫外研究第一作者；ODYSSEUS I 合作者 |
| BP Tau | `164832740220756608` | ODYSSEUS I 明确列出的 CTTS 目标 |
| GM Aur | `156907907082740480` | ODYSSEUS I 明确列出的 CTTS 目标 |

四颗星均出现在 Herczeg 合著的 [ODYSSEUS I：HST FUV/NUV 光谱](https://arxiv.org/abs/2405.21038v1)。
另外三篇是 [TW Hya 的 25 年吸积研究](https://arxiv.org/abs/2308.14590v1)、
[RU Lup 的紫外谱研究](https://arxiv.org/abs/astro-ph/0504654v1)，以及
[ODYSSEUS III：光学光谱](https://arxiv.org/abs/2408.04465v1)。
**Herczeg 不在第三篇 ODYSSEUS 光学论文的作者名单中**；它因同一观测计划而作为补充。

Gaia 身份于 2026-11-11 通过 SIMBAD 的明确 DR3 别名核对，源 ID 始终按字符串保存。
这是文献目标清单，不是几何区域搜索或成员目录；目标间不要求属于同一协会。
选源依据、固定 arXiv 版本和题目定义保存在
[tasks.json](../benchmarks/ctts_herczeg/tasks.json) 和
[cases.json](../benchmarks/ctts_herczeg/cases.json)。四星样本偏向已充分研究的吸积目标，不能代表全部 CTTS。

## 一条语料包含什么

编译后的 `bundle.json` 将以下内容分开保存：

| 部分 | 主要字段与用途 |
|---|---|
| 天体身份与数据 | 名称、`release`、字符串 `source_id`、原始字段、单位、源数据 run ID 和校验和 |
| 论文版本 | arXiv 固定版本、库内 version ID、作者、解析器版本、内容校验和、许可与解析质量记录 |
| 原文材料 | `material.id`、论文与块 ID、段落/表注/单元格、字符起止位置、原始 HTML 定位 |
| 研究任务 | 问题、目标、任务类型、允许使用的材料 ID、开发集及待复核状态 |
| 参考解答 | 答案、单位、容差、所需证据 ID、简短可核验推导、适用假设；综合案例按 `checkpoints` 分项保存 |
| 缺证据对照 | `ablation` 指定移除的材料及对应参考项变化；不进入模型问题文本 |

字符区间采用 Python 的 `[start:end]`，表格行列从 0 开始计数。
编译时检查不可变 run、文件 SHA-256、论文版本、精确引文和表格行标签；这些检查证明
材料与原文件一致，不能代替科学判断。当前全文取得完整，但解析状态为 `partial`，
包括未取得图像等限制；每篇的实际质量记录随 bundle 保留。

例如 `CTTS-012` 使用 TW Hya 论文对 veiling 的定义和中位值：

- 材料给出 `r = Facc/Fphot`，在 5000–5100 Å 区间的中位 `r = 0.67`。
- 题目明确假设连续谱只有光球和吸积两部分，询问吸积占总连续谱的比例。
- 参考推导为 `r/(1+r) = 0.67/1.67 ≈ 0.4012`，单位为无量纲 `1`。
- 答案必须引用定义与数值两处材料；不能把 `0.67` 直接当作总连续谱中的占比。

这是基于可见证据编写的简短解题过程，包含显式假设，不是对论文作者未发表思维过程的还原。
它可以成为后续训练样本的候选形式。所有参考解答目前保持 `pending`，未称作金标准。

## 12 个综合案例检验什么

同一个吸积率差异，可能来自观测时刻、距离、消光、采用的恒星参数或诊断方法。
综合案例要求先计算可比较的量，再判断证据能够支持什么结论。每个案例使用至少两篇论文，
包含 4–5 个检查点；原来的 16 道题继续作为基础能力检查，不冒充 16 个完整研究案例。

| 案例 | 需要连接的内容 |
|---|---|
| C01 · TW Hya veiling 到吸积率 | 连续谱比例、分辨率对应标定、恒星质量与半径、中位值与均值 |
| C02 · 截断半径与辐射预算 | 重力能公式、半径假设、漏计辐射的上限情景与真实变化的区别 |
| C03 · 日内增强与长期稳定 | 两次紫外观测、非对称区间、长期连续谱研究与时间尺度 |
| C04 · 散布和误差 | log 分布宽度、Gaia 视差形式误差、均值误差与系统误差 |
| C05 · RU Lup 新旧结果 | 距离和半径更新、原始误差、不同观测时期与诊断 |
| C06 · GM Aur 距离与诊断差异 | Gaia 倒数视差、论文采用距离、光学与激波诊断的均值差异 |
| C07 · BP Tau 前人结果 | 引用归属、观测日期、均值比与非同期中心值比 |
| C08 · RU Lup 消光敏感性 | 消光来源、颜色污染、显式假设下的通量修正 |
| C09 · 光度标定复现 | 时间窗口、以天为单位的权重、标定覆盖率和缺失数据 |
| C10 · BP Tau 表格脚注 | 日期上标、同期光度缺口、误差区间和质量限制 |
| C11 · RU Lup 采样代表性 | 两轮各4个 visit 的统计量、短窗口与年度结论 |
| C12 · 跨星标定转用 | 四星平均偏差、百分比的分母、群体与逐历元关系 |

例如 C06 先用 GM Aur 的 Gaia 视差近似求距离，再将它与论文采用的 155 pc 比较。
在固定质量、半径、消光和截断半径比例的题设下，吸积率的距离修正约为 1.04 倍。
光学论文所报线标定均值比激波均值低 56.1%，将其拉齐需要约 2.28 倍。
模型应据此判断：这项约 4% 的距离修正不足以解释该均值差异；同时保留倒数视差近似、
Gaia 数据质量以及平均诊断差异不能替代逐历元比较等限制。

C09 的三点光度是明确标注的教学模拟；C08 的波长消光系数等也是题设，
不伪装成新增实测。全部参考解答是基于公开证据编写的候选标注，仍需科学复核。
它们比单步题更接近实际分析，但尚未通过真实模型试答证明难度、区分度或训练价值。

## 保留的 16 道基础题

| 类型 | 数量 | 具体检查 |
|---|---:|---|
| 时间与采样 | 4 | MJD 首尾跨度、不同目标 Epoch 1 是否同期、两轮观测次数 |
| 测量读取 | 2 | TW Hya 平均吸积率、RU Lup 视觉消光及其单位 |
| 结果归属 | 2 | 表格上标与引文对应、标定关系来自本文还是前人 |
| 结论适用范围 | 2 | 短时变化与长期稳定、平均标定与单历元精度 |
| 数值推理 | 4 | dex 转倍数、veiling 转通量比例、相对不确定度、明确假设下的修正 |
| 信息不足 | 2 | 天测字段不能唯一给出吸积率、旧观测不能给出指定未来历元的瞬时值 |

这些是用于检查流程和发现错误的开发题。4 星和多篇论文属于关联观测计划，
全部放在同一个 `leakage_group`，没有拆成训练集与独立测试集。
未来应以不共享天体、论文和关联观测计划的组建立外部测试集，再检验泛化。

## 在本项目中运行

从仓库根目录执行。现有本机数据已准备好，可以从 `prepare` 开始。
新 clone 先执行联网采集：仅查询这 4 颗星和 4 篇固定版本论文，不请求 XP 产品。
配置需使用 Gaia DR3 和默认启用的共享库；代理沿用运行环境设置。

```bash
uv run --locked python scripts/research_tasks.py acquire
uv run --locked python scripts/research_tasks.py prepare
uv run --locked python scripts/research_tasks.py export
uv run --locked python scripts/research_tasks.py self-check
uv run --locked python scripts/research_tasks.py --suite cases prepare
uv run --locked python scripts/research_tasks.py --suite cases export
uv run --locked python scripts/research_tasks.py --suite cases self-check
uv run --locked python scripts/research_tasks.py --suite cases package
```

`prepare` 从当前发布指针选定不可变 run，之后的导出和评分只使用这一 bundle。
也可传入 `prepare --source-manifest <源快照manifest> --paper-manifest <论文快照manifest>`
复用指定历史版本。全文解析器升级导致块位置或内容变化时，需要重新核对选择器。

基础题产物位于 `data/research_tasks/ctts_herczeg_v1/`，综合案例位于
`data/research_tasks/ctts_herczeg_cases_v2/`，不进入 Git：

- `bundle.json`：任务、原文材料、来源记录和参考解答，供开发者保存。
- `prompts-A.json` / `prompts-B.json` / `prompts-C.json`：分别发送给模型的输入；综合案例另有 D。
- `predictions-*.template.json`：外部模型回答文件的空模板。
- `reference-self-check*.json`：参考答案回灌的评分器自测，**不是模型成绩**。
- `evaluation-package.zip`：解压后有模型输入、回答模板、参考数据与 Python 评分器；
  使用 Python 3.10+ 即可运行，不依赖本项目安装、数据库或网络。解压后的 README 给出步骤。

Git 中只保存选择规则、题目定义、代码和文档。原文许可记录随数据保留；
RU Lup 2005 版本未取得明确许可信息，当前也没有把论文全文作为公共训练集发布。

## 在全库观测台查看与同步

本机和在线版使用同一套“科研任务”界面。顶部导航进入后，可按天体、综合案例/基础题和任务类型筛选；
选择观测台中的天体也会筛选关联任务。展开“材料与证据”可查看 Gaia 字段或论文摘录，
点击“查看原文位置”显示固定版本高亮；综合案例还可展开“需要回答的检查点”。
参考解答与推导默认折叠，每个检查点分别列出答案和证据。论文区也会显示任务选文，
这种关联明确标为“科研任务选文”，不会被算作已完成的关系标注。

本机版通过 `scripts/library.py serve` 读取已生成的 `data/research_tasks/*/bundle.json`。
任务目录可用 `serve --research-dir <目录>` 指定。重新生成任务后点击“立即同步”即可更新；
它不自动采集、执行模型请求或修改数据库。

[在线观测台](https://cetizhao.github.io/Astro-Corpus/) 使用公开示例快照，包含 12 颗星、12 个综合案例与 16 道基础题。
本机能查看已下载的完整论文上下文；在线只带选定摘录，完整论文通过外链查看。
网页不会直接连接本地库，因此本地增加数据不会立即改变线上内容。更新流程为：

```bash
uv run --locked python scripts/research_tasks.py prepare
uv run --locked python scripts/research_tasks.py --suite cases prepare
uv run --locked python scripts/research_tasks.py --suite cases export-observatory --include-bundle data/research_tasks/ctts_herczeg_v1/bundle.json
python scripts/build_pages.py
```

`export-observatory` 只读取任务包绑定的源快照；以上命令合并两个任务包，导出所选 4 颗 CTTS、28 项任务及证据摘录，
写入 `examples/observatory/research.json`。构建程序将它与已有 TWA 示例合并；生成页面位于
`dist/pages/`。检查后提交示例文件及相关改动、推送到 `main`，GitHub Actions 就会自动发布。
本地数据库、凭据、完整论文及其他天体不会随此导出公开。

观测台是人工浏览界面。评测模型时仍使用下面的 A/B/C 提示词文件，避免把页面中的参考解答一起发给模型。

## A/B/C/D 怎样检验价值

| 条件 | 输入 | 可回答的问题 |
|---|---|---|
| A | 问题、选项和统一回答格式 | 模型已有知识能做到什么 |
| B | A 加上逐字段展开的原文摘录/数据 | 给定相关材料是否帮助作答 |
| C | A 加上同一份材料的结构化 JSON | 明确的字段结构是否比 B 更易使用 |
| D（综合案例） | 与 C 同题同格式，移除一处关键材料 | 缺少必要数值或方法时，模型是否恰当回答信息不足 |

**B 和 C 使用完全相同的选定材料和定位信息**，没有只向 C 增加答案、推导或额外事实。
它们比较的是材料的呈现形式。这一版的材料是预先选定的，尚不测自动检索质量，
也不把 C 优于 A 解释为训练改善。模型可能不受益，甚至更差，这也是需要记录的结果。

D 保持问题和输出字段不变。它对缺少输入后无法确定的检查点采用
`insufficient_information` 参考答案，仍然可回答的检查点继续评分；不会要求模型在缺证据时猜出原数值。
一般性的科学限制如果仍能从其他材料得出，就仍应作答。各案例的具体变化写在 `ablation` 中供评测者审阅，
不发送给模型。D 也不是盲测科学知识：它检查遵循当前证据作答的行为，模型可能已在预训练见过论文。

之后若安排试答，选择一个已有模型并固定版本、温度和输出预算。
综合案例完整对照为 **12×4=48 次独立请求**；基础题可另做 16×3=48 次请求。
每题新建上下文，只发送对应 `prompts-*.json` 中的 `messages`；不发送 bundle 或本说明中的解答。
记录原始回答、实际 token 用量、解析失败和截断情况。导出文件给出字符数，未宣称 token 数相等。
不要将同一题 A/B/C/D 放入连续对话，以免前面的材料或答案污染后续条件。

综合案例使用 `checkpoints` 列表作答，每项包括 `id`、`answer`、`unit`、`evidence_ids`。
导出的回答模板已经列出全部字段 ID，数值题必须使用 JSON 数字；信息不足项的 `answer`
为字符串 `insufficient_information`、`unit` 为 `null`，不得编造材料 ID。示例结构如下，空值需替换为真实回答：

```json
{"task_id":"CTTS-C06","checkpoints":[
  {"id":"distance","answer":null,"unit":"pc","evidence_ids":[]},
  {"id":"distance_sigma","answer":null,"unit":"pc","evidence_ids":[]},
  {"id":"update_factor","answer":null,"unit":"1","evidence_ids":[]},
  {"id":"required_factor","answer":null,"unit":"1","evidence_ids":[]},
  {"id":"verdict","answer":null,"unit":null,"evidence_ids":[]}
]}
```

填好包含模型信息的整个模板后，在仓库评分：

```bash
uv run --locked python scripts/research_tasks.py --suite cases score data/research_tasks/ctts_herczeg_cases_v2/predictions-C.json
```

或在解压后的便携包中运行 `python grade.py predictions-C.json score-C.json`。

基础题仍使用原格式，把模型、设置和回答填入模板，例如（`answer` 应使用真实模型返回值）：

```json
{
  "schema_version": "research-predictions-1",
  "bundle_sha256": "从对应提示词文件复制",
  "condition": "C",
  "run_kind": "model_inference",
  "model": "记录实际模型及版本",
  "settings": {"temperature": 0, "max_output_tokens": 512},
  "responses": [
    {"task_id": "CTTS-001", "answer": null, "unit": "day", "evidence_ids": []}
  ]
}
```

```bash
uv run --locked python scripts/research_tasks.py score data/research_tasks/ctts_herczeg_v1/predictions-C.json
```

评分按所选任务包的全部题目计分；漏答算失败，重复或未知题号、检查点 ID 会报错。
数值检查容差与单位，选择题检查选项，B/C/D 另检查每项是否引用了所需材料 ID。
综合案例的 `checkpoint_pass_rate` 是通过的检查点占比；`automated_pass_rate`
是所有检查点均通过的整题比例。例如一题4项答对3项，检查点通过率为75%，整题尚未通过。
每个检查点等权；检查点数不相同的案例在总检查点比例中权重不同，因此两个指标都应报告。
`reference_evidence_match` **只表示材料 ID 匹配，不验证模型解释是否被证据蕴含**；
A 未提供材料，其证据指标为不适用。自由文本解释保留给后续误差分析，不被自动判为正确。

优先比较各题和各类型的“答案＋单位正确率”，再查看原文定位、归属和缺信息题的错误。
当前 12 个综合案例、51 个检查点及 16 道基础题都来自同一关联观测计划，
不能把这些数目当作独立样本量计算可靠的泛化置信区间。
先看哪些计算、归属或科学判断在 C 相对 A 改善，以及 D 是否在应该弃答时停止猜测；
再检查自由文本解释是否真正由证据支持。若全部答对或全部失败，需要调整难度及评分条件，
而不是从准备完成或评分器自测推断语料价值。
后续扩大独立测试集，再用小模型或合作方训练做前后对照，才能回答参数训练是否带来收益。
