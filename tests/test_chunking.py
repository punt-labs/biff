"""Tests for ``biff.chunking``."""

from __future__ import annotations

from biff.chunking import MAX_CHUNK_CHARS, chunk_message


class TestChunkMessage:
    def test_short_text_single_chunk(self) -> None:
        text = "hello world"
        assert chunk_message(text) == [text]

    def test_exact_limit_single_chunk(self) -> None:
        text = "x" * MAX_CHUNK_CHARS
        assert chunk_message(text) == [text]

    def test_one_over_limit_splits(self) -> None:
        text = "x" * (MAX_CHUNK_CHARS + 1)
        chunks = chunk_message(text)
        assert len(chunks) == 2
        assert all(len(c) <= MAX_CHUNK_CHARS for c in chunks)

    def test_splits_at_word_boundary(self) -> None:
        # 10-char words + space = 11 chars each; 50 words = 550 chars
        words = ["abcdefghij"] * 50
        text = " ".join(words)
        assert len(text) > MAX_CHUNK_CHARS

        chunks = chunk_message(text)
        assert len(chunks) >= 2
        for chunk in chunks:
            assert len(chunk) <= MAX_CHUNK_CHARS
            # No word should be split mid-word
            for word in chunk.split():
                assert word == "abcdefghij"

    def test_single_huge_word_hard_split(self) -> None:
        word = "a" * 1024
        chunks = chunk_message(word)
        assert len(chunks) == 2
        assert chunks[0] == "a" * MAX_CHUNK_CHARS
        assert chunks[1] == "a" * (1024 - MAX_CHUNK_CHARS)
        assert all(len(c) <= MAX_CHUNK_CHARS for c in chunks)

    def test_chunks_reconstruct_original(self) -> None:
        words = [f"word{i}" for i in range(120)]
        text = " ".join(words)
        chunks = chunk_message(text)
        reconstructed = " ".join(chunks)
        assert reconstructed == text

    def test_each_chunk_within_limit(self) -> None:
        text = "hello " * 200  # 1200 chars
        chunks = chunk_message(text.strip())
        assert all(len(c) <= MAX_CHUNK_CHARS for c in chunks)

    def test_empty_after_split_not_included(self) -> None:
        # Message that splits exactly at limit should not produce empty trailing chunk
        text = "x" * MAX_CHUNK_CHARS + " " + "y" * 10
        chunks = chunk_message(text)
        assert all(c for c in chunks)  # No empty strings
