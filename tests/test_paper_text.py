from __future__ import annotations

import gzip
import io
import tarfile

import pytest

from astro_pipeline.paper_text import ParseError, markdown, parse_html, parse_latex, parse_pdf


HTML_PAPER = '''<!doctype html>
<html><body><article>
<section class="ltx_section" id="S1">
<h1 class="ltx_title">1 Introduction</h1>
<p>We measure a distance of 140.2 pc and temperature 3,500 K; <span class="ltx_Math" data-latex="\\alpha = 0.5">α = 0.5</span>.</p>
<div class="ltx_equation" id="eq:one"><math display="block" alttext="E = mc^2"><semantics><mrow><mi>E</mi><mo>=</mo><mi>mc</mi><mn>2</mn></mrow><annotation encoding="application/x-tex">E = mc^2</annotation></semantics></math></div>
<table id="tab:measurements"><caption>Table 1: Measured flux</caption>
<thead><tr><th rowspan="2">Band</th><th colspan="2">Flux</th></tr></thead>
<tbody><tr><td>G</td><td>12.34</td><td>± 0.05</td></tr></tbody></table>
<figure id="fig:missing"><img alt="plot"><figcaption>Figure 1: A synthetic source.</figcaption></figure>
</section>
<section id="references"><ol class="ltx_biblist"><li class="ltx_bibitem" id="bib1">A. Author (2024), Synthetic results.</li></ol></section>
</article></body></html>'''.encode()


def _types(document):
    return [block["type"] for block in document["blocks"]]


