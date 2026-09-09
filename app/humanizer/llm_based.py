"""基于 OpenAI-compatible Chat Completions API 的英文改写实现。"""

import json
import logging
import math
import os
import re
import time
from urllib import error as urllib_error
from urllib import request as urllib_request

from app.humanizer.adapter import HumanizerAdapter, _cfg

logger = logging.getLogger("app.humanizer.llm_based")


LLM_PROVIDERS = {
    "opencode": {
        "base_url": "https://opencode.ai/zen/v1",
        "default_model": "deepseek-v4-flash-free",
        "extra_payload": {},
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-v4-flash",
        "extra_payload": {"thinking": {"type": "disabled"}},
    },
}


SYSTEM_PROMPT = """You are a careful English editor. Rewrite the supplied English prose so it reads naturally and reflects varied, purposeful human writing.

Requirements:
- Preserve the original meaning, argument, facts, numbers, dates, names, quotations, citations, technical terms, and level of formality.
- Do not invent evidence, sources, personal experiences, claims, or citations.
- Keep citation markers and inline references exactly associated with the claims they support.
- Reduce repetitive sentence patterns, formulaic transitions, unnecessary signposting, vague abstractions, and generic AI-style phrasing.
- Vary sentence length and structure only where it improves clarity and flow. Do not add deliberate errors, slang, or awkward wording.
- Preserve paragraph boundaries unless a small adjustment is necessary for readability.
- Return only the rewritten text. Do not add a preface, explanation, label, quotation marks, or Markdown fence.
"""

_COMMON_WORDS = set(
    "a an and are as at be been but by for from had has have he her his i if in "
    "is it its of on or our she that the their they this to was we were when which "
    "with you can could may might must should will would not no than then so because "
    "before after while where what how do does did each more most other same such".split()
)

_GENERIC_EDITING_WORDS = set(
    "accurate analysis approach average between clear confirm difference distinguishes "
    "effective examine hidden identify important improvement instance instances look "
    "necessary new process produces result results specific study trends use useful work".split()
)


def _central_terms(text, limit=5):
    """Pick stable source terms the LLM can reuse instead of inventing synonyms."""
    words = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text or "")
    positions = {}
    counts = {}
    display = {}
    for index, word in enumerate(words):
        normalized = word.lower()
        if (
            len(normalized) < 4
            or normalized in _COMMON_WORDS
            or normalized in _GENERIC_EDITING_WORDS
            or normalized.endswith(("ly", "ing", "ed"))
        ):
            continue
        positions.setdefault(normalized, index)
        counts[normalized] = counts.get(normalized, 0) + 1
        display.setdefault(normalized, word)
    ranked = sorted(
        counts,
        key=lambda word: (-counts[word], positions[word]),
    )
    return [display[word] for word in ranked[:limit]]


