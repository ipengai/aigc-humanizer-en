"""NMT 翻译客户端：百度翻译为主，阿里云兜底，Google 最后兜底。

配置统一从项目 config.py 读取（_cfg 安全读取，缺字段用默认值）：
  BAIDU_TRANSLATE_APPID / BAIDU_TRANSLATE_KEY  百度翻译开放平台凭证
  ALIYUN_ACCESS_KEY_ID / ALIYUN_ACCESS_KEY_SECRET  阿里云 RAM 凭证
  ALIYUN_MT_REGION                            阿里云机器翻译地域（默认 cn-hangzhou）
  GOOGLE_TRANSLATE_KEY                         Google 付费 key（留空则兜底走免费 client=gtx，
                                               仅限实验环境，生产禁用 gtx）
"""

import hashlib
import json
import logging
import random
import time

import requests

from app.humanizer.adapter import _cfg

logger = logging.getLogger("app.humanizer.translation_nmt")

BAIDU_API = "https://fanyi-api.baidu.com/api/trans/vip/translate"
GOOGLE_API = "https://translate.googleapis.com/translate_a/single"


def _baidu_appid():
    return _cfg("BAIDU_TRANSLATE_APPID", "")


def _baidu_key():
    return _cfg("BAIDU_TRANSLATE_KEY", "")


def _aliyun_key_id():
    return _cfg("ALIYUN_ACCESS_KEY_ID", "")


def _aliyun_key_secret():
    return _cfg("ALIYUN_ACCESS_KEY_SECRET", "")


def _aliyun_region():
    return _cfg("ALIYUN_MT_REGION", "cn-hangzhou")


def _google_key():
    return _cfg("GOOGLE_TRANSLATE_KEY", "")


def _chunk_chars(text, max_chars=1800):
    """按字符保守分段。默认 1800 适配百度；阿里云可传 4500。"""
    if len(text) <= max_chars:
        return [text]
    chunks, cur = [], ""
    for ch in text:
        if len(cur) + 1 > max_chars and cur:
            chunks.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        chunks.append(cur)
    return chunks


# 不同厂商语言代码差异映射：key=本项目/百度代码，value=阿里云代码
_ALIYUN_LANG_MAP = {
    "en": "en",
    "zh": "zh",
    "jp": "ja",
    "ja": "ja",
    "fin": "fi",
    "fi": "fi",
}


def baidu_translate(text, src, dst, retries=3):
    """百度翻译 API。src/dst 用百度语言代码（en/zh/jp/fin...）。"""
    appid, key = _baidu_appid(), _baidu_key()
    if not appid or not key:
        raise RuntimeError(
            "缺少百度翻译配置：请在 config.py 填 BAIDU_TRANSLATE_APPID（纯数字）"
            " / BAIDU_TRANSLATE_KEY"
        )
    out = []
    for chunk in _chunk_chars(text):
        last_err = None
        for attempt in range(retries):
            try:
                salt = random.randint(32768, 65536)
                sign = hashlib.md5(
                    (appid + chunk + str(salt) + key).encode("utf-8")
                ).hexdigest()
                r = requests.get(
                    BAIDU_API,
                    params={
                        "q": chunk,
                        "from": src,
                        "to": dst,
                        "appid": appid,
                        "salt": salt,
                        "sign": sign,
                    },
                    timeout=30,
                )
                r.raise_for_status()
                data = r.json()
                if "error_code" in data:
                    raise RuntimeError(
                        f"百度API {data['error_code']}: {data.get('error_msg')}"
                    )
                # Baidu returns one ``trans_result`` item per input line when
                # ``q`` contains newlines.  Keeping only the first item drops
                # paragraph markers and makes the DOCX structure guard retry
                # every paragraph separately.  Preserve every returned line
                # so Lynote's batch markers survive the round trip.
                translated_lines = [
                    item.get("dst", "")
                    for item in data.get("trans_result", [])
                ]
                if not translated_lines or any(
                    not value for value in translated_lines
                ):
                    raise RuntimeError("百度API返回了空翻译结果")
                out.append("\n".join(translated_lines))
                last_err = None
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                time.sleep(2 * attempt + 1)
        if last_err is not None:
            raise last_err
        time.sleep(0.5)  # 百度 QPS 限制（按量 10）
    return "".join(out)