def _tar_bytes(entries: list[tuple[str, bytes, str]]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, payload, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = "main.tex"
                archive.addfile(info)
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            else:
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
    return stream.getvalue()


def _pdf_bytes(text: str | None) -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    if text:
        # Synthetic selectable text; no real paper content or OCR fixture.
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        font_ref = writer._add_object(font)
        resources = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})})
        content = DecodedStreamObject()
        content.set_data(b"BT /F1 12 Tf 20 250 Td (Synthetic 140.2 pc) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(content)
        page[NameObject("/Resources")] = writer._add_object(resources)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_html_extracts_structure_math_spans_references_without_duplicate_text():
    document = parse_html(HTML_PAPER, paper={"title": "Synthetic paper", "year": 2024})
    assert document["schema_version"] == "1.0"
    assert document["paper"]["title"] == "Synthetic paper"
    assert document["parser"]["format"] == "html"
    assert document["parser"]["dependencies"]["beautifulsoup4"]
    assert document["parser"]["limits"]["max_input_bytes"] > 0
    assert {"heading", "paragraph", "equation", "table", "figure_caption", "reference"} <= set(_types(document))
    paragraph = next(b for b in document["blocks"] if b["type"] == "paragraph")
    assert "140.2 pc" in paragraph["text"] and "3,500 K" in paragraph["text"]
    assert paragraph["text"].count("140.2 pc") == 1
    assert any(item.get("text") == r"\alpha = 0.5" for item in paragraph.get("inline_math", []))
    equation = next(b for b in document["blocks"] if b["type"] == "equation")
    assert equation["latex"] == "E = mc^2"
    assert "<math" in equation["mathml"]
    table = next(b for b in document["blocks"] if b["type"] == "table")
    assert table["cells"][0][0]["rowspan"] == 2
    assert table["cells"][0][1]["colspan"] == 2
    assert table["caption"] == "Table 1: Measured flux"
    assert sum(b["type"] == "reference" for b in document["blocks"]) == 1
    assert any("image" in warning for warning in document["quality"]["warnings"])
    assert document["quality"]["status"] == "partial"
    assert [b["id"] for b in document["blocks"]] == [f"b{i:06d}" for i in range(1, len(document["blocks"]) + 1)]


def test_html_rejects_empty_abstract_only_and_error_pages():
    for payload in (
        b"",
        b"<html><body><p>Abstract</p></body></html>",
        b"<html><body><div id='abstract'><p>Only summary.</p></div></body></html>",
        b"<html><body><h1>Abstract</h1><p>Only summary.</p></body></html>",
        b"<html><body>404 Not Found</body></html>",
    ):
        with pytest.raises(ParseError):
            parse_html(payload)


def test_latex_single_source_preserves_math_raw_table_and_references():
    tex = r'''\documentclass{article}
\begin{document}
\begin{abstract}A summary only is not the whole paper.\end{abstract}
\section{Introduction}
The star is at 140.2~pc and has temperature 3,500~K. Inline $\alpha=0.5$.
\begin{equation}\label{eq:mass} M = 2\,M_\odot \end{equation}
\begin{table}\caption{Synthetic measurements}\begin{tabular}{lc}Band & Flux \\
G & 12.34 \\
\end{tabular}\end{table}
\section{Results}
Measured value is $12.34\pm0.05$.
\begin{thebibliography}{1}\bibitem{synthetic} A. Author (2024). Synthetic results.\end{thebibliography}
\end{document}'''
    document = parse_latex(tex.encode(), paper={"arxiv_id": "0000.00000"})
    assert document["paper"]["arxiv_id"] == "0000.00000"
    assert document["parser"]["format"] == "latex"
    assert {"heading", "paragraph", "equation", "table", "reference"} <= set(_types(document))
    assert any("140.2" in b["text"] and "3,500" in b["text"] for b in document["blocks"] if b["type"] == "paragraph")
    assert any(b.get("inline_math") for b in document["blocks"] if b["type"] == "paragraph")
    paragraph = next(b for b in document["blocks"] if b["type"] == "paragraph" and b.get("inline_math"))
    assert "$\\alpha=0.5$" in paragraph["text"]
    assert any("M = 2" in b["latex"] for b in document["blocks"] if b["type"] == "equation")
    table = next(b for b in document["blocks"] if b["type"] == "table")
    assert "12.34" in table["raw"] and table["caption"] == "Synthetic measurements"
    assert table["representation"] == "latex" and "cells" not in table
    ref = next(b for b in document["blocks"] if b["type"] == "reference")
    assert ref["key"] == "synthetic" and "2024" in ref["text"]
    assert document["quality"]["metrics"]["main_file"] == "main.tex"


def test_latex_archive_expands_bounded_local_includes_and_unknown_macro_is_partial():
    main = b"\\documentclass{article}\\begin{document}\\section{Results}\\input{sections/data}\\end{document}"
    section = b"A custom macro \\mystery{value} and $x=1$."
    data = _tar_bytes([("main.tex", main, "file"), ("sections/data.tex", section, "file")])
    document = parse_latex(gzip.compress(data))
    paragraph = next(b for b in document["blocks"] if b["type"] == "paragraph")
    assert "mystery" in paragraph["latex"]
    assert paragraph["locator"]["filename"] == "sections/data.tex"
    assert document["quality"]["status"] == "partial"
    assert any("unrecognized TeX macros" in warning for warning in document["quality"]["warnings"])


def test_latex_rejects_include_cycles_traversal_symlinks_and_oversized_archives():
    cycle = _tar_bytes([("main.tex", b"\\begin{document}\\section{A}\\input{child}\\end{document}", "file"), ("child.tex", b"\\input{main}", "file")])
    with pytest.raises(ParseError, match="cyclic"):
        parse_latex(cycle)
    traversal = _tar_bytes([("../main.tex", b"\\begin{document}text\\end{document}", "file")])
    with pytest.raises(ParseError, match="unsafe archive path"):
        parse_latex(traversal)
    linked = _tar_bytes([("main.tex", b"\\begin{document}text\\end{document}", "file"), ("alias.tex", b"", "symlink")])
    with pytest.raises(ParseError, match="unsafe non-regular"):
        parse_latex(linked)
    too_big = gzip.compress(b"x" * (40 * 1024 * 1024 + 1))
    with pytest.raises(ParseError, match="expanded-size"):
        parse_latex(too_big)


def test_html_equation_tables_groups_subscripts_citations_and_image_sources():
    source = b'''<html><body><article>
<h1>Paper title</h1><section><h2>Results</h2>
<p>Mass M<sub>sun</sub>, radius x<sup>2</sup>, from <a href="#bib.source">[1]</a>.</p>
<table class="ltx_equation" id="eq:table"><tr><td><math display="block" alttext="x = 1"><mi>x</mi></math></td></tr></table>
<div class="ltx_equationgroup" id="eq:group">
<div class="ltx_equation"><math display="block" alttext="y = 2"><mi>y</mi></math></div>
<div class="ltx_equation"><math display="block" alttext="z = 3"><mi>z</mi></math></div>
</div>
<figure id="fig:one"><img src="https://example.invalid/plot.png"><figcaption>Synthetic plot</figcaption></figure>
</section></article></body></html>'''
    document = parse_html(source)
    paragraph = next(b for b in document["blocks"] if b["type"] == "paragraph")
    assert "M_{sun}" in paragraph["text"] and "x^{2}" in paragraph["text"]
    assert paragraph["citations"] == [{"text": "[1]", "target": "#bib.source", "target_id": "bib.source"}]
    equations = [b for b in document["blocks"] if b["type"] == "equation"]
    assert [b["latex"] for b in equations] == ["x = 1", "y = 2", "z = 3"]
    assert len({b["group_id"] for b in equations[1:]}) == 1
    assert not any(b["type"] == "table" and b["locator"].get("html_id") == "eq:table" for b in document["blocks"])
    figure = next(b for b in document["blocks"] if b["type"] == "figure_caption")
    assert figure["image_sources"] == ["https://example.invalid/plot.png"]
    assert any("not downloaded" in warning for warning in document["quality"]["warnings"])


def test_html_document_title_and_abstract_alone_is_rejected():
    source = b"<html><body><article><h1>Paper title</h1><h2>Abstract</h2><p>Only abstract text.</p></article></body></html>"
    with pytest.raises(ParseError):
        parse_html(source)


def test_latex_comments_inline_delimiters_unknown_commands_and_source_locators():
    tex = r'''\\documentclass{article}
% \\input{does-not-exist}
\\begin{document}
\\section{Results}
% a fake \\section{not parsed} and \\input{also-missing}
A source value $M_s$ and \\(\\alpha=0.5\\), plus \\ion{Fe}{II}.
\\end{document}'''.replace("\\\\", "\\")
    document = parse_latex(tex.encode())
    paragraph = next(b for b in document["blocks"] if b["type"] == "paragraph")
    assert "$M_s$" in paragraph["text"] and r"$\alpha=0.5$" in paragraph["text"]
    assert r"\ion{Fe}{II}" in paragraph["text"]
    assert len(paragraph["inline_math"]) == 2
    assert "also-missing" in paragraph["latex"]
    assert "does-not-exist" not in paragraph["latex"]
    start, end = paragraph["locator"]["start"], paragraph["locator"]["end"]
    assert paragraph["locator"]["filename"] == "main.tex"
    assert "A source value" in tex[start:end]
    assert not any("local include not found" in warning for warning in document["quality"]["warnings"])
    assert not any("not parsed" in block.get("text", "") for block in document["blocks"])


def test_latex_rejects_title_plus_abstract_pdf_and_binary_tex():
    abstract_only = b"\\\\begin{document}\\\\section{Paper title}\\\\begin{abstract}Only an abstract.\\\\end{abstract}\\\\end{document}".replace(b"\\\\", b"\\")
    with pytest.raises(ParseError):
        parse_latex(abstract_only)
    with pytest.raises(ParseError, match="PDF data"):
        parse_latex(b"%PDF-1.7 synthetic")
    with pytest.raises(ParseError, match="binary data"):
        parse_latex(b"\\begin{document}" + bytes([0]) + b"text\\end{document}")


def test_latex_main_document_preference_safe_directory_names_and_bibliography_warning():
    main = b"\\\\documentclass{article} placeholder without document".replace(b"\\\\", b"\\")
    paper = b"\\\\begin{document}\\\\section{Results}Useful value 140.2 pc.\\\\bibliography{references}\\\\end{document}".replace(b"\\\\", b"\\")
    archive = _tar_bytes([("./sections/", b"", "dir"), ("main.tex", main, "file"), ("paper.tex", paper, "file")])
    document = parse_latex(archive)
    assert document["quality"]["metrics"]["main_file"] == "paper.tex"
    assert any(".bbl" in warning for warning in document["quality"]["warnings"])


def test_latex_rejects_truncated_trailing_and_concatenated_gzip():
    tex = gzip.compress(b"\\\\begin{document}\\\\section{Results}Text.\\\\end{document}".replace(b"\\\\", b"\\"))
    for payload in (tex[:-3], tex + b"trailing", tex + gzip.compress(b"extra")):
        with pytest.raises(ParseError, match="gzip"):
            parse_latex(payload)


def test_nested_scientific_macros_and_table_notes_are_preserved():
    source = rb"""\begin{document}\section{Results}
The value is \custom{\ion{Fe}{II}} with $M_\star=0.8\pm0.1$.
\begin{table}\caption{A measurement}
\begin{tabular}{cc}Mass & Uncertainty\\ 0.8 & 0.1\end{tabular}
\tablecomments{Values are in solar masses.}\end{table}
\end{document}"""
    result = parse_latex(source)
    paragraph = next(b for b in result["blocks"] if b["type"] == "paragraph")
    assert r"\custom{\ion{Fe}{II}}" in paragraph["text"]
    assert "ASTROPARSER" not in paragraph["text"]
    table = next(b for b in result["blocks"] if b["type"] == "table")
    assert "solar masses" in table["raw"]
    assert "cells" not in table
    assert result["quality"]["status"] == "partial"


def test_nested_html_math_alttext_and_table_footnotes():
    data = b'<html><article><h1>Results</h1><p>Value <span class="ltx_Math"><math alttext="x^2"><mi>x</mi><mn>2</mn></math></span>.</p><figure class="ltx_table"><figcaption>Mass measurements</figcaption><table><tr><td>0.8</td></tr></table><div class="ltx_tablenotes">Solar masses</div></figure></article></html>'
    result = parse_html(data)
    paragraph = next(b for b in result["blocks"] if b["type"] == "paragraph")
    assert "$x^2$" in paragraph["text"]
    table = next(b for b in result["blocks"] if b["type"] == "table")
    assert "Solar masses" in table["raw"]
    assert table["notes"] == ["Solar masses"]


def test_markdown_is_deterministic_and_keeps_tables_raw():
    document = parse_html(HTML_PAPER)
    first = markdown(document)
    assert first == markdown(document)
    assert "\\alpha = 0.5" in first
    assert "<table" in first
    assert "12.34" in first


def test_pdf_extracts_text_as_partial_and_rejects_blank_or_invalid_documents():
    text_pdf = _pdf_bytes("text")
    document = parse_pdf(text_pdf, paper={"title": "Synthetic PDF"})
    assert document["quality"]["status"] == "partial"
    assert document["quality"]["metrics"]["text_pages"] == 1
    assert any("140.2 pc" in b["text"] for b in document["blocks"])
    with pytest.raises(ParseError, match="no extractable text"):
        parse_pdf(_pdf_bytes(None))
    with pytest.raises(ParseError, match="signature"):
        parse_pdf(b"not a pdf")
