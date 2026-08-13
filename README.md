# astro-pipeline 详细说明

> **版本 0.4.0（2026-08-13）** — 新增 VizieR 成员星表搜索/预览/下载、
> 通用 corpus XP Sampled/Continuous、原子 manifest 进度，以及 Gaia DR3
> `gaia_source` 的 HEALPix 分块下载底座。开发主线继续聚焦 **Gaia + arXiv**；
> LAMOST/SDSS 适配保留可用但暂不投入新开发。
> 详见 [CHANGELOG.md](CHANGELOG.md)。

---

本文档说明 `astro-pipeline` 的设计目标、当前实现、数据流、模块职责、可运行演示方式，以及后续要补齐的清洗、cross-match、科学分析与结构化输出层。它面向两类读者：

- **开发者**：知道代码在哪里、每一步做什么、怎么扩展。
- **下游使用者**：理解这个 pipeline 能把公开天文数据库转成统一、可复用、可被 AI 调用的数据接口。

## 1. 项目定位

`astro-pipeline` 的现实目标不是训练基础模型，而是先建立一套可自动运行的天文数据准备管线：

```text
公开天文数据库
  -> 按需查询与下载
  -> 格式统一
  -> 质量控制
  -> cross-match
  -> 科学分析/特征提取
  -> 结构化输出
  -> 供检索、训练、知识库或 agent skill 调用
```

当前策略是 **pipeline 优先、按需获取、演示可跑通**。一般任务不需要把 Gaia 全量数据下载到本地；v0.4.0 同时保留了可分块物化 `gaiadr3.gaia_source` 的能力，供确有本地全表需求时使用。后续仍根据下游消费者的具体格式要求适配输出。

## 2. 当前实现状态

当前代码已经实现一个可被 MCP agent 调用的数据获取与 corpus 框架。**v0.2.1 起开发主线收窄至 Gaia + arXiv**——LAMOST/SDSS 适配仍可调用，但不在近期开发计划内。v0.4.0 把输入从单一天区扩展到用户 CSV、公开成员星表和可恢复的大规模 Gaia 分块任务。

| 能力 | 状态 | 说明 |
|---|---|---|
| Gaia DR3 星表查询 | 已实现 | 通过 `astroquery.gaia` 和 ADQL 查询 `gaiadr3.gaia_source` |
| Gaia XP 光谱源筛选 | 已实现 | 分别使用 `has_xp_sampled` 和 `has_xp_continuous` 标志 |
| Gaia XP 光谱下载 | 已实现 | 通过 Gaia DataLink 获取 BP/RP Sampled/Continuous FITS，支持分批与 checkpoint |
| 公开成员星表 | 已实现 | VizieR 搜索→表预览→显式选择；保留原字段并匹配 Gaia DR3 |
| 用户星表 | 已实现 | CSV 中的 Gaia ID 或 RA/Dec 位置匹配 |
| corpus manifest / 进度 | 已实现 | `manifest.json` 原子更新，MCP 可读阶段与 XP 进度 |
| Gaia 主表大规模下载 | 已实现·底座 | 按 `source_id` HEALPix 区间和游标分页保存 Parquet；默认一次一块 |
| LAMOST DR11 星表查询 | 已实现 · 暂缓 | 通过 VizieR 查询 LRS 星表；v0.2.1 起不在主线 |
| LAMOST 光谱下载 | 已实现 · 暂缓 | 按 `obsid` 下载 FITS；v0.2.1 起不在主线 |
| SDSS DR17 星表查询 | 已实现 · 暂缓 | 通过 `astroquery.sdss` 查询 PhotoObj 与 SpecObj JOIN |
| SDSS 光谱下载 | 已实现 · 暂缓 | 按 `plate/mjd/fiberid` 下载 FITS |
| 统一查询对象 | 已实现 | `ConeSearchQuery` 与 `ParameterQuery` |
| 统一返回对象 | 已实现 | `CatalogResult` 与 `SpectrumResult` |
| Parquet/FITS 输出 | 已实现 | 星表默认保存为 Parquet，可支持 FITS |
| 下载断点续传 | 已实现 | `DownloadCheckpoint` 记录 completed/failed |
| 网络重试 | 已实现 | `tenacity` 指数退避重试 |
| 双向「源↔论文」语料库 API | 已实现 | `astro_pipeline.corpus`：源→论文 + 论文→源，SIMBAD 权威书目 |
| arxiv_id 反向解析 | 已实现 | DOI→arXiv 批量 API，76% 覆盖（v0.2.1 修复） |
| corpus 可视化 | 已实现 | `astro_pipeline.viz`：6 张概览图 + 搜索/点击 focus + XP 光谱面板 |
| cross-match 引擎 | 部分实现 | 成员/用户星表→Gaia DR3 已有批量位置匹配；跨巡天统一匹配仍待扩展 |
| 质量控制模块 | 未实现 | 质量过滤目前散落在查询条件或文档建议中 |
| 光谱分析模块 | 未实现 | 尚未提取等值宽度、S/N、恒星参数等统一特征 |
| JSONL 输出 | 已实现·简化 schema | 每行一个 Gaia 源；下游 schema 稳定后再固化版本 |

## 3. 数据源选择

当前 pipeline 以 Gaia、LAMOST、SDSS 为第一批数据源。

| 数据源 | 代码配置版本 | 核心价值 | 当前接入方式 |
|---|---:|---|---|
| Gaia | DR3 | 全天高精度天测、测光、BP/RP 低分辨率光谱、部分 RVS | Gaia Archive TAP/ADQL + DataLink |
| LAMOST | DR11 v2.0 | 北天大规模低/中分辨率光谱，恒星参数、视向速度、金属丰度 | VizieR 星表 + LAMOST FITS 下载 |
| SDSS | DR17 | 光学测光、星系/类星体/恒星光谱，APOGEE 近红外高分辨恒星光谱 | SDSS SkyServer/SAS via astroquery |

