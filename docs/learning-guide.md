# astro-pipeline 工程与 AI 语料学习指南

> 面向非计算机专业的天体物理研究生。本文档不是 API 速查表，而是围绕本仓库建立
> 软件工程、科学数据工程与 AI 语料工程知识体系的“活教材”。
>
> 当前版本：2026-08-14。后续每完成一个学习闭环，继续在本文档中补充。

## 1. 学习目标与使用方法

最终目标不是记住仓库中每个函数，而是能够独立完成以下工作：

1. 从用户需求和科学问题中提取可执行的输入、输出与约束；
2. 追踪一次请求在模块、函数和数据文件之间的完整流动；
3. 用测试表达科学和工程不变量，并诊断失败原因；
4. 设计带有 schema、provenance、质量语义和恢复机制的数据产品；
5. 判断一份天文数据是否适合某个 AI 任务；
6. 独立实现一项功能，并同时交付测试、文档与评估。

本文档不应从头背诵。每次学习使用如下闭环：

```text
概念依赖定位
  → 阅读一个最小真实例子
  → 预测代码行为
  → 运行或修改一个测试
  → 用自己的话复述
  → 用反例验证理解
  → 更新本学习指南
```

### 1.1 当前能力诊断

用户自评采用 `0 = 陌生，1 = 能跟读，2 = 能独立解释并修改`：

| 领域 | 当前自评 | 学习安排 |
|---|---:|---|
| 终端、路径、环境 | 1 | 随真实任务补齐 |
| Git | 1 | 随版本修改补齐 |
| Python 函数与数据结构 | 2 | 不从基础语法重学 |
| 类、模块与 import | 1 | 当前优先补齐 |
| pandas / 表格处理 | 2 | 直接用于项目实验 |
| SQL / ADQL | 1 | HTTP/API 之后进入 |
| HTTP / API | 0 | 第二阶段系统学习 |
| pytest / 单元测试 | 0 | 当前优先补齐 |
| FITS / Parquet / JSONL | 1 | 数据工程阶段深化 |
| schema / provenance | 0 | 与测试并行学习 |
| retry / checkpoint / 幂等 | 0 | 可靠性阶段学习 |
| embedding / RAG | 0 | 数据工程基础完成后学习 |

诊断结论：已经具备科学样本意识、Python 函数和 pandas 基础。当前最底层缺口不是
RAG，而是“对象与调用 → 模块 → 契约 → 测试 → schema/provenance”。

## 2. 项目文档地图

| 文档 | 作用 | 何时阅读 |
|---|---|---|
| [README](../README.md) | 项目总览、能力清单、演示与路线图 | 需要全局定位时查阅 |
| [技术栈与架构讲解](technical-overview.md) | 分层架构、数据流、存储原理、MCP 作用域和分发边界 | 先理解整个系统，再进入单项练习 |
| [integration guide](integration-guide.md) | MCP 安装、本人/协作者接入和工具工作流 | 配置客户端或学习请求如何进入系统时 |
| [architecture](architecture.md) | 系统边界、数据契约、目录结构 | 学架构和 schema 时 |
| [operator runbook](operator-runbook.md) | 运行、恢复与故障排查 | 学可靠性和运维时 |
| [CHANGELOG](../CHANGELOG.md) | 版本演进与真实 bug | 学工程权衡和回归测试时 |
| [AGENTS](../AGENTS.md) | 科学语义与安全边界 | 判断系统不变量时 |
| 本学习指南 | 概念依赖、带读、练习与答案 | 持续复习 |

代码健康基线（2026-08-13）：

```text
uv run pytest -q
103 passed, 6 deselected
```

`6 deselected` 主要是默认不运行的网络测试。测试全通过只说明代码满足当前已有测试，
不表示所有科学假设都正确，也不表示测试覆盖了所有行为。

## 3. 总体知识依赖

```text
对象、变量和调用
  → 函数、方法、类和模块
  → 参数传递、异常、上下文管理器
  → 函数也是对象、装饰器
  → pytest、fixture、mock
  → 接口契约、schema、provenance
  → HTTP/API、ADQL、文件格式
  → retry、checkpoint、幂等、原子写入
  → 可复现科学数据管线
  → AI 语料选择、去重、划分、许可和质量评估
  → embedding、检索、RAG 和训练实验
```

