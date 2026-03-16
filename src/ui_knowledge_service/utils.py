"""Shared helpers."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "unknown"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def tokenize(value: str) -> list[str]:
    return TOKEN_PATTERN.findall(value.lower())


def chunk_text(text: str, *, max_tokens: int = 180, overlap: int = 30) -> list[str]:
    tokens = tokenize(text)
    if not tokens:
        return []
    if max_tokens <= overlap:
        raise ValueError("max_tokens must be greater than overlap")
    chunks: list[str] = []
    start = 0
    step = max_tokens - overlap
    while start < len(tokens):
        chunk_tokens = tokens[start : start + max_tokens]
        chunks.append(" ".join(chunk_tokens))
        start += step
    return chunks


def unique_strings(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        cleaned = value.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            output.append(cleaned)
    return output


def infer_extension(url: str, content_type: str | None) -> str:
    suffix = Path(url).suffix.lower()
    if suffix in {".html", ".htm", ".md", ".markdown", ".json", ".txt"}:
        return suffix
    if not content_type:
        return ".txt"
    lowered = content_type.lower()
    if "html" in lowered:
        return ".html"
    if "json" in lowered:
        return ".json"
    if "markdown" in lowered:
        return ".md"
    return ".txt"


def make_snippet(text: str, query: str, *, width: int = 240) -> str:
    if not text:
        return ""
    lowered = text.lower()
    for token in tokenize(query):
        idx = lowered.find(token)
        if idx >= 0:
            start = max(0, idx - width // 3)
            end = min(len(text), start + width)
            return text[start:end].strip()
    return text[:width].strip()

