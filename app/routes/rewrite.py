"""
Rewrite routes — execute text humanization and save order record.
"""

import hashlib
import logging
from flask import Blueprint, request, jsonify, session
from app.extensions import limiter
from app.helpers import generate_order_id, get_db, login_required, rewrite_and_analyze
from config import PRICE_PER_1000_WORDS

rewrite_bp = Blueprint('rewrite', __name__)


def _preview_rate_limit_key():
    """Rate-limit paid upstream preview work per account, not shared proxy IP."""
    user_id = session.get('user_id')
    if user_id:
        return f"rewrite-preview:user:{user_id}"
    return f"rewrite-preview:ip:{request.remote_addr or 'unknown'}"


@rewrite_bp.route('/api/rewrite', methods=['POST'])
@limiter.limit("30 per minute")
@login_required
def api_rewrite():
    """
    Rewrite text to reduce AI detection score.
    Executes the humanization immediately and saves the order record.
    Requires login.

    Payment:
    1. 检查用户词数余额（word_balance），足够则扣余额改写
    2. 余额不足 → 返回 402 要求支付
    """
    from app.models import Order, User, BalanceTransaction

    data = request.get_json(silent=True) or {}
    text = data.get('text') or session.get('last_text', '')
    # mode 语义为"改写粒度"：low/median/high（默认 median，聚合段数可配）
    mode = data.get('mode')
    if mode not in ('low', 'median', 'high'):
        mode = 'median'

    if not text:
        return jsonify({"error": "没有可改写的文本，请先分析"}), 400

    word_count = len(text.split())
    user_id = session.get('user_id')
    order_id = generate_order_id()
    payment_status = None
    balance_deducted = 0

    # ── 检查词数余额，足够则扣余额改写 ──
    conn = get_db()
    balance = User.get_balance(conn, user_id)
    if balance < word_count:
        shortfall = word_count - balance
        return jsonify({
            "error": f"余额不足（当前 {balance} 词，需 {word_count} 词），还差 {shortfall} 词",
            "balance": balance,
            "word_count": word_count,
            "shortfall": shortfall,
            "need_payment": True
        }), 402

    # 余额扣减与消费流水必须在同一事务中提交。
    try:
        balance_remaining = User.deduct_balance(conn, user_id, word_count)
        if balance_remaining is not None:
            BalanceTransaction.create(
                conn, user_id, 'rewrite_consumption', -word_count,
                balance_remaining, order_id=order_id, description='改写任务扣费'
            )
            conn.commit()
    except Exception:
        conn.rollback()
        logging.exception(f"[BALANCE] Failed to charge user {user_id}")
        return jsonify({"error": "余额扣费失败，请稍后重试"}), 500

    if balance_remaining is None:
        conn.rollback()
        balance = User.get_balance(conn, user_id)
        shortfall = word_count - balance
        return jsonify({
            "error": f"余额不足（当前 {balance} 词，需 {word_count} 词），还差 {shortfall} 词",
            "balance": balance,
            "word_count": word_count,
            "shortfall": shortfall,
            "need_payment": True
        }), 402
    balance_deducted = word_count
    payment_status = 'balance'
    logging.info(f"[BALANCE] User {user_id} used balance: deducted {word_count} words, remaining: {balance_remaining}")

    price = round(PRICE_PER_1000_WORDS * (word_count / 1000), 2)

    # ── 异步改写：建 processing 订单 → 后台线程改写 → 立即返回 order_id ──
    try:
        paragraphs = session.get('last_paragraphs')
        original_format = session.get('last_original_format', 'txt')
        original_filename = session.get('last_original_filename', None)
        source_file_key = session.get('last_source_file_key')
        analysis_context = dict(session.get('order_attribution') or {})
        analysis_context['input_type'] = session.get('last_input_type') or (
            'upload' if original_filename else 'paste'
        )

        # 建 processing 订单（改写结果由后台线程写回）
        conn = get_db()
        Order.create_processing_order(
            conn,
            user_id=user_id,
            order_id=order_id,
            original_text=text,
            original_format=original_format,
            original_filename=original_filename,
            word_count=word_count,
            price=price,
            mode=mode,
            paragraphs=paragraphs,
            source_file_key=source_file_key,
            analysis_context=analysis_context,
        )

        # 提交后台改写线程（复用支付后改写的 do_background_rewrite，含进度写入）
        from app.helpers.tasks import submit_rewrite_task
        submit_rewrite_task(order_id, text, mode, paragraphs)

        return jsonify({
            "success": True,
            "order_id": order_id,
            "status": "processing",
            "payment_status": payment_status,
            "balance_remaining": User.get_balance(get_db(), user_id)
        })

    except Exception:
        # 只有建单失败才回滚扣费；提交失败由 processing 订单自动重投。
        if balance_deducted:
            try:
                conn = get_db()
                User.add_balance(conn, user_id, balance_deducted)
                balance_after = User.get_balance(conn, user_id)
                BalanceTransaction.create(
                    conn, user_id, 'rewrite_refund', balance_deducted,
                    balance_after, order_id=order_id, description='改写任务启动失败退回词数'
                )
                conn.commit()
                logging.warning(f"[BALANCE] Refunded {balance_deducted} words to user {user_id} after rewrite start failure")
            except Exception:
                conn.rollback()
                logging.exception(f"[BALANCE] Failed to refund {balance_deducted} words to user {user_id}")
        logging.exception("Rewrite start failed")
        return jsonify({"error": "改写出错，请稍后重试"}), 500