补充说明：

- Gaia DR3 是当前代码的稳定基准；Gaia DR4 预计在 2026 年底前后发布，代码通过 `db_schema` 预留了切换版本的入口。
- LAMOST DR11 v2.0 已在 2025 年面向全球发布，当前配置使用 DR11 的公开地址和 VizieR 星表编号。
- SDSS 代码使用 DR17，因为它是 SDSS-IV 的最终完整发布，接口成熟；SDSS-V 已有更新公开版本，后续可作为新 `SdssSource` 配置或新 source 扩展。

参考入口：

- Gaia Archive: https://gea.esac.esa.int/archive/
- LAMOST DR11 v2.0: https://lamost.org/dr11/v2.0/
- SDSS DR17: https://www.sdss4.org/dr17/

## 4. 代码结构

```text
astro-pipeline/
  config/
    default.yaml            # 默认配置
    benchmark.yaml          # 小样本 benchmark 配置
  src/astro_pipeline/
    config.py               # Pydantic 配置模型
    retry.py                # 网络重试策略
    spectra.py              # Gaia XP Sampled/Continuous 解析与 PNG 渲染
    corpus.py               # 【v0.2】双向「源↔论文」查询 API + CLI
    corpus_builder.py       # 通用 corpus 编排：保存、论文、XP、HTML
    corpus_products.py      # corpus 级 Gaia XP 可用性与续传下载
    membership.py           # VizieR 成员目录搜索、预览、Gaia DR3 匹配
    manifest.py             # corpus 原子 manifest / 下载计划
    bulk_gaia.py            # Gaia gaia_source HEALPix 分块下载底座
    mcp_server.py           # 跨 agent 的 MCP 工具接口
    models/
      query.py              # 查询输入模型
      results.py            # 查询/下载返回模型
      paper.py              # 论文边表 + arxiv/bibcode 列装配
    io/
      writer.py             # Parquet/FITS 输出与 checkpoint
      exporter.py           # 知识表构建（多模态标志 + 论文列）
    crossmatch/             # cross-match 引擎（Gaia id / 位置）
    sources/
      base.py               # 数据源抽象基类
      gaia.py               # Gaia DR3 适配器
      lamost.py             # LAMOST DR11 适配器
      sdss.py               # SDSS DR17 适配器
      external.py           # 外部星表（如 TWAxgdr3.csv）加载
      simbad_refs.py        # 【v0.2】SIMBAD 权威书目 -> 论文边
      arxiv_search.py       # arXiv 关键词检索（仅用于发现，不作归属）
    viz/                    # 交互式 corpus 可视化
      source_paper.py       #   源↔论文概览（6 张图 + focus + XP 光谱）
      vendor/echarts.min.js #   Apache ECharts 5.5.0，构建时内嵌
  scripts/
    build_twa_paper_edges.py # 【v0.2】重建 TWA 论文边表（两阶段、可续跑）
    resolve_arxiv_ids.py    # 【v0.2.1】批量 arXiv DOI -> arxiv_id 反查（~11 min/3k 篇）
    build_viz.py            # 【v0.2.1】重生成 data/twa/source_paper_viz.html
    export_jsonl.py         # 【v0.2.1】corpus → 简化 schema JSONL 导出
    download_twa_xp.py      # 【v0.3.0】下载 TWA XP Sampled FITS
    download_twa_xp_continuous.py # 【v0.3.0】分批下载 TWA XP Continuous FITS
    build_corpus.py         # 通用 name/cone/region/CSV corpus CLI
    gaia_bulk.py            # Gaia 主表分块 plan/run/status CLI
  examples/
    twa_sample.py           # TWA 端到端 demo（SIMBAD 关联 + 正确标志）
  tests/
    test_gaia.py            # Gaia 小样本查询测试
    test_spectra_viz.py     # XP 解析、JSONL 与可视化回归测试
    test_membership.py      # 成员目录搜索/预览/匹配回归测试
    test_corpus_products.py # XP product + manifest 回归测试
    test_bulk_gaia.py       # HEALPix 分页、续传、失败恢复测试
    test_lamost.py          # LAMOST 小样本查询测试
    test_sdss.py            # SDSS 小样本查询测试
    test_exporter_idcol.py  # 【v0.2】多模态 id 列回归测试
    test_bibcode_to_arxiv.py# 【v0.2】bibcode 解析测试
    test_corpus_offline.py  # 【v0.2】双向查询离线测试
    test_paper_edges.py     # 【v0.2】论文边结构 + 过匹配防护测试
    benchmark.py            # 多数据源、多模态下载验证脚本
```

## 5. 核心数据模型

### 5.1 查询输入

`ConeSearchQuery` 表示天区锥形搜索：

| 字段 | 含义 |
|---|---|
| `ra` | 赤经，单位 deg |
| `dec` | 赤纬，单位 deg |
| `radius_deg` | 搜索半径，单位 deg |
| `extra_conditions` | 额外过滤条件，如 `phot_g_mean_mag_max` |
| `limit` | 返回行数上限 |

`ParameterQuery` 表示按字段条件搜索：

| 字段 | 含义 |
|---|---|
| `conditions` | 形如 `{column: (operator, value)}` 的过滤条件 |
| `limit` | 返回行数上限 |
| `order_by` | 排序字段 |

目前 Gaia 支持 cone search 和 parameter query；SDSS 支持二者（v0.2 已修复 `ParameterQuery` 的 `{limit}` SQL 模板缺陷）；LAMOST 当前只支持 cone search。

### 5.2 返回对象

`CatalogResult` 统一封装星表查询结果：

| 字段 | 含义 |
|---|---|
| `source_name` | 数据源名称 |
| `data` | `pandas.DataFrame` |
| `row_count` | 返回行数 |
| `query_description` | ADQL/SQL 或查询描述 |
| `output_path` | 保存到本地的文件路径 |

