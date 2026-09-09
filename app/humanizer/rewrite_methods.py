"""实验性改写方法（2026-09-03 对比实验落地，只新增、不改原有 humanizer 代码）。

三个方法均实现 HumanizerAdapter.humanize 接口：
  LynoteTranslationHumanizer  低成本翻译链：EN->ZH(NMT)->EN(NMT)
                              百度为主、Google 兜底（translation_nmt.py）
  BladerPatternsHumanizer     blader/humanizer 35 patterns 规则驱动 LLM 重写
  HarshaneelHumanizer         harshaneel/humanize 9 杠杆 prompt LLM 重写

LynoteTranslationHumanizer 已注册到 create_humanizer 工厂，供风险路由的低成本档使用；
另外两种 prompt 实验实现暂未接入线上路由。
"""

import logging
import re
import time

import requests

from app.humanizer.adapter import HumanizerAdapter, _cfg
from app.humanizer.translation_nmt import translate as nmt_translate

logger = logging.getLogger("app.humanizer.rewrite_methods")

LLM_PROVIDERS = {
    "opencode": {
        "base_url": "https://opencode.ai/zen/v1",
        "default_model": "deepseek-v4-flash-free",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-chat",
    },
}


def _llm_chat(system, user, max_tokens=4000, retries=3, temperature=1.3):
    """复用 config.py 的 LLM_PROVIDER / LLM_API_KEY / LLM_MODEL 配置。"""
    provider = _cfg("LLM_PROVIDER", "opencode")
    conf = LLM_PROVIDERS.get(provider)
    if conf is None:
        raise ValueError(f"不支持的 LLM_PROVIDER: {provider}")
    api_key = _cfg("LLM_API_KEY", "")
    model = _cfg("LLM_MODEL", "") or conf["default_model"]
    payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}]}
    if provider == "deepseek":
        payload["thinking"] = {"type": "disabled"}
    last = None
    for attempt in range(retries):
        try:
            r = requests.post(
                f"{conf['base_url'].rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json=payload, timeout=180)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 * attempt + 1)
    raise RuntimeError(f"LLM 调用失败: {last}")


def _chunk_paras(text, max_words=1500):
    paras = text.split("\n\n")
    groups, cur, cnt = [], [], 0
    for p in paras:
        w = len(p.split())
        if cnt + w > max_words and cur:
            groups.append("\n\n".join(cur))
            cur, cnt = [], 0
        cur.append(p)
        cnt += w
    if cur:
        groups.append("\n\n".join(cur))
    return groups or [text]


def _llm_rewrite(text, system, max_words_single=2200):
    if len(text.split()) <= max_words_single:
        return _llm_chat(system, text)
    return "\n\n".join(_llm_chat(system, p) for p in _chunk_paras(text))


def _structured_from_text(out_text):
    """把整篇结果按空行拆段，生成简版结构化段落（实验方法用，标题保护不做）。"""
    return [
        {"text": p.strip(), "is_heading": False, "heading_level": None, "style": None}
        for p in out_text.split("\n\n") if p.strip()
    ]


class _RewriteMethodMixin:
    """统一 humanize/humanize_structured 骨架：子类实现 _rewrite(text)。"""

    def humanize(self, text, mode="low", paragraphs=None):
        return self._rewrite(text)

    def humanize_structured(self, text, mode="low", paragraphs=None):
        out = self._rewrite(text)
        return out, _structured_from_text(out)


# --------------------------------------------------------------------------
# 1. lynote 翻译链
# --------------------------------------------------------------------------
class LynoteTranslationHumanizer(_RewriteMethodMixin, HumanizerAdapter):
    """EN -> 中文 -> EN，两个方向均只使用 NMT，不经过 LLM。"""

    _BATCH_CHARS = 1400

    @staticmethod
    def _paragraphs(text):
        normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        return [
            value.strip()
            for value in re.split(r"\n[ \t]*\n+", normalized)
            if value.strip()
        ]

    @classmethod
    def _paragraph_batches(cls, paragraphs):
        """Keep separator tokens away from the NMT client's hard chunk boundary."""
        batches = []
        current = []
        current_chars = 0
        for paragraph in paragraphs:
            added = len(paragraph) + (32 if current else 0)
            if current and current_chars + added > cls._BATCH_CHARS:
                batches.append(current)
                current = []
                current_chars = 0
            current.append(paragraph)
            current_chars += added
        if current:
            batches.append(current)
        return batches

    @staticmethod
    def _separator(index):
        return f"[[ZXQ_{index:06d}_QXZ]]"

    def _translate_batch(self, paragraphs, offset):
        separators = [
            self._separator(offset + index)
            for index in range(len(paragraphs) - 1)
        ]
        tagged = paragraphs[0]
        for separator, paragraph in zip(separators, paragraphs[1:]):
            tagged += f"\n{separator}\n{paragraph}"
        zh, engine1 = nmt_translate(tagged, "en", "zh")
        en, engine2 = nmt_translate(zh, "zh", "en")
        values = [en]
        for separator in separators:
            next_values = []
            for value in values:
                next_values.extend(value.split(separator))
            values = next_values
        values = [value.strip() for value in values]
        if len(values) != len(paragraphs) or any(not value for value in values):
            raise RuntimeError(
                "NMT paragraph markers were not preserved; upgrade this block to Huma"
            )
        return values, engine1, engine2

    def _rewrite(self, text):
        paragraphs = self._paragraphs(text)
        if len(paragraphs) <= 1:
            zh, engine1 = nmt_translate(text, "en", "zh")
            en, engine2 = nmt_translate(zh, "zh", "en")
            logger.info(
                "lynote短链完成 paragraphs=1 NMT引擎: EN->ZH=%s, ZH->EN=%s",
                engine1, engine2,
            )
            return en

        translated = []
        engines = []
        offset = 0
        for batch in self._paragraph_batches(paragraphs):
            values, engine1, engine2 = self._translate_batch(batch, offset)
            translated.extend(values)
            engines.append((engine1, engine2))
            offset += len(batch)
        logger.info(
            "lynote短链完成 paragraphs=%d batches=%d NMT引擎=%s",
            len(paragraphs), len(engines),
            ",".join(sorted({f"{left}->{right}" for left, right in engines})),
        )
        return "\n\n".join(translated)


# --------------------------------------------------------------------------
# 2. blader/humanizer（35 patterns，源自 Wikipedia "Signs of AI writing"）
# --------------------------------------------------------------------------
BLADER_PATTERNS = """
- Remove AI-favorite words: delve, tapestry, vibrant, crucial, pivotal, meticulously,
  intricate, nuanced, comprehensively, foster, leverage, robust, showcase, underscore,
  boast, realm, landscape, elevate, unlock, harness, embark, journey, testament,
  "plays a vital/crucial role", "it's important to note", "it's worth noting".
- Break "rule of three" constructions (every list of exactly three parallel items).
- Remove inflated, subjective claims of significance ("groundbreaking", "revolutionary").
- Replace vague attributions ("experts say", "studies show") with concrete sources or deletion.
- Remove excessive hedging ("can", "may", "might", "could potentially") where direct statements fit.
- Kill formulaic transitions: "Moreover", "Furthermore", "Additionally", "In conclusion",
  "Overall", "Ultimately" at paragraph starts.
- Avoid "not only ... but also", "whether ... or ..." symmetry overuse.
- Reduce em-dash overuse; use commas or split sentences.
- Avoid bolding random phrases mid-sentence; no decorative Markdown emphasis.
- Do not end sections with a generic "In summary, X is a powerful tool for Y" paragraph.
- Vary paragraph length; some paragraphs can be one sentence.
- Prefer active voice and concrete verbs over nominalizations.
- Keep all facts, numbers, names, dates, citations exactly as in the source.
"""


class BladerPatternsHumanizer(_RewriteMethodMixin, HumanizerAdapter):
    """把 35 patterns 作为规则清单注入 prompt，让 LLM 主动改结构与用词。"""

    def __init__(self, extra_rules=""):
        self.extra_rules = extra_rules

    def _rewrite(self, text):
        system = (
            "You are a meticulous human editor. Rewrite the text to remove the "
            "following recognizable AI-writing patterns, while restructuring "
            "sentences freely as long as meaning is preserved:\n"
            f"{BLADER_PATTERNS}\n"
            + (self.extra_rules + "\n" if self.extra_rules else "")
            + "Requirements: preserve meaning, facts, numbers, names, citations and "
              "level of formality; do not invent content; return ONLY the rewritten text."
        )
        return _llm_rewrite(text, system)

# --------------------------------------------------------------------------
# 3. harshaneel/humanize（9 杠杆，主打 perplexity/burstiness 类检测器）
# --------------------------------------------------------------------------
HARSHANEEL_LEVERS = """
1. Sentence-length variance (burstiness): mix 4-word sentences with 30-word ones.
2. Lexical diversity: replace the most predictable word in each sentence with a
   precise, less-probable synonym (no thesaurus-flavored oddities).
3. Concrete specificity: swap generic nouns for concrete ones already implied by context.
4. Rhythm breaks: start some sentences with conjunctions, short fragments, or questions.
5. Kill template transitions; connect ideas through content, not connective phrases.
6. Perplexity spikes: allow one genuinely surprising-but-accurate phrasing per paragraph.
7. Human imperfection: slight informality where register allows (contractions, asides).
8. Structural reordering: reorder clauses within sentences; keep paragraph order.
9. No moralizing conclusions; end on a specific fact or pointed observation.

Constraints: never change facts, numbers, names, dates, citations; keep meaning intact;
return ONLY the rewritten text.
"""


class HarshaneelHumanizer(_RewriteMethodMixin, HumanizerAdapter):
    """9 杠杆 prompt 重写；对 perplexity/burstiness 型检测器有效。"""

    def _rewrite(self, text):
        system = (
            "You are a human writing coach applying these nine levers to make the "
            "text read as authentic human writing:\n"
            f"{HARSHANEEL_LEVERS}"
        )
        return _llm_rewrite(text, system)
