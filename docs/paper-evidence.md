# 天体在论文中的证据定位（第一版）

## 原有 Gaia 网页入口

### 在线模式

选择源后，原文献列表每篇提供复选框和“检索关联”。结果直接显示在论文下方，
以“出现在：标题、正文、表格……”概括，原文证据可展开。顶部提供“全选”、
“清空选择”、“检索所选”、“批量检索未完成”和“停止”。明确选择的论文
按该选择处理，不受原先每批10篇限制；重复检索替换该篇结果而不追加重复条目。
后台实时查询 SIMBAD ident 别名
和书目，优先处理标题命中及较新的论文，复用本地 arXiv 标识符或 DOI 解析，
只请求固定版本的 arXiv HTML，默认不请求 PDF 或 LaTeX。逐篇展示摘要、
章节、图注、表格证据；没有 arXiv 编号、HTML 不可用、网络失败明确显示。
每批完成后可继续下一批，停止在当前请求完成后生效。切换源不取消后台任务。
标题匹配只是标题证据，不等于整篇围绕该源，也不排除其他论文。

成功分析只缓存提取的证据、摘要、版本和原文校验值，不存原始HTML/PDF；
缓存使用共享SQLite既有lease/fencing。任务进度目前在服务进程内，重启后
需要重新发起，成功证据缓存可复用。当前未在线检查VizieR附表或图中文字，
没有arXiv编号的论文不尝试猜测标题对应的全文。查看页面本身不发远端请求，
必须点击在线按钮。代理使用服务器进程的 HTTP_PROXY/HTTPS_PROXY 环境配置。

`python scripts/library.py serve` 启动原有全库观测台，默认
`http://127.0.0.1:8765`。搜索并选择天体后，源详情自动请求
`/api/v1/evidence/<release>/<source_id>`，复用书目关系和合作者的
结构化全文 JSON（校验原文件 checksum），展示分类、摘要和命中段落/表格行。
未缓存全文会显示“全文尚未下载”，不误判为论文没有提及。当前仅使用库内
主名称和别名，最多检查100篇关联论文；不在只读网页请求中自动下载全文。
全文需先通过既有 paper-build/download_paper_corpus 工作流获取。


这是已有 SIMBAD 书目关联之上的可选处理层。输入一篇论文 HTML、一个
release-qualified Gaia 源和经调用方核实的 SIMBAD 别名，输出所有命中位置。
不会因为 SIMBAD 收录了论文就伪造原文证据，也不推断整篇文章研究对象。

## Python 使用

```python
from pathlib import Path
from astro_pipeline.paper_evidence import analyze_paper, save_analysis
from astro_pipeline.library import get_library

result = analyze_paper(
    source_id="5412403269717562240", release="dr3",
    aliases=["TW Hya"], alias_provenance="SIMBAD identifiers; manually verified",
    bibcode="实际论文bibcode", paper_version="实际版本",
    html=Path("paper.html").read_text(), document_url="实际原文URL",
)
analysis_id = save_analysis(get_library(create=True), result)
```

示例中的源与别名对应关系应按实际输入核实；接口不会自动查询 SIMBAD。
结果是 JSON 可序列化字典，可由调用方导出 JSON/JSONL。

## MCP

- `analyze_paper_evidence`：同样的输入，默认保存到共享 SQLite，返回
  `analysis_id`、证据、分组、检查范围；`save=False` 仅提取。
- `get_paper_evidence`：按 analysis_id 读取原始 HTML 和完整解析结果，
  不创建不存在的数据库。

## 输出语义

- `groups` 允许重叠：title、abstract、body、figure_caption、table、vizier。
- 论文标题和摘要保存一次，每条 evidence 保存命中别名、原文、位置和方法。
- 正文带章节和版本绑定的段落编号；存在 HTML id 时提供原文锚点。
- 表格保留行号、单元格及表头；复杂跨行/跨列表标记为未完成列结构解析。
- 位置 ordinal 是解析顺序，不冒充出版物页码或正式 Figure/Table 编号。
- `coverage` 仅说明输入材料内检查了什么，不声称拿到了完整论文。
- 未命中返回 `no_match_in_supplied_materials`，不是“论文未提到”。
- 别名命中是文本证据；源身份、双星分量、短编号歧义需要复核。

VizieR 可传 `vizier_tables`：每项必须含与论文相同的 `bibcode`、`table_id`、
`association_provenance` 和由列名/值字典组成的 `rows`。保留原行和查询链接，
论文-目录关系标记为调用方声明，而非程序已独立验证。支持的是提供的行，
不自动下载、坐标匹配或声称扫描整个目录。

## 存储和边界

使用既有 cache_observations/cache_current、锁和 fencing；namespace 为
`paper-evidence-v1`。analysis_id 对输入及结果内容寻址，输入、别名、论文版本或
解析版本变化会产生新 ID，旧结果保留。原始 HTML 与输入表行保存在分析记录中。
这是一版小规模验证实现，尚未将大全文拆成独立共享对象，也未自动挂接 corpus
构建及源级查询。无需修改数据库 schema。

当前解析 HTML 的 h1、摘要容器、p、figcaption、table；适合结构化期刊/arXiv
HTML。未覆盖网站特有结构、PDF/OCR、图内文字、公式解析、自动全文获取和
自动 SIMBAD 别名获取。不要把未识别的容器当作已检查正文。