`SpectrumResult` 统一封装光谱下载结果：

| 字段 | 含义 |
|---|---|
| `source_id` | 源 ID 或光谱 ID |
| `path` | 本地 FITS 路径 |
| `success` | 下载是否成功 |
| `error` | 失败原因 |

## 6. Pipeline 分层说明

### 6.1 配置层

配置入口在 `config/default.yaml` 与 `config/benchmark.yaml`。

主要配置分为四组：

| 配置段 | 作用 |
|---|---|
| `output` | 本地数据目录：星表、光谱、checkpoint |
| `network` | 超时、重试次数、指数退避间隔、下载 chunk size |
| `gaia` | Gaia TAP 地址、schema、默认字段、账号密码 |
| `lamost` | LAMOST DR11 地址、光谱下载地址、VizieR 星表编号 |
| `sdss` | SDSS SkyServer/SAS 地址、data release |

当前默认输出目录：

```text
data/
  catalogs/       # Parquet 星表
  spectra/        # FITS 光谱
  checkpoints/    # 预留目录；实际 checkpoint 当前放在各 spectra 子目录
```

### 6.2 数据源抽象层

所有数据源都继承 `CatalogSource`，必须实现三个方法：

| 方法 | 作用 |
|---|---|
| `get_source_name()` | 返回人类可读的数据源名称 |
| `query_catalog(query, output_path)` | 查询星表并可选保存 |
| `download_spectra(identifiers, output_dir, resume=True)` | 下载光谱，支持断点续传 |

这个抽象让上层 workflow 不必关心 Gaia、LAMOST、SDSS 各自的 API 差异，只需要知道：

```python
source.query_catalog(query, output_path)
source.download_spectra(ids_or_dataframe, output_dir)
```

### 6.3 查询层

查询层负责把统一的 query object 转成各数据库能理解的语言。

Gaia：

- 将 `ConeSearchQuery` 转换为 ADQL 的 `CONTAINS(POINT, CIRCLE)`。
- 将 `ParameterQuery` 转换为 ADQL 的 `WHERE column op value`。
- 默认查询字段包括 `source_id`、`ra`、`dec`、`parallax`、`pmra`、`pmdec`、`phot_g_mean_mag`、`radial_velocity`、`ruwe` 等。
- `query_xp_sources()` 会额外筛选 `has_xp_sampled = true`。

LAMOST：

- 使用 `astroquery.vizier.Vizier.query_region()` 做 cone search。
- 默认查询 LRS 星表 `V/162/dr11l`。
- 返回后将列名统一转为小写，并处理 VizieR 可能产生的重复列名。

SDSS：

- cone search 当前实现为 RA/Dec 矩形近似筛选，再 JOIN `PhotoObj` 与 `SpecObj`。
- 返回字段包括 `ra`、`dec`、`objid`、`specobjid`、`plate`、`mjd`、`fiberID`。
- 查询结果列名统一转为小写。

### 6.4 下载层

下载层负责把星表中的 ID 转换成实际 FITS 文件。

Gaia XP 光谱：

1. 输入 `source_id` 列表或包含 `source_id` 的 DataFrame。
2. 选择 `XP_SAMPLED` 或 `XP_CONTINUOUS` retrieval type。
3. 按配置分批调用 `Gaia.load_data()`；TWA Continuous 脚本默认每批 5 个源。
4. 分别保存到 `gaia_xp_sampled/<source_id>.fits` 或
   `gaia_xp_continuous/<source_id>.fits`。
5. 每批更新 checkpoint，失败后重跑会跳过已完成源。

LAMOST 光谱：

1. 输入 `obsid` 列表或包含 `obsid` 的 DataFrame。
2. 拼接 `spectrum_base_url + obsid`。
3. 保存为 `lamost_spec_{obsid}.fits`。
4. 记录 checkpoint。

SDSS 光谱：

1. 输入必须是包含 `plate`、`mjd`、`fiberid` 的 DataFrame。
2. 调用 `SDSS.get_spectra(plate, mjd, fiberID)`。
3. 保存为 `sdss_spec_{plate}-{mjd}-{fiberid}.fits`。
4. 记录 checkpoint。

### 6.5 输出层

当前输出分为两类：

| 数据 | 当前格式 | 用途 |
|---|---|---|
| 星表 | Parquet | 结构化表格，适合后续 pandas/pyarrow 处理 |
| 光谱 | FITS | 保留天文标准格式、header 和多 HDU 结构 |

`io/writer.py` 也提供了 `save_catalog_fits()`，可将 DataFrame 转为 FITS 表。

后续面向下游消费或大模型训练的输出建议增加三层：

| 输出层 | 建议格式 | 说明 |
|---|---|---|
| 机器可读主表 | Parquet/Arrow | 大规模中间数据，保留列类型 |
| Agent/API 输出 | JSON Lines | 每个天体或每条光谱一条 JSON，适合 RAG/检索 |
| 人类可读报告 | Markdown/HTML/PDF | 用于演示、审核与论文草稿 |

## 7. 当前 benchmark 演示流程

`tests/benchmark.py` 是当前最接近完整 demo 的脚本。它验证六个模态：

| 模态 | 操作 |
|---|---|
| Gaia DR3 / Astrometry | 查询 Kepler 天区附近 Gaia 星表 |
| Gaia DR3 / XP Spectra | 筛选有 XP 光谱的源并下载少量 FITS |
| LAMOST DR11 / LRS Catalog | 查询 Kepler 天区附近 LAMOST LRS 星表 |
| LAMOST DR11 / LRS Spectra | 下载少量 LAMOST 光谱 FITS |
| SDSS DR17 / SpecObj | 查询 SDSS 覆盖天区中的测光-光谱 JOIN |
| SDSS DR17 / Spectra | 下载少量 SDSS 光谱 FITS |

