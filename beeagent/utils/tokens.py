_CACHE = {}


def _encoding(model: str):
    """The tokenizer to budget with.

    `encoding_for_model` raises KeyError for every non-OpenAI id, which is the
    whole g4f catalogue, so an unknown model falls back to cl100k_base — a real
    tokenizer — rather than to a char count that under-reads Cyrillic by ~2x.
    """
    import tiktoken

    name = (model or "").lower()
    if name not in _CACHE:
        try:
            _CACHE[name] = tiktoken.encoding_for_model(model)
        except Exception:
            _CACHE[name] = tiktoken.get_encoding("cl100k_base")
    return _CACHE[name]


def count_tokens(text: str, model: str = "gpt-4") -> int:
    """Tokens this text will cost the model.

    Getting this wrong in the optimistic direction is what made small-context
    models forget the conversation: we thought the request fit, the endpoint
    trimmed it and the history never reached the model.
    """
    if not text:
        return 0
    try:
        return len(_encoding(model).encode(text))
    except Exception:
        # tiktoken is optional — Android has no wheel for it and no compiler to
        # build one. Without it, over-count rather than send an oversized request.
        return max(1, int(len(text) * 0.6))


def cut_tokens(text: str, tokens: int, model: str = "gpt-4", head: float = 0.7) -> tuple[str, str, int]:
    """Split `text` into (head, tail, removed_tokens) costing about `tokens` in total.

    Cutting on real token boundaries rather than a chars/4 guess: Cyrillic
    costs roughly twice the tokens that estimate allows, so a char-based clip
    kept sending messages that were still too big.
    """
    if tokens <= 0:
        return "", "", count_tokens(text, model)
    try:
        enc = _encoding(model)
        ids = enc.encode(text)
    except Exception:
        total = count_tokens(text, model)
        if total <= tokens:
            return text, "", 0
        chars = max(1, int(tokens * 2))          # conservative for Cyrillic
        return text[:chars], text[-chars:], max(0, total - tokens)
    if len(ids) <= tokens:
        return text, "", 0
    keep_head = max(1, int(tokens * head))
    keep_tail = max(1, tokens - keep_head)
    return (enc.decode(ids[:keep_head]), enc.decode(ids[-keep_tail:]),
            max(0, len(ids) - keep_head - keep_tail))
