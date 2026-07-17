"""Neuron Vector Embeddings — local FastEmbed with BAAI/bge-small-en-v1.5 (384 dim).

Lazy-loaded singleton. Provides:
  - get_embedding(text) → numpy array
  - cosine_sim(query_vec, node_embeddings_dict) → {node_id: score}
"""

from __future__ import annotations

import threading
import numpy as np
from typing import Any

_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
_EMBEDDING_DIM = 384


class _Embedder:
    def __init__(self):
        self._model = None
        self._lock = threading.Lock()
        self._ready = False

    def _init(self):
        with self._lock:
            if self._ready:
                return
            try:
                from fastembed import TextEmbedding
                import os
                cache_dir = os.path.join("data", "fastembed_cache")
                self._model = TextEmbedding(
                    model_name=_EMBEDDING_MODEL,
                    cache_dir=cache_dir,
                )
                # Warmup
                test = list(self._model.embed(["warmup"]))
                if test:
                    self._ready = True
            except Exception:
                self._ready = False

    def embed(self, texts: list[str]) -> list[np.ndarray]:
        """Embed a list of texts. Returns list of float32 numpy arrays."""
        self._init()
        if not self._ready or not texts:
            return [np.zeros(_EMBEDDING_DIM, dtype="float32") for _ in texts]
        try:
            vecs = list(self._model.embed(texts))
            return [np.array(v, dtype="float32") for v in vecs]
        except Exception:
            return [np.zeros(_EMBEDDING_DIM, dtype="float32") for _ in texts]

    def embed_single(self, text: str) -> np.ndarray:
        """Embed a single text string."""
        if not text.strip():
            return np.zeros(_EMBEDDING_DIM, dtype="float32")
        results = self.embed([text])
        return results[0] if results else np.zeros(_EMBEDDING_DIM, dtype="float32")


# Module-level singleton
_embedder = _Embedder()


def get_embedder() -> _Embedder:
    return _embedder


def embedding_dim() -> int:
    return _EMBEDDING_DIM


def cosine_similarity_single(query: np.ndarray, node_embeddings: dict[str, np.ndarray]) -> dict[str, float]:
    """Compute cosine similarity between query vector and all node embeddings.

    Returns {node_id: score} for all nodes with non-zero embeddings.
    """
    if query is None or query.size == 0 or not node_embeddings:
        return {}

    # Normalize query
    q_norm = np.linalg.norm(query)
    if q_norm < 1e-8:
        return {}

    results = {}
    for nid, vec in node_embeddings.items():
        if vec is None or vec.size == 0:
            continue
        v_norm = np.linalg.norm(vec)
        if v_norm < 1e-8:
            continue
        # Cosine similarity = dot product of normalized vectors
        sim = float(np.dot(query, vec) / (q_norm * v_norm))
        if sim > 0.01:  # noise floor
            results[nid] = round(sim, 4)

    return results


def cosine_similarity_batch(query: np.ndarray, node_embeddings: dict[str, np.ndarray]) -> dict[str, float]:
    """Vectorized cosine similarity for many nodes (faster for >50 nodes)."""
    if query is None or query.size == 0 or not node_embeddings:
        return {}

    q_norm = np.linalg.norm(query)
    if q_norm < 1e-8:
        return {}

    nids = list(node_embeddings.keys())
    vecs = np.array([node_embeddings[nid] for nid in nids], dtype="float32")

    # Normalize all vectors at once
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1, norms)
    vecs_norm = vecs / norms

    # Dot product
    q_normed = (query / q_norm).reshape(1, -1).astype("float32")
    sims = (vecs_norm @ q_normed.T).flatten()

    results = {}
    for i, nid in enumerate(nids):
        sim = float(sims[i])
        if sim > 0.01:
            results[nid] = round(sim, 4)

    return results