不能跳过中间层直接学 RAG。否则可能会调用现成框架，却无法判断数据泄漏、错误关联、
服务失败或结果不可复现等问题。

---

# 第一部分：从“契约”到可执行测试

## 4. 什么是软件契约

“契约”不是 Python 关键字。经典术语是 Bertrand Meyer 提出的 **Design by Contract**
（契约式设计）。广义的软件接口契约表示：

> 调用者需要满足什么条件，以及被调用组件承诺什么行为。

一个实用的契约至少可以包含：

| 组成 | 问题 | 本项目示例 |
|---|---|---|
| 参数与类型 | 能传入什么形式？ | `region: tuple[str, float]` |
| 前置条件 | 调用前必须满足什么？ | `cone/catalog/name/region` 必须且只能提供一种 |
| 后置条件 | 成功后保证什么？ | 返回具有标准列名的 `DataFrame` |
| 不变量 | 哪些性质始终成立？ | Gaia ID 在标准表中保存为字符串 |
| 失败行为 | 什么情况下如何失败？ | 没有输入时抛出 `ValueError` |
| 副作用 | 是否联网、写文件或修改状态？ | 名称/天区解析需要访问 SIMBAD/Gaia |
| 语义边界 | 结果能代表什么？ | `region` 是几何场源，不是物理成员 |

### 4.1 类型注解不是完整契约

考虑：

```python
def search(region: tuple[str, float]) -> pd.DataFrame:
    ...
```

它没有回答：

- 半径能否为负数；
- 名称解析失败时发生什么；
- 返回的是场源还是物理成员；
- 最多返回多少行；
- 网络失败是否重试；
- 输出必须有哪些列。

而且 Python 默认不在运行时强制执行类型注解。注解主要供人、编辑器和静态检查工具
使用；只有额外的运行时机制才会主动验证它们。

### 4.2 类型正确仍可能违反契约

```python
resolve_members(
    name="TW Hya",
    region=("Taurus", 2.0),
)
```

两个参数单独看都符合各自类型，但组合起来违反“只能选择一种输入模式”的前置条件。
因此 [resolve_members](../src/astro_pipeline/intake.py) 还需要主动执行：

```python
provided = [
    key
    for key, value in {
        "cone": cone,
        "catalog": catalog,
        "name": name,
        "region": region,
    }.items()
    if value is not None
]

if len(provided) != 1:
    raise ValueError("Exactly one input mode is required")
```

契约可能分散在函数签名、docstring、校验代码、测试、schema、manifest 和架构文档中。
测试是契约的可执行样例，但不是对完整契约的数学证明。

### 4.3 科学语义也是契约

`region=("Taurus", 2.0)` 的选择函数是：

```text
SIMBAD 解析 Taurus 的天空中心
  → 以该中心进行半径 2 度的 Gaia 几何锥形搜索
```

它可能包含前景星、背景星和真正的 Taurus 成员。二维天区选择无法单独证明共同距离、
运动学、年龄或形成历史。因此输出不能声称为“Taurus 物理成员表”。

物理成员必须走：

```text
搜索公开成员星表
  → 预览候选表及其成员/候选语义
  → 明确选择一张文献表
  → 与 Gaia 匹配并保留原始成员字段和未匹配行
```

反例：即使所有测试都通过，用户仍可给几何场源 corpus 起名 `taurus_members`。名称不会
改变样本选择函数。这说明测试只能验证已有规则，无法自动替研究者完成科学解释。

## 5. 读 Python 代码所需的对象模型

这一部分是理解跨文件调用、fixture 和 monkeypatch 的前置知识。如果读到测试时感觉
“不知道这个名字从哪里来、为什么可以被替换”，应暂停测试并回到这里。

### 5.1 名字指向对象

Python 赋值最重要的心智模型不是“把内容装进变量盒子”，而是“让名字指向对象”：

```python
x = [1, 2]
y = x
y.append(3)
```

此时 `x` 和 `y` 指向同一个列表对象，所以二者看到的内容都是 `[1, 2, 3]`。`y = x`
没有复制列表。

但重新绑定名字只改变那个名字本身：

```python
y = [9]
```