运行方式：

```bash
cd /path/to/astro-pipeline
uv run python tests/benchmark.py
```

如需要 Gaia 账号，可设置环境变量：

```bash
export GAIA_USERNAME="your_username"
export GAIA_PASSWORD="your_password"
uv run python tests/benchmark.py
```

脚本会输出一个 Rich table，展示每个模态的星表行数、文件有效性、光谱下载数量、耗时和 PASS/PARTIAL/FAIL 状态。

## 7.5 双向「源 ↔ 论文」语料库查询（v0.2 核心 demo）

这是本项目当前的核心交付：输入一个 TWA 源，得到它对应的论文（id / title /
链接）以及该源的多模态数据（Gaia / LAMOST / SDSS）；输入一篇论文，得到它研究的源
列表以及每个源的多模态数据。

### 论文关联方式（权威，非子串匹配）

论文关联来自 **SIMBAD 的权威书目**：SIMBAD 为每个天体维护一个经过人工/算法整理的参
考文献表，每一条 bibcode 都是真正研究过该源的论文。流程：

```text
Gaia DR3 source_id
  -> SIMBAD query_object("Gaia DR3 <id>") 得到该源的参考文献 bibcode 列表
  -> bibcode -> arXiv id（arXiv 预印本 bibcode 直接解析；期刊论文用 arXiv DOI API 反查）
  -> bibcode -> title / DOI / year / journal（SIMBAD query_bibcode）
```

这替换了 v0.1 的子串匹配（曾把 94 篇论文匹配到全部 49 个源，是垃圾结果）。论文行缓存
到 `data/twa/simbad_refs.parquet`；已查询源（包括零论文源）和待重试失败记录在
`data/twa/simbad_refs.checkpoint.json`。两者都原子更新，可断点续跑、离线复用，且不会用
空论文占位行污染语料。

### 命令行使用

```bash
# 查询一个源：多模态数据 + 论文列表（带 arxiv id / bibcode / title / url）
python -m astro_pipeline.corpus source 5412403269717562240
python -m astro_pipeline.corpus source 5412403269717562240 --offline --json

# 反向查询一篇论文：它研究的源列表 + 每个源的多模态数据
python -m astro_pipeline.corpus paper 1806.05059                 # 按 arxiv id
python -m astro_pipeline.corpus paper "2018A&A...620A.172Z"      # 按 bibcode
python -m astro_pipeline.corpus paper "Census of the TW Hya"     # 按 title 子串

# 语料覆盖概览
python -m astro_pipeline.corpus summary
```

### Python API

```python
from astro_pipeline.corpus import Corpus

corpus = Corpus(data_dir="data/twa", catalog_path="TWAxgdr3.csv", offline=False)

# 源 -> 多模态 + 论文
res = corpus.lookup_source("5412403269717562240")
# res["multimodal"]["gaia_xp"] | ["lamost"] | ["sdss"]  -> {available, status, rows, data}
# res["papers"] -> [{arxiv_id, bibcode, title, year, journal, doi, url}, ...]

# 论文 -> 源
res = corpus.lookup_paper("1806.05059")
# res["paper"]  -> 论文元数据
# res["sources"] -> [{gaia_id, astrometry, multimodal}, ...]
```

### 多模态数据策略

- **Live + cached fallback**：优先实时查询 Gaia/LAMOST/SDSS；失败时回退到 `data/twa`
  缓存，保证 demo 永远有结果。`--offline` 仅用缓存。
- **优雅降级**：单个巡天失败不会让整体查询崩溃，该模态返回
  `status="unavailable"`/`"not_in_catalog"`。
- **诚实**：若一个源确实没有某巡天数据（例如 TWA 多数成员不在 LAMOST/SDSS 覆盖内），
  对应标志如实为 `False`，而不是像 v0.1 那样全部误报 `False`。

### 重新生成 TWA 演示数据

```bash
# 阶段一（可靠、较快）：SIMBAD 源->bibcode + bibcode 元数据（title/DOI/year）
python scripts/build_twa_paper_edges.py

# 阶段二（推荐）：批量反查 arxiv_id
# 用 arXiv DOI API 的 boolean-OR 批量查询（~20 DOI/请求），3k 篇 ~11 分钟
# 幂等、可续跑，自动备份 paper_edges.parquet + simbad_refs.parquet
python scripts/resolve_arxiv_ids.py

# （legacy）阶段二旧路径：build_twa_paper_edges.py --resolve-arxiv
# 走单 DOI 串行查询 + SIMBAD 重查，~2.6 h/3k 篇，已被 resolve_arxiv_ids.py 取代
```

## 7.6 corpus 可视化（v0.3.0）

`astro_pipeline.viz.build_overview_html` 把 corpus 数据渲染成一个独立的 HTML
页面（默认 `data/twa/source_paper_viz.html`），完全离线、可邮件附件分发。

**包含 6 张概览图**：天空分布散点（颜色 = log 论文数）、Top 15 源 × 论文数、
Top 15 论文 × 覆盖源数、arXiv 覆盖率环形、论文年份分布、源-论文二分网络子图。

**两种 focus 方式**：

- 搜索框：输入 Gaia `source_id`（支持 `Gaia DR3 X` 前缀）→ 该源的论文列表；或
  输入 `arxiv_id` / `bibcode` / 唯一 title 子串 → 该论文的源列表。
- 点击交互：在天空图、横条图、二分图上点击任意节点 → focus 面板同步刷新。

源 focus 面板会优先读取当前 corpus 下的
`gaia_xp_continuous/<source_id>.fits`，没有时回退到
`gaia_xp_sampled/<source_id>.fits`，展示 BP/RP 波长-流量曲线和 flux
uncertainty 阴影。没有本地缓存时，页面明确显示 XP 不可用，不影响其他 corpus
图表和查询。

