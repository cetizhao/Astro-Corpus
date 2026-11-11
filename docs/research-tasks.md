# CTTS 科研任务试点：先检验语料是否帮助模型使用证据

这一版把 **4 颗经典 T Tauri 星（CTTS）的 Gaia DR3 数据、4 篇论文和 16 道科研任务**
连接起来，提供原文定位、待复核参考解答、A/B/C 提示词导出和离线评分。
无需本地训练硬件；后续可用已有模型的推理接口完成第一轮实验。
目前已完成数据取得、任务编译和评分器自测，**尚未运行真实模型评测，也没有模型训练提升的结果**。

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
[tasks.json](../benchmarks/ctts_herczeg/tasks.json)。四星样本偏向已充分研究的吸积目标，不能代表全部 CTTS。

## 一条语料包含什么

编译后的 `bundle.json` 将以下内容分开保存：

| 部分 | 主要字段与用途 |
|---|---|
| 天体身份与数据 | 名称、`release`、字符串 `source_id`、原始字段、单位、源数据 run ID 和校验和 |
| 论文版本 | arXiv 固定版本、库内 version ID、作者、解析器版本、内容校验和、许可与解析质量记录 |
| 原文材料 | `material.id`、论文与块 ID、段落/表注/单元格、字符起止位置、原始 HTML 定位 |
| 研究任务 | 问题、目标、任务类型、允许使用的材料 ID、开发集及待复核状态 |
| 参考解答 | 答案、单位、容差、所需证据 ID、简短可核验推导、适用假设 |

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

## 16 道题覆盖什么

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
```

`prepare` 从当前发布指针选定不可变 run，之后的导出和评分只使用这一 bundle。
也可传入 `prepare --source-manifest <源快照manifest> --paper-manifest <论文快照manifest>`
复用指定历史版本。全文解析器升级导致块位置或内容变化时，需要重新核对选择器。

默认产物位于 `data/research_tasks/ctts_herczeg_v1/`，不进入 Git：

- `bundle.json`：任务、原文材料、来源记录和参考解答，供开发者保存。
- `prompts-A.json` / `prompts-B.json` / `prompts-C.json`：分别发送给模型的输入。
- `predictions-*.template.json`：外部模型回答文件的空模板。
- `reference-self-check*.json`：参考答案回灌的评分器自测，**不是模型成绩**。

Git 中只保存选择规则、题目定义、代码和文档。原文许可记录随数据保留；
RU Lup 2005 版本未取得明确许可信息，当前也没有把论文全文作为公共训练集发布。

## 在全库观测台查看与同步

本机和在线版使用同一套“科研任务”界面。顶部导航进入后，可按天体和任务类型筛选；
选择观测台中的天体也会筛选关联任务。展开“材料与证据”可查看 Gaia 字段或论文摘录，
点击“查看原文位置”显示固定版本高亮；参考解答与推导默认折叠。论文区也会显示任务选文，
这种关联明确标为“科研任务选文”，不会被算作已完成的关系标注。

本机版通过 `scripts/library.py serve` 读取已生成的 `data/research_tasks/*/bundle.json`。
任务目录可用 `serve --research-dir <目录>` 指定。重新生成任务后点击“立即同步”即可更新；
它不自动采集、执行模型请求或修改数据库。

[在线观测台](https://cetizhao.github.io/Astro-Corpus/) 使用公开示例快照，包含 12 颗星与全部 16 道任务。
本机能查看已下载的完整论文上下文；在线只带选定摘录，完整论文通过外链查看。
网页不会直接连接本地库，因此本地增加数据不会立即改变线上内容。更新流程为：

```bash
uv run --locked python scripts/research_tasks.py prepare
uv run --locked python scripts/research_tasks.py export-observatory
python scripts/build_pages.py
```

`export-observatory` 只读取任务包绑定的源快照，导出所选 4 颗 CTTS、16 道任务及证据摘录，
写入 `examples/observatory/research.json`。构建程序将它与已有 TWA 示例合并；生成页面位于
`dist/pages/`。检查后提交示例文件及相关改动、推送到 `main`，GitHub Actions 就会自动发布。
本地数据库、凭据、完整论文及其他天体不会随此导出公开。

观测台是人工浏览界面。评测模型时仍使用下面的 A/B/C 提示词文件，避免把页面中的参考解答一起发给模型。

## A/B/C 怎样检验价值

| 条件 | 输入 | 可回答的问题 |
|---|---|---|
| A | 问题、选项和统一回答格式 | 模型已有知识能做到什么 |
| B | A 加上逐字段展开的原文摘录/数据 | 给定相关材料是否帮助作答 |
| C | A 加上同一份材料的结构化 JSON | 明确的字段结构是否比 B 更易使用 |

**B 和 C 使用完全相同的选定材料和定位信息**，没有只向 C 增加答案、推导或额外事实。
它们比较的是材料的呈现形式。这一版的材料是预先选定的，尚不测自动检索质量，
也不把 C 优于 A 解释为训练改善。模型可能不受益，甚至更差，这也是需要记录的结果。

选择一个已有模型，固定模型版本、温度和输出预算，分别进行 **16×3=48 次独立请求**。
每题新建上下文，只发送对应 `prompts-*.json` 中的 `messages`；不发送 bundle 或本说明中的解答。
记录原始回答、实际 token 用量、解析失败和截断情况。导出文件给出字符数，未宣称 token 数相等。
不要将同一题 A/B/C 放入连续对话，以免前面的材料或答案污染后续条件。

把模型、设置和回答填入模板，例如结构如下（`answer` 应使用真实模型返回值）：

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

评分按全部 16 题计分；漏答算失败，重复或未知题号会报错。
数值检查容差与单位，选择题检查选项，B/C 另检查是否引用了所需材料 ID。
`reference_evidence_match` **只表示材料 ID 匹配，不验证模型解释是否被证据蕴含**；
A 未提供材料，其证据指标为不适用。自由文本解释保留给后续误差分析，不被自动判为正确。

优先比较各题和各类型的“答案＋单位正确率”，再查看原文定位、归属和缺信息题的错误。
当前 16 题规模小且相互关联，不能作为 16 个独立科研案例计算可靠的泛化置信区间。
若已有模型在这些题上全部答对，应增加更具区分度的任务，而不是宣称语料已经提高了能力。
后续扩大独立测试集，再用小模型或合作方训练做前后对照，才能回答参数训练是否带来收益。