现在 `y` 指向新列表，而 `x` 仍指向原来的 `[1, 2, 3]`。这个“共享对象”和“重新绑定”
的区别，正是理解跨模块 monkeypatch 的关键。

### 5.2 函数、方法、类、对象、属性和模块

| 表达式 | 点号左边是什么 | 右边是什么 | 解释 |
|---|---|---|---|
| `resolve_members()` | 无 | 函数 | 调用项目函数 |
| `pd.DataFrame(...)` | `pd` 模块 | 类 | 创建一个 DataFrame 对象 |
| `df.head(5)` | DataFrame 对象 | 方法 | 调用对象的行为 |
| `df.columns` | DataFrame 对象 | 属性 | 读取对象保存的信息 |
| `pytest.raises(...)` | `pytest` 模块 | 函数 | 创建异常检查上下文 |
| `monkeypatch.setattr(...)` | fixture 对象 | 方法 | 临时替换某个属性 |
| `Path("x").exists()` | Path 对象 | 方法 | 检查路径是否存在 |

方法可以暂时理解为“与类或对象绑定的函数”。阅读 `a.b(...)` 时依次问：

1. `a` 是模块、类，还是一个对象？
2. `a` 在哪里被导入、创建或传入？
3. `b` 是属性还是可调用的方法？
4. 这个调用返回什么对象？

### 5.3 用数据流读嵌套调用

```python
out = _canonicalize(_gaia_native_df())
assert "Gaia" in out.columns
```

不要一口气读成一句话，拆成：

```text
_gaia_native_df()
  → 返回原生 Gaia DataFrame
  → 作为参数传给 _canonicalize(...)
  → 返回标准化 DataFrame
  → 保存为 out
  → 读取 out.columns
  → 检查其中是否有 "Gaia"
```

### 5.4 模块与跨文件 import

一个 `.py` 文件被导入后，会产生一个模块对象。模块拥有自己的命名空间，可以暂时理解
成“名字到对象的映射表”。导入不是把另一个文件的文字机械粘贴过来。

假设 `gaia.py` 中定义：

```python
class GaiaSource:
    ...
```

在 `intake.py` 中执行：

```python
from astro_pipeline.sources.gaia import GaiaSource
```

这会在 `intake` 模块的命名空间中创建名字 `GaiaSource`，使它指向 `gaia` 模块当前保存的
那个类对象。它不是复制一份类，但也不是永远自动同步的“活链接”。初始关系近似为：

```text
gaia 模块命名空间
  GaiaSource ─────────────┐
                          ├──> 同一个类对象 A
intake 模块命名空间       │
  GaiaSource ─────────────┘
```

若之后重新绑定：

```python
gaia.GaiaSource = FakeGaiaSource
```

只改变 `gaia` 模块里的名字；`intake.GaiaSource` 仍可能指向类对象 A：

```text
gaia.GaiaSource   ──> FakeGaiaSource 类对象 B
intake.GaiaSource ──> 原 GaiaSource 类对象 A
```

因此测试需要替换被测代码真正查找的名字：

```python
monkeypatch.setattr(intake, "GaiaSource", _FakeGaiaSource)
```

另一种导入形式是：

```python
import astro_pipeline.sources.gaia as gaia_module

source = gaia_module.GaiaSource(...)
```

这种代码运行时会通过 `gaia_module` 查询 `GaiaSource`，其 patch 位置也随之不同。判断 patch
位置时，不要只问“类最初定义在哪里”，而要问“被测代码运行到这一行时从哪里查找名字”。

### 5.5 类、实例、`self` 和方法

初学阶段可以把类理解为一类对象的结构和行为定义，把实例理解为一份具体对象：

```python
class Survey:
    def __init__(self, name):
        self.name = name

    def describe(self):
        return f"Survey: {self.name}"


survey = Survey("Gaia")
```

逐项解释：

| 表达式 | 含义 |
|---|---|
| `class Survey:` | 创建一个名为 `Survey` 的类对象 |
| `Survey("Gaia")` | 调用类，创建一个实例 |
| `survey` | 指向该实例的名字 |
| `__init__` | 实例创建时执行的初始化方法 |
| `self` | 当前正在操作的实例 |
| `self.name = name` | 在当前实例上保存属性 `name` |
| `survey.describe()` | 调用绑定到实例的方法 |