**重新生成**：

```bash
python scripts/build_viz.py
```

默认读取 TWA 的 `paper_edges.parquet` + `TWAxgdr3.csv`，写出 HTML（一次性备份
旧版到 `.bak`）。ECharts 随 Python 包保存在
`src/astro_pipeline/viz/vendor/echarts.min.js`；生成的 HTML 会把图表库和 corpus
数据全部内嵌，因此可作为单个文件离线打开。



TWA 67 个成员中，部分源本身在 LAMOST/SDSS 没有光谱覆盖（演示如实反映）；少数源在
SIMBAD 没有条目或没有论文引用（结果如实返回空列表），不会伪造关联。

## 8. 建议的完整科学数据流



当前代码完成了"获取"和"原始保存"。面向下游消费，完整 pipeline 应扩展为：

```mermaid
flowchart TD
  A["输入任务: 天区 / 目标列表 / 科学问题"] --> B["配置解析"]
  B --> C["查询 Gaia / LAMOST / SDSS"]
  C --> D["保存原始星表: Parquet"]
  D --> E["下载光谱: FITS"]
  E --> F["基础清洗与质量控制"]
  F --> G["cross-match 与统一主键"]
  G --> H["科学特征提取"]
  H --> I["统一知识表"]
  I --> J["JSONL / Markdown / 图表 / API 输出"]
```

### 8.1 输入任务

建议支持三种入口：

| 入口 | 示例 |
|---|---|
| 天区搜索 | 给定 RA/Dec/radius，查询一个星团或恒星形成区 |
| 目标列表 | 给定 Gaia source_id、LAMOST obsid 或 SDSS plate/mjd/fiber |
| 科学样本条件 | 例如 `G < 15`、`ruwe < 1.4`、`snrg > 10` |

### 8.2 基础清洗

建议新增 `cleaning/` 模块，将不同数据源的质量过滤固化为函数，而不是散落在查询参数里。

Gaia 推荐质量控制：

| 条件 | 作用 |
|---|---|
| `ruwe < 1.4` | 保证天测解可靠 |
| `parallax_over_error > 5` | 控制距离/视差信噪比 |
| `phot_g_mean_mag` 合理范围 | 避免过亮/过暗极端源 |
| BP/RP excess factor 过滤 | 控制测光质量 |
| 视差零点改正 | 后续科学分析必须做 |

LAMOST 推荐质量控制：

| 条件 | 作用 |
|---|---|
| `snrg > 10` 或按科学目标设定 S/N | 控制光谱质量 |
| `fibermask == 0` | 排除问题光纤 |
| `z != -9999` | 排除无效红移 |
| `with_norm_flux == 1` | 确认是否有归一化光谱 |
| 检查 `ANDMASK/ORMASK` | 排除坏像素 |

SDSS 推荐质量控制：

| 条件 | 作用 |
|---|---|
| `clean = 1` | 使用科学级测光 |
| `zWarning = 0` | 使用可靠红移 |
| 按 `class` 区分 STAR/GALAXY/QSO | 避免混合样本 |
| 检查 plate/mjd/fiber 唯一性 | 下载光谱时避免重复 |

### 8.3 Cross-match

cross-match 是本类项目最有价值的部分，因为它把"一个天体在不同巡天中的信息"合并成统一知识单元。

建议统一主键优先级：

```text
Gaia source_id
  > LAMOST gaia_source_id
  > SDSS 与 Gaia 的位置匹配结果
  > 源坐标哈希 uid
```

推荐方案：

| 匹配对象 | 推荐方法 | 说明 |
|---|---|---|
| Gaia × LAMOST | 优先用 LAMOST `gaia_source_id` | DR11 星表已包含 Gaia DR3 交叉字段 |
| Gaia × SDSS DR13 | Gaia Archive 内置 cross-match | 可直接 ADQL JOIN |
| Gaia × SDSS DR17 | 自行位置匹配或 CDS XMatch | Gaia 内置的是 DR13，不是 DR17 |
| 多源泛化匹配 | `astropy.coordinates.SkyCoord.match_to_catalog_sky()` | 小样本和 demo 足够 |

建议新增 `crossmatch/` 模块：

```text
crossmatch/
  gaia_lamost.py       # 用 gaia_source_id JOIN
  gaia_sdss.py         # 位置匹配或外部 xmatch
  generic.py           # SkyCoord 最近邻匹配
```

### 8.4 科学分析与特征提取

建议先实现“足够能演示、也能服务论文方向”的轻量分析。

| 模块 | 输入 | 输出 |
|---|---|---|
| 光谱基础统计 | FITS 光谱 | 波长范围、S/N、中位流量、坏像素比例 |
| Gaia HR 图参数 | Gaia 测光 + 视差 | 绝对星等、BP-RP 颜色 |
| 运动学 | Gaia 自行/视差/RV | 距离、切向速度、6D 相空间子集 |
| LAMOST 恒星参数 | LAMOST 星表 | Teff/logg/[Fe/H]/RV 统一字段 |
| 青年恒星特征 | LAMOST/SDSS 光谱 | Hα 等值宽度、Li 线、红外 excess 接口预留 |

与青年恒星 / 恒星形成研究方向最自然的 demo 题目：

```text
给定一个恒星形成区或年轻星团名称
  -> 搜索并显式选择公开成员星表（或仅做几何天区搜索）
  -> 匹配 Gaia DR3，保留成员概率/分组/标志和未匹配行
  -> LAMOST/SDSS 匹配光谱
  -> 提取运动学、HR 图位置、Hα 活动指标
  -> 输出年轻恒星候选知识表
```

这比泛泛做"全宇宙数据库"更容易形成可展示的科学故事。

### 8.5 大模型/知识库输出（JSONL）

