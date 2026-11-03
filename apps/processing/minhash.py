"""Дословные заимствования (README §4, шаг 5): шинглы из слов, MinHash и LSH-пояса.

Подпись — 128 перестановок; 32 пояса по 4 строки. Хэши поясов лежат в
Document.minhash_bands (GIN-индекс), кандидаты ищутся пересечением массивов.
Вероятность стать кандидатом: J=0,6 → 99 %, J=0,3 → 23 %, J=0,1 → 0,3 %.
"""

import hashlib

import numpy as np
from datasketch import MinHash

from .text import words

NUM_PERM = 128
BANDS = 32
ROWS = NUM_PERM // BANDS
SEED = 1  # фиксирован: подписи разных запусков сравнимы
MIN_SHINGLES = 10  # короче — совпадение пары фраз ещё не заимствование


def shingles(text: str, size: int) -> set[str]:
    tokens = words(text)
    return {" ".join(tokens[i : i + size]) for i in range(len(tokens) - size + 1)}


def signature(shingle_set: set[str]) -> np.ndarray | None:
    if len(shingle_set) < MIN_SHINGLES:
        return None
    minhash = MinHash(num_perm=NUM_PERM, seed=SEED)
    minhash.update_batch(s.encode("utf-8") for s in shingle_set)
    return minhash.hashvalues.astype(np.uint64)


def bands(hashvalues: np.ndarray) -> list[int]:
    """Хэш каждого пояса со своим номером — в int64 для массива PostgreSQL."""
    out = []
    for band in range(BANDS):
        chunk = hashvalues[band * ROWS : (band + 1) * ROWS].tobytes()
        digest = hashlib.blake2b(bytes([band]) + chunk, digest_size=8).digest()
        out.append(int.from_bytes(digest, "little", signed=True))
    return out


def to_bytes(hashvalues: np.ndarray) -> bytes:
    return hashvalues.astype("<u8").tobytes()


def from_bytes(data) -> np.ndarray:
    return np.frombuffer(bytes(data), dtype="<u8")


def jaccard(a: np.ndarray, b: np.ndarray) -> float:
    """Оценка Jaccard по доле совпавших позиций подписи."""
    return float(np.mean(a == b))


def containment(jaccard_value: float, size_source: int, size_target: int) -> float:
    """Какая доля шинглов заимствующего текста есть в оригинале: |A∩B| / |B|.

    Выводится из Jaccard и размеров множеств; нужна, когда короткий пост
    дословно повторяет абзац длинной статьи (Jaccard при этом мал).
    """
    if not size_target:
        return 0.0
    intersection = jaccard_value * (size_source + size_target) / (1 + jaccard_value)
    return min(1.0, intersection / size_target)