调用：

```python
survey.describe()
```

可以近似理解为：

```python
Survey.describe(survey)
```

Python 自动把点号左边的实例作为第一个参数 `self` 传入。`self` 不是关键字，但按惯例必须
这样命名。

类并不只用于“现实世界建模”。工程代码经常用实例保存一组需要共同使用的状态，例如
配置、网络策略、登录状态和日志器。

### 5.6 本项目中的真实对象链

[intake.py](../src/astro_pipeline/intake.py) 中：

```python
cfg = PipelineConfig.load()
src = GaiaSource(cfg.gaia, cfg.network)
res = src.query_catalog(
    ConeSearchQuery(ra=ra, dec=dec, radius_deg=radius_deg, limit=limit)
)
df = _canonicalize(res.data)
```

按对象分类：

| 名字/表达式 | 类别 |
|---|---|
| `PipelineConfig` | 类对象 |
| `PipelineConfig.load` | 类方法；返回配置实例 |
| `cfg` | `PipelineConfig` 实例 |
| `cfg.gaia`、`cfg.network` | 配置实例上的属性，值也是对象 |
| `GaiaSource` | 类对象，从另一个模块导入 |
| `src` | `GaiaSource` 实例 |
| `ConeSearchQuery` | dataclass 类对象 |
| `ConeSearchQuery(...)` | 一次查询的实例 |
| `src.query_catalog` | 绑定到 `src` 的实例方法 |
| `res` | `CatalogResult` 实例 |
| `res.data` | `res` 实例上的 DataFrame 属性 |
| `_canonicalize` | `intake` 模块中的普通函数 |
| `df` | DataFrame 实例 |

`GaiaSource` 当前写成：

```python
class GaiaSource(CatalogSource):
    ...
```

括号中的 `CatalogSource` 表示继承。现阶段只需知道：`GaiaSource` 复用基类的一部分状态和
行为，并承诺提供 `query_catalog` 等方法。`super().__init__(...)` 表示先执行基类的初始化。
继承、抽象基类和多态将在类与实例闭环后再展开。

### 5.7 为什么 monkeypatch 能改变 `_from_cone`

正常情况下：

```text
intake.GaiaSource ──> 真实 GaiaSource 类
                         ↓ 调用类
src               ──> 真实 GaiaSource 实例
```

patch 之后：

```text
intake.GaiaSource ──> _FakeGaiaSource 类
                         ↓ 调用类
src               ──> _FakeGaiaSource 实例
```

`_from_cone` 的源码没有改变。它仍执行 `GaiaSource(...)` 和 `src.query_catalog(...)`，只是
名字现在指向假类。假类实现了被测代码需要的方法，因此这条调用链仍可运行而无需访问
真实网络。

## 6. 函数签名和参数传递

项目中的真实签名：

```python
def resolve_members(
    *,
    cone: Optional[tuple[float, float, float]] = None,
    catalog: Optional[str | Path] = None,
    name: Optional[str] = None,
    region: Optional[tuple[str, float]] = None,
    limit: Optional[int] = None,
) -> pd.DataFrame:
    ...
```

### 6.1 符号逐项解释

| 语法 | 含义 |
|---|---|
| `name: str` | `name` 预期是字符串 |
| `Optional[int]` | 预期是整数或 `None` |
| `str | Path` | 预期是字符串或 Path 对象 |
| `tuple[str, float]` | 二元 tuple，第一项字符串、第二项浮点数 |
| `= None` | 未提供时默认使用 `None` |
| `-> pd.DataFrame` | 预期返回 DataFrame |
| 裸 `*` | 后面的参数只能按名字传递 |

裸 `*` 意味着下面是正确的：

```python
resolve_members(region=("Taurus", 2.0), limit=5)
```

下面则不允许：

```python
resolve_members(("Taurus", 2.0))
```

因为调用者没有说明这个 tuple 是 `cone`、`region` 还是其他参数。要求写出参数名可降低
误传风险，尤其适合多个类型相似但语义不同的参数。

### 6.2 `kwargs` 和 `**kwargs`

`kwargs` 只是一个普通变量名，通常存放字典；`**` 才是特殊语法。

调用函数时，`**` 表示把字典展开为具名参数：