`scripts/export_jsonl.py` 已实现一个**简化 schema** 的 JSONL 导出（每行一个 Gaia 源）。下游 schema 约定明确后会在 `to_record()` 适配层里调整，不影响上游。

**当前 schema**（v0.4.0 沿用，对应 `data/twa/twa_corpus.jsonl`）：

```json
{
  "gaia_id": "5401795662560500352",
  "ra_deg": 165.4659,
  "dec_deg": -34.7048,
  "parallax_mas": 16.629,
  "parallax_error_mas": 0.015,
  "ruwe": 1.175,
  "g_mag": 10.451,
  "pm_ra_masyr": -68.31,
  "pm_dec_masyr": -13.90,
  "multimodal": {
    "gaia_xp": {
      "sampled": {
        "available": true,
        "cached": true,
        "retrieval_type": "XP_SAMPLED",
        "datalink_url": "https://gea.esac.esa.int/data-server/datalink/links?...",
        "path": "gaia_xp_sampled/<source_id>.fits",
        "n_points": 343
      },
      "continuous": {
        "available": true,
        "cached": true,
        "retrieval_type": "XP_CONTINUOUS",
        "bp_coefficients": ["55 coefficients"],
        "bp_coefficient_errors": ["55 errors"],
        "rp_coefficients": ["55 coefficients"],
        "rp_coefficient_errors": ["55 errors"],
        "quality": {"bp_n_relevant_bases": 20, "rp_n_relevant_bases": 16},
        "path": "gaia_xp_continuous/<source_id>.fits"
      }
    },
    "lamost": false,
    "sdss": false
  },
  "papers": [
    {"arxiv_id": "2305.03557", "bibcode": "2023AJ....165..269L",
     "title": "A Census of the TW Hya Association with Gaia.",
     "year": 2023, "url": "https://arxiv.org/abs/2305.03557"}
  ],
  "n_papers": 2154,
  "provenance": {"pipeline_version": "0.4.0"}
}
```

**计划扩展**（待下游 spec 确认后再加）：

- 测光：BP/RP 星等（数据已在 catalog，未导出）
- 恒星参数：Teff / logg / [Fe/H]（Gaia DU 译文已存）
- 视向速度：`RV_gdr3` / `e_RV_gdr3`
- 质量标志：`astrometry_ok`（RUWE<1.4 等）、`photometry_ok`
- Gaia XP 的完整相关矩阵继续保留在原始 FITS 中，不重复膨胀 JSONL。

这个结构的核心原则：

- 保留 provenance，确保每个字段能追溯到数据源。
- 区分 missing value 和 false，避免模型误解。
- 字段名使用清晰单位后缀，如 `_deg`、`_mas`、`_kms`。
- 大表仍然用 Parquet 保存，JSONL 只作为导出层。

## 9. 已知技术问题

v0.2 已修复其中数项（见下表「状态」列）。剩余项为后续改进方向。

| 问题 | 位置 | 影响 | 状态 / 建议 |
|---|---|---|---|
| SDSS `ParameterQuery` SQL 模板未格式化 `{limit}` | `sources/sdss.py` | 参数查询会生成非法 SQL | ✅ v0.2 已修复（f-string） |
| SDSS 返回 HTML 错误页被当作星表缓存 | `sources/sdss.py` | 缓存污染、`<html>` 列 | ✅ v0.2 已修复（HTML 检测 + 抛错） |
| 多模态 `has_*` 标志全部为 False（id 列误判） | `io/exporter.py` | 知识表可用性失真 | ✅ v0.2 已修复（显式 per-survey Gaia 列） |
| 论文子串匹配产生 94×49 垃圾关联 | `sources/arxiv_search.py` | 论文关联完全不可信 | ✅ v0.2 已替换为 SIMBAD 权威书目 |
| `corpus paper <arxiv_id>` 查不到论文 | `paper_edges.parquet` 的 `arxiv_id` 列 | arxiv_id 全空，按 arxiv id 查询路径死的 | ✅ v0.2.1 已修复（`resolve_arxiv_ids.py` 反查，76% 覆盖） |
| Gaia 查询后若异常，`Gaia.ROW_LIMIT` 可能无法恢复 | `sources/gaia.py` | 后续查询可能继承错误 limit | ✅ v0.4.0 已用 `try/finally` 恢复 |
| 名称天区被误解为成员样本 | `intake.py` / MCP | Taurus/Orion 锥形源被当成物理成员 | ✅ v0.4.0 分离 region 与成员目录工作流 |
| Gaia TAP upload 长时间排队 | 成员位置匹配 | 小星表也可能等待数分钟 | ✅ v0.4.0 改为普通小圆 union ADQL + 本地最近邻 |
| XP / SIMBAD 长任务进度不透明 | corpus builder | 用户不知道是否卡住 | ✅ v0.4.0 原子 manifest + `corpus_status` |
| LAMOST 只接 LRS | `sources/lamost.py` | MRS 暂不能查询/下载 | 增加 `mode=lrs/mrs` 或独立 source |
| 光谱下载 URL 规则需实测 | `sources/lamost.py` | 直接 `base_url + obsid` 可能不适配所有文件 | 用官方下载接口或星表中的文件路径字段 |
| 实时查询依赖外网 | `sources/*` | CI/离线环境不稳定 | corpus 已支持 cache fallback；单测已离线化 |

## 10. 下一步开发建议

建议按"能展示、能解释、能扩展"的顺序推进。**v0.2.1 起主线聚焦 Gaia + arXiv**，下文标注「主线」/「暂缓」以区分。

### 阶段 A：把现有原型打磨成稳定 demo（主线）

1. ✅ 修复 SDSS `ParameterQuery`（v0.2 完成）。
2. ✅ 给 Gaia `ROW_LIMIT` 加 `try/finally`（v0.4.0）。
3. ⏳ 给 `benchmark.py` 增加结果 JSON 保存。
4. ⏳ 为每个 source 增加 mock 单元测试。
5. ✅ 固化一个演示天区：TWA（v0.2 起 corpus demo 已基于此）。

