"""Convert v2 attribution into bounded, human-readable rewrite directives."""

from __future__ import annotations

import re


POLICY_VERSION = "v3"
# Production evidence on 2026-09-11 showed that a second targeted attempt only
# rescued documents already close to the target. Above this score, the second
# candidate added latency/cost without producing another <20% result.
SECOND_ROUND_MAX_SCORE = 30.0

DIRECTIVES = {
    "average_word_length": "Keep necessary technical terms, but simplify non-technical wording and prefer concrete verbs.",
    "long_word_ratio": "Keep necessary technical terms, but replace avoidable long abstractions with plain precise wording.",
    "nominalization_ratio": "Turn avoidable nominalizations into direct actions without changing the claims.",
    "sentence_length_mean": "Reshape sentence length where clarity permits instead of keeping an even repeated rhythm.",
    "sentence_length_std": "Vary sentence structure naturally while keeping every logical relationship intact.",
    "sentence_length_cv": "Vary sentence structure naturally while keeping every logical relationship intact.",
    "sentence_length_iqr": "Vary sentence structure naturally while keeping every logical relationship intact.",
    "repeated_sentence_start_ratio": "Change repetitive sentence openings through genuine clause restructuring.",
    "function_word_ratio": "Use ordinary articles, prepositions, pronouns, and conjunctions where they make the relationships explicit; avoid compressed noun phrases and keep roughly one third of the words as ordinary connecting or function words.",
    "transition_ratio": "Replace formulaic transitions with connections that follow from the actual content.",
    "modal_ratio": "Use direct statements where the evidence supports them while preserving real uncertainty.",
    "hedge_ratio": "Preserve necessary qualifications but remove stacked or generic hedging.",
    "dash_per_sentence": "Rebuild affected sentences instead of mechanically swapping dash punctuation.",
    "semicolon_per_sentence": "Rebuild affected sentences instead of mechanically swapping punctuation.",
    "comma_per_sentence": "Reduce overloaded clause chains by restructuring the sentence where useful.",
    "type_token_ratio": "Repeat the same important nouns and verbs where they refer to the same thing; do not replace them with one-off synonyms, and favor a compact working vocabulary over maximum variety.",
    "hapax_ratio": "Reuse the passage's key vocabulary and avoid introducing unnecessary words that appear only once.",
    "repeated_bigram_ratio": "Use natural recurrence only where the topic requires it; do not mechanically add or delete repeated phrases.",
}

PROTECTION_DIRECTIVES = {
    "dash_per_sentence": "Preserve the source punctuation pattern. Do not introduce em dashes or en dashes unless they already appear in the source.",
    "average_word_length": "Keep average word length at or below about 5.4 letters. Prefer short, common, precise words and never replace plain wording with longer academic synonyms.",
    "long_word_ratio": "Keep necessary technical terms, but do not add avoidable long words or more formal synonyms.",
    "sentence_length_mean": "Keep the source's broad sentence-length profile; do not regularize every sentence to a similar medium length.",
    "sentence_length_std": "Preserve the source's existing contrast between shorter and longer sentences.",
    "sentence_length_cv": "Preserve the source's existing variation in sentence length and structure.",
    "sentence_length_iqr": "Preserve the source's existing variation in sentence length and structure.",
    "repeated_bigram_ratio": "Do not remove natural recurring terminology or repeated phrases that keep the passage coherent.",
    "function_word_ratio": "Do not mechanically remove or add articles, prepositions, pronouns, or conjunctions.",
    "type_token_ratio": "Do not force extra synonyms; retain natural repetition of the source's key terms.",
    "hapax_ratio": "Do not introduce unnecessary one-off vocabulary merely to make the wording more varied.",
    "comma_per_sentence": "Preserve the source's punctuation rhythm unless a sentence is genuinely unclear.",
    "modal_ratio": "Do not add or remove modality unless required to preserve the original degree of certainty.",
    "hedge_ratio": "Preserve the original degree of qualification and uncertainty.",
}

DEFAULT_DIRECTIVE = "Restructure formulaic phrasing while preserving the exact meaning and level of formality."

_PROTECTED_PATTERNS = (
    re.compile(r"https?://\S+"),
    re.compile(r"\b\d+(?:[.,]\d+)*(?:%|mm|cm|kg|km|GB|MB)?\b", re.I),
    re.compile(r"\([^)]*(?:19|20)\d{2}[^)]*\)"),
)


