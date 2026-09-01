"""主备改写适配器。"""

import logging

from app.humanizer.adapter import HumanizerAdapter, _cfg
from app.humanizer.events import REWRITE_FALLBACK_EVENT

logger = logging.getLogger("app.humanizer.failover")


class FailoverHumanizer(HumanizerAdapter):
    """主适配器异常时自动调用备用适配器。"""

    def __init__(self, primary, fallback):
        self.primary = primary
        self.fallback = fallback

    def humanize(self, text, mode=None, paragraphs=None):
        return self.humanize_structured(text, mode=mode, paragraphs=paragraphs)[0]

    def humanize_structured(self, text, mode=None, paragraphs=None, progress_cb=None):
        # 有结构化段落时只切分一次，并在每个块的调用边界独立兜底。
        # 第 N 块主服务失败时只由备用服务处理当前块；第 N+1 块仍优先
        # 尝试主服务，且前 N-1 块的结果不会丢失或重跑。
        if paragraphs is not None:
            return self._humanize_with_block_failover(
                text, mode=mode, paragraphs=paragraphs,
                progress_cb=progress_cb,
            )

        # 无段落结构时无法安全定位失败块，保留整篇主备切换行为。
        try:
            return self.primary.humanize_structured(
                text, mode=mode, paragraphs=paragraphs, progress_cb=progress_cb
            )
        except Exception:
            logger.exception(
                "Primary humanizer %s failed; switching to %s",
                type(self.primary).__name__, type(self.fallback).__name__,
            )
            if progress_cb:
                progress_cb(stage="rewrite",
                            message="正在切换备用改写服务",
                            event=REWRITE_FALLBACK_EVENT)
            try:
                return self.fallback.humanize_structured(
                    text, mode=mode, paragraphs=paragraphs, progress_cb=progress_cb
                )
            except Exception as fallback_error:
                raise RuntimeError("主改写服务和备用改写服务均不可用") from fallback_error

    def _humanize_with_block_failover(self, text, mode, paragraphs,
                                       progress_cb=None):
        return self._humanize_segmented_structured(
            mode,
            paragraphs,
            lambda block_text: self.primary.humanize(
                block_text, mode=mode, paragraphs=None
            ),
            progress_cb=progress_cb,
            fallback_rewriter=lambda block_text: self.fallback.humanize(
                block_text, mode=mode, paragraphs=None
            ),
            primary_label=type(self.primary).__name__,
            fallback_label=type(self.fallback).__name__,
            batch_short_blocks=(
                bool(getattr(
                    self.primary, "supports_short_block_batching", False
                )) and bool(_cfg("REWRITE_BATCH_SHORT_BLOCKS", True))
            ),
        )