def aliyun_translate(text, src, dst, retries=3):
    """阿里云机器翻译通用版 TranslateGeneral。src/dst 自动映射到阿里云代码。"""
    key_id = _aliyun_key_id()
    key_secret = _aliyun_key_secret()
    if not key_id or not key_secret:
        raise RuntimeError(
            "缺少阿里云翻译配置：请在 config.py 填 ALIYUN_ACCESS_KEY_ID / "
            "ALIYUN_ACCESS_KEY_SECRET"
        )

    try:
        from aliyunsdkcore.client import AcsClient
        from aliyunsdkcore.acs_exception.exceptions import ClientException, ServerException
        from aliyunsdkalimt.request.v20181012 import TranslateGeneralRequest
    except ImportError as exc:
        raise RuntimeError(
            "阿里云翻译 SDK 未安装，请运行：\n"
            "pip install aliyun-python-sdk-core aliyun-python-sdk-alimt"
        ) from exc

    src = _ALIYUN_LANG_MAP.get(src, src)
    dst = _ALIYUN_LANG_MAP.get(dst, dst)
    region = _aliyun_region()
    client = AcsClient(key_id, key_secret, region)

    out = []
    for chunk in _chunk_chars(text, max_chars=4500):
        last_err = None
        for attempt in range(retries):
            try:
                request = TranslateGeneralRequest.TranslateGeneralRequest()
                request.set_SourceLanguage(src)
                request.set_TargetLanguage(dst)
                request.set_SourceText(chunk)
                request.set_FormatType("text")
                request.set_method("POST")
                response = client.do_action_with_exception(request)
                data = json.loads(response)
                translated = data.get("Data", {}).get("Translated", "")
                if not translated:
                    raise RuntimeError(f"阿里云返回空翻译: {data}")
                out.append(translated)
                last_err = None
                break
            except (ClientException, ServerException) as exc:
                last_err = exc
                logger.warning("阿里云翻译异常: %s", exc)
                time.sleep(2 * attempt + 1)
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                time.sleep(2 * attempt + 1)
        if last_err is not None:
            raise last_err
    return "".join(out)


def google_translate(text, src, dst, retries=3):
    """Google 翻译最后兜底。有付费 key 走 key 参数，否则免费 client=gtx（仅实验用）。"""
    key = _google_key()
    out = []
    for chunk in _chunk_chars(text):
        last_err = None
        for attempt in range(retries):
            try:
                params = {"q": chunk, "sl": src, "tl": dst}
                if key:
                    params["key"] = key
                else:
                    params["client"] = "gtx"
                    params["dt"] = "t"
                r = requests.get(GOOGLE_API, params=params, timeout=30)
                r.raise_for_status()
                if key:
                    data = r.json()
                    out.append("".join(t["translatedText"] for t in data["translations"]))
                else:
                    data = r.json()
                    out.append("".join(seg[0] for seg in data[0] if seg and seg[0]))
                last_err = None
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                # 免费 gtx 有 IP 级 429 限流，退避更久
                time.sleep((2 * attempt + 1) * (1 if key else 5))
        if last_err is not None:
            raise last_err
    return "".join(out)


def translate(text, src, dst):
    """统一入口：百度为主，阿里云兜底，Google 最后兜底。返回 (译文, 实际使用的引擎)。"""
    try:
        return baidu_translate(text, src, dst), "baidu"
    except Exception as exc:  # noqa: BLE001
        logger.warning("百度翻译失败，切换阿里云兜底: %s", exc)
        try:
            return aliyun_translate(text, src, dst), "aliyun"
        except Exception as exc2:  # noqa: BLE001
            logger.warning("阿里云翻译失败，切换 Google 兜底: %s", exc2)
            return google_translate(text, src, dst), "google"


def translate_many(texts, src, dst):
    """Translate multiple paragraphs while preserving their cardinality.

    Baidu already returns one ``trans_result`` entry per input line.  Joining
    paragraphs with newlines therefore preserves boundaries more reliably
    than asking the translation model to echo artificial marker tokens, which
    it may alter according to the surrounding prose.
    """
    values = list(texts or [])
    if not values:
        return [], "baidu"
    if any("\n" in value or "\r" in value for value in values):
        # A Word paragraph normally has no hard newline.  Handle unusual input
        # independently rather than confusing input lines with paragraph rows.
        translated = []
        engines = []
        for value in values:
            output, engine = translate(value, src, dst)
            translated.append(output)
            engines.append(engine)
        return translated, "+".join(sorted(set(engines)))

    try:
        output = baidu_translate("\n".join(values), src, dst)
        translated = output.splitlines()
        if len(translated) != len(values) or any(not item.strip() for item in translated):
            raise RuntimeError(
                "百度批量翻译段落数不一致: "
                f"expected={len(values)} actual={len(translated)}"
            )
        return translated, "baidu"
    except Exception as exc:  # noqa: BLE001
        logger.warning("百度批量翻译失败，逐段使用统一兜底链: %s", exc)
        translated = []
        engines = []
        for value in values:
            output, engine = translate(value, src, dst)
            translated.append(output)
            engines.append(engine)
        return translated, "+".join(sorted(set(engines)))
