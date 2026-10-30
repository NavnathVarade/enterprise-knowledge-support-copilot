from __future__ import annotations

import pytest
from rag.chunking.tokenizer import count_tokens


@pytest.mark.unit
def test_empty_string_is_zero_tokens() -> None:
    assert count_tokens("") == 0


@pytest.mark.unit
def test_short_text_is_at_least_one_token() -> None:
    assert count_tokens("a") >= 1


@pytest.mark.unit
def test_longer_text_has_more_tokens() -> None:
    assert count_tokens("a" * 400) > count_tokens("a" * 40)


@pytest.mark.unit
def test_token_count_is_deterministic() -> None:
    text = "The quick brown fox jumps over the lazy dog."
    assert count_tokens(text) == count_tokens(text)


@pytest.mark.unit
def test_roughly_four_characters_per_token() -> None:
    text = "x" * 400
    tokens = count_tokens(text)
    assert 90 <= tokens <= 110
