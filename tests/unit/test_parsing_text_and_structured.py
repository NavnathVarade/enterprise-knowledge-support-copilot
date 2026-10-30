from __future__ import annotations

import pytest
from packages.domain.errors import ParsingFailed
from rag.parsing.csv_parser import CsvParser
from rag.parsing.json_parser import JsonParser
from rag.parsing.text_parser import MarkdownParser, TextParser


@pytest.mark.unit
def test_text_parser_decodes_utf8() -> None:
    result = TextParser().parse("Hello, world. Café.".encode())
    assert result.text == "Hello, world. Café."


@pytest.mark.unit
def test_text_parser_strips_bom() -> None:
    # Construct bytes the way a real BOM-prefixed file actually looks: the 3-byte
    # UTF-8 BOM marker followed by plain UTF-8 content -- NOT
    # `"\ufeffHello".encode("utf-8-sig")`, which double-includes the BOM (once as the
    # encoding's marker, once because the source string already contains a literal
    # \ufeff character) and does not represent a real file.
    content = b"\xef\xbb\xbf" + b"Hello"
    result = TextParser().parse(content)
    assert not result.text.startswith("\ufeff")
    assert result.text == "Hello"


@pytest.mark.unit
def test_text_parser_normalizes_crlf_line_endings() -> None:
    result = TextParser().parse(b"line1\r\nline2\r\nline3")
    assert result.text == "line1\nline2\nline3"
    assert "\r" not in result.text


@pytest.mark.unit
def test_text_parser_falls_back_to_latin1_for_non_utf8_bytes() -> None:
    content = "café".encode("latin-1")
    result = TextParser().parse(content)
    assert "caf" in result.text


@pytest.mark.unit
def test_markdown_parser_extracts_headings_with_correct_levels() -> None:
    content = b"# Title\n\nSome text.\n\n## Section One\n\nMore text.\n\n### Subsection\n"
    result = MarkdownParser().parse(content)
    assert [h.text for h in result.headings] == ["Title", "Section One", "Subsection"]
    assert [h.level for h in result.headings] == [1, 2, 3]


@pytest.mark.unit
def test_markdown_parser_heading_offsets_point_into_text() -> None:
    content = b"intro\n\n## Heading\n\nbody"
    result = MarkdownParser().parse(content)
    heading = result.headings[0]
    assert result.text[heading.character_offset : heading.character_offset + 2] == "##"


@pytest.mark.unit
def test_markdown_parser_with_no_headings_returns_empty_tuple() -> None:
    result = MarkdownParser().parse(b"just plain text, no markup")
    assert result.headings == ()


@pytest.mark.unit
def test_csv_parser_renders_valid_csv_as_markdown_table() -> None:
    content = b"name,age\nAlice,30\nBob,25"
    result = CsvParser().parse(content)
    assert "| name | age |" in result.text
    assert "| Alice | 30 |" in result.text
    assert result.metadata["row_count"] == 2
    assert result.metadata["column_count"] == 2


@pytest.mark.unit
def test_csv_parser_rejects_inconsistent_column_count() -> None:
    content = b"name,age\nAlice,30\nBob"
    with pytest.raises(ParsingFailed):
        CsvParser().parse(content)


@pytest.mark.unit
def test_csv_parser_rejects_empty_content() -> None:
    with pytest.raises(ParsingFailed):
        CsvParser().parse(b"")


@pytest.mark.unit
def test_json_parser_pretty_prints_valid_json() -> None:
    content = b'{"a": 1, "b": [1, 2, 3]}'
    result = JsonParser().parse(content)
    assert result.text == '{\n  "a": 1,\n  "b": [\n    1,\n    2,\n    3\n  ]\n}'
    assert result.metadata["top_level_type"] == "dict"
    assert result.metadata["top_level_length"] == 2


@pytest.mark.unit
def test_json_parser_rejects_malformed_json() -> None:
    with pytest.raises(ParsingFailed):
        JsonParser().parse(b'{"a": not valid json}')


@pytest.mark.unit
def test_json_parser_handles_top_level_array() -> None:
    result = JsonParser().parse(b"[1, 2, 3]")
    assert result.metadata["top_level_type"] == "list"
    assert result.metadata["top_level_length"] == 3