```python
params = {
    "region": ("Taurus", 2.0),
    "limit": 5,
}

resolve_members(**params)
```

等价于：

```python
resolve_members(region=("Taurus", 2.0), limit=5)
```

但下面不是等价调用：

```python
resolve_members(params)
```

它试图把整个字典作为一个位置参数传入；而 `resolve_members` 的裸 `*` 禁止任何位置
参数，因此会抛出 `TypeError`。

字典成功展开也不表示参数一定合法。例如：

```python
params = {
    "region": ("Taurus", 2.0),
    "radius": 5,
}

resolve_members(**params)
```

会被展开成：

```python
resolve_members(region=("Taurus", 2.0), radius=5)
```

但是函数签名没有名为 `radius` 的参数，因此 Python 会在进入函数体前抛出：

```text
TypeError: resolve_members() got an unexpected keyword argument 'radius'
```

这说明 `**` 只负责展开映射，不负责纠正键名或验证业务语义。展开后的每个键仍必须与
函数签名中的参数名匹配（除非被调用函数显式定义了 `**kwargs` 来接收额外参数）。

定义函数时，`**kwargs` 表示收集未显式列出的具名参数：

```python
def show_parameters(**kwargs):
    return kwargs

show_parameters(name="TW Hya", limit=5)
# 返回 {"name": "TW Hya", "limit": 5}
```

两个方向要分清：

```text
调用处的 **字典    → 展开
定义处的 **kwargs  → 收集
```

补充：单个 `*` 对序列执行类似操作。

```python
coordinates = (165.466, -34.705, 0.1)

def cone(ra, dec, radius):
    return ra, dec, radius

cone(*coordinates)
# 等价于 cone(165.466, -34.705, 0.1)
```

## 7. 装饰器

理解装饰器依赖一个事实：Python 函数本身也是对象，可以作为参数传给其他函数，也可以
被重新赋值。

```python
@decorate
def calculate():
    ...
```

概念上近似：

```python
def calculate():
    ...

calculate = decorate(calculate)
```

装饰器可以包装函数、登记函数或附加元数据。`@` 是通用 Python 语法；具体行为由
`decorate` 决定。

### 7.1 pytest 参数化装饰器

```python
@pytest.mark.parametrize("kwargs", [
    {"cone": (1.0, 2.0, 3.0), "name": "X"},
    {"name": "X", "region": ("Y", 1.0)},
])
def test_rejects_multiple_inputs(kwargs):
    with pytest.raises(ValueError):
        resolve_members(**kwargs)
```

pytest 会使用两份字典分别运行测试，因此执行两次。测试函数每次只接收一份字典，而
不是接收整个列表。

第一轮近似：

```python
kwargs = {"cone": (1.0, 2.0, 3.0), "name": "X"}
resolve_members(cone=(1.0, 2.0, 3.0), name="X")
```

第二轮近似：

```python
kwargs = {"name": "X", "region": ("Y", 1.0)}
resolve_members(name="X", region=("Y", 1.0))
```

### 7.2 其他项目装饰器

```python
@pytest.mark.network
def test_live_query():
    ...
```

给测试附加“需要网络”的标记。项目默认排除该标记。

```python
@mcp.tool()
def download_corpus(...):
    ...
```

把普通 Python 函数登记为 MCP 客户端可发现、可调用的工具。两者使用相同的装饰器语法，
但用途完全不同。

## 8. pytest 基础

### 8.1 测试仍然是 Python 函数

```python
def test_qc_extras_are_nonempty_and_strings():
    assert QC_EXTRA_COLUMNS
    assert all(isinstance(c, str) and c for c in QC_EXTRA_COLUMNS)
```

含义：

1. `QC_EXTRA_COLUMNS` 不能是空列表；
2. 列表中的每一项都必须是非空字符串；
3. 任一表达式为假，测试失败。

pytest 通常自动发现名字以 `test_` 开头的文件和函数。一个测试可按三段阅读：

```text
Arrange：准备输入、假对象和环境
Act：调用被测函数
Assert：验证返回值、异常、日志或文件
```

### 8.2 验证异常：`pytest.raises`

```python
def test_resolve_members_rejects_no_input():
    with pytest.raises(ValueError, match="Exactly one of"):
        resolve_members()
```

