"""Structured progress events shared by rewrite producers and callers.

改写适配器通过 progress_cb 上报进度时，除了给人看的 message 文案外，
还可以携带一个结构化 event 标识。消费方（改写埋点、进度下发）应优先
读取 event，避免依赖中文文案做控制流判断——文案随时可能调整，而依赖
文案的判断会静默失效。

新增事件时在此处定义常量，并在生产端传 event=<常量>、消费端按常量判断。
"""

# 某个改写块（block 非 None）或整篇（block 为 None）改由备用服务处理。
REWRITE_FALLBACK_EVENT = 'rewrite_fallback'

# 兼容兜底：旧适配器只发文案、不带 event 时仍按此标记识别降级。
# 新代码一律使用 event 参数，不要依赖此标记。
FALLBACK_MESSAGE_MARK = '备用改写服务'


def is_fallback_event(event, message=None):
    """判断一条 progress 上报是否为降级事件。

    Args:
        event: progress_cb 携带的结构化事件标识，可为 None。
        message: progress_cb 携带的文案，可为 None。

    Returns:
        bool: 命中结构化 event，或命中历史文案兜底标记时为 True。
    """
    if event == REWRITE_FALLBACK_EVENT:
        return True
    return bool(message) and FALLBACK_MESSAGE_MARK in message
