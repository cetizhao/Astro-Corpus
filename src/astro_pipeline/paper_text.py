"""Conservative, offline extraction of scholarly paper text.

The parser produces structural observations only. It does not validate scientific
claims, units, equations, or the correctness of extracted metadata.
"""

from __future__ import annotations

import html
import importlib.metadata
import io
import re
import tarfile
import zlib
from pathlib import PurePosixPath
from typing import Any, Iterable

PARSER_VERSION = "0.1.0"

MAX_HTML_BYTES = 20 * 1024 * 1024
MAX_LATEX_INPUT_BYTES = 20 * 1024 * 1024
MAX_LATEX_EXPANDED_BYTES = 40 * 1024 * 1024
MAX_ARCHIVE_FILES = 500
MAX_INCLUDE_DEPTH = 12
MAX_PDF_BYTES = 100 * 1024 * 1024
MAX_PDF_PAGES = 2000


class ParseError(ValueError):
    """Raised when input is empty, unsupported, or has no useful paper text."""


def _document(fmt: str, paper: dict[str, Any] | None, blocks: list[dict[str, Any]], warnings: Iterable[str], metrics: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
    warning_list = list(dict.fromkeys(warnings))
    dependency_names = {"html": ("beautifulsoup4",), "latex": ("pylatexenc",), "pdf": ("pypdf",)}.get(fmt, ())
    dependencies = {}
    for package in dependency_names:
        try:
            dependencies[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            dependencies[package] = None
    return {
        "schema_version": "1.0",
        "parser": {
            "name": "astro_pipeline.paper_text",
            "version": PARSER_VERSION,
            "format": fmt,
            "dependencies": dependencies,
            "limits": {
                "max_input_bytes": {"html": MAX_HTML_BYTES, "latex": MAX_LATEX_INPUT_BYTES, "pdf": MAX_PDF_BYTES}.get(fmt),
                "max_expanded_latex_bytes": MAX_LATEX_EXPANDED_BYTES if fmt == "latex" else None,
                "max_archive_files": MAX_ARCHIVE_FILES if fmt == "latex" else None,
                "max_include_depth": MAX_INCLUDE_DEPTH if fmt == "latex" else None,
                "max_pdf_pages": MAX_PDF_PAGES if fmt == "pdf" else None,
            },
        },
        "paper": dict(paper or {}),
        "blocks": blocks,
        "quality": {
            "status": "partial" if partial or warning_list else "parsed",
            "warnings": warning_list,
            "metrics": metrics,
        },
    }


def _block(blocks: list[dict[str, Any]], kind: str, text: str, section_path: list[str], locator: dict[str, Any], **extra: Any) -> dict[str, Any]:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned and kind not in {"table", "figure_caption", "equation"}:
        return {}
    result = {
        "id": f"b{len(blocks) + 1:06d}",
        "type": kind,
        "text": cleaned,
        "section_path": list(section_path),
        "locator": locator,
    }
    result.update({k: v for k, v in extra.items() if v is not None})
    blocks.append(result)
    return result


def _has_substantive_text(blocks: list[dict[str, Any]]) -> bool:
    return any(b.get("type") in {"paragraph", "equation", "table", "figure_caption"} and b.get("text", "").strip() for b in blocks)


def _required_bytes(data: bytes, maximum: int, what: str) -> None:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ParseError(f"{what} input must be bytes")
    if not data:
        raise ParseError(f"{what} input is empty")
    if len(data) > maximum:
        raise ParseError(f"{what} input exceeds the {maximum}-byte safety limit")


def _class_tokens(tag: Any) -> set[str]:
    values = tag.get("class", []) if hasattr(tag, "get") else []
    if isinstance(values, str):
        values = values.split()
    return set(values)


def _math_payload(tag: Any) -> tuple[str, list[dict[str, str]]]:
    """Return readable math text plus exact available TeX/MathML strings."""
    try:
        from bs4 import Tag
    except ImportError as exc:  # pragma: no cover - declared runtime dependency
        raise ParseError("HTML parsing requires beautifulsoup4") from exc
    if not isinstance(tag, Tag):
        return "", []
    reps: list[dict[str, str]] = []
    latex = tag.get("alttext") or tag.get("data-latex") or tag.get("data-tex")
    if latex:
        reps.append({"format": "latex", "text": str(latex)})
    mathml = tag if tag.name == "math" else tag.find("math")
    if mathml is not None:
        nested_latex = mathml.get("alttext") or mathml.get("data-latex") or mathml.get("data-tex")
        if nested_latex and not any(r["format"] == "latex" for r in reps):
            reps.append({"format": "latex", "text": str(nested_latex)})
        serialized = str(mathml)
        if not any(r["format"] == "mathml" and r["text"] == serialized for r in reps):
            reps.append({"format": "mathml", "text": serialized})
        annotation = mathml.find("annotation", attrs={"encoding": re.compile("tex", re.I)})
        if annotation and annotation.get_text(strip=True):
            reps.insert(0, {"format": "latex", "text": annotation.get_text(strip=True)})
    if not any(r["format"] == "latex" for r in reps):
        # LaTeXML often stores the source expression in a math annotation or
        # in an alt attribute on an image-based math representation.
        alt = tag.get("alt")
        if alt:
            reps.insert(0, {"format": "latex", "text": str(alt)})
    readable = next((r["text"] for r in reps if r["format"] == "latex"), "")
    if not readable and mathml is not None:
        readable = mathml.get_text(" ", strip=True)
    return readable, reps


def _html_text(node: Any) -> tuple[str, list[dict[str, str]]]:
    """Extract text while replacing math nodes with their preserved TeX."""
    try:
        from bs4 import NavigableString, Tag
    except ImportError as exc:  # pragma: no cover
        raise ParseError("HTML parsing requires beautifulsoup4") from exc
    math_items: list[dict[str, str]] = []

    def walk(item: Any) -> str:
        if isinstance(item, NavigableString):
            return str(item)
        if not isinstance(item, Tag):
            return ""
        classes = _class_tokens(item)
        if item.name == "math" or item.name in {"mrow", "semantics"} or bool(classes & {"ltx_math", "ltx_mathinline", "ltx_math_display", "ltx_Math"}):
            value, reps = _math_payload(item)
            math_items.extend(reps)
            return f" ${value}$ " if value else " [math] "
        if item.name == "annotation":
            # Annotation is represented alongside its parent MathML, not twice.
            return ""
        if item.name in {"script", "style", "noscript"}:
            return ""
        if item.name == "img":
            alt = item.get("alt", "")
            return f" {alt} " if alt else ""
        text = "".join(walk(child) for child in item.children)
        if item.name == "sub":
            return f"_{{{text}}}"
        if item.name == "sup":
            return f"^{{{text}}}"
        if item.name in {"br", "li", "td", "th", "tr", "div", "p"} or "ltx_break" in classes:
            return f" {text} "
        return text

    return re.sub(r"\s+", " ", walk(node)).strip(), math_items


def _html_citations(node: Any) -> list[dict[str, str]]:
    if node is None or not hasattr(node, "find_all"):
        return []
    citations = []
    for anchor in node.find_all("a", href=True):
        href = str(anchor.get("href", ""))
        if href.startswith("#") and len(href) > 1:
            citations.append({"text": anchor.get_text(" ", strip=True), "target": href, "target_id": href[1:]})
    return citations


def _html_math_nodes(node: Any) -> list[Any]:
    """Return math leaves in document order without duplicating nested MathML."""
    if getattr(node, "name", None) == "math":
        return [node]
    found = list(node.find_all("math")) if hasattr(node, "find_all") else []
    if found:
        return found
    if hasattr(node, "find_all"):
        candidates = node.find_all(True, class_=re.compile(r"(?:^|\s)(?:ltx_Math|ltx_math|ltx_mathinline|ltx_math_display)(?:$|\s)"))
        return [candidate for candidate in candidates if not candidate.find(True, class_=re.compile(r"ltx_Math|ltx_math"))]
    return []


def _html_equation_blocks(blocks: list[dict[str, Any]], node: Any, section_path: list[str], locator_id: str | None, group_id: str | None = None) -> int:
    math_nodes = _html_math_nodes(node)
    if not math_nodes:
        latex, reps = _math_payload(node)
        math_nodes = [node] if latex or reps else []
    for ordinal, math_node in enumerate(math_nodes, 1):
        latex, reps = _math_payload(math_node)
        readable = latex or math_node.get_text(" ", strip=True) or "Equation"
        _block(
            blocks,
            "equation",
            readable,
            section_path,
            {"html_id": locator_id, "equation_ordinal": ordinal},
            latex=latex or None,
            mathml=next((rep["text"] for rep in reps if rep["format"] == "mathml"), None),
            representations=reps or None,
            group_id=group_id if len(math_nodes) > 1 else None,
        )
    return len(math_nodes)


def parse_html(data: bytes, *, paper: dict[str, Any] | None = None) -> dict[str, Any]:
    """Parse an arXiv/LaTeXML-style HTML document using Python's HTML parser."""
    _required_bytes(data, MAX_HTML_BYTES, "HTML")
    try:
        from bs4 import BeautifulSoup, Tag
    except ImportError as exc:  # pragma: no cover
        raise ParseError("HTML parsing requires beautifulsoup4") from exc
    try:
        source = bytes(data).decode("utf-8-sig")
    except UnicodeDecodeError:
        source = bytes(data).decode("utf-8", errors="replace")
    soup = BeautifulSoup(source, "html.parser")
    root = soup.find("article") or soup.find("main") or soup.body or soup
    if not isinstance(root, Tag):
        raise ParseError("HTML document has no parseable body")
    blocks: list[dict[str, Any]] = []
    warnings: list[str] = []
    section_path: list[str] = []
    seen_tables = 0
    meaningful_outside_abstract = False

    def is_reference_container(tag: Tag) -> bool:
        classes = _class_tokens(tag)
        return bool(classes & {"ltx_biblist", "ltx_bibliography", "references", "thebibliography"}) or tag.get("id", "").lower() in {"references", "bibliography"}

    def reference_items(container: Tag) -> list[Tag]:
        return list(container.select("li.ltx_bibitem, li[id^='bib'], .ltx_bibitem"))

    def walk(parent: Tag, inside_abstract: bool = False) -> None:
        nonlocal seen_tables, meaningful_outside_abstract, section_path
        for child in parent.children:
            if not isinstance(child, Tag):
                continue
            classes = _class_tokens(child)
            tagname = child.name.lower() if child.name else ""
            this_abstract = inside_abstract or ("abstract" in " ".join(classes).lower() or child.get("id", "").lower() == "abstract") or (bool(section_path) and section_path[-1].lower() == "abstract")

            if is_reference_container(child):
                for ref in reference_items(child):
                    text, math_items = _html_text(ref)
                    _block(blocks, "reference", text, section_path, {"html_id": ref.get("id"), "ordinal": len(blocks) + 1}, inline_math=math_items or None, citations=_html_citations(ref) or None)
                # Some generic reference lists have direct list items without LaTeXML classes.
                if not reference_items(child):
                    for idx, ref in enumerate(child.find_all("li", recursive=False), 1):
                        text, math_items = _html_text(ref)
                        _block(blocks, "reference", text, section_path, {"html_id": ref.get("id"), "ordinal": idx}, inline_math=math_items or None)
                continue

            # LaTeXML can represent equations and equation groups as tables.
            # Detect these before generic tables so math is never flattened into cells.
            is_equation_node = bool(classes & {"ltx_equation", "ltx_equationgroup", "ltx_eqn", "equation"})
            if tagname == "table" and is_equation_node:
                emitted = _html_equation_blocks(blocks, child, section_path, child.get("id"), child.get("id"))
                if emitted and not this_abstract:
                    meaningful_outside_abstract = True
                continue

            # A table is emitted once, with actual span attributes and its raw HTML.
            if tagname == "table" or "ltx_table" in classes:
                actual = child if tagname == "table" else child.find("table")
                if actual is None:
                    actual = child
                caption_tag = actual.find("caption") or child.find("figcaption") or child.find(class_=re.compile(r"ltx_caption|caption"))
                caption, _ = _html_text(caption_tag) if caption_tag else ("", [])
                rows = []
                for tr in actual.find_all("tr"):
                    row = []
                    for cell in tr.find_all(["th", "td"], recursive=False):
                        cell_text, _ = _html_text(cell)
                        row.append({"text": cell_text, "rowspan": int(cell.get("rowspan", 1)), "colspan": int(cell.get("colspan", 1)), "header": cell.name == "th"})
                    if row:
                        rows.append(row)
                raw = str(child)
                notes = [note.get_text(" ", strip=True) for note in child.select(".ltx_note, .ltx_tablenotes")]
                _block(blocks, "table", caption or "Table", section_path, {"html_id": child.get("id"), "ordinal": seen_tables + 1}, caption=caption or None, cells=rows, raw=raw, notes=notes or None, representation="html")
                seen_tables += 1
                if not this_abstract:
                    meaningful_outside_abstract = True
                continue

            # A figure is represented as its caption and declared image sources;
            # the parser never fetches image URLs.
            if tagname == "figure" or "ltx_figure" in classes:
                caption_tag = child.find("figcaption") or child.find(class_=re.compile(r"ltx_caption|caption"))
                caption, math_items = _html_text(caption_tag) if caption_tag else ("", [])
                imgs = child.find_all("img")
                image_sources = [str(img.get("src") or img.get("data-src")) for img in imgs if img.get("src") or img.get("data-src")]
                if imgs:
                    warnings.append("figure image not downloaded; remote resources are not fetched")
                if any(not (img.get("src") or img.get("data-src")) for img in imgs) or not imgs:
                    warnings.append("figure image is missing or has no source")
                _block(
                    blocks,
                    "figure_caption",
                    caption,
                    section_path,
                    {"html_id": child.get("id"), "ordinal": sum(b["type"] == "figure_caption" for b in blocks) + 1},
                    inline_math=math_items or None,
                    citations=_html_citations(caption_tag) if caption_tag else None,
                    image_sources=image_sources,
                )
                if caption and not this_abstract:
                    meaningful_outside_abstract = True
                continue

            # Display equations may be a single MathML node or a LaTeXML wrapper
            # containing several equations (for example an equation group).
            is_display_math = tagname == "math" and str(child.get("display", "")).lower() == "block"
            is_display_math = is_display_math or bool(classes & {"ltx_equation", "ltx_equationgroup", "ltx_eqn", "equation"})
            if is_display_math:
                emitted = _html_equation_blocks(blocks, child, section_path, child.get("id"), child.get("id"))
                if emitted and not this_abstract:
                    meaningful_outside_abstract = True
                continue

            # Recognize headings from standard HTML and LaTeXML class names.
            heading_level = None
            if re.fullmatch(r"h[1-6]", tagname):
                heading_level = int(tagname[1])
            else:
                title_class = next((c for c in classes if re.fullmatch(r"ltx_title(?:_([1-6]))?", c)), None)
                if title_class:
                    suffix = re.search(r"_(\d)$", title_class)
                    heading_level = int(suffix.group(1)) if suffix else 1
            if heading_level:
                title, _ = _html_text(child)
                if title:
                    normalized = title.strip()
                    if heading_level == 1 and normalized.lower() == "abstract":
                        section_path = [normalized]
                    else:
                        section_path = section_path[: max(0, heading_level - 1)] + [normalized]
                    _block(blocks, "heading", normalized, section_path, {"html_id": child.get("id"), "level": heading_level})
                continue

            # Explicit paragraphs (and LaTeXML paragraph containers) are emitted
            # as a unit; do not recurse into their nested spans and duplicate text.
            paragraph_container = tagname == "p" or "ltx_para" in classes
            if paragraph_container:
                # Avoid an outer LaTeXML wrapper swallowing headings or tables.
                if tagname != "p" and child.find(["p", "table", "figure", "h1", "h2", "h3", "h4", "h5", "h6"]):
                    walk(child, this_abstract)
                    continue
                text, math_items = _html_text(child)
                if text:
                    _block(blocks, "paragraph", text, section_path, {"html_id": child.get("id"), "ordinal": sum(b["type"] == "paragraph" for b in blocks) + 1}, inline_math=math_items or None, citations=_html_citations(child) or None)
                    if not this_abstract and text.strip().lower() != "abstract":
                        meaningful_outside_abstract = True
                continue

            # Inline math outside a paragraph is retained as an equation only
            # for explicit display nodes; otherwise process the containing text.
            walk(child, this_abstract)

    walk(root)
    if not _has_substantive_text(blocks) or not meaningful_outside_abstract:
        raise ParseError("HTML contains no substantive paper text outside an abstract")
    return _document("html", paper, blocks, warnings, {"input_bytes": len(data), "block_count": len(blocks), "table_count": seen_tables})


# ------------------------------- LaTeX ------------------------------------

_TEX_INCLUDE = re.compile(r"\\(?:input|include|subfile)\s*(?:\[[^\]]*\]\s*)?\{([^{}]+)\}|\\(?:input|include)\s+([^\s%]+)")
_MAIN_HINTS = ("main.tex", "paper.tex", "article.tex", "ms.tex")
_TEX_ENV = re.compile(r"\\begin\s*\{([^{}]+)\}")


def _safe_relative(name: str) -> str:
    normalized = name.replace("\\", "/")
    if not normalized or normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized) or "\x00" in normalized:
        raise ParseError(f"unsafe archive path: {name!r}")
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise ParseError(f"unsafe archive path: {name!r}")
    return "/".join(parts) if parts else "."


def _inflate_gzip(data: bytes) -> bytes:
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = decoder.decompress(data, MAX_LATEX_EXPANDED_BYTES + 1)
        if len(out) > MAX_LATEX_EXPANDED_BYTES or decoder.unconsumed_tail:
            raise ParseError("gzip LaTeX source exceeds expanded-size safety limit")
        if not decoder.eof:
            raise ParseError("truncated gzip LaTeX source")
        if decoder.unused_data:
            raise ParseError("trailing or concatenated gzip data is not accepted")
        out += decoder.flush(MAX_LATEX_EXPANDED_BYTES + 1 - len(out))
    except zlib.error as exc:
        raise ParseError(f"invalid gzip LaTeX source: {exc}") from exc
    if len(out) > MAX_LATEX_EXPANDED_BYTES:
        raise ParseError("gzip LaTeX source exceeds expanded-size safety limit")
    return out


def _validate_tex_file(name: str, payload: bytes) -> None:
    if payload.startswith(b"%PDF-"):
        raise ParseError(f"PDF data cannot be parsed as TeX source: {name}")
    if b"\x00" in payload:
        raise ParseError(f"binary data cannot be parsed as TeX source: {name}")
    sample = payload[:8192]
    controls = sum(byte < 32 and byte not in {9, 10, 12, 13} for byte in sample)
    if sample and controls / len(sample) > 0.05:
        raise ParseError(f"binary data cannot be parsed as TeX source: {name}")


def _mask_tex_comments(text: str) -> str:
    """Replace TeX comments with equal-length spaces, preserving source offsets."""
    chars = list(text)
    i = 0
    verbatim_names = {"verbatim", "verbatim*", "Verbatim", "lstlisting", "minted"}
    begin_re = re.compile(r"\\begin\s*\{(" + "|".join(re.escape(n) for n in verbatim_names) + r")\}")
    while i < len(text):
        begin = begin_re.match(text, i)
        if begin:
            end_re = re.compile(r"\\end\s*\{" + re.escape(begin.group(1)) + r"\}")
            end = end_re.search(text, begin.end())
            i = end.end() if end else len(text)
            continue
        if text[i] == "%":
            slash_count = 0
            j = i - 1
            while j >= 0 and text[j] == "\\":
                slash_count += 1
                j -= 1
            if slash_count % 2 == 0:
                end = text.find("\n", i)
                if end < 0:
                    end = len(text)
                for position in range(i, end):
                    chars[position] = " "
                i = end
                continue
        i += 1
    return "".join(chars)


def _archive_files(data: bytes) -> dict[str, bytes]:
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except (tarfile.TarError, OSError) as exc:
        raise ParseError(f"invalid LaTeX tar archive: {exc}") from exc
    files: dict[str, bytes] = {}
    seen_paths: set[str] = set()
    total = 0
    try:
        members = archive.getmembers()
        if len(members) > MAX_ARCHIVE_FILES:
            raise ParseError("LaTeX archive has too many entries")
        for member in members:
            path = _safe_relative(member.name)
            if path in seen_paths:
                raise ParseError(f"duplicate path in LaTeX archive: {member.name!r}")
            seen_paths.add(path)
            if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                raise ParseError(f"unsafe non-regular entry in LaTeX archive: {member.name!r}")
            if member.isdir():
                continue
            total += member.size
            if total > MAX_LATEX_EXPANDED_BYTES:
                raise ParseError("LaTeX archive exceeds expanded-size safety limit")
            if member.size > MAX_LATEX_EXPANDED_BYTES:
                raise ParseError("LaTeX archive member exceeds size safety limit")
            stream = archive.extractfile(member)
            if stream is None:
                raise ParseError(f"cannot read LaTeX archive member: {member.name!r}")
            payload = stream.read(member.size + 1)
            if len(payload) != member.size:
                raise ParseError(f"truncated LaTeX archive member: {member.name!r}")
            files[path] = payload
    finally:
        archive.close()
    if not files:
        raise ParseError("LaTeX archive contains no regular files")
    return files


def _decode_tex_sources(data: bytes) -> tuple[dict[str, bytes], list[str]]:
    warnings: list[str] = []
    if len(data) > MAX_LATEX_INPUT_BYTES:
        raise ParseError("LaTeX input exceeds compressed-size safety limit")
    if data.startswith(b"\x1f\x8b"):
        expanded = _inflate_gzip(data)
        if expanded.startswith((b"ustar",)) or b"\x00" in expanded[:512]:
            files = _archive_files(expanded)
            return files, warnings
        return {"main.tex": expanded}, warnings
    if len(data) > 262 and data[257:262] == b"ustar":
        return _archive_files(data), warnings
    return {"main.tex": data}, warnings


def _choose_main(files: dict[str, bytes]) -> str:
    tex_files = [name for name in files if name.lower().endswith((".tex", ".ltx"))]
    if not tex_files:
        raise ParseError("no TeX source found")
    for name in tex_files:
        _validate_tex_file(name, files[name])
    full_docs = [
        name
        for name in tex_files
        if "\\begin{document}" in _mask_tex_comments(files[name].decode("utf-8", errors="replace"))
    ]
    candidates = full_docs or tex_files
    for hint in _MAIN_HINTS:
        matches = [name for name in candidates if PurePosixPath(name).name.lower() == hint]
        if matches:
            return sorted(matches)[0]
    return sorted(candidates, key=lambda n: (n.count("/"), len(n), n))[0]


def _expand_includes(files: dict[str, bytes], main: str, warnings: list[str]) -> tuple[str, str, list[tuple[int, int, str, int]]]:
    chunks: list[str] = []
    raw_chunks: list[str] = []
    spans: list[tuple[int, int, str, int]] = []
    current_bytes = 0
    current_chars = 0
    active: list[str] = []

    def append(text: str, raw_text: str, filename: str, source_offset: int) -> None:
        nonlocal current_bytes, current_chars
        byte_count = len(raw_text.encode("utf-8"))
        if current_bytes + byte_count > MAX_LATEX_EXPANDED_BYTES:
            raise ParseError("expanded LaTeX includes exceed size safety limit")
        start = current_chars
        chunks.append(text)
        raw_chunks.append(raw_text)
        end = start + len(text)
        spans.append((start, end, filename, source_offset))
        current_chars = end
        current_bytes += byte_count

    def visit(filename: str, depth: int) -> None:
        if depth > MAX_INCLUDE_DEPTH:
            raise ParseError("LaTeX include depth exceeds safety limit")
        if filename in active:
            raise ParseError(f"cyclic LaTeX include detected: {' -> '.join(active + [filename])}")
        if filename not in files:
            warnings.append(f"local include not found: {filename}")
            return
        _validate_tex_file(filename, files[filename])
        active.append(filename)
        raw_text = files[filename].decode("utf-8", errors="replace")
        text = _mask_tex_comments(raw_text)
        cursor = 0
        for match in _TEX_INCLUDE.finditer(text):
            append(text[cursor:match.start()], raw_text[cursor:match.start()], filename, cursor)
            reference = (match.group(1) or match.group(2) or "").strip()
            reference = reference.removesuffix(".tex")
            base = PurePosixPath(filename).parent
            candidate = str(base / reference) if str(base) != "." else reference
            candidate = _safe_relative(candidate)
            candidate_with_ext = candidate + ".tex"
            target = candidate_with_ext if candidate_with_ext in files else candidate
            if target in files:
                visit(target, depth + 1)
            else:
                warnings.append(f"local include not found: {target}")
            cursor = match.end()
        append(text[cursor:], raw_text[cursor:], filename, cursor)
        active.pop()

    visit(main, 0)
    return "".join(chunks), "".join(raw_chunks), spans


def _source_locator(spans: list[tuple[int, int, str, int]], start: int, end: int) -> dict[str, Any]:
    locations = []
    for left, right, filename, source_offset in spans:
        if max(left, start) < min(right, end):
            locations.append({"filename": filename,
                              "start": source_offset + max(left, start) - left,
                              "end": source_offset + min(end, right) - left})
    if locations:
        return {**locations[0], **({"source_spans": locations} if len(locations) > 1 else {})}
    return {"filename": "main.tex", "start": start, "end": end}


def _balanced_group(text: str, start: int, open_char: str = "{", close_char: str = "}") -> tuple[str, int] | None:
    if start >= len(text) or text[start] != open_char:
        return None
    depth = 1
    i = start + 1
    body_start = i
    while i < len(text):
        char = text[i]
        if char == "\\":
            i += 2
            continue
        if char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return text[body_start:i], i + 1
        i += 1
    return None


_TEX_TEXT_COMMANDS = set(
    "text textbf textit emph mathrm textrm texttt operatorname frac sqrt sum int prod cdot times pm infty approx sim leq geq neq alpha beta gamma delta epsilon theta lambda mu nu pi sigma omega rho tau phi chi psi right left partial nabla overline underline hat bar vec boldsymbol mathbb mathcal mathbf ensuremath AA angstrom hspace vspace label ref eqref cite citep citet url href footnote ldots cdots times geq leq neq percent degree copyright TeX LaTeX".split()
)
_INLINE_MATH_RE = re.compile(r"(?<!\\)\$(.+?)(?<!\\)\$|\\\((.+?)\\\)", re.S)


def _tex_to_text(source: str) -> str:
    """Convert prose while preserving inline math and unknown TeX commands."""
    math_values: dict[str, str] = {}

    def hold_math(match: re.Match[str]) -> str:
        expression = match.group(1) if match.group(1) is not None else match.group(2)
        token = f"ASTROPARSERINLINE{len(math_values)}TOKEN"
        math_values[token] = f"${expression}$"
        return token

    protected = _INLINE_MATH_RE.sub(hold_math, source)
    macro_values: dict[str, str] = {}
    command_matches = list(re.finditer(r"\\([A-Za-z@]+)\*?", protected))
    for match in reversed(command_matches):
        name = match.group(1)
        if name in _TEX_TEXT_COMMANDS:
            continue
        end = match.end()
        cursor = end
        while cursor < len(protected) and protected[cursor].isspace():
            cursor += 1
        optional = _balanced_group(protected, cursor, "[", "]")
        if optional:
            end = optional[1]
            cursor = end
        while cursor < len(protected) and protected[cursor].isspace():
            cursor += 1
        group = _balanced_group(protected, cursor)
        if group:
            end = group[1]
            cursor = end
            while cursor < len(protected) and protected[cursor].isspace():
                cursor += 1
            group = _balanced_group(protected, cursor)
            if group:
                end = group[1]
        token = f"ASTROPARSERMACRO{len(macro_values)}TOKEN"
        macro_values[token] = protected[match.start():end]
        protected = protected[:match.start()] + token + protected[end:]
    try:
        from pylatexenc.latex2text import LatexNodes2Text

        converted = LatexNodes2Text().latex_to_text(protected)
    except ImportError:
        converted = protected
    converted = re.sub(r"\\(?:textbf|textit|emph|mathrm|textrm|texttt|operatorname)\s*\{([^{}]*)\}", r"\1", converted)
    converted = html.unescape(re.sub(r"\s+", " ", converted)).strip()
    for token, value in reversed(list(macro_values.items())):
        converted = converted.replace(token, value)
    for token, value in math_values.items():
        converted = converted.replace(token, value)
    return converted


def _find_env_end(text: str, name: str, start: int) -> tuple[int, int] | None:
    token = re.compile(r"\\(begin|end)\s*\{" + re.escape(name) + r"\}")
    depth = 1
    for match in token.finditer(text, start):
        if match.group(1) == "begin":
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return match.start(), match.end()
    return None


def parse_latex(data: bytes, *, paper: dict[str, Any] | None = None) -> dict[str, Any]:
    """Parse a single TeX file, gzip-compressed source, or bounded tar source."""
    _required_bytes(data, MAX_LATEX_INPUT_BYTES, "LaTeX")
    if bytes(data).startswith(b"%PDF-"):
        raise ParseError("PDF data cannot be parsed as LaTeX source")
    files, warnings = _decode_tex_sources(bytes(data))
    main = _choose_main(files)
    source, raw_source, spans = _expand_includes(files, main, warnings)
    expanded_source = source
    body_start = source.find("\\begin{document}")
    if body_start >= 0:
        body_start += len("\\begin{document}")
    else:
        body_start = 0
    body_end = source.find("\\end{document}", body_start)
    if body_end < 0:
        body_end = len(source)
    trimmed_spans = []
    for left, right, filename, source_offset in spans:
        kept_left = max(left, body_start)
        kept_right = min(right, body_end)
        if kept_left < kept_right:
            trimmed_spans.append((kept_left - body_start, kept_right - body_start, filename, source_offset + kept_left - left))
    source = source[body_start:body_end]
    raw_source = raw_source[body_start:body_end]
    spans = trimmed_spans
    blocks: list[dict[str, Any]] = []
    section_path: list[str] = []
    meaningful_outside_abstract = False
    abstract_only = True
    standard = set("begin end documentclass usepackage title author date maketitle abstract section subsection subsubsection paragraph subparagraph label ref eqref cite citep citet text textbf textit emph mathrm textrm texttt operatorname frac sqrt sum int prod cdot times pm infty approx sim leq geq neq alpha beta gamma delta epsilon theta lambda mu nu pi sigma omega rho tau phi chi psi right left partial nabla overline underline hat bar vec boldsymbol mathbb mathcal mathbf ensuremath newcommand renewcommand def providecommand input include subfile caption includegraphics centering hline toprule midrule bottomrule cline multicolumn multirow item bibitem bibliography bibliographystyle begin end footnote par newpage noindent vspace hspace endinput refname doi url href hypersetup graphicspath".split())

    def emit_paragraphs(segment: str, abs_flag: bool, base_offset: int) -> None:
        nonlocal meaningful_outside_abstract, abstract_only
        # Inline math is replaced in prose with $...$ while exact source is retained.
        for para_match in re.finditer(r"(?:^|\n\s*\n)(.*?)(?=\n\s*\n|\Z)", segment, flags=re.S):
            raw_para = para_match.group(1).strip()
            if not raw_para:
                continue
            source_start = base_offset + para_match.start()
            source_end = base_offset + para_match.end()
            raw_para_source = raw_source[source_start:source_end].strip()
            math_items: list[dict[str, str]] = []
            placeholders: dict[str, str] = {}

            def keep_math(match: re.Match[str]) -> str:
                expression = match.group(1) if match.group(1) is not None else match.group(2)
                math_items.append({"format": "latex", "text": expression})
                token = f"ASTROPAPERINLINE{len(placeholders)}TOKEN"
                placeholders[token] = f"${expression}$"
                return token

            readable_source = _INLINE_MATH_RE.sub(keep_math, raw_para)
            text = _tex_to_text(readable_source)
            for token, expression in placeholders.items():
                text = text.replace(token, expression)
            if not text:
                continue
            citations = [{"keys": [key.strip() for key in match.group(1).split(",")],
                          "raw": match.group(0)}
                         for match in re.finditer(r"\\cite[a-zA-Z]*\*?(?:\[[^\]]*\]){0,2}\s*\{([^{}]+)\}", raw_para)]
            _block(blocks, "paragraph", text, section_path, _source_locator(spans, source_start, source_end), latex=raw_para_source, inline_math=math_items or None, citations=citations or None)
            if not abs_flag:
                meaningful_outside_abstract = True
                abstract_only = False

    i = 0
    plain_start = 0
    while i < len(source):
        env_match = _TEX_ENV.search(source, i)
        heading_match = re.search(r"\\(section|subsection|subsubsection|paragraph|subparagraph)\*?\s*(?:\[[^]]*\]\s*)?\{", source[i:])
        # Display math environments and delimiters are handled as structures.
        next_positions = [m.start() for m in (env_match,) if m]
        if heading_match:
            next_positions.append(i + heading_match.start())
        math_match = re.search(r"\\\[|\$\$", source[i:])
        if math_match:
            next_positions.append(i + math_match.start())
        special = min(next_positions) if next_positions else len(source)
        if special > plain_start:
            emit_paragraphs(source[plain_start:special], "abstract" in section_path[-1].lower() if section_path else False, plain_start)
        if special >= len(source):
            break
        if env_match and env_match.start() == special:
            name = env_match.group(1).strip()
            env_end = _find_env_end(source, name, env_match.end())
            if not env_end:
                warnings.append(f"unclosed LaTeX environment: {name}")
                i = env_match.end()
                plain_start = i
                continue
            content_start, content_end = env_match.end(), env_end[0]
            raw_content = source[content_start:content_end]
            locator = _source_locator(spans, special, env_end[1])
            low_name = name.lower()
            if low_name in {"equation", "equation*", "align", "align*", "gather", "gather*", "multline", "multline*", "displaymath", "eqnarray", "eqnarray*"}:
                latex = raw_source[content_start:content_end].strip()
                _block(blocks, "equation", latex or "Equation", section_path, locator, latex=latex or None, raw=raw_source[special:env_end[1]])
                if not section_path or section_path[-1].lower() != "abstract":
                    meaningful_outside_abstract = True
                    abstract_only = False
            elif low_name in {"tabular", "tabular*", "longtable", "array"}:
                raw = raw_source[special:env_end[1]]
                _block(blocks, "table", "Table", section_path, locator, representation="latex", raw=raw)
                warnings.append("LaTeX table retained as raw source; cell structure was not inferred")
                if not section_path or section_path[-1].lower() != "abstract":
                    meaningful_outside_abstract = True
                    abstract_only = False
            elif low_name in {"table", "table*", "deluxetable", "deluxetable*", "deluxetable+"}:
                caption_match = re.search(r"\\caption\*?(?:\[[^]]*\])?\s*\{", raw_content)
                caption = ""
                if caption_match:
                    group = _balanced_group(raw_content, caption_match.end() - 1)
                    if group:
                        caption = _tex_to_text(group[0])
                raw_table = raw_source[special:env_end[1]]
                _block(blocks, "table", caption or "Table", section_path, locator, caption=caption or None, representation="latex", raw=raw_table)
                warnings.append("LaTeX table retained as raw source; cell structure was not inferred")
                if not section_path or section_path[-1].lower() != "abstract":
                    meaningful_outside_abstract = True
                    abstract_only = False
            elif low_name in {"figure", "figure*"}:
                caption_match = re.search(r"\\caption\*?(?:\[[^]]*\])?\s*\{", raw_content)
                caption = ""
                if caption_match:
                    group = _balanced_group(raw_content, caption_match.end() - 1)
                    if group:
                        caption = _tex_to_text(group[0])
                graphics = re.findall(r"\\includegraphics(?:\[[^]]*\])?\s*\{([^{}]+)\}", raw_content)
                if graphics:
                    warnings.append("figure image not downloaded; local or remote resources are not fetched")
                else:
                    warnings.append("figure has no extractable image source")
                _block(blocks, "figure_caption", caption, section_path, locator, latex=raw_source[content_start:content_end], image_sources=graphics)
                if caption and (not section_path or section_path[-1].lower() != "abstract"):
                    meaningful_outside_abstract = True
                    abstract_only = False
            elif low_name == "thebibliography":
                refs = list(re.finditer(r"\\bibitem(?:\[[^]]*\])?\s*\{([^{}]*)\}", raw_content))
                for idx, ref in enumerate(refs):
                    stop = refs[idx + 1].start() if idx + 1 < len(refs) else len(raw_content)
                    entry = raw_source[content_start + ref.end():content_start + stop].strip()
                    _block(blocks, "reference", _tex_to_text(entry), section_path + ["References"], _source_locator(spans, content_start + ref.start(), content_start + stop), key=ref.group(1), latex=entry)
            elif low_name == "abstract":
                emit_paragraphs(raw_content, True, content_start)
            # Other environments (itemize, center, etc.) are processed as prose.
            elif low_name not in {"document"}:
                emit_paragraphs(raw_content, "abstract" in section_path[-1].lower() if section_path else False, content_start)
            i = env_end[1]
            plain_start = i
            continue
        if heading_match and i + heading_match.start() == special:
            absolute = special
            match = re.match(r"\\(section|subsection|subsubsection|paragraph|subparagraph)\*?\s*(?:\[[^]]*\]\s*)?\{", source[absolute:])
            if not match:
                i = special + 1
                continue
            title_group = _balanced_group(source, absolute + match.end() - 1)
            if not title_group:
                warnings.append("unclosed heading argument")
                i = absolute + match.end()
                plain_start = i
                continue
            title_masked, end = title_group
            title_raw = raw_source[absolute + match.end():end - 1]
            level = {"section": 1, "subsection": 2, "subsubsection": 3, "paragraph": 4, "subparagraph": 5}[match.group(1)]
            title = _tex_to_text(title_masked)
            section_path = section_path[:level - 1] + [title]
            _block(blocks, "heading", title, section_path, _source_locator(spans, absolute, end), level=level, latex=title_raw)
            i = end
            plain_start = end
            continue
        if math_match and i + math_match.start() == special:
            marker = math_match.group()
            endmarker = r"\]" if marker == r"\[" else "$$"
            close = source.find(endmarker, special + len(marker))
            if close < 0:
                warnings.append("unclosed display math")
                i = special + len(marker)
                plain_start = i
                continue
            latex = raw_source[special + len(marker):close].strip()
            end = close + len(endmarker)
            _block(blocks, "equation", latex or "Equation", section_path, _source_locator(spans, special, end), latex=latex or None)
            if not section_path or section_path[-1].lower() != "abstract":
                meaningful_outside_abstract = True
                abstract_only = False
            i = end
            plain_start = end
            continue
        i = special + 1

    reference_count = sum(block.get("type") == "reference" for block in blocks)
    if re.search(r"\\bibliography\s*\{", expanded_source) and reference_count == 0:
        warnings.append("BibTeX bibliography was not parsed; resolve the corresponding .bbl to extract references")
    # Unknown control sequences are not expanded or executed. Report conservative
    # partial status rather than silently pretending macros were understood.
    commands = set(re.findall(r"\\([A-Za-z@]+)\*?", expanded_source))
    unknown = sorted(c for c in commands - standard if len(c) > 1)
    if unknown:
        warnings.append("unrecognized TeX macros retained conservatively: " + ", ".join(unknown[:20]))
        if len(unknown) > 20:
            warnings.append(f"{len(unknown) - 20} additional unrecognized macros")
    if not _has_substantive_text(blocks) or not meaningful_outside_abstract or abstract_only:
        raise ParseError("LaTeX contains no substantive paper text outside an abstract")
    warnings.append("LaTeX parsed without typesetting or custom macro expansion; structure may be incomplete")
    return _document("latex", paper, blocks, warnings, {"input_bytes": len(data), "expanded_bytes": len(source.encode("utf-8")), "file_count": len(files), "block_count": len(blocks), "main_file": main}, partial=True)


# -------------------------------- PDF -------------------------------------

def parse_pdf(data: bytes, *, paper: dict[str, Any] | None = None) -> dict[str, Any]:
    """Extract selectable text from PDF pages. PDF output is always partial."""
    _required_bytes(data, MAX_PDF_BYTES, "PDF")
    if not bytes(data).startswith(b"%PDF-"):
        raise ParseError("input does not have a PDF signature")
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise ParseError("PDF parsing requires pypdf") from exc
    try:
        reader = PdfReader(io.BytesIO(bytes(data)), strict=False)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:
                raise ParseError("encrypted PDF cannot be parsed") from exc
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ParseError("PDF exceeds page-count safety limit")
        blocks: list[dict[str, Any]] = []
        warnings: list[str] = ["PDF text extraction is structural only and always partial"]
        section_path: list[str] = []
        nonempty_pages = 0
        for page_no, page in enumerate(reader.pages, 1):
            try:
                page_text = page.extract_text() or ""
            except Exception:
                warnings.append(f"text extraction failed on page {page_no}")
                continue
            page_text = page_text.replace("\x00", "")
            if not page_text.strip():
                continue
            nonempty_pages += 1
            for para in re.split(r"\n\s*\n+", page_text):
                lines = [line.strip() for line in para.splitlines() if line.strip()]
                if not lines:
                    continue
                # A modest heading heuristic; no content is promoted based only on style.
                if len(lines) == 1 and len(lines[0]) < 120 and (lines[0].isupper() or re.match(r"^\d+(?:\.\d+)*\s+\S", lines[0])):
                    title = lines[0]
                    section_path = [title]
                    _block(blocks, "heading", title, section_path, {"page": page_no})
                else:
                    _block(blocks, "paragraph", " ".join(lines), section_path, {"page": page_no, "ordinal": sum(b["type"] == "paragraph" and b["locator"].get("page") == page_no for b in blocks) + 1})
        if not nonempty_pages or not _has_substantive_text(blocks):
            raise ParseError("PDF contains no extractable text (blank or scanned-only PDF)")
        return _document("pdf", paper, blocks, warnings, {"input_bytes": len(data), "page_count": len(reader.pages), "text_pages": nonempty_pages, "block_count": len(blocks)}, partial=True)
    except ParseError:
        raise
    except Exception as exc:
        raise ParseError(f"cannot parse PDF: {exc}") from exc


def markdown(document: dict[str, Any]) -> str:
    """Render a parser JSON document deterministically as conservative Markdown."""
    if not isinstance(document, dict) or not isinstance(document.get("blocks"), list):
        raise ValueError("document must be a parser JSON object with a blocks list")
    lines: list[str] = []
    for block in document["blocks"]:
        kind = block.get("type")
        text = str(block.get("text", "")).strip()
        if kind == "heading":
            level = int(block.get("level") or len(block.get("section_path") or []) or 1)
            lines.extend(["#" * max(1, min(level, 6)) + " " + text, ""])
        elif kind == "equation":
            latex = block.get("latex")
            if latex:
                lines.extend(["$$", str(latex).strip(), "$$", ""])
            else:
                lines.extend([text, ""])
        elif kind == "table":
            raw = block.get("raw")
            lines.extend([f"[Table] {block.get('caption') or text}"])
            if raw:
                lines.extend(["```text", str(raw).strip(), "```"])
            lines.append("")
        elif kind == "figure_caption":
            lines.extend([f"[Figure] {text}", ""])
        elif kind == "reference":
            lines.append("- " + text)
        elif text:
            lines.extend([text, ""])
    return "\n".join(lines).rstrip() + ("\n" if lines else "")