### 阶段 B：实现统一知识表（主线）

1. 新增 `cleaning/` 模块。
2. 新增 `schema/objects.py`，定义统一字段。
3. 输出 `catalogs/unified_objects.parquet`。
4. 导出 `exports/unified_objects.jsonl`。

> LAMOST/SDSS 相关项（cross-match、多模态合并）原列于此处，**v0.2.1 起归入暂缓**；
> 现阶段统一表只合并 Gaia 测光/天测/XP 光谱可用性 + SIMBAD 论文边。

### 阶段 C：做一个青年恒星科学 demo（主线）

1. 选择 Taurus、Orion 或其他青年恒星区。
2. 搜索并预览公开成员目录，明确选择 established members 或 candidates；也可另建几何 field-source 对照样本。
3. 匹配 SIMBAD 论文书目（已有 corpus API）。
4. 做 HR 图、运动学筛选、活动指标（Hα 等接口预留）。
5. 输出可展示的 Markdown/PDF 报告。

### 阶段 D：沉淀为 agent skill

每个 pipeline 步骤都可以写成 skill：

| Skill | 输入 | 输出 | 状态 |
|---|---|---|---|
| Gaia 查询 MCP | 天区/条件 | Gaia 结构化结果 | 已实现 |
| corpus 查询 MCP | Gaia id / arxiv id | 源↔论文结构化数据 | 已实现 |
| 成员目录 MCP | 星团/协会名 | 原目录 + Gaia 匹配表 | 已实现 |
| Gaia bulk MCP | HEALPix 计划 | 分区 Parquet | 已实现·底座 |
| 跨巡天 cross-match | 两个任意星表 | 匹配表 | 暂缓 |
| 质量控制 skill | 原始星表 | 清洗星表 | 待编写 |
| 语料导出 skill | 统一知识表 | JSONL/Markdown | 待编写 |

## 11. 交付物定义

当前项目的交付物定义为：

| 交付物 | 文件/目录 | 验收标准 |
|---|---|---|
| Pipeline 代码 | `src/astro_pipeline/` | 能查询 Gaia/LAMOST/SDSS 小样本 |
| 配置文件 | `config/*.yaml` | 可切换数据目录、网络参数、数据源版本 |
| 双向 corpus API | `astro_pipeline.corpus` CLI/Python | `source/paper/summary` 三个子命令端到端可用 |
| MCP agent 接口 | `.mcp.json` + `mcp_server.py` | clone 后可搜索/预览/下载/查进度 |
| 成员目录语料 | `data/<label>/membership_catalog.*` + `members.*` | 原表、Gaia 匹配、未匹配行均可审计 |
| 下载状态 | `data/<label>/manifest.json` | 阶段、计数、警告、错误和产物原子更新 |
| Gaia 主表分块 | `data/gaia_bulk/<label>/` | HEALPix 区间无重叠、游标分页、可恢复 |
| 交互式可视化 | `data/twa/source_paper_viz*.html` | 6 张图 + 搜索/点击 focus + 天测与 XP 光谱面板 |
| JSONL 语料导出 | `data/twa/twa_corpus.jsonl` | 67 源、每行 schema 见 §8.5 |
| SIMBAD 论文边表 | `data/twa/paper_edges.parquet` | 6505 边、76% 有 arxiv_id |
| 离线测试套件 | `tests/` | `uv run pytest -q` 全过；网络测试默认排除 |
| 说明文档 | 本 README + `CHANGELOG.md` | 新人能理解架构和下一步 |

> 注：`data/` 目录被 `.gitignore` 排除（数据可重生），新合作者 clone 后需要按 §13 跑一次 bootstrap 才能复现上述文件。

## 12. 一句话总结

`astro-pipeline` 的本质是把 Gaia 等公开天文档案库包装成统一的查询、下载、匹配和导出工作流。**当前阶段（v0.4.0）聚焦 Gaia + arXiv 链路**：双向「源↔论文」、公开成员目录、用户/天区输入、Gaia XP、进度 manifest、分块主表下载、HTML 与 JSONL 已具备；下一步关键是把质量控制、科学特征和更多 Gaia 产品做成可配置层，而不是默认隐式筛选。

## 13. 新合作者上手（bootstrap）

仓库 `git clone` 后是**纯代码**——`data/` 和 `TWAxgdr3.csv` 都被 `.gitignore` 排除（数据可重生、且第三方目录有版权）。要本地跑通，按以下顺序：

### 13.1 安装

```bash
git clone <repo>
cd astro-pipeline
uv sync                              # 装依赖
uv run pytest -q                     # 离线测试；network 标记默认排除
```

### 13.2 获取 TWA 输入星表

`TWAxgdr3.csv` 来自 Ducourant et al. 2023（**bibcode `2023AJ....165..269L`**，即 arxiv `2305.03557`，本 corpus 里就有）。两种获取方式：

- **VizieR**: 访问 https://vizier.cds.unistra.fr/viz-bin/VizieR-3?-source=J/AJ/165/269/table1，选 ASCII/CSV 输出，下载后重命名为 `TWAxgdr3.csv` 放在仓库**根目录**
- **向维护者索取**：仓库 owner（cetizhao）处有现成文件

### 13.3 重建 corpus 数据（首次约 1 小时，主要是网络）

```bash
# 1) SIMBAD 论文书目（~30 min，受 SIMBAD 限流）
python scripts/build_twa_paper_edges.py

# 2) 反查 arxiv_id（~11 min，受 arXiv 限流）
python scripts/resolve_arxiv_ids.py

# 3) 下载 Gaia XP Sampled 与 Continuous
python scripts/download_twa_xp.py
python scripts/download_twa_xp_continuous.py

# 4) 生成可视化 + JSONL
python scripts/build_viz.py
python scripts/build_viz.py --inline --output data/twa/source_paper_viz_share.html
python scripts/export_jsonl.py
```

