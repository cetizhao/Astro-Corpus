#!/usr/bin/env python3
"""Build the public static observatory from a curated, committed example only."""
import argparse
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]

def build(output: Path) -> None:
    assets = ROOT / 'src/astro_pipeline/viz/assets'
    output.mkdir(parents=True, exist_ok=True)
    (output / 'assets').mkdir(exist_ok=True)
    for name in ('library.css','library.js','evidence.css','evidence.js','demo.js'):
        shutil.copyfile(assets / name, output / 'assets' / name)
    shutil.copyfile(assets.parent / 'vendor/echarts.min.js', output / 'assets/echarts.min.js')
    shutil.copyfile(ROOT / 'examples/observatory/demo.json', output / 'demo.json')
    document = (assets / 'library.html').read_text(encoding='utf-8').replace('href="/assets/', 'href="assets/').replace('src="/assets/', 'src="assets/')
    document = document.replace('<script defer src="assets/library.js">', '<script defer src="assets/demo.js"></script>\n  <script defer src="assets/library.js">')
    document = document.replace('ASTRO-CORPUS / LOCAL LIBRARY','ASTRO-CORPUS / PUBLIC DEMO')
    document = document.replace('<nav class="section-nav"', '<p class="notice">公开示例 · 8 个 Gaia DR3 天体 · 固定数据快照。搜索 TWA 或选择 TWA 31 查看文献证据；原文仅展示短摘录，标注待复核。<a href="about.html">数据来源与说明</a></p>\n    <nav class="section-nav"')
    document = document.replace('立即同步','重新载入').replace('只读 SQLite 视图','固定示例快照').replace('本地只读 · 不暴露文件目录或凭据','公开演示 · 不连接本地数据库')
    document = document.replace('点开前校验文件；不等于 availability', '本次快照未包含 XP 光谱文件').replace('已登记 ready 光谱源', '示例包含光谱的源')
    document = document.replace('正在读取共享库…', '正在读取示例数据…').replace('当前发布、进行中和历史记录分别标注；已入库不等于所有阶段完成。Legacy 数据不被自动认证为成员表。', '本页使用经过选取的公开数据快照；样本来源见各天体详情及数据说明。')
    (output / 'index.html').write_text(document,encoding='utf-8')
    (output / '.nojekyll').write_text('',encoding='utf-8')
    shutil.copyfile(ROOT / 'examples/observatory/about.html',output / 'about.html')
    print(output)

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'dist/pages')
    build(parser.parse_args().output)
