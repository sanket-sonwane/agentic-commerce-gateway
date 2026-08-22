"""Deterministic consent classifier — the ONLY gate that can trigger payment.

The LLM never decides that the user approved. When a payment question is armed
(``Conversation.awaiting_payment_consent``), incoming chat text is classified here:

    approve  -> resume_after_decision(True)   (money movement starts)
    cancel   -> cancel the live order deterministically
    soft_no  -> don't charge, keep chatting
    other    -> normal LLM turn (edits, questions, small talk)

Rules are deliberately strict: approval only counts when EVERY token of a short
message belongs to an affirmation/filler whitelist. Any edit verb, negation or
unknown noun ("yes but remove rice") automatically falls through to "other".
"""
import re

_FILLERS = {"please", "bhai", "ji", "na", "yaar", "zara", "then"}

_AFFIRM_WORDS = {
    "y", "ya", "yas", "yeah", "yep", "yup", "yes",
    "ok", "okay", "k", "kk", "sure",
    "approve", "approved", "confirm", "confirmed",
    "proceed", "pay", "done", "final", "finalize", "finalise",
    "haan", "haanja", "haanji", "haa", "han",
}
_AFFIRM_PHRASES = {
    "go ahead", "go on", "place order", "place the order", "place it",
    "pay now", "do it", "do na", "sounds good", "looks good", "good",
    "that's all", "that's it", "thats all", "thats it", "all good",
    "its final", "it's final", "order it", "buy it", "charge me",
    "theek hai", "thik hai", "theek", "thik",
    "kar do", "kardo", "karo", "kar de", "karde",
    "ho jaye", "ho ja", "laga do", "lagado", "lagade",
    "paisa de do", "bhej do", "bhejo", "order karo", "order kar do",
    "ban gaya", "sahi hai", "perfect", "great", "awesome",
}

_CANCEL_WORDS = {"cancel", "abort"}
_CANCEL_PHRASES = {
    "cancel order", "cancel the order", "cancel my order", "cancel this order",
    "order cancel", "order cancel karo", "order cancel kar do", "cancel karo",
    "cancel kar do", "stop", "discard it", "discard order", "band karo",
}

_SOFTNO_WORDS = {"no", "nope", "nah", "nahi", "nhi", "wait", "later", "ruko"}
_SOFTNO_PHRASES = {
    "not yet", "don't pay", "dont pay", "abhi nahi", "hold on", "hold",
    "wait please", "thoda ruk", "ruk ja", "ruko zara", "no thanks", "no thank you",
}

_MAX_WORDS = 6


def _norm(text: str) -> list[str]:
    t = re.sub(r"[^\w\s']", " ", (text or "").lower())
    return t.split()


def _match(words: list[str], single: set[str], phrases: set[str]) -> bool:
    if not words or len(words) > _MAX_WORDS:
        return False
    joined = " ".join(words)
    if joined in phrases:
        return True

    def ok_rest(rest: list[str]) -> bool:
        return all(w in single or w in _FILLERS for w in rest)

    # A known phrase embedded among affirmation/filler tokens ("ok place order please").
    for phrase in phrases:
        p = phrase.split()
        n = len(p)
        if n > len(words):
            continue
        for i in range(len(words) - n + 1):
            if words[i : i + n] == p and ok_rest(words[:i] + words[i + n :]):
                return True
    return ok_rest(words)


def classify_reply(text: str) -> str:
    """Return 'approve' | 'cancel' | 'soft_no' | 'other'. Deterministic."""
    words = _norm(text)
    if not words or len(words) > _MAX_WORDS:
        return "other"
    if _match(words, _CANCEL_WORDS, _CANCEL_PHRASES):
        return "cancel"
    if _match(words, _AFFIRM_WORDS, _AFFIRM_PHRASES):
        return "approve"
    if _match(words, _SOFTNO_WORDS, _SOFTNO_PHRASES):
        return "soft_no"
    return "other"


def is_explicit_cancel(text: str) -> bool:
    """True when a short message clearly asks to cancel the whole order."""
    words = _norm(text)
    return _match(words, _CANCEL_WORDS, _CANCEL_PHRASES)
