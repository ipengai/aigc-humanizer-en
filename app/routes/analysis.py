"""AI 率检测路由。

文件上传后先通过 extract_text 提取结构化节点，再由
paragraph_list_to_text 将所有带 text 字段的节点拼成纯文本。因此，原文 AI
率检测会分析标题、正文、参考文献、代码块和短段落等全部已提取文本；表格
占位、图片本身等不含 text 字段的节点不参与检测。

结构化节点会另外保存在 session 中，仅供用户确认改写后进行 segment 分块、
结构保护和原文格式回填；首次 AI 率检测不会经过 segmenter。
"""

import uuid
import hashlib
import os
import logging
import shutil
from flask import Blueprint, request, jsonify, session
from app.extensions import limiter
from app.helpers import login_required
from app.text_extract import extract_text, paragraph_list_to_text
from config import ALLOWED_UPLOAD_MIMETYPES, PRICE_PER_1000_WORDS, DELETE_UPLOADED_FILE

analysis_bp = Blueprint('analysis', __name__)


@analysis_bp.route('/api/analyze', methods=['POST'])
@limiter.limit("60 per minute")
@login_required
def api_analyze():
    """
    Analyze text for AI content.
    Accepts: text (direct paste) OR file (upload)
    Returns: AI score, paragraph analysis, suggestions
    """
    text = None
    paragraphs = None
    filename = None
    original_format = 'txt'
    original_filename = None
    source_file_key = None
    input_type = 'paste'
    from flask import current_app
    app = current_app

    # Check if file was uploaded
    if 'file' in request.files:
        file = request.files['file']
        if file and file.filename:
            input_type = 'upload'
            if file.content_type and file.content_type not in ALLOWED_UPLOAD_MIMETYPES:
                return jsonify({"error": "不支持的文件格式，仅支持 .docx、.pdf、.txt、.md"}), 400

            ext = os.path.splitext(file.filename)[1].lower()
            if ext not in ['.docx', '.pdf', '.txt', '.md']:
                return jsonify({"error": "仅支持 .docx、.pdf、.txt、.md 格式"}), 400

            original_filename = file.filename
            original_format = ext[1:]
            filename = f"{uuid.uuid4().hex}{ext}"
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            try:
                paragraphs = extract_text(filepath)
                text = paragraph_list_to_text(paragraphs)
                if ext in ('.docx', '.pdf'):
                    source_file_key = filename
                    shutil.copy2(
                        filepath,
                        os.path.join(app.config['SOURCE_DOCS_FOLDER'], source_file_key)
                    )
            except Exception:
                logging.exception(f"Failed to extract text from {filepath}")
                return jsonify({"error": "文件解析失败，请确认文件格式正确"}), 400
            finally:
                if DELETE_UPLOADED_FILE:
                    try:
                        os.remove(filepath)
                    except OSError:
                        logging.warning(f"Failed to remove temp file: {filepath}")

    # Check if text was pasted
    if not text:
        data = request.get_json(silent=True) or {}
        text = data.get('text', '').strip()
        if not text:
            return jsonify({"error": "请上传文档或粘贴英文文本"}), 400

    text = text.strip()

    # Store original format info in session
    session['last_original_format'] = original_format
    session['last_original_filename'] = original_filename
    session['last_source_file_key'] = source_file_key
    session['last_input_type'] = input_type
    session['last_text'] = text
    # 段落结构（含 style），供改写阶段判断标题/短段用；无样式信息时为 None
    session['last_paragraphs'] = paragraphs

    word_count = len(text.split())
    if len(text) < 300 or word_count < 40:
        return jsonify({
            "error": "文本太短，请提供至少 300 个字符（约 40 个英文单词）"
        }), 400

    # 返回余额，让已有余额的用户直接进入全文改写，不再被免费预览流程拦截。
    from app.models import User, get_connection
    balance_conn = get_connection()
    try:
        user_id = session.get('user_id')
        word_balance = User.get_balance(balance_conn, user_id)
        det_free, det_paid = User.get_detection_quota(balance_conn, user_id)
        detection_balance = det_free + det_paid
    finally:
        balance_conn.close()

    # 检测免费；返回改写费用预估（改写扣 word_balance，price 仅作展示）
    price = round(PRICE_PER_1000_WORDS * (word_count / 1000), 2)

    # Run AI detection (整篇 AI 率检测；段落/维度分析已移除，供改写对比使用)
    from app.extensions import ai_detector
    try:
        full_analysis = ai_detector(text, stage="analyze")
    except Exception:
        logging.exception("AI analysis failed")
        return jsonify({"error": "分析出错，请稍后重试"}), 500
    if full_analysis.get("error_code"):
        logging.error("AI detector unavailable: %s", full_analysis.get("error"))
        if full_analysis.get("error_code") == "v2_insufficient_supported_text":
            return jsonify({
                "error": "当前检测器需要至少 40 个英文单词，请补充内容后重试",
                "error_code": full_analysis.get("error_code"),
            }), 400
        return jsonify({
            "error": "AI 检测暂时不可用，请稍后重试",
            "error_code": full_analysis.get("error_code"),
        }), 503

    session['last_text'] = text
    # D 方案：缓存原文检测，供 /api/rewrite 复用（省 1 次 sapling 调用）
    from app.helpers.tasks import cache_original_analysis
    cache_original_analysis(text, full_analysis)
    session['last_analysis_summary'] = {
        'text_hash': hashlib.md5(text.encode('utf-8')).hexdigest(),
        'backend': full_analysis.get('backend'),
        'model_version': full_analysis.get('model_version'),
        'ai_score': full_analysis.get('ai_score'),
        'risk_percent': full_analysis.get('risk_percent'),
        'coverage': full_analysis.get('coverage', 1.0),
        'error_code': full_analysis.get('error_code'),
    }

    import config as project_config
    score = full_analysis.get("risk_percent")
    if score is None:
        score = full_analysis.get("ai_score")
    protected_no_charge = (
        getattr(project_config, "REWRITE_ROUTING_POLICY", "")
        == "risk_band_segmented"
        and score is not None
        and float(score) < 20
        and float(full_analysis.get("coverage", 1.0)) >= float(
            getattr(project_config, "V2_REJECT_COVERAGE", 0.60)
        )
    )
    rewrite_words = 0 if protected_no_charge else word_count

    return jsonify({
        "success": True,
        "analysis": full_analysis,
        "text": text,
        "text_preview": text[:500] + "..." if len(text) > 500 else text,
        "word_count": word_count,
        "price": round(price, 2),
        "rewrite_words": rewrite_words,
        "balance": word_balance,
        "rewrite_balance": word_balance,
        "detection_balance": detection_balance,
        "balance_sufficient": word_balance >= rewrite_words,
        "protected_no_charge": protected_no_charge,
        "has_extracted_text": original_format != 'txt',
        "original_format": original_format,
        "original_filename": original_filename
    })