含义：在 `with` 代码块中，预期抛出 `ValueError`，且错误消息包含
`Exactly one of`。

| 实际行为 | 结果 |
|---|---|
| 抛出匹配消息的 `ValueError` | 通过 |
| 正常返回 DataFrame | 失败 |
| 抛出其他异常 | 失败 |
| 抛出 `ValueError` 但消息不匹配 | 失败 |

仅写 `pytest.raises(ValueError)` 可能误接住另一个无关 bug 导致的 `ValueError`；检查消息
能让测试更准确地指向预期失败原因。

### 8.3 fixture：pytest 自动提供的测试依赖

```python
def test_example(monkeypatch, tmp_path, caplog):
    ...
```

调用处没有显式传参。pytest 查看测试函数的参数名，找到同名 fixture，准备对象并注入。

| fixture | 作用 |
|---|---|
| `tmp_path` | 创建本次测试专用的临时目录 |
| `monkeypatch` | 临时替换属性、环境变量或当前目录 |
| `caplog` | 捕获日志记录 |
| `capsys` | 捕获标准输出和错误输出 |

测试结束后，pytest 负责清理这些临时变化。fixture 类似实验装置：先提供受控环境，测试
函数只负责在其中开展实验。

pytest 运行带 fixture 参数的测试时，可以近似理解为：

```text
发现 test_ 开头的函数
  → 读取函数签名中的参数名
  → 在内置 fixture、插件和 conftest.py 中查找同名 fixture
  → 创建或取得 fixture 对象
  → 调用 test_function(monkeypatch=..., caplog=...)
  → 执行 fixture 的清理工作
```

因此：

```python
def test_example(monkeypatch):
    ...
```

不是说任意普通调用者都能省略参数。如果直接在 Python 中运行 `test_example()`，仍会因
缺少参数而抛出 `TypeError`；只有 pytest runner 知道如何按名字解析和注入 fixture。

项目也可以创建自定义 fixture：

```python
@pytest.fixture
def sample_members():
    return pd.DataFrame({"Gaia": ["100", "200"]})


def test_member_count(sample_members):
    assert len(sample_members) == 2
```

这里 `sample_members` 同时是 fixture 的注册名和测试函数的参数名。pytest 先调用
fixture 函数，再把它的返回值传给测试。

### 8.4 `monkeypatch` 和假对象

真实 Gaia 服务可能限流、离线或返回变化数据，不适合作为每次单元测试的控制变量。
项目测试会定义一个行为固定的假类：

```python
class _FakeGaiaSource:
    def query_catalog(self, query, output_path=None):
        class _Result:
            data = fake_df.copy()

        return _Result()
```

然后临时替换：

```python
monkeypatch.setattr(intake, "GaiaSource", _FakeGaiaSource)
```

意思是：仅在当前测试期间，`intake` 模块中的 `GaiaSource` 名字指向假类。测试结束后恢复。

必须替换“被测代码实际查找这个名字的位置”。`intake.py` 使用：

```python
from astro_pipeline.sources.gaia import GaiaSource
```

导入后，`intake` 模块拥有自己的 `GaiaSource` 名字。因此 `resolve_members` 运行时查找的是
`intake.GaiaSource`。只替换原始定义位置
`astro_pipeline.sources.gaia.GaiaSource`，不一定会改变 `intake` 中已经绑定的名字。

可把它类比成：实验室已经从仓库领走一台设备并放在自己的工作台上。此时只替换仓库里
的设备，不会自动替换实验室工作台上的那一台；测试必须替换被测模块真正使用的引用。

这样可以稳定检验：

```text
假 Gaia 固定返回 100 行
  → resolve_members(..., limit=5)
  → 本地代码必须截成 5 行
  → 同时产生一条 warning 日志
```

注意：这个测试证明的是“无论上游多返回多少行，本地仍执行上限”。它没有证明真实 Gaia
服务可访问，也没有证明这 5 行在科学上是无偏样本。

## 9. 看不懂名称时如何查功能

不要从文件第一行开始逐个查所有陌生词。先分类：