跑完后 `corpus summary`、`corpus source <gaia_id>`、可视化 HTML 等都可用。

### 13.4 新建任意 corpus

日常推荐通过 MCP 描述需求。命令行等价入口：

```bash
# 几何场源（不是星团成员）
uv run python scripts/build_corpus.py --cone 84 -5 0.2 \
  --label orion_field --out-dir data/orion_field --limit 50 --skip-simbad

# 同时下载可用的 Gaia XP Sampled
uv run python scripts/build_corpus.py --name "TW Hya" \
  --label tw_hya --out-dir data/tw_hya --xp-sampled

# Gaia 主表分块：先计划，再明确跑一个块
uv run python scripts/gaia_bulk.py plan gaia_demo \
  --level 12 --pixel-start 157212945 --pixel-stop 157212946
uv run python scripts/gaia_bulk.py run gaia_demo --max-chunks 1
```

公开成员目录目前以 MCP/Python API 为主要入口，完整顺序见 [docs/integration-guide.md](docs/integration-guide.md)。

### 13.5 日常使用（不需要重建数据时）

参见 §7.5 / §7.6 的 CLI 命令清单。`--offline` 用本地缓存，不联网。

### 13.6 常见坑

- **`FileNotFoundError: TWAxgdr3.csv`** → §13.2 没做
- **`corpus paper <arxiv_id>` 返回 "No paper matched"** → §13.3 第 2 步没跑（arxiv_id 列为空）
- **`pytest` 全挂在网络错误** → 你大概率跑成了 `pytest -m network`；默认就是离线的，直接 `pytest` 即可
- **指定 Taurus/Orion 后得到的不是成员** → `region`/`cone` 是场源；用成员目录三步工作流
- **XP 下载失败但 members 已生成** → DataLink 是可选阶段；看 `manifest.json` 后用同一 label 重跑

## 14. MCP 集成（让 AI agent 调用本 pipeline）

本仓库自带 [MCP server](https://modelcontextprotocol.io/)（`src/astro_pipeline/mcp_server.py`），覆盖 corpus 查询、名称/天区输入、公开成员目录、XP 下载、进度查询和 Gaia 主表分块。用户可以只描述目标，不必亲自运行命令。

### 14.1 三条主要工作流

| 需求 | Agent 调用顺序 |
|---|---|
| 某个天区的所有 Gaia 场源 | `download_plan` → `download_corpus(cone/region=...)` → `corpus_status` |
| Taurus/Pleiades 等物理成员 | `search_membership_catalogs` → `preview_membership_catalog` → `download_membership_corpus` |
| 大规模物化 Gaia 主表 | `create_gaia_bulk_plan` → `run_gaia_bulk_download` → `gaia_bulk_status` |

`region` 只是以名称为中心的几何锥形搜索，不等于成员。成员工作流保留原 VizieR 表、成员概率/标志、Gaia 匹配角距离和未匹配行，不默认筛选。

`download_corpus` 与 `download_membership_corpus` 可用 `xp_sampled=true` / `xp_continuous=true` 按需下载光谱；默认关闭。每次构建在 `data/<label>/manifest.json` 记录阶段、数量、错误和产物，另一个 agent 可随时调 `corpus_status(label)`。

完整工具表、参数与警告见 [docs/integration-guide.md](docs/integration-guide.md)，数据结构见 [docs/architecture.md](docs/architecture.md)。

### 14.2 Clone 后连接

仓库根目录的 `.mcp.json` 使用项目相对命令。支持项目 MCP 配置的 agent 在 clone 后从仓库根目录启动即可：

```bash
git clone <repo>
cd astro-pipeline
uv sync
uv run pytest -q
```

其他 MCP 客户端配置相同的工作目录与命令：

| 字段 | 值 |
|---|---|
| working directory | `<你的 repo 绝对路径>` |
| command | `uv` |
| args | `["run", "python", "-m", "astro_pipeline.mcp_server"]` |

没有 TWA 演示数据时 server 仍可启动，实时搜索与构建工具可用；只有默认缓存的 `lookup_source` / `lookup_paper` / `summarize_corpus` 需要先准备对应数据。

### 14.3 配置与凭据

环境变量可切换启动时加载的默认 corpus：

```bash
ASTRO_PIPELINE_DATA_DIR=/path/to/data/twa
ASTRO_PIPELINE_CATALOG=/path/to/TWAxgdr3.csv
```

个人 Gaia 凭据放在 gitignored `config/local.yaml` 的 `gaia.username` / `gaia.password`；不要提交或输出真实密码。匿名 Gaia TAP 不依赖该账号，XP DataLink 才可能需要。

### 14.4 典型自然语言请求

- “查一下 Gaia DR3 5401795662560500352 的论文。”
- “搜索 Taurus 的公开成员星表，先让我看候选表，不要直接下载。”
- “选择 Luhman 2018 的正式成员表，先取 10 个，下载 Sampled XP，不查论文。”
- “查看 taurus_members 的下载进度。”
- “给 Gaia 主表建 level-5 全天下载计划，但先只跑一个 chunk。”
- “画 taurus_members 中这个源的 Gaia XP 光谱。”（`gaia_xp_spectrum` 传 `label`）

### 14.5 调试

```bash
# 启动 server 自己测一下（看启动日志，按 Ctrl+C 退出）
uv run python -m astro_pipeline.mcp_server

# 用 MCP inspector 浏览器交互调试（需要 mcp[cli] extra）
uv run mcp dev src/astro_pipeline/mcp_server.py

# 在 Claude Code 里查 MCP 状态
/mcp
```

常见服务错误、resume 与真实 smoke test 见 [docs/operator-runbook.md](docs/operator-runbook.md)。