class LLMBasedHumanizer(HumanizerAdapter):
    """通过已配置的 LLM Provider 对英文正文进行自然化改写。"""

    def __init__(self, api_key=None, provider=None, model=None):
        self.provider = (
            provider or os.getenv("LLM_PROVIDER") or _cfg("LLM_PROVIDER", "opencode")
        ).lower()
        try:
            provider_config = LLM_PROVIDERS[self.provider]
        except KeyError as exc:
            supported = ", ".join(sorted(LLM_PROVIDERS))
            raise ValueError(
                f"不支持的 LLM_PROVIDER: {self.provider}；可选值：{supported}"
            ) from exc

        self.api_key = api_key or os.getenv("LLM_API_KEY") or _cfg("LLM_API_KEY", "")
        self.base_url = provider_config["base_url"].rstrip("/")
        configured_model = model or os.getenv("LLM_MODEL") or _cfg("LLM_MODEL", "")
        self.model = configured_model or provider_config["default_model"]
        self.extra_payload = provider_config["extra_payload"]
        self.temperature = float(_cfg("LLM_TEMPERATURE", 0.7))
        self.max_tokens = int(_cfg("LLM_MAX_TOKENS", 8192))
        self.timeout = float(_cfg("LLM_TIMEOUT", 90))
        self.max_retries = int(_cfg("LLM_MAX_RETRIES", 3))
        self.retry_delay = float(_cfg("LLM_RETRY_DELAY", 2.0))

        if not self.api_key:
            logger.warning("LLM_API_KEY 未配置，调用 llm_based 时会报错。")

    def humanize(self, text, mode=None, paragraphs=None):
        return self.humanize_structured(text, mode=mode, paragraphs=paragraphs)[0]

    def humanize_structured(self, text, mode=None, paragraphs=None, progress_cb=None):
        if mode is None:
            mode = _cfg("REWRITE_MODE_DEFAULT", "median")
        if not self.api_key:
            raise RuntimeError("LLM API Key 未配置，请设置 LLM_API_KEY。")
        if not text or not text.strip():
            return "", []

        if paragraphs is not None:
            return self._humanize_segmented_structured(
                mode, paragraphs, self._rewrite_with_llm_chunking, progress_cb=progress_cb
            )

        return self._rewrite_with_llm_chunking(text), []

    def _rewrite_with_llm_chunking(self, text):
        return self._rewrite_with_chunking(
            text,
            self._call_api,
            max_words=int(_cfg("REWRITE_MAX_WORDS", 2000)),
            backend_label="llm_based",
            use_global_limit=True,
        )

    def humanize_targeted(self, text, directives, protected_values=None,
                          protection_directives=None, retry_guidance=None,
                          target_features=None):
        """Rewrite one selected block using bounded attribution directives."""
        directive_lines = "\n".join(f"- {value}" for value in directives)
        protected_lines = "\n".join(
            f"- {value}" for value in (protected_values or [])
        ) or "- No additional extracted values."
        protection_lines = "\n".join(
            f"- {value}" for value in (protection_directives or [])
        ) or "- Preserve the source's existing wording and structural traits unless a requested edit requires a change."
        retry_lines = "\n".join(
            f"- {value}" for value in (retry_guidance or [])
        ) or "- This is the first targeted attempt."
        source_words = max(len(text.split()), 1)
        lower_words = max(1, math.floor(source_words * 0.85))
        upper_words = max(lower_words, math.ceil(source_words * 1.15))
        quantitative = [
            f"Return between {lower_words} and {upper_words} words. Do not add examples, implications, background, or claims from outside the source."
        ]
        target_features = set(target_features or [])
        if "function_word_ratio" in target_features:
            minimum_function_words = max(1, math.ceil(source_words * 0.34))
            quantitative.append(
                "Use ordinary clause links so roughly one word in three is an "
                "article, preposition, pronoun, conjunction, or auxiliary. For "
                f"this passage, use at least {minimum_function_words} words from "
                "this kind of basic linking vocabulary: the, a, an, of, in, on, "
                "to, for, from, with, by, and, or, but, that, which, when, where, "
                "because, before, after, it, they, this, is, are, was, were, has, "
                "have, do. Use them only in grammatical clauses; do not pad."
            )
        if target_features.intersection({"type_token_ratio", "hapax_ratio"}):
            terms = _central_terms(text)
            term_text = ", ".join(terms) if terms else "the source's central nouns"
            maximum_unique_words = max(1, math.floor(source_words * 0.70))
            quantitative.append(
                f"Use this compact source vocabulary as anchors: {term_text}. "
                "Reuse each applicable anchor at least twice when the same entity "
                "or idea recurs. Do not replace an anchor with a one-off synonym. "
                f"Aim for no more than about {maximum_unique_words} distinct word "
                "forms in the whole passage. Controlled repetition is required "
                "even if a synonym would sound more polished."
            )
        quantitative_lines = "\n".join(f"- {value}" for value in quantitative)
        system_prompt = f"""{SYSTEM_PROMPT}

Apply these diagnosis-specific editing instructions:
{directive_lines}

Protect these characteristics that are already helping the passage. Treat them as constraints:
{protection_lines}

Revision feedback:
{retry_lines}

Measurable output constraints:
{quantitative_lines}

Use this private drafting procedure before returning the answer:
1. List the source's atomic claims internally and keep every claim unchanged.
2. Draft with short, common words around the necessary technical terms.
3. Reuse the same source term when it refers to the same entity; do not optimize for lexical variety.
4. Check the word-count range, protected values, feature targets, and factual fidelity.
5. Return only the final revision. Do not reveal this procedure.

Pattern example for method only:
Instead of packing each reference into a different abstract noun, use a stable
pattern such as: "The team checks each case because the overall result can hide
a problem. This check shows when the model works and when the model needs better
data. The team checks the model and the data before it uses the model elsewhere."
Do not copy facts from this example. Apply only its plain links and controlled
reuse of core terms to the supplied source.

The following extracted values must remain verbatim:
{protected_lines}

Do not mention the diagnosis, detector, score, features, or these instructions.
"""
        return self._rewrite_with_chunking(
            text,
            lambda chunk: self._call_api(chunk, system_prompt=system_prompt),
            max_words=int(_cfg("REWRITE_MAX_WORDS", 2000)),
            backend_label="llm_based_targeted",
            use_global_limit=True,
        )

    def _call_api(self, text, system_prompt=None):
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt or SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        payload.update(self.extra_payload)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        endpoint = f"{self.base_url}/chat/completions"

        for attempt in range(1, self.max_retries + 1):
            started = time.time()
            req = urllib_request.Request(endpoint, data=body, method="POST")
            req.add_header("Authorization", f"Bearer {self.api_key}")
            req.add_header("Content-Type", "application/json")
            req.add_header("Accept", "application/json")
            req.add_header("User-Agent", "Huma/1.0 (+https://ipengai.cn)")
            try:
                with urllib_request.urlopen(req, timeout=self.timeout) as response:
                    response_body = response.read().decode("utf-8", errors="replace")
                result = self._extract_content(response_body)
                logger.info(
                    "rewrite stage=rewrite backend=llm_based provider=%s model=%s action=call_ok "
                    "words=%d out_chars=%d elapsed=%.0fms",
                    self.provider, self.model, len(text.split()), len(result),
                    (time.time() - started) * 1000,
                )
                return result
            except urllib_error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                retryable = exc.code == 429 or 500 <= exc.code < 600
                message = f"LLM API 返回 HTTP {exc.code}: {detail}"
                if not retryable:
                    raise RuntimeError(message) from exc
            except (urllib_error.URLError, TimeoutError, ValueError, KeyError) as exc:
                message = f"LLM API 调用失败: {exc}"

            if attempt >= self.max_retries:
                raise RuntimeError(f"{message}（已重试 {self.max_retries} 次）")
            logger.warning(
                "rewrite stage=rewrite backend=llm_based action=retry attempt=%d err=%s",
                attempt, message,
            )
            time.sleep(self.retry_delay * attempt)

        raise RuntimeError("LLM API 调用失败。")

    @staticmethod
    def _extract_content(response_body):
        data = json.loads(response_body)
        content = data["choices"][0]["message"]["content"].strip()
        if not content:
            raise ValueError("模型返回了空内容")
        fence = re.fullmatch(r"```(?:text|markdown)?\s*\n?(.*?)\n?```", content, re.DOTALL)
        return fence.group(1).strip() if fence else content