| 类别 | 例子 | 首选方法 |
|---|---|---|
| Python 语法 | `@`、`with`、`**`、`->` | Python 官方文档或本指南 |
| 本项目名称 | `resolve_members`、`CorpusManifest` | 用 `rg` 搜定义，再读调用点 |
| 第三方库 | `pd.DataFrame`、`pytest.raises` | 对应库的官方文档和 `help()` |
| pytest fixture | `tmp_path`、`monkeypatch` | `pytest --fixtures` 和 pytest 文档 |

### 9.1 推荐命令

搜索定义和使用位置：

```bash
rg -n "def resolve_members|resolve_members\(" src tests
rg -n "class GaiaSource|GaiaSource\(" src tests
```

查看函数签名、说明和源码：

```python
from astro_pipeline.intake import resolve_members
import inspect

print(inspect.signature(resolve_members))
print(resolve_members.__doc__)
print(inspect.getsource(resolve_members))
```

检查对象类型和第三方方法：

```python
import pandas as pd

df = pd.DataFrame({"x": [1, 2]})
print(type(df))
help(pd.DataFrame.head)
```

查看 pytest 可用 fixture：

```bash
uv run pytest --fixtures -q
```

只运行一个测试，减少噪声：

```bash
uv run pytest -q \
  tests/test_intake.py::test_resolve_members_rejects_no_input
```

### 9.2 每个测试的阅读模板

```text
测试名称：
准备了什么数据：
调用了哪个函数：
预期返回什么：
预期抛出什么异常：
替换了哪些外部依赖：
它验证的是哪条契约：
它没有证明什么：
```

## 10. 阶段练习

### 练习 1：字典展开

```python
params = {
    "region": ("Taurus", 2.0),
    "limit": 5,
}

resolve_members(**params)
```

1. 将最后一行完全展开。
2. 为什么 `resolve_members(params)` 不等价？
3. 裸 `*` 在这里防止了什么歧义？

### 练习 2：参数化测试

```python
@pytest.mark.parametrize("kwargs", [
    {"cone": (1.0, 2.0, 3.0), "name": "X"},
    {"name": "X", "region": ("Y", 1.0)},
])
def test_rejects_multiple_inputs(kwargs):
    with pytest.raises(ValueError, match="Exactly one of"):
        resolve_members(**kwargs)
```

回答：

1. 测试执行几次？
2. 测试函数每次收到列表还是一份字典？
3. `**kwargs` 做什么？
4. 哪些实际结果会让测试失败？

### 练习 3：测试的证明边界

某测试用假 Gaia 服务返回 100 行，随后验证 `limit=5` 时最终只有 5 行。

1. 它证明了哪个工程行为？
2. 它是否证明真实 Gaia 网络正常？
3. 它是否证明这 5 个源是 Taurus 成员？
4. 它是否证明前 5 行构成统计无偏样本？

### 练习 4：fixture 和 monkeypatch

阅读 [test_intake.py](../tests/test_intake.py) 中
`test_from_cone_truncates_and_warns_when_over_limit`，回答：

1. `monkeypatch` 和 `caplog` 是谁创建并传给测试函数的？
2. `_FakeGaiaSource` 的作用是什么？
3. 为什么要替换 `intake.GaiaSource`？
4. 如果删除 `monkeypatch.setattr(...)`，测试最可能发生什么变化？
5. 该测试属于 Arrange、Act、Assert 的代码分别在哪里？

### 练习答案

1. 展开为 `resolve_members(region=("Taurus", 2.0), limit=5)`；不带 `**` 会把整个
   字典当成位置参数，而该函数因裸 `*` 禁止位置参数。裸 `*` 迫使调用者写明参数语义。
2. 执行两次；每次收到一份字典；`**kwargs` 将字典展开为具名参数；正常返回、异常类型
   不同或错误消息不匹配都会失败。
3. 它证明本地行数上限不完全依赖远程服务；不证明网络正常、不证明物理成员身份，也不
   证明截取前 5 行具有统计代表性。
4. pytest runner 根据测试函数签名注入两个 fixture；假类提供固定、无需网络的 Gaia
   行为；必须替换被测模块实际查找的 `intake.GaiaSource`。删除替换后，测试会尝试构造并
   使用真实 Gaia 数据源，可能联网、变慢或因外部状态失败。假 DataFrame、假类和 patch
   属于 Arrange，`resolve_members(...)` 属于 Act，最后两条 `assert` 属于 Assert。