@rewrite_bp.route('/api/rewrite-progress', methods=['GET'])
@login_required
def api_rewrite_progress():
    """查询某订单的改写/检测真实进度（供前端轮询步骤条）。

    stage='done' 时顺带返回完整改写结果（result 字段），供前端直接展示，
    无需再查 /api/payment-status（该接口只负责支付状态）。
    """
    from app.helpers.tasks import get_rewrite_progress
    from app.models import Order, User
    order_id = request.args.get('order_id', '')
    if not order_id:
        return jsonify({"error": "缺少 order_id"}), 400

    # Validate ownership before exposing even the task stage. Otherwise an
    # authenticated user could enumerate order IDs and observe other users'
    # rewrite activity, then receive the full text once the task completes.
    conn = get_db()
    order = Order.get_by_order_id(conn, order_id)
    if not order:
        return jsonify({"error": "订单不存在"}), 404
    if order['user_id'] != session.get('user_id'):
        return jsonify({"error": "无权访问该订单"}), 403

    progress = get_rewrite_progress(order_id)
    if not progress:
        if order.get('status') == 'completed':
            progress = {"stage": "done", "message": "改写完成"}
        elif order.get('status') == 'failed':
            progress = {"stage": "failed", "message": "改写失败"}
        else:
            progress = {"stage": "detect", "message": "正在处理改写任务"}

    # Do not mutate the process-wide progress cache when attaching user data.
    progress = dict(progress)

    if progress.get('stage') == 'done':
        # 改写完成：从 DB 读改写结果，附带 result 字段
        if order and order.get('status') == 'completed' and order.get('rewritten_text'):
            from app.helpers import derive_risk_level
            original_score = order.get('original_score', 0) or 0
            rewritten_score = order.get('rewritten_score', 0) or 0
            user_id = session.get('user_id')
            balance_after = User.get_balance(conn, user_id) if user_id else None
            progress['result'] = {
                "success": True,
                "order_id": order_id,
                "original": {
                    "text": order['original_text'],
                    "ai_score": round(original_score, 1),
                    "risk_level": derive_risk_level(original_score)
                },
                "rewritten": {
                    "text": order['rewritten_text'],
                    "ai_score": round(rewritten_score, 1),
                    "risk_level": derive_risk_level(rewritten_score)
                },
                "improvement": round(original_score - rewritten_score, 1),
                "original_format": order.get('original_format', 'txt'),
                "original_filename": order.get('original_filename'),
                "balance_after": balance_after
            }
        else:
            # 进度缓存可能先于订单结果可见；结果未就绪时继续轮询，避免前端收到空 done。
            progress = {"stage": "detect_again", "message": "正在保存改写结果"}
    response = jsonify(progress)
    response.headers['Cache-Control'] = 'no-store, private'
    return response


@rewrite_bp.route('/api/rewrite-preview', methods=['POST'])
@limiter.limit("10 per day", key_func=_preview_rate_limit_key)
@limiter.limit("3 per minute", key_func=_preview_rate_limit_key)
@login_required
def api_rewrite_preview():
    """免费预览：改写用户文档正文前 200 词，供付费前建立信任。

    不扣用户词数余额、不建订单；仅消耗极少第三方改写额度作为获客成本。
    取正文（跳过封面/目录噪声）前 200 词改写并返回前后对比。
    """
    from app.helpers.preview import (
        cache_preview,
        extract_body_preview,
        get_cached_preview,
    )
    from app.helpers.tasks import rewrite_and_analyze
    from app.helpers.analysis_helpers import derive_risk_level

    data = request.get_json(silent=True) or {}
    # Preview only the document that passed /api/analyze. Accepting arbitrary
    # request text would turn this endpoint into a free chunked rewrite API.
    text = (session.get('last_text') or '').strip()
    if not text:
        return jsonify({"error": "请先上传文档或粘贴英文文本"}), 400

    mode = data.get('mode')
    if mode not in ('low', 'median', 'high'):
        mode = 'median'

    user_id = session.get('user_id')
    text_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
    cache_key = f"{user_id}:{mode}:{text_hash}"
    cached = get_cached_preview(cache_key)
    if cached:
        cached['cached'] = True
        return jsonify(cached)

    preview_text = extract_body_preview(
        text, max_words=200, paragraphs=session.get('last_paragraphs')
    )
    if not preview_text:
        return jsonify({"error": "未能提取正文预览"}), 400

    try:
        result = rewrite_and_analyze(preview_text, mode=mode)
    except Exception:
        logging.exception("Preview rewrite failed")
        return jsonify({"error": "预览改写失败，请稍后重试"}), 500

    original_analysis = result.get('original_analysis') or {}
    rewritten_analysis = result.get('rewritten_analysis') or {}
    original_score = original_analysis.get('ai_score', 0) or 0
    rewritten_score = rewritten_analysis.get('ai_score', 0) or 0

    response_data = {
        "success": True,
        "is_preview": True,
        "cached": False,
        "mode": mode,
        "original": {
            "text": preview_text,
            "ai_score": round(original_score, 1),
            "risk_level": derive_risk_level(original_score),
        },
        "rewritten": {
            "text": result.get('humanized', ''),
            "ai_score": round(rewritten_score, 1),
            "risk_level": derive_risk_level(rewritten_score),
        },
        "improvement": round(original_score - rewritten_score, 1),
        "preview_words": len(preview_text.split()),
        "full_word_count": len(text.split()),
    }
    cache_preview(cache_key, response_data)
    return jsonify(response_data)
