"""Order list, detail, and rewrite-result feedback routes."""

import os
import uuid

from flask import Blueprint, current_app, jsonify, request, session
from app.extensions import limiter
from app.helpers import get_db

orders_bp = Blueprint('orders', __name__)


@orders_bp.route('/api/orders')
@limiter.limit("30 per minute")
def api_orders():
    """Get user's order list with pagination. Requires login."""
    from app.models import Order, RewriteFeedback

    user_id = session.get('user_id')
    if not user_id:
        return jsonify({"error": "未登录"}), 401

    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 10, type=int), 50)

    conn = get_db()
    orders, total = Order.get_by_user_id(
        conn, user_id, page=page, per_page=per_page, history_only=True
    )

    _safe_keys = ['id', 'order_id', 'user_id', 'original_format', 'original_filename',
                  'word_count', 'price', 'mode', 'original_score', 'rewritten_score',
                  'status', 'payment_status',
                  'recharge_words', 'balance_words_used', 'balance_after',
                  'paid_at', 'created_at']
    orders_safe = []
    for order in orders:
        safe_order = {k: order[k] for k in _safe_keys if k in order}
        safe_order['has_feedback'] = bool(
            RewriteFeedback.get_by_order_id(conn, order['order_id'])
        )
        orders_safe.append(safe_order)

    total_pages = max(1, (total + per_page - 1) // per_page)

    return jsonify({
        "orders": orders_safe,
        "total": total,
        "page": page,
        "pages": total_pages
    })


@orders_bp.route('/api/orders/<order_id>')
def api_order_detail(order_id):
    """Get details for a specific order. Requires login."""
    from app.models import Order, RewriteFeedback

    user_id = session.get('user_id')
    if not user_id:
        return jsonify({"error": "未登录"}), 401

    conn = get_db()
    order = Order.get_by_order_id(conn, order_id)
    if not order:
        return jsonify({"error": "订单不存在"}), 404

    if order['user_id'] != user_id:
        return jsonify({"error": "无权访问该订单"}), 403

    _safe = {k: v for k, v in order.items() if k not in ('original_text', 'rewritten_text')}
    feedback = RewriteFeedback.get_by_order_id(conn, order_id)
    feedback_safe = None
    if feedback:
        issue_types = RewriteFeedback.get_issue_types(feedback)
        feedback_safe = {
            'issue_type': issue_types[0] if issue_types else None,
            'issue_types': issue_types,
            'external_score': feedback['external_score'],
            'comment': feedback['comment'],
            'contact_allowed': bool(feedback['contact_allowed']),
            'has_screenshot': bool(feedback['screenshot_file_key']),
            'updated_at': feedback['updated_at'],
        }
    return jsonify({"order": _safe, "feedback": feedback_safe})


_FEEDBACK_ISSUE_TYPES = {
    'satisfied', 'high_ai_score', 'content_disorder',
    'meaning_changed', 'details_lost', 'other',
}
_FEEDBACK_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp'}
_FEEDBACK_MAX_IMAGE_BYTES = 5 * 1024 * 1024


def _feedback_image_signature_supported(header):
    return (
        header.startswith(b'\x89PNG\r\n\x1a\n')
        or header.startswith(b'\xff\xd8\xff')
        or (len(header) >= 12 and header[:4] == b'RIFF' and header[8:12] == b'WEBP')
    )


def _save_feedback_screenshot(upload):
    """Validate and save a feedback screenshot outside the public static folder."""
    if not upload or not upload.filename:
        return None

    extension = upload.filename.rsplit('.', 1)[-1].lower() if '.' in upload.filename else ''
    if extension not in _FEEDBACK_IMAGE_EXTENSIONS:
        raise ValueError('截图仅支持 PNG、JPG 或 WEBP 格式')

    header = upload.stream.read(16)
    upload.stream.seek(0)
    if not _feedback_image_signature_supported(header):
        raise ValueError('截图文件内容无法识别')

    upload.stream.seek(0, os.SEEK_END)
    size = upload.stream.tell()
    upload.stream.seek(0)
    if size <= 0 or size > _FEEDBACK_MAX_IMAGE_BYTES:
        raise ValueError('截图大小需在 5MB 以内')

    normalized_extension = 'jpg' if extension == 'jpeg' else extension
    file_key = f"{uuid.uuid4().hex}.{normalized_extension}"
    upload.save(os.path.join(current_app.config['FEEDBACK_UPLOAD_FOLDER'], file_key))
    return file_key


@orders_bp.route('/api/orders/<order_id>/feedback', methods=['POST'])
@limiter.limit("10 per minute")
def api_order_feedback(order_id):
    """Create or update structured feedback for a user's completed rewrite."""
    from app.models import Order, RewriteFeedback

    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': '未登录'}), 401

    conn = get_db()
    order = Order.get_by_order_id(conn, order_id)
    if not order:
        return jsonify({'error': '订单不存在'}), 404
    if order['user_id'] != user_id:
        return jsonify({'error': '无权访问该订单'}), 403
    if order.get('status') != 'completed':
        return jsonify({'error': '改写完成后才能提交反馈'}), 400

    issue_types = [value.strip() for value in request.form.getlist('issue_types')]
    if not issue_types:
        legacy_issue_type = (request.form.get('issue_type') or '').strip()
        issue_types = [legacy_issue_type] if legacy_issue_type else []
    issue_types = list(dict.fromkeys(value for value in issue_types if value))
    score_text = (request.form.get('external_score') or '').strip()
    comment = (request.form.get('comment') or '').strip()[:1000]
    contact_allowed = request.form.get('contact_allowed') == 'true'

    if not issue_types or any(value not in _FEEDBACK_ISSUE_TYPES for value in issue_types):
        return jsonify({'error': '请至少选择一项有效反馈'}), 400
    if 'other' in issue_types and not comment:
        return jsonify({'error': '选择“其他问题”时请补充说明'}), 400

    external_score = None
    if score_text:
        try:
            external_score = float(score_text)
        except ValueError:
            return jsonify({'error': '实际 AI 率需填写 0 到 100 的数字'}), 400
        if not 0 <= external_score <= 100:
            return jsonify({'error': '实际 AI 率需在 0 到 100 之间'}), 400

    existing = RewriteFeedback.get_by_order_id(conn, order_id)
    new_file_key = None
    try:
        new_file_key = _save_feedback_screenshot(request.files.get('screenshot'))
        RewriteFeedback.upsert(
            conn, user_id, order_id, issue_types,
            external_score=external_score,
            comment=comment or None,
            contact_allowed=contact_allowed,
            screenshot_file_key=new_file_key,
        )
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception:
        if new_file_key:
            try:
                os.remove(os.path.join(current_app.config['FEEDBACK_UPLOAD_FOLDER'], new_file_key))
            except OSError:
                pass
        raise

    old_file_key = existing.get('screenshot_file_key') if existing else None
    if new_file_key and old_file_key and old_file_key != new_file_key:
        try:
            os.remove(os.path.join(current_app.config['FEEDBACK_UPLOAD_FOLDER'], old_file_key))
        except OSError:
            pass

    return jsonify({
        'success': True,
        'message': '感谢反馈，我们会用于排查问题和优化改写效果。',
    })
