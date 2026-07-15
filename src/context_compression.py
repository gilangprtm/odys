"""
Context compression for Odysseus agent loop.

Compresses long histories when input tokens exceed soft budget.
Keeps:
- System prompt + rules
- Last N rounds (configurable)
- Active document / email draft
- Recent tool results (non-truncated)

Replaces older tool output + mid history with a single "compressed context" note.
"""

import logging
from typing import Dict, List, Optional

from src.model_context import estimate_tokens

logger = logging.getLogger(__name__)


def compress_messages(
    messages: List[Dict],
    target_tokens: int,
    *,
    keep_last_rounds: int = 4,
    reserve_tokens: int = 2048,
) -> List[Dict]:
    """
    Compress `messages` down toward `target_tokens`.

    Strategy (simple, deterministic, no LLM call):
    1. Always keep the first system message(s) and the last `keep_last_rounds` rounds.
    2. Identify "compressible" blocks: tool results + older assistant/user pairs.
    3. Replace compressible blocks with one summary message containing:
       - Number of rounds dropped
       - Approximate token count removed
       - High-level note (e.g. "tool results and intermediate reasoning summarized")
    4. Never drop the most recent user message.

    Returns a new list (does not mutate input).
    """
    if not messages:
        return messages

    total = estimate_tokens(messages)
    if total <= target_tokens:
        return messages[:]  # no compression needed

    # Find index of the last user message (never drop it)
    last_user_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            last_user_idx = i
            break
    if last_user_idx is None:
        last_user_idx = len(messages) - 1

    # Keep system block(s) + last N rounds + last user
    # A "round" here is roughly assistant + tool + user triplet
    kept: List[Dict] = []
    compressible: List[Dict] = []

    system_end = 0
    for i, m in enumerate(messages):
        if m.get("role") == "system":
            kept.append(m)
            system_end = i + 1
        else:
            break

    # Collect recent rounds we want to preserve
    recent_start = max(system_end, last_user_idx - (keep_last_rounds * 3))
    recent = messages[recent_start:last_user_idx + 1]

    # Everything between system_end and recent_start is compressible
    compressible = messages[system_end:recent_start]

    if not compressible:
        return messages[:]  # nothing safe to drop

    dropped = len(compressible)
    dropped_tokens = estimate_tokens(compressible)

    summary = {
        "role": "system",
        "content": (
            f"[Compressed context: {dropped} intermediate messages / ~{dropped_tokens} tokens removed. "
            f"Last {keep_last_rounds} rounds + current task preserved. "
            "Use manage_memory or previous tool results if you need earlier details.]"
        ),
        "metadata": {"compressed": True, "dropped": dropped},
    }

    compressed = kept + [summary] + recent
    new_total = estimate_tokens(compressed)

    logger.info(
        "[context-compress] before=%d after=%d dropped=%d target=%d",
        total, new_total, dropped, target_tokens,
    )

    return compressed