def directives_for(groups, limit=3):
    directives = []
    names = []
    for item in groups or []:
        name = item.get("feature_group")
        directive = DIRECTIVES.get(name)
        if not directive or directive in directives:
            continue
        names.append(name)
        directives.append(directive)
        if len(directives) >= limit:
            break
    if not directives:
        directives.append(DEFAULT_DIRECTIVE)
    return names, directives


def protection_directives_for(groups, limit=3):
    """Turn score-lowering features into explicit constraints for the LLM."""
    names = []
    directives = []
    for item in groups or []:
        name = item.get("feature_group")
        directive = PROTECTION_DIRECTIVES.get(name)
        if not directive or directive in directives:
            continue
        names.append(name)
        directives.append(directive)
        if len(directives) >= limit:
            break
    return names, directives


def feature_target_guidance(profile, edit_features, protect_features):
    """Give the LLM concrete v2 target bands instead of vague directions."""
    profile = profile or {}
    messages = []
    edit_features = set(edit_features or [])
    protect_features = set(protect_features or [])

    targets = {
        "function_word_ratio": (0.34, 0.40, "function-word share"),
        "type_token_ratio": (0.65, 0.75, "unique-word share"),
        "hapax_ratio": (0.50, 0.65, "one-off-word share"),
        "repeated_bigram_ratio": (0.02, 0.06, "repeated-bigram share"),
    }
    for name, (low, high, label) in targets.items():
        if name not in edit_features or name not in profile:
            continue
        messages.append(
            f"The current {label} is {profile[name]:.2f}; revise it into the {low:.2f}-{high:.2f} range without adding content."
        )
    if "average_word_length" in protect_features and "average_word_length" in profile:
        limit = min(profile["average_word_length"], 5.40)
        messages.append(
            f"Keep average word length at or below {limit:.2f} letters."
        )
    if "dash_per_sentence" in protect_features:
        messages.append("Keep the em-dash and en-dash count at zero.")
    if "modal_ratio" in edit_features:
        messages.append("Add no modal verbs; express necessity with a direct verb phrase.")
    return messages


def feature_retry_guidance(profile, edit_features, protect_features,
                           previous_profile=None):
    """Describe measurable misses in the last LLM output for the next attempt."""
    profile = profile or {}
    previous_profile = previous_profile or {}
    edit_features = set(edit_features or [])
    protect_features = set(protect_features or [])
    messages = []

    checks = {
        "function_word_ratio": (0.34, 0.40, "function-word share"),
        "type_token_ratio": (0.65, 0.75, "unique-word share"),
        "hapax_ratio": (0.50, 0.65, "one-off-word share"),
        "repeated_bigram_ratio": (0.02, 0.06, "repeated-bigram share"),
    }
    for name, (low, high, label) in checks.items():
        if name not in edit_features or name not in profile:
            continue
        value = profile[name]
        if value < low or value > high:
            direction = "below" if value < low else "above"
            messages.append(
                f"The previous output's {label} was {value:.2f}, still {direction} "
                f"the required {low:.2f}-{high:.2f} range. Correct this specific miss."
            )

    if "average_word_length" in protect_features and "average_word_length" in profile:
        maximum = min(previous_profile.get("average_word_length", 5.40), 5.40)
        if profile["average_word_length"] > maximum:
            messages.append(
                f"The previous output raised average word length to "
                f"{profile['average_word_length']:.2f}; keep it at or below {maximum:.2f}."
            )
    if "dash_per_sentence" in protect_features and profile.get("dash_per_sentence", 0) > 0:
        messages.append("The previous output introduced dash punctuation. Use zero dashes.")
    return messages


def protected_tokens(text):
    found = []
    for pattern in _PROTECTED_PATTERNS:
        found.extend(match.group(0) for match in pattern.finditer(text or ""))
    return list(dict.fromkeys(found))


def validate_targeted_output(before, after, maximum_word_deviation=0.25):
    if not after or not after.strip():
        return False, "empty_output"
    before_words = len((before or "").split())
    after_words = len(after.split())
    if before_words and abs(after_words - before_words) / before_words > maximum_word_deviation:
        return False, "word_count_deviation"
    missing = [token for token in protected_tokens(before) if token not in after]
    if missing:
        return False, "protected_token_missing"
    return True, None