## 11. 当前学习进度

截至 2026-08-14：

| 节点 | 状态 | 验证记录 |
|---|---|---|
| `pytest.mark.parametrize` 的运行次数 | 已理解 | 能判断两组参数执行两次 |
| `pytest.raises` 的基本通过条件 | 已理解 | 能判断正常返回时测试失败 |
| 字典与 `**` 关键字参数展开 | 已理解 | 能说明直接传字典与展开为 `region`、`limit` 的区别 |
| 裸 `*` 与 keyword-only 参数 | 已理解 | 能判断直接位置传入 `params` 会在进入函数体前触发 `TypeError` |
| 名字、对象与重新绑定 | 学习中 | 当前回退节点 |
| 模块命名空间与跨文件 import | 学习中 | 当前回退节点 |
| 类、实例、`self` 与方法 | 学习中 | 当前回退节点 |
| fixture 注入 | 已暂停 | 完成模块与类的闭环后恢复 |
| `monkeypatch` 与 mock | 已暂停 | 完成模块与类的闭环后恢复 |
| schema / provenance | 待学习 | 测试基础之后 |

## 12. 后续学习路线

| 阶段 | 主题 | 本仓库实验 |
|---|---|---|
| 1 | 对象、模块、参数、异常、装饰器 | 读 `intake.py` 的最小调用链 |
| 2 | pytest、fixture、mock、测试边界 | 拆解并修改 `test_intake.py` |
| 3 | schema、provenance、文件格式 | 追踪 DataFrame → Parquet → manifest |
| 4 | HTTP/API、ADQL、外部服务 | 追踪一次小型 Gaia 查询 |
| 5 | retry、checkpoint、幂等、原子写入 | 人为注入中途失败并恢复 |
| 6 | 科学样本与数据产品 | 比较几何场源与成员目录 |
| 7 | AI 语料工程 | 审计 JSONL、建立 datasheet 与质量报告 |
| 8 | embedding / RAG | 建立可评测的小型天文检索基线 |

## 13. AI 语料概念边界

本项目当前的 corpus 主要是：

```text
Gaia 天体
  ↔ SIMBAD/论文元数据
  ↔ Gaia XP 产品
  ↔ 可追溯的 Parquet/FITS/JSONL
```

它不是完整的大模型训练语料系统。尚需另行学习和实现的内容包括：全文获取与许可、文本
解析与切分、tokenization、去重、数据划分、污染检测、embedding、检索和 RAG 评测。

“适合 AI 的天文语料”取决于任务，但任务相关性仍不是唯一标准：

```text
AI 语料
= 数据记录
+ 选择函数
+ schema
+ provenance
+ 处理历史
+ 质量与缺失值语义
+ 去重和数据划分
+ 许可边界
+ 与任务一致的评测
```

例如，数量巨大的光谱若缺少仪器、波长网格、误差、归一化过程和源级数据划分，仍可能
因为域差异或同源泄漏而成为低质量训练语料。

## 14. 主要参考资料

- Bertrand Meyer, *Design by Contract*：
  <https://se.inf.ethz.ch/~meyer/publications/old/dbc_chapter.pdf>
- Python 官方语言参考，函数定义、装饰器与注解：
  <https://docs.python.org/3/reference/compound_stmts.html#function-definitions>
- pytest 官方参数化说明：
  <https://docs.pytest.org/en/stable/how-to/parametrize.html>
- pytest 官方 fixture 说明：
  <https://docs.pytest.org/en/stable/how-to/fixtures.html>
- pytest 官方 monkeypatch 说明：
  <https://docs.pytest.org/en/stable/how-to/monkeypatch.html>
- Gaia DR3 Archive 官方教程：
  <https://gea.esac.esa.int/archive/documentation/GDR3/Gaia_archive/chap_archive/sec_cu9arch_tutorials/>
- Gebru et al., *Datasheets for Datasets*：
  <https://arxiv.org/abs/1803.09010>
- Wilkinson et al., *The FAIR Guiding Principles for scientific data management and stewardship*：
  <https://doi.org/10.1038/sdata.2016.18>
- Lewis et al., *Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks*：
  <https://papers.nips.cc/paper/2020/hash/6b493230205f780e1bc26945df7481e5-Abstract.html>
