#!/usr/bin/env python3
"""
Admin dashboard for AI Humanizer — standalone app, zero coupling with main app.

Usage:
    python admin.py                     # default port 5001
    ADMIN_PORT=5002 python admin.py    # custom port

Authentication:
    Set ADMIN_PASSWORD in .env, otherwise defaults to 'admin123'.
    Login via session cookie, auto-expires after 2 hours of inactivity.
"""

import os
import sqlite3
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (
    Flask, abort, jsonify, redirect, render_template_string, request,
    send_from_directory, session, url_for,
)
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

# Load .env from project root
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

# ---------- Config ----------
PROJ_ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(PROJ_ROOT, 'instance', 'aigc_humanizer.db')
FEEDBACK_UPLOAD_FOLDER = os.path.join(PROJ_ROOT, 'instance', 'feedback_uploads')
ADMIN_PORT = int(os.environ.get('ADMIN_PORT', 5001))
ADMIN_SECRET_KEY = os.environ.get('ADMIN_SECRET_KEY', os.urandom(24).hex())
ADMIN_PASSWORD_HASH = generate_password_hash(
    os.environ.get('ADMIN_PASSWORD', 'admin123'), method='pbkdf2:sha256'
)
SESSION_LIFETIME_MINUTES = 120  # 2 hours

# ---------- App ----------
admin_app = Flask(__name__, template_folder=os.path.join(PROJ_ROOT, 'templates'))
admin_app.secret_key = ADMIN_SECRET_KEY
admin_app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=SESSION_LIFETIME_MINUTES)


def get_db():
    """Get a read-only SQLite connection to the main database."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def login_required(f):
    """Decorator: redirect to login if not authenticated; API routes get JSON 401 instead of HTML redirect."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_authenticated'):
            if request.path.startswith('/admin/api/'):
                return jsonify({'error': '未登录或会话已过期，请重新登录'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


# ============================================================
#  Routes
# ============================================================

@admin_app.route('/admin/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        password = request.form.get('password', '')
        if check_password_hash(ADMIN_PASSWORD_HASH, password):
            session.permanent = True
            session['admin_authenticated'] = True
            session['admin_login_time'] = datetime.now(timezone.utc).isoformat()
            return redirect(url_for('dashboard'))
        error = '密码错误'
    return render_template_string(LOGIN_TEMPLATE, error=error)


@admin_app.route('/admin/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@admin_app.route('/admin')
@login_required
def dashboard():
    return render_template_string(DASHBOARD_TEMPLATE)


# ---------- API ----------

@admin_app.route('/admin/api/orders')
@login_required
def api_orders():
    """Return orders for a given date range as JSON."""
    start_date = request.args.get('start', '')
    end_date = request.args.get('end', '')
    page = int(request.args.get('page', 1))

    # Validate dates
    try:
        start_dt = datetime.strptime(start_date, '%Y-%m-%d').date()
        end_dt = datetime.strptime(end_date, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return jsonify({'error': '请提供有效的日期，格式 YYYY-MM-DD'}), 400

    if start_dt > end_dt:
        return jsonify({'error': '开始日期不能晚于结束日期'}), 400

    start_iso = datetime.combine(start_dt, datetime.min.time()).isoformat()
    end_iso = datetime.combine(end_dt + timedelta(days=1), datetime.min.time()).isoformat()

    conn = get_db()
    per_page = 50

    # ── 字段筛选（空值即不过滤）──
    f_ps = (request.args.get('ps') or '').strip()
    f_status = (request.args.get('status') or '').strip()
    f_mode = (request.args.get('mode') or '').strip()
    f_backend = (request.args.get('backend') or '').strip()

    where = ["o.created_at >= ?", "o.created_at < ?"]
    params = [start_iso, end_iso]
    if f_ps:
        where.append("o.payment_status = ?")
        params.append(f_ps)
    if f_status:
        where.append("o.status = ?")
        params.append(f_status)
    if f_mode:
        if f_mode == 'median':
            where.append("o.mode IN ('median', 'academic')")
        else:
            where.append("o.mode = ?")
            params.append(f_mode)
    if f_backend:
        where.append("o.detector_backend = ?")
        params.append(f_backend)
    where_sql = " AND ".join(where)

    try:
        # Total count
        total = conn.execute(
            "SELECT COUNT(*) as total FROM orders o WHERE " + where_sql,
            params
        ).fetchone()['total']

        # Summary stats
        paid_count = conn.execute(
            "SELECT COUNT(*) as total FROM orders o "
            "WHERE " + where_sql + " AND o.payment_status = 'paid'",
            params
        ).fetchone()['total']

        total_revenue = conn.execute(
            "SELECT COALESCE(SUM(o.price), 0) as total FROM orders o "
            "WHERE " + where_sql + " AND o.payment_status = 'paid'",
            params
        ).fetchone()['total']

        # Status breakdown
        status_counts = {}
        for row in conn.execute(
            "SELECT o.payment_status, COUNT(*) as cnt FROM orders o "
            "WHERE " + where_sql + " GROUP BY o.payment_status",
            params
        ).fetchall():
            status_counts[row['payment_status']] = row['cnt']

        # Orders page
        offset = (page - 1) * per_page
        cursor = conn.execute(
            """SELECT o.*, u.email as user_email
               FROM orders o
               LEFT JOIN users u ON o.user_id = u.id
               WHERE """ + where_sql + """
               ORDER BY o.created_at DESC
               LIMIT ? OFFSET ?""",
            params + [per_page, offset]
        )

        orders = []

        for row in cursor.fetchall():
            order = dict(row)
            if order.get('original_text'):
                order['original_text_preview'] = order['original_text'][:200]
            if order.get('rewritten_text'):
                order['rewritten_text_preview'] = order['rewritten_text'][:200]
            orders.append(order)

        return jsonify({
            'start_date': start_date,
            'end_date': end_date,
            'summary': {
                'total_orders': total,
                'paid_orders': paid_count,
                'pending_orders': status_counts.get('pending', 0),
                'expired_orders': status_counts.get('expired', 0),
                'failed_orders': status_counts.get('failed', 0),
                'total_revenue': round(total_revenue, 2),
            },
            'orders': orders,
            'page': page,
            'per_page': per_page,
            'total_pages': max((total + per_page - 1) // per_page, 1),
        })
    finally:
        conn.close()


@admin_app.route('/admin/api/order/<order_id>')
@login_required
def api_order_detail(order_id):
    """Return full detail for a single order."""
    conn = get_db()
    try:
        cursor = conn.execute(
            """SELECT o.*, u.email as user_email
               FROM orders o
               LEFT JOIN users u ON o.user_id = u.id
               WHERE o.order_id = ?""",
            (order_id,)
        )
        row = cursor.fetchone()
        if not row:
            return jsonify({'error': '订单不存在'}), 404
        return jsonify(dict(row))
    finally:
        conn.close()


@admin_app.route('/admin/api/trends')
@login_required
def api_trends():
    """Daily aggregated time series: new users, orders, paid orders, revenue."""
    start_date = request.args.get('start', '')
    end_date = request.args.get('end', '')

    try:
        start_dt = datetime.strptime(start_date, '%Y-%m-%d').date()
        end_dt = datetime.strptime(end_date, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return jsonify({'error': '请提供有效的日期，格式 YYYY-MM-DD'}), 400

    if start_dt > end_dt:
        return jsonify({'error': '开始日期不能晚于结束日期'}), 400
    if (end_dt - start_dt).days > 366:
        return jsonify({'error': '时间范围不能超过一年'}), 400

    start_iso = datetime.combine(start_dt, datetime.min.time()).isoformat()
    end_iso = datetime.combine(end_dt + timedelta(days=1), datetime.min.time()).isoformat()

    conn = get_db()
    try:
        user_rows = conn.execute(
            "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS cnt FROM users "
            "WHERE created_at >= ? AND created_at < ? GROUP BY day",
            (start_iso, end_iso)
        ).fetchall()
        order_rows = conn.execute(
            "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS cnt, "
            "SUM(CASE WHEN payment_status = 'paid' THEN 1 ELSE 0 END) AS paid_cnt, "
            "COALESCE(SUM(CASE WHEN payment_status = 'paid' THEN price ELSE 0 END), 0) AS revenue "
            "FROM orders WHERE created_at >= ? AND created_at < ? GROUP BY day",
            (start_iso, end_iso)
        ).fetchall()
    finally:
        conn.close()

    users_by_day = {r['day']: r['cnt'] for r in user_rows}
    orders_by_day = {r['day']: r['cnt'] for r in order_rows}
    paid_by_day = {r['day']: (r['paid_cnt'] or 0) for r in order_rows}
    revenue_by_day = {r['day']: (r['revenue'] or 0) for r in order_rows}

    days, new_users, orders_series, paid_series, revenue_series = [], [], [], [], []
    cur = start_dt
    while cur <= end_dt:
        key = cur.isoformat()
        days.append(key)
        new_users.append(users_by_day.get(key, 0))
        orders_series.append(orders_by_day.get(key, 0))
        paid_series.append(paid_by_day.get(key, 0))
        revenue_series.append(round(revenue_by_day.get(key, 0), 2))
        cur += timedelta(days=1)

    return jsonify({
        'start_date': start_date,
        'end_date': end_date,
        'days': days,
        'series': {
            'new_users': new_users,
            'orders': orders_series,
            'paid_orders': paid_series,
            'revenue': revenue_series,
        },
        'totals': {
            'new_users': sum(new_users),
            'orders': sum(orders_series),
            'paid_orders': sum(paid_series),
            'revenue': round(sum(revenue_series), 2),
        },
    })


# ============================================================
#  Rewrite Effect Statistics
# ============================================================

@admin_app.route('/admin/api/rewrite-stats')
@login_required
def api_rewrite_stats():
    """Combine system scores, structural signals, and user-reported results."""
    start_date = request.args.get('start', '')
    end_date = request.args.get('end', '')

    try:
        start_dt = datetime.strptime(start_date, '%Y-%m-%d').date()
        end_dt = datetime.strptime(end_date, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return jsonify({'error': '请提供有效的日期，格式 YYYY-MM-DD'}), 400

    if start_dt > end_dt:
        return jsonify({'error': '开始日期不能晚于结束日期'}), 400

    start_iso = datetime.combine(start_dt, datetime.min.time()).isoformat()
    end_iso = datetime.combine(end_dt + timedelta(days=1), datetime.min.time()).isoformat()

    conn = get_db()
    try:
        order_columns = {
            row['name'] for row in conn.execute("PRAGMA table_info(orders)").fetchall()
        }
        optional_columns = {
            name: name if name in order_columns else f"NULL AS {name}"
            for name in (
                'humanizer_backend', 'word_count_change_ratio',
                'heading_count_changed', 'completed_at',
            )
        }
        rows = conn.execute(
            f"""SELECT mode, detector_backend,
                      {optional_columns['humanizer_backend']},
                      original_score, rewritten_score,
                      {optional_columns['word_count_change_ratio']},
                      {optional_columns['heading_count_changed']},
                      created_at, {optional_columns['completed_at']}
               FROM orders
            """
            "WHERE status = 'completed' "
            "AND created_at >= ? AND created_at < ? "
            "AND original_score IS NOT NULL AND rewritten_score IS NOT NULL "
            "AND original_score BETWEEN 0 AND 100 AND rewritten_score BETWEEN 0 AND 100",
            (start_iso, end_iso)
        ).fetchall()

        feedback_table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'rewrite_feedback'"
        ).fetchone()
        if feedback_table_exists:
            feedback_rows = conn.execute(
                """SELECT f.*, o.mode, o.detector_backend, o.rewritten_score,
                          u.email AS user_email
                   FROM rewrite_feedback f
                   JOIN orders o ON o.order_id = f.order_id
                   LEFT JOIN users u ON u.id = f.user_id
                   WHERE o.status = 'completed'
                     AND o.created_at >= ? AND o.created_at < ?
                   ORDER BY f.updated_at DESC
                   LIMIT 50""",
                (start_iso, end_iso)
            ).fetchall()
        else:
            feedback_rows = []
    finally:
        conn.close()

    n = len(rows)
    below20 = sum(1 for r in rows if r['rewritten_score'] < 20)
    originals = [r['original_score'] for r in rows]
    rewrites = [r['rewritten_score'] for r in rows]
    improved = sum(1 for r in rows if r['rewritten_score'] < r['original_score'])
    worsened = sum(1 for r in rows if r['rewritten_score'] > r['original_score'])
    word_changes = [
        abs(r['word_count_change_ratio']) for r in rows
        if r['word_count_change_ratio'] is not None
    ]
    heading_warnings = sum(1 for r in rows if r['heading_count_changed'])

    durations = []
    for row in rows:
        if not row['completed_at'] or not row['created_at']:
            continue
        try:
            duration = (
                datetime.fromisoformat(row['completed_at'])
                - datetime.fromisoformat(row['created_at'])
            ).total_seconds()
            if duration >= 0:
                durations.append(duration)
        except (TypeError, ValueError):
            continue

    issue_counts = {}
    external_scores = []
    recent_feedback = []
    for row in feedback_rows:
        issue_counts[row['issue_type']] = issue_counts.get(row['issue_type'], 0) + 1
        if row['external_score'] is not None:
            external_scores.append(row['external_score'])
        recent_feedback.append({
            'order_id': row['order_id'],
            'user_email': row['user_email'],
            'issue_type': row['issue_type'],
            'detector_platform': row['detector_platform'],
            'external_score': row['external_score'],
            'comment': row['comment'],
            'contact_allowed': bool(row['contact_allowed']),
            'updated_at': row['updated_at'],
            'screenshot_url': (
                url_for('feedback_screenshot', file_key=row['screenshot_file_key'])
                if row['screenshot_file_key'] else None
            ),
        })

    external_below20 = sum(1 for score in external_scores if score < 20)

    return jsonify({
        'start_date': start_date,
        'end_date': end_date,
        'sample_count': n,
        'below20_count': below20,
        'below20_ratio': round(below20 / n, 4) if n else 0,
        'improved_count': improved,
        'improved_ratio': round(improved / n, 4) if n else 0,
        'worsened_count': worsened,
        'worsened_ratio': round(worsened / n, 4) if n else 0,
        'avg_original_score': round(sum(originals) / n, 1) if n else 0,
        'avg_rewritten_score': round(sum(rewrites) / n, 1) if n else 0,
        'avg_improvement': round((sum(originals) - sum(rewrites)) / n, 1) if n else 0,
        'avg_abs_word_change_ratio': (
            round(sum(word_changes) / len(word_changes), 4) if word_changes else 0
        ),
        'heading_warning_count': heading_warnings,
        'avg_processing_seconds': (
            round(sum(durations) / len(durations), 1) if durations else 0
        ),
        'feedback_count': len(feedback_rows),
        'feedback_issue_counts': issue_counts,
        'external_score_count': len(external_scores),
        'external_below20_count': external_below20,
        'external_below20_ratio': (
            round(external_below20 / len(external_scores), 4)
            if external_scores else 0
        ),
        'recent_feedback': recent_feedback,
    })


@admin_app.route('/admin/feedback-screenshot/<file_key>')
@login_required
def feedback_screenshot(file_key):
    """Serve private feedback evidence to authenticated admins only."""
    if file_key != os.path.basename(file_key):
        abort(404)
    return send_from_directory(FEEDBACK_UPLOAD_FOLDER, file_key)


# ============================================================
#  User Management
# ============================================================

@admin_app.route('/admin/api/users')
@login_required
def api_users():
    """List users with aggregated stats: balance, total recharge, total spent, order count."""
    search = (request.args.get('search') or '').strip()

    # Use a regular (non-read-only) connection to query aggregate functions.
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    try:
        where_clause = ''
        params = []
        if search:
            where_clause = 'WHERE u.email LIKE ?'
            params = [f'%{search}%']

        # Per-user aggregation: balance + total recharge + total spent (in words)
        sql = f'''
            SELECT
                u.id, u.email, u.word_balance, u.created_at, u.last_login_at,
                COALESCE(SUM(CASE WHEN bt.transaction_type = 'payment_recharge' THEN bt.words ELSE 0 END), 0) AS total_recharged,
                COALESCE(SUM(CASE WHEN bt.transaction_type = 'rewrite_consumption' THEN ABS(bt.words) ELSE 0 END), 0) AS total_spent,
                (SELECT COUNT(*) FROM orders o WHERE o.user_id = u.id) AS order_count,
                (SELECT COUNT(*) FROM orders o WHERE o.user_id = u.id AND o.payment_status = 'paid') AS paid_count
            FROM users u
            LEFT JOIN balance_transactions bt ON bt.user_id = u.id
            {where_clause}
            GROUP BY u.id
            ORDER BY u.id DESC
        '''
        users = [dict(r) for r in conn.execute(sql, params).fetchall()]

        # Overall stats
        stats_row = conn.execute('''
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN word_balance > 0 THEN 1 ELSE 0 END) AS with_balance,
                COALESCE(SUM(word_balance), 0) AS total_balance,
                COALESCE((
                    SELECT SUM(ABS(words)) FROM balance_transactions
                    WHERE transaction_type = 'rewrite_consumption'
                ), 0) AS total_spent
            FROM users
        ''').fetchone()
        stats = dict(stats_row) if stats_row else {
            'total': 0, 'with_balance': 0, 'total_balance': 0, 'total_spent': 0
        }

        return jsonify({'stats': stats, 'users': users})
    finally:
        conn.close()


# ============================================================
#  Activation Code Management
# ============================================================

@admin_app.route('/admin/api/activation-codes', methods=['GET', 'POST'])
@login_required
def api_activation_codes():
    """GET: list codes with stats. POST: generate new codes."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if request.method == 'GET':
        try:
            # Stats
            total = conn.execute("SELECT COUNT(*) as c FROM activation_codes").fetchone()['c']
            used = conn.execute("SELECT COUNT(*) as c FROM activation_codes WHERE status = 'redeemed'").fetchone()['c']
            unused = conn.execute("SELECT COUNT(*) as c FROM activation_codes WHERE status = 'unused'").fetchone()['c']
            total_words = conn.execute(
                "SELECT COALESCE(SUM(word_quota), 0) as s FROM activation_codes WHERE status = 'redeemed'"
            ).fetchone()['s']

            # List codes
            page = int(request.args.get('page', 1))
            per_page = 50
            offset = (page - 1) * per_page
            count_row = conn.execute("SELECT COUNT(*) as total FROM activation_codes").fetchone()
            total_count = count_row['total'] if count_row else 0
            cursor = conn.execute(
                """SELECT ac.*, u.email as redeemed_by_email
                   FROM activation_codes ac
                   LEFT JOIN users u ON ac.redeemed_by = u.id
                   ORDER BY ac.created_at DESC LIMIT ? OFFSET ?""",
                (per_page, offset)
            )
            codes = []
            for row in cursor.fetchall():
                c = dict(row)
                c['created_at'] = c.get('created_at', '')
                c['redeemed_at'] = c.get('redeemed_at', '')
                codes.append(c)

            return jsonify({
                'stats': {
                    'total': total, 'used': used, 'unused': unused,
                    'total_redeemed_words': total_words
                },
                'codes': codes,
                'page': page,
                'per_page': per_page,
                'total_pages': max((total_count + per_page - 1) // per_page, 1),
            })
        finally:
            conn.close()

    # POST: generate new codes
    data = request.get_json(silent=True) or {}
    count = int(data.get('count', 10))
    word_quota = int(data.get('word_quota', 2000))

    if count < 1 or count > 100:
        return jsonify({'error': '数量在 1-100 之间'}), 400
    if word_quota < 100 or word_quota > 100000:
        return jsonify({'error': '词数在 100-100000 之间'}), 400

    # quota label: 2000→2K, 10000→1W, 50000→5W
    if word_quota >= 10000 and word_quota % 10000 == 0:
        quota_label = f"{word_quota // 10000}W"
    elif word_quota >= 1000 and word_quota % 1000 == 0:
        quota_label = f"{word_quota // 1000}K"
    else:
        quota_label = str(word_quota)

    try:
        generated = []
        for _ in range(count):
            # Format: HUMA-{quota}-XXXX-XXXX
            part1 = secrets.token_hex(2).upper()
            part2 = secrets.token_hex(2).upper()
            code = f"HUMA-{quota_label}-{part1}-{part2}"
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO activation_codes (code, word_quota, created_at) VALUES (?, ?, ?)",
                (code, word_quota, now)
            )
            generated.append({'code': code, 'word_quota': word_quota})
        conn.commit()
        return jsonify({'success': True, 'count': count, 'word_quota': word_quota, 'codes': generated})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ============================================================
#  Jinja2 Templates (inline to keep everything in one file)
# ============================================================

LOGIN_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>管理后台 - 登录</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f1f5f9;
            display: flex; align-items: center; justify-content: center;
            min-height: 100vh;
        }
        .login-card {
            background: #fff; border-radius: 12px; padding: 40px; width: 380px;
            box-shadow: 0 4px 24px rgba(0,0,0,0.08);
        }
        .login-card h1 {
            font-size: 1.5rem; font-weight: 700; color: #1e293b;
            margin-bottom: 8px; text-align: center;
        }
        .login-card p {
            font-size: 0.875rem; color: #94a3b8; text-align: center;
            margin-bottom: 28px;
        }
        .form-group { margin-bottom: 20px; }
        .form-group label {
            display: block; font-size: 0.875rem; font-weight: 600;
            color: #334155; margin-bottom: 6px;
        }
        .form-group input {
            width: 100%; padding: 10px 14px; border: 1px solid #e2e8f0;
            border-radius: 8px; font-size: 1rem; color: #1e293b;
            outline: none; transition: border-color 0.15s;
        }
        .form-group input:focus { border-color: #4f46e5; box-shadow: 0 0 0 3px rgba(79,70,229,0.1); }
        .btn {
            width: 100%; padding: 10px; background: #4f46e5; color: #fff;
            border: none; border-radius: 8px; font-size: 1rem; font-weight: 600;
            cursor: pointer; transition: background 0.15s;
        }
        .btn:hover { background: #4338ca; }
        .error {
            background: #fef2f2; color: #dc2626; padding: 10px 14px;
            border-radius: 8px; font-size: 0.875rem; margin-bottom: 16px;
        }
    </style>
</head>
<body>
    <div class="login-card">
        <h1>🔐 管理后台</h1>
        <p>AI Humanizer Admin</p>
        {% if error %}
        <div class="error">{{ error }}</div>
        {% endif %}
        <form method="POST">
            <div class="form-group">
                <label for="password">管理员密码</label>
                <input type="password" id="password" name="password" placeholder="请输入密码" autofocus required>
            </div>
            <button type="submit" class="btn">登 录</button>
        </form>
    </div>
</body>
</html>"""


DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>管理后台 - AI Humanizer</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f1f5f9; color: #1e293b; min-height: 100vh;
        }
        .header {
            background: #fff; border-bottom: 1px solid #e2e8f0;
            padding: 0 24px; height: 60px; display: flex;
            align-items: center; justify-content: space-between;
        }
        .header h1 { font-size: 1.125rem; font-weight: 700; }
        .header-right { display: flex; align-items: center; gap: 16px; }
        .btn-logout {
            background: #fee2e2; color: #dc2626; border: none;
            padding: 6px 16px; border-radius: 6px; font-size: 0.875rem;
            cursor: pointer; font-weight: 500;
        }
        .btn-logout:hover { background: #fecaca; }
        .tabs {
            display: flex; gap: 0; border-bottom: 1px solid #e2e8f0;
            background: #fff; padding: 0 24px;
        }
        .tab-btn {
            padding: 12px 24px; border: none; background: none;
            font-size: 0.9rem; font-weight: 500; color: #64748b;
            cursor: pointer; border-bottom: 2px solid transparent;
            transition: all 0.15s; font-family: inherit;
        }
        .tab-btn:hover { color: #1e293b; }
        .tab-btn.active { color: #4f46e5; border-bottom-color: #4f46e5; }
        .main { max-width: 1400px; margin: 0 auto; padding: 24px; }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        /* Toolbar */
        .toolbar {
            display: flex; align-items: center; gap: 10px; margin-bottom: 24px;
            flex-wrap: wrap;
        }
        .toolbar label {
            font-size: 0.875rem; font-weight: 600; color: #475569;
        }
        .toolbar input[type="date"] {
            padding: 8px 12px; border: 1px solid #e2e8f0; border-radius: 8px;
            font-size: 0.9rem; color: #1e293b; outline: none;
        }
        .toolbar input[type="date"]:focus { border-color: #4f46e5; }
        .date-sep { color: #94a3b8; font-weight: 500; }
        .btn-query {
            padding: 8px 20px; background: #4f46e5; color: #fff;
            border: none; border-radius: 8px; font-size: 0.9rem; font-weight: 600;
            cursor: pointer;
        }
        .btn-query:hover { background: #4338ca; }
        .btn-preset {
            padding: 6px 14px; background: #fff; color: #475569;
            border: 1px solid #e2e8f0; border-radius: 8px; font-size: 0.825rem;
            cursor: pointer; white-space: nowrap;
        }
        .btn-preset:hover { background: #f1f5f9; border-color: #cbd5e1; }
                .btn-preset.active { background: #eef2ff; color: #4f46e5; border-color: #4f46e5; }
        .filter-select {
            padding: 8px 12px; border: 1px solid #e2e8f0; border-radius: 8px;
            font-size: 0.85rem; color: #1e293b; background: #fff; outline: none;
            cursor: pointer;
        }
        .filter-select:focus { border-color: #4f46e5; }
        /* Summary cards */
        .summary {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
            gap: 14px; margin-bottom: 24px;
        }
        .summary-card {
            background: #fff; border-radius: 12px; padding: 18px 20px;
            box-shadow: 0 1px 4px rgba(0,0,0,0.04);
        }
        .summary-card .label {
            font-size: 0.75rem; color: #94a3b8; margin-bottom: 4px;
            text-transform: uppercase; letter-spacing: 0.05em;
        }
        .summary-card .value {
            font-size: 1.5rem; font-weight: 700; color: #1e293b;
        }
        .summary-card .value.revenue { color: #059669; }
        .summary-card .value.pending { color: #ca8a04; }
        /* Loading */
        .loading { text-align: center; padding: 60px 0; color: #94a3b8; }
        .spinner {
            display: inline-block; width: 28px; height: 28px;
            border: 3px solid #e2e8f0; border-top-color: #4f46e5;
            border-radius: 50%; animation: spin 0.8s linear infinite;
            margin-bottom: 12px;
        }
        @keyframes spin { to { transform: rotate(360deg); } }
        /* Table */
        .table-wrapper {
            background: #fff; border-radius: 12px;
            box-shadow: 0 1px 4px rgba(0,0,0,0.04); overflow: hidden;
        }
        .table-header {
            padding: 16px 20px; display: flex; align-items: center;
            justify-content: space-between; border-bottom: 1px solid #f1f5f9;
        }
        .table-header h2 { font-size: 1rem; font-weight: 600; }
        .count-badge {
            background: #eef2ff; color: #4f46e5; font-size: 0.8rem;
            padding: 2px 12px; border-radius: 12px; font-weight: 600;
        }
        table {
            width: 100%; border-collapse: collapse;
        }
        th {
            text-align: left; padding: 10px 16px;
            font-size: 0.725rem; font-weight: 600; color: #94a3b8;
            text-transform: uppercase; letter-spacing: 0.05em;
            background: #f8fafc; border-bottom: 1px solid #e2e8f0;
            white-space: nowrap;
        }
        td {
            padding: 10px 16px; font-size: 0.85rem;
            border-bottom: 1px solid #f1f5f9; vertical-align: top;
        }
        tr.row-order { cursor: pointer; transition: background 0.1s; }
        tr.row-order:hover { background: #f8fafc; }
        tr.row-detail { background: #f8fafc; }
        tr.row-detail td { padding: 16px; }
        .detail-grid {
            display: grid; grid-template-columns: 1fr 1fr; gap: 20px;
        }
        @media (max-width: 768px) {
            .detail-grid { grid-template-columns: 1fr; }
            .summary { grid-template-columns: repeat(2, 1fr); }
        }
        .detail-box h4 {
            font-size: 0.75rem; font-weight: 700; color: #64748b;
            text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px;
        }
        .detail-box .text-content {
            background: #fff; border: 1px solid #e2e8f0; border-radius: 8px;
            padding: 12px; font-size: 0.85rem; line-height: 1.6;
            color: #334155; white-space: pre-wrap; word-break: break-word;
            max-height: 300px; overflow-y: auto;
        }
        .detail-meta {
            display: flex; flex-wrap: wrap; gap: 8px;
            font-size: 0.78rem; color: #64748b;
        }
        .detail-meta span {
            background: #f1f5f9; padding: 2px 10px; border-radius: 4px;
        }
        .badge {
            display: inline-block; padding: 2px 10px; border-radius: 12px;
            font-size: 0.725rem; font-weight: 600; white-space: nowrap;
        }
        .badge-paid { background: #dcfce7; color: #16a34a; }
        .badge-pending { background: #fef9c3; color: #ca8a04; }
        .badge-expired { background: #f1f5f9; color: #64748b; }
        .badge-failed { background: #fee2e2; color: #dc2626; }
        .badge-completed { background: #dbeafe; color: #2563eb; }
        .badge-processing { background: #f3e8ff; color: #9333ea; }
        .badge-balance { background: #fef3c7; color: #b45309; }
        .badge-free { background: #ecfeff; color: #0e7490; }
        /* Pagination */
        .pagination {
            display: flex; align-items: center; justify-content: center;
            gap: 12px; padding: 16px 20px; border-top: 1px solid #f1f5f9;
        }
        .pagination button {
            padding: 6px 16px; border: 1px solid #e2e8f0; border-radius: 6px;
            background: #fff; font-size: 0.875rem; cursor: pointer; color: #334155;
        }
        .pagination button:hover:not(:disabled) { background: #f1f5f9; }
        .pagination button:disabled { opacity: 0.4; cursor: not-allowed; }
        .pagination .page-info { font-size: 0.875rem; color: #64748b; }
        /* Empty */
        .empty { text-align: center; padding: 60px 20px; color: #94a3b8; }
        .empty-icon { font-size: 2.5rem; margin-bottom: 12px; }
        /* Error */
        .error-banner {
            background: #fef2f2; color: #dc2626; padding: 12px 20px;
            border-radius: 8px; margin-bottom: 16px; font-size: 0.875rem;
        }
        /* Links */
        .header a { text-decoration: none; color: #4f46e5; font-size: 0.875rem; }
        /* Trends */
        .metric-chip {
            display: inline-flex; align-items: center; gap: 6px;
            font-size: 0.85rem; font-weight: 500; color: #334155;
            background: #fff; border: 1px solid #e2e8f0; border-radius: 8px;
            padding: 7px 12px; cursor: pointer; user-select: none;
        }
        .metric-chip input { accent-color: #4f46e5; cursor: pointer; margin: 0; }
        .metric-chip .dot, .legend-item .dot, .chart-tooltip .dot {
            display: inline-block; width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
        }
        .chart-card {
            background: #fff; border-radius: 12px; padding: 20px;
            box-shadow: 0 1px 4px rgba(0,0,0,0.04);
        }
        .chart-legend { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 12px; }
        .legend-item {
            display: inline-flex; align-items: center; gap: 6px;
            font-size: 0.8rem; color: #475569; cursor: pointer; user-select: none;
            padding: 3px 10px; border-radius: 6px; background: #f8fafc;
        }
        .legend-item:hover { background: #f1f5f9; }
        .legend-item.off { opacity: 0.35; text-decoration: line-through; }
        #trend-svg { width: 100%; height: auto; display: block; }
        .chart-tooltip {
            position: absolute; display: none; pointer-events: none; z-index: 10;
            background: #1e293b; color: #fff; border-radius: 8px;
            padding: 8px 12px; font-size: 0.78rem; line-height: 1.6;
            white-space: nowrap; box-shadow: 0 4px 12px rgba(0,0,0,0.18);
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>📊 管理后台</h1>
        <div class="header-right">
            <button class="btn-logout" onclick="location.href='/admin/logout'">退出登录</button>
        </div>
    </div>

    <div class="tabs">
        <button class="tab-btn active" onclick="switchTab('orders')" id="tab-orders">📋 订单</button>
                <button class="tab-btn" onclick="switchTab('stats')" id="tab-stats">📊 改写效果</button>
        <button class="tab-btn" onclick="switchTab('activation')" id="tab-activation">🎯 兑换码</button>
        <button class="tab-btn" onclick="switchTab('users')" id="tab-users">👤 用户</button>
    </div>

    <!-- ============ TAB: ORDERS ============ -->
    <div class="tab-content active" id="content-orders">
    <div class="main">
        <!-- Date range picker -->
        <div class="toolbar">
            <label>时间范围：</label>
            <input type="date" id="date-start">
            <span class="date-sep">至</span>
            <input type="date" id="date-end">
            <button class="btn-query" onclick="loadOrders()">查询</button>
            <button class="btn-preset" onclick="setPreset('today')">今天</button>
            <button class="btn-preset" onclick="setPreset('yesterday')">昨天</button>
            <button class="btn-preset" onclick="setPreset('7days')">近7天</button>
            <button class="btn-preset" onclick="setPreset('30days')">近30天</button>
            <button class="btn-preset" onclick="setPreset('thisMonth')">本月</button>
            <span style="font-size:0.8rem;color:#94a3b8;margin-left:auto;">
                点击订单行展开/折叠详情
            </span>
        </div>

        <!-- Field filters -->
        <div class="toolbar">
            <label>筛选：</label>
            <select id="f-payment" class="filter-select" onchange="applyFilter()">
                <option value="">支付方式：全部</option>
                <option value="paid">扫码已付</option>
                <option value="balance">余额支付</option>
                <option value="free">免费</option>
                <option value="pending">待支付</option>
                <option value="expired">已过期</option>
                <option value="failed">支付失败</option>
            </select>
            <select id="f-status" class="filter-select" onchange="applyFilter()">
                <option value="">任务状态：全部</option>
                <option value="completed">已完成</option>
                <option value="processing">处理中</option>
                <option value="pending">待处理</option>
                <option value="failed">处理失败</option>
                <option value="expired">已过期</option>
                <option value="awaiting_balance">余额待补足</option>
            </select>
            <select id="f-mode" class="filter-select" onchange="applyFilter()">
                <option value="">改写方法：全部</option>
                <option value="low">low</option>
                <option value="median">median</option>
                <option value="high">high</option>
            </select>
            <select id="f-backend" class="filter-select" onchange="applyFilter()">
                <option value="">检测方法：全部</option>
                <option value="sapling">Sapling</option>
                <option value="rule_based">本地规则</option>
                <option value="originality">Originality</option>
            </select>
            <button class="btn-preset" onclick="clearFilter()">清空筛选</button>
        </div>

        <div class="error-banner" id="error-banner" style="display:none;"></div>

        <!-- Summary cards -->
        <div class="summary" id="summary" style="display:none;">
            <div class="summary-card">
                <div class="label">订单总数</div>
                <div class="value" id="stat-total">0</div>
            </div>
            <div class="summary-card">
                <div class="label">扫码已支付</div>
                <div class="value" id="stat-paid" style="color:#16a34a;">0</div>
            </div>
            <div class="summary-card">
                <div class="label">扫码待支付</div>
                <div class="value pending" id="stat-pending">0</div>
            </div>
            <div class="summary-card">
                <div class="label">支付已过期</div>
                <div class="value" id="stat-expired" style="color:#64748b;">0</div>
            </div>
            <div class="summary-card">
                <div class="label">支付失败</div>
                <div class="value" id="stat-failed" style="color:#dc2626;">0</div>
            </div>
            <div class="summary-card">
                <div class="label">实收营收 (¥)</div>
                <div class="value revenue" id="stat-revenue">0.00</div>
            </div>
        </div>

        <!-- Loading -->
        <div class="loading" id="loading">
            <div class="spinner"></div>
            <div>加载中</div>
        </div>

        <!-- Table -->
        <div class="table-wrapper" id="table-wrapper" style="display:none;">
            <div class="table-header">
                <h2>订单明细</h2>
                <span class="count-badge" id="count-badge">0 条</span>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>订单号</th>
                        <th>用户</th>
                        <th>来源</th>
                        <th>字数</th>
                        <th>金额 / 消耗</th>
                        <th>支付方式</th>
                        <th>支付状态</th>
                        <th>任务状态</th>
                        <th>检测方法</th>
                        <th>改写方法</th>
                        <th>AI 率（原→改写）</th>
                        <th>创建时间</th>
                    </tr>
                </thead>
                <tbody id="orders-tbody"></tbody>
            </table>
            <div class="pagination" id="pagination" style="display:none;">
                <button id="btn-prev" onclick="goPage(-1)">← 上一页</button>
                <span class="page-info" id="page-info">第 1 / 1 页</span>
                <button id="btn-next" onclick="goPage(1)">下一页 →</button>
            </div>
        </div>

        <!-- Empty -->
        <div class="table-wrapper" id="empty-state" style="display:none;">
            <div class="empty">
                <div class="empty-icon">📭</div>
                <p>该时间范围暂无订单</p>
            </div>
        </div>
    </div>
    </div>

    <!-- ============ TAB: TRENDS ============ -->
    <div class="tab-content" id="content-trends">
    <div class="main">
        <!-- Metric selector -->
        <div class="toolbar">
            <h2 style="font-size:1rem;font-weight:600;margin-right:16px;">📈 趋势图</h2>
            <label>指标：</label>
            <label class="metric-chip"><input type="checkbox" id="metric-new_users" checked onchange="renderTrendChart()"><span class="dot" style="background:#8b5cf6;"></span>每日用户数</label>
            <label class="metric-chip"><input type="checkbox" id="metric-orders" checked onchange="renderTrendChart()"><span class="dot" style="background:#2563eb;"></span>订单量</label>
            <label class="metric-chip"><input type="checkbox" id="metric-paid_orders" checked onchange="renderTrendChart()"><span class="dot" style="background:#16a34a;"></span>付费订单量</label>
            <label class="metric-chip"><input type="checkbox" id="metric-revenue" checked onchange="renderTrendChart()"><span class="dot" style="background:#f59e0b;"></span>营收额</label>
        </div>

        <!-- Date range -->
        <div class="toolbar">
            <label>时间范围：</label>
            <input type="date" id="trend-date-start">
            <span class="date-sep">至</span>
            <input type="date" id="trend-date-end">
            <button class="btn-query" onclick="loadTrends()">查询</button>
            <button class="btn-preset" onclick="setTrendPreset('7days')">近7天</button>
            <button class="btn-preset" onclick="setTrendPreset('30days')">近30天</button>
            <button class="btn-preset" onclick="setTrendPreset('90days')">近90天</button>
            <button class="btn-preset" onclick="setTrendPreset('thisMonth')">本月</button>
        </div>

        <div class="error-banner" id="trend-error-banner" style="display:none;"></div>

        <!-- Totals for selected range -->
        <div class="summary" id="trend-summary" style="display:none;">
            <div class="summary-card">
                <div class="label">新增用户</div>
                <div class="value" id="ts-users" style="color:#8b5cf6;">0</div>
            </div>
            <div class="summary-card">
                <div class="label">订单总数</div>
                <div class="value" id="ts-orders" style="color:#2563eb;">0</div>
            </div>
            <div class="summary-card">
                <div class="label">付费订单</div>
                <div class="value" id="ts-paid" style="color:#16a34a;">0</div>
            </div>
            <div class="summary-card">
                <div class="label">总营收 (¥)</div>
                <div class="value revenue" id="ts-revenue">0.00</div>
            </div>
        </div>

        <!-- Chart -->
        <div class="chart-card" id="trend-chart-card" style="display:none;">
            <div class="chart-legend" id="trend-legend"></div>
            <div style="position:relative;">
                <svg id="trend-svg"></svg>
                <div class="chart-tooltip" id="trend-tooltip"></div>
            </div>
        </div>

        <!-- Loading / Empty -->
        <div class="loading" id="trend-loading" style="display:none;">
            <div class="spinner"></div>
            <div>加载中</div>
        </div>
        <div class="table-wrapper" id="trend-empty" style="display:none;">
            <div class="empty">
                <div class="empty-icon">📉</div>
                <p>该时间范围暂无数据</p>
            </div>
        </div>
    </div>
    </div>

    <!-- ============ TAB: REWRITE EFFECT STATS ============ -->
    <div class="tab-content" id="content-stats">
    <div class="main">
        <div class="toolbar">
            <h2 style="font-size:1rem;font-weight:600;margin-right:16px;">📊 改写效果统计</h2>
            <span style="font-size:0.85rem;color:#64748b;">统计所选时间范围内的改写完成记录，改写后 AI 率小于 20% 视为达标</span>
        </div>
        <div class="toolbar">
            <label>时间范围：</label>
            <input type="date" id="stats-date-start">
            <span class="date-sep">至</span>
            <input type="date" id="stats-date-end">
            <button class="btn-query" onclick="loadStats()">查询</button>
            <button class="btn-preset" onclick="setStatsPreset('7days', this)">近7天</button>
            <button class="btn-preset" onclick="setStatsPreset('30days', this)">近30天</button>
            <button class="btn-preset" onclick="setStatsPreset('thisMonth', this)">本月</button>
            <button class="btn-preset" onclick="setStatsPreset('all', this)">全部</button>
        </div>

        <div class="error-banner" id="stats-error-banner" style="display:none;"></div>

        <div class="summary" id="stats-summary" style="display:none;">
            <div class="summary-card">
                <div class="label">改写样本</div>
                <div class="value" id="st-sample">0</div>
            </div>
            <div class="summary-card">
                <div class="label">达标 &lt;20%</div>
                <div class="value" id="st-below20">0</div>
            </div>
            <div class="summary-card">
                <div class="label">达标比例</div>
                <div class="value" id="st-ratio" style="color:#059669;">0%</div>
            </div>
            <div class="summary-card">
                <div class="label">平均降幅 (pp)</div>
                <div class="value" id="st-improve">0</div>
            </div>
            <div class="summary-card">
                <div class="label">改写后反而升高</div>
                <div class="value" id="st-worsened" style="color:#dc2626;">0</div>
            </div>
            <div class="summary-card">
                <div class="label">平均篇幅变动</div>
                <div class="value" id="st-word-change">0%</div>
            </div>
            <div class="summary-card">
                <div class="label">标题结构异常</div>
                <div class="value" id="st-heading-warning">0</div>
            </div>
            <div class="summary-card">
                <div class="label">平均处理耗时</div>
                <div class="value" id="st-duration">0s</div>
            </div>
            <div class="summary-card">
                <div class="label">用户反馈</div>
                <div class="value" id="st-feedback">0</div>
            </div>
            <div class="summary-card">
                <div class="label">外部实测 &lt;20%</div>
                <div class="value" id="st-external-ratio" style="color:#2563eb;">-</div>
            </div>
        </div>

        <div class="table-wrapper" id="stats-feedback-wrapper" style="display:none;margin-top:20px;">
            <div style="padding:16px 18px 8px;font-weight:600;">用户真实反馈</div>
            <div id="stats-feedback-summary" style="padding:0 18px 12px;color:#64748b;font-size:0.85rem;"></div>
            <table>
                <thead><tr>
                    <th>更新时间</th><th>订单</th><th>用户</th><th>反馈</th>
                    <th>外部检测</th><th>说明</th><th>允许联系</th><th>截图</th>
                </tr></thead>
                <tbody id="stats-feedback-body"></tbody>
            </table>
        </div>

        <div class="loading" id="stats-loading" style="display:none;">
            <div class="spinner"></div>
            <div>加载中</div>
        </div>
        <div class="table-wrapper" id="stats-empty" style="display:none;">
            <div class="empty">
                <div class="empty-icon">📊</div>
                <p>暂无改写完成记录</p>
            </div>
        </div>
    </div>
    </div>

    <!-- ============ TAB: ACTIVATION CODES ============ -->
    <div class="tab-content" id="content-activation">
    <div class="main">
        <div class="toolbar">
            <h2 style="font-size:1rem;font-weight:600;margin-right:16px;">🎯 兑换码管理</h2>
            <label>生成数量：</label>
            <input type="number" id="gen-count" value="10" min="1" max="100" style="width:70px;padding:8px 12px;border:1px solid #e2e8f0;border-radius:8px;font-size:0.9rem;">
            <label>每码词数：</label>
            <input type="number" id="gen-words" value="2000" min="100" max="100000" step="100" style="width:90px;padding:8px 12px;border:1px solid #e2e8f0;border-radius:8px;font-size:0.9rem;">
            <button class="btn-query" onclick="generateCodes()">生成兑换码</button>
            <span id="gen-result" style="font-size:0.85rem;color:#059669;margin-left:12px;"></span>
        </div>

        <!-- Activation stats -->
        <div class="summary" id="activation-summary">
            <div class="summary-card">
                <div class="label">总码数</div>
                <div class="value" id="ac-total">0</div>
            </div>
            <div class="summary-card">
                <div class="label">已使用</div>
                <div class="value" id="ac-used" style="color:#16a34a;">0</div>
            </div>
            <div class="summary-card">
                <div class="label">未使用</div>
                <div class="value" id="ac-unused" style="color:#4f46e5;">0</div>
            </div>
            <div class="summary-card">
                <div class="label">已兑换词数</div>
                <div class="value" id="ac-words" style="color:#059669;">0</div>
            </div>
        </div>

        <!-- Codes table -->
        <div class="table-wrapper">
            <div class="table-header">
                <h2>兑换码列表</h2>
                <span class="count-badge" id="ac-count-badge">0 条</span>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>兑换码</th>
                        <th>词数</th>
                        <th>状态</th>
                        <th>兑换用户</th>
                        <th>创建时间</th>
                        <th>兑换时间</th>
                    </tr>
                </thead>
                <tbody id="ac-tbody"></tbody>
            </table>
        </div>
    </div>
    </div>

    <!-- ============ TAB: USERS ============ -->
    <div class="tab-content" id="content-users">
    <div class="main">
        <!-- Search toolbar -->
        <div class="toolbar">
            <h2 style="font-size:1rem;font-weight:600;margin-right:16px;">👤 用户管理</h2>
            <label>邮箱搜索：</label>
            <input type="text" id="user-search" placeholder="输入邮箱或邮箱前缀..." style="padding:8px 12px;border:1px solid #e2e8f0;border-radius:8px;font-size:0.9rem;width:240px;" onkeyup="if(event.key==='Enter') loadUsers();">
            <button class="btn-query" onclick="loadUsers()">查询</button>
            <span id="users-result" style="font-size:0.85rem;color:#64748b;margin-left:12px;"></span>
        </div>

        <!-- User stats -->
        <div class="summary" id="users-summary">
            <div class="summary-card">
                <div class="label">总用户数</div>
                <div class="value" id="u-total">0</div>
            </div>
            <div class="summary-card">
                <div class="label">有余额用户</div>
                <div class="value" id="u-with-balance" style="color:#4f46e5;">0</div>
            </div>
            <div class="summary-card">
                <div class="label">总余额（词）</div>
                <div class="value" id="u-total-balance" style="color:#059669;">0</div>
            </div>
            <div class="summary-card">
                <div class="label">已消费（词）</div>
                <div class="value" id="u-total-spent" style="color:#ca8a04;">0</div>
            </div>
        </div>

        <!-- Users table -->
        <div class="table-wrapper">
            <div class="table-header">
                <h2>用户列表</h2>
                <span class="count-badge" id="users-count-badge">0 条</span>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>邮箱</th>
                        <th>余额（词）</th>
                        <th>累计充值</th>
                        <th>累计消费</th>
                        <th>订单数</th>
                        <th>扫码支付订单</th>
                        <th>注册时间</th>
                        <th>最后登录</th>
                    </tr>
                </thead>
                <tbody id="users-tbody"></tbody>
            </table>
        </div>
    </div>
    </div>

    <script>
        let currentPage = 1;
        let totalPages = 1;
        let expandedOrderId = null;
        let currentTab = 'orders';

        const STATUS_BADGE = {
            paid: 'badge-paid', pending: 'badge-pending',
            expired: 'badge-expired', failed: 'badge-failed',
            balance: 'badge-balance', free: 'badge-free'
        };
        const STATUS_LABEL = {
            paid: '已支付', pending: '待支付', expired: '已过期',
            failed: '支付失败', balance: '已扣余额', free: '无需支付'
        };
        const PAYMENT_METHOD_LABEL = {
            paid: '扫码充值', pending: '扫码充值', expired: '扫码充值',
            failed: '扫码充值', balance: '余额支付', free: '免费'
        };
        const ORDER_STATUS_BADGE = {
            completed: 'badge-completed', processing: 'badge-processing',
            pending: 'badge-pending', failed: 'badge-failed', expired: 'badge-expired',
            awaiting_balance: 'badge-balance'
        };
        const ORDER_STATUS_LABEL = {
            completed: '已完成', processing: '处理中', pending: '待处理',
            failed: '处理失败', expired: '已过期', awaiting_balance: '余额待补足'
        };
        const MODE_LABEL = {
            low: 'low', median: 'median', high: 'high', academic: 'median'
        };
        const DETECTOR_LABEL = {
            sapling: 'Sapling', originality: 'Originality', rule_based: '本地规则',
            sapling_mock: 'Sapling(测试)', originality_mock: 'Originality(测试)'
        };
        function modeLabel(m) { return MODE_LABEL[m] || m || '未知'; }
        function detectorLabel(d) { return DETECTOR_LABEL[d] || d || '未知'; }
        function aiRateCell(o) {
            if (o.original_score == null && o.rewritten_score == null) return '-';
            const orig = o.original_score != null ? o.original_score + '%' : '-';
            const rew = o.rewritten_score != null ? o.rewritten_score + '%' : '-';
            const color = rew !== '-' && parseFloat(rew) < 20 ? '#059669' : '';
            const style = 'font-size:0.82rem;white-space:nowrap;' + (color ? 'color:' + color + ';' : '');
            return `<span style="${style}">${orig} → ${rew}</span>`;
        }

        function fmtDate(d) {
            return d.toISOString().split('T')[0];
        }

        // Init: default to today
        const today = fmtDate(new Date());
        document.getElementById('date-start').value = today;
        document.getElementById('date-end').value = today;
        // 改写效果统计默认看全部数据
        document.getElementById('stats-date-start').value = '2026-01-01';
        document.getElementById('stats-date-end').value = today;

        function setPreset(type) {
            const now = new Date();
            let start, end;
            switch (type) {
                case 'today':
                    start = end = fmtDate(now);
                    break;
                case 'yesterday':
                    const y = new Date(now); y.setDate(y.getDate() - 1);
                    start = end = fmtDate(y);
                    break;
                case '7days':
                    start = new Date(now); start.setDate(start.getDate() - 6);
                    start = fmtDate(start); end = fmtDate(now);
                    break;
                case '30days':
                    start = new Date(now); start.setDate(start.getDate() - 29);
                    start = fmtDate(start); end = fmtDate(now);
                    break;
                case 'thisMonth':
                    start = new Date(now.getFullYear(), now.getMonth(), 1);
                    start = fmtDate(start); end = fmtDate(now);
                    break;
            }
            document.getElementById('date-start').value = start;
            document.getElementById('date-end').value = end;
            // Highlight active preset
            document.querySelectorAll('.btn-preset').forEach(b => b.classList.remove('active'));
            event.target.classList.add('active');
            loadOrders();
        }

        function applyFilter() {
            currentPage = 1;
            loadOrders();
        }

        function clearFilter() {
            ['f-payment', 'f-status', 'f-mode', 'f-backend'].forEach(id => {
                document.getElementById(id).value = '';
            });
            currentPage = 1;
            loadOrders();
        }

        function showError(msg) {
            const el = document.getElementById('error-banner');
            el.textContent = msg;
            el.style.display = 'block';
        }

        function hideError() {
            document.getElementById('error-banner').style.display = 'none';
        }

        async function loadOrders(page) {
            if (page !== undefined) currentPage = page;
            const start = document.getElementById('date-start').value;
            const end = document.getElementById('date-end').value;
            if (!start || !end) return;

            hideError();
            document.getElementById('loading').style.display = 'block';
            document.getElementById('table-wrapper').style.display = 'none';
            document.getElementById('summary').style.display = 'none';
            document.getElementById('empty-state').style.display = 'none';

            try {
                const qs = new URLSearchParams({
                    start: start, end: end, page: currentPage,
                    ps: document.getElementById('f-payment').value,
                    status: document.getElementById('f-status').value,
                    mode: document.getElementById('f-mode').value,
                    backend: document.getElementById('f-backend').value
                });
                const resp = await fetch('/admin/api/orders?' + qs.toString());
                if (!resp.ok) {
                    const data = await resp.json();
                    throw new Error(data.error || '请求失败');
                }
                const data = await resp.json();

                // Summary
                document.getElementById('stat-total').textContent = data.summary.total_orders;
                document.getElementById('stat-paid').textContent = data.summary.paid_orders;
                document.getElementById('stat-pending').textContent = data.summary.pending_orders;
                document.getElementById('stat-expired').textContent = data.summary.expired_orders;
                document.getElementById('stat-failed').textContent = data.summary.failed_orders;
                document.getElementById('stat-revenue').textContent = data.summary.total_revenue.toFixed(2);
                document.getElementById('summary').style.display = 'grid';

                // Table
                if (data.orders.length === 0) {
                    document.getElementById('empty-state').style.display = 'block';
                } else {
                    document.getElementById('count-badge').textContent = data.summary.total_orders + ' 条';
                    document.getElementById('table-wrapper').style.display = 'block';
                    renderOrders(data.orders);
                    currentPage = data.page;
                    totalPages = data.total_pages;
                    updatePagination();
                }
            } catch (e) {
                showError(e.message);
            } finally {
                document.getElementById('loading').style.display = 'none';
            }
        }

        function renderOrders(orders) {
            const tbody = document.getElementById('orders-tbody');
            let html = '';

            for (const o of orders) {
                const ps = o.payment_status || 'pending';
                const ss = o.status || 'pending';
                const paymentMethod = PAYMENT_METHOD_LABEL[ps] || '其他';
                let amountDisplay = `¥${(o.price || 0).toFixed(2)}`;
                if (ps === 'balance') {
                    amountDisplay = `消耗 ${o.balance_words_used || o.word_count || 0} 词`;
                } else if (ps === 'free') {
                    amountDisplay = '免费';
                }
                html += `<tr class="row-order" onclick="toggleDetail('${escapeHtml(o.order_id)}')" id="row-${escapeHtml(o.order_id)}">
                    <td style="font-family:monospace;font-size:0.78rem;">${escapeHtml(o.order_id)}</td>
                    <td>${escapeHtml(o.user_email || '游客')}</td>
                    <td style="font-family:monospace;font-size:0.75rem;color:#64748b;">${escapeHtml(o.original_format || 'txt')}</td>
                    <td>${o.word_count || '-'}</td>
                    <td>${amountDisplay}</td>
                    <td><span class="badge ${STATUS_BADGE[ps] || 'badge-pending'}">${paymentMethod}</span></td>
                    <td><span class="badge ${STATUS_BADGE[ps] || 'badge-pending'}">${STATUS_LABEL[ps] || ps}</span></td>
                    <td><span class="badge ${ORDER_STATUS_BADGE[ss] || 'badge-pending'}">${ORDER_STATUS_LABEL[ss] || ss}</span></td>
                    <td>${detectorLabel(o.detector_backend)}</td>
                    <td>${modeLabel(o.mode)}</td>
                    <td>${aiRateCell(o)}</td>
                    <td style="font-size:0.78rem;color:#64748b;">${formatTime(o.created_at)}</td>
                </tr>`;
                html += `<tr class="row-detail" id="detail-${escapeHtml(o.order_id)}" style="display:none;">
                    <td colspan="12">
                        <div class="detail-meta">
                            <span>文件: ${escapeHtml(o.original_filename || '-')}</span>
                            <span>改写方法: ${modeLabel(o.mode)}</span>
                            <span>检测方法: ${detectorLabel(o.detector_backend)}</span>
                            <span>改写后端: ${escapeHtml(o.humanizer_backend || '-')}</span>
                            <span>充值词数: ${o.recharge_words || '-'}</span>
                            <span>余额消耗: ${o.balance_words_used || '-'}</span>
                            <span>改写后词数: ${o.rewritten_word_count || '-'}</span>
                            <span>篇幅变化: ${o.word_count_change_ratio == null ? '-' : (o.word_count_change_ratio * 100).toFixed(1) + '%'}</span>
                            <span>标题结构: ${o.heading_count_changed ? '⚠️ 数量变化' : '正常'}</span>
                            <span>原始评分: ${o.original_score != null ? o.original_score + '%' : '-'}</span>
                            <span>改写评分: ${o.rewritten_score != null ? o.rewritten_score + '%' : '-'}</span>
                            <span>支付时间: ${o.paid_at ? formatTime(o.paid_at) : '-'}</span>
                            <span>交易号: ${escapeHtml(o.alipay_trade_no || '-')}</span>
                        </div>
                        <div class="detail-grid" style="margin-top:16px;">
                            <div class="detail-box">
                                <h4>📄 原始文本</h4>
                                <div class="text-content">${escapeHtml(o.original_text || '')}</div>
                            </div>
                            <div class="detail-box">
                                <h4>✨ 改写结果</h4>
                                <div class="text-content">${escapeHtml(o.rewritten_text || '（暂无）')}</div>
                            </div>
                        </div>
                    </td>
                </tr>`;
            }
            tbody.innerHTML = html;
        }

        function toggleDetail(orderId) {
            const detailRow = document.getElementById('detail-' + orderId);
            if (!detailRow) return;

            if (expandedOrderId === orderId) {
                detailRow.style.display = 'none';
                expandedOrderId = null;
            } else {
                if (expandedOrderId) {
                    const prev = document.getElementById('detail-' + expandedOrderId);
                    if (prev) prev.style.display = 'none';
                }
                detailRow.style.display = 'table-row';
                expandedOrderId = orderId;
            }
        }

        function updatePagination() {
            document.getElementById('pagination').style.display = totalPages > 1 ? 'flex' : 'none';
            document.getElementById('page-info').textContent = `第 ${currentPage} / ${totalPages} 页`;
            document.getElementById('btn-prev').disabled = currentPage <= 1;
            document.getElementById('btn-next').disabled = currentPage >= totalPages;
        }

        function goPage(delta) {
            const newPage = currentPage + delta;
            if (newPage >= 1 && newPage <= totalPages) {
                loadOrders(newPage);
            }
        }

        function escapeHtml(text) {
            if (!text) return '';
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        function formatTime(isoStr) {
            if (!isoStr) return '-';
            try {
                const d = new Date(isoStr);
                return d.toLocaleString('zh-CN', {
                    month: '2-digit', day: '2-digit',
                    hour: '2-digit', minute: '2-digit', second: '2-digit'
                });
            } catch (e) { return isoStr; }
        }

        // Load on page ready
        loadOrders();
    </script>

    <script>
    /* ========== TAB SWITCHING ========== */
    function switchTab(tab) {
        currentTab = tab;
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        document.getElementById('tab-' + tab).classList.add('active');
        document.getElementById('content-' + tab).classList.add('active');
        if (tab === 'activation') loadActivationCodes();
        if (tab === 'users') loadUsers();
        if (tab === 'trends') loadTrends();
        if (tab === 'stats') loadStats();
    }

    /* ========== REWRITE EFFECT STATS ========== */
    async function loadStats() {
        const loading = document.getElementById('stats-loading');
        const summary = document.getElementById('stats-summary');
        const empty = document.getElementById('stats-empty');
        document.getElementById('stats-error-banner').style.display = 'none';

        summary.style.display = 'none';
        empty.style.display = 'none';
        document.getElementById('stats-feedback-wrapper').style.display = 'none';
        loading.style.display = 'block';

        const start = document.getElementById('stats-date-start').value;
        const end = document.getElementById('stats-date-end').value;
        if (!start || !end) { renderStatsError('请选择时间范围'); return; }

        let data;
        try {
            const resp = await fetch(
                `/admin/api/rewrite-stats?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`
            );
            data = await resp.json();
        } catch (e) {
            renderStatsError('加载失败: ' + e.message);
            return;
        }
        loading.style.display = 'none';
        if (data.error) { renderStatsError(data.error); return; }

        document.getElementById('st-sample').textContent = data.sample_count.toLocaleString();
        document.getElementById('st-below20').textContent = data.below20_count.toLocaleString();
        document.getElementById('st-ratio').textContent = (data.below20_ratio * 100).toFixed(1) + '%';
        document.getElementById('st-improve').textContent = data.avg_improvement.toFixed(1);
        document.getElementById('st-worsened').textContent =
            data.worsened_count.toLocaleString() + ' (' + (data.worsened_ratio * 100).toFixed(1) + '%)';
        document.getElementById('st-word-change').textContent =
            (data.avg_abs_word_change_ratio * 100).toFixed(1) + '%';
        document.getElementById('st-heading-warning').textContent =
            data.heading_warning_count.toLocaleString();
        document.getElementById('st-duration').textContent =
            data.avg_processing_seconds >= 60
                ? (data.avg_processing_seconds / 60).toFixed(1) + 'm'
                : data.avg_processing_seconds.toFixed(0) + 's';
        document.getElementById('st-feedback').textContent = data.feedback_count.toLocaleString();
        document.getElementById('st-external-ratio').textContent = data.external_score_count
            ? (data.external_below20_ratio * 100).toFixed(1) + '% (' + data.external_score_count + '份)'
            : '-';
        summary.style.display = 'grid';

        renderFeedbackStats(data);
        if (data.sample_count === 0 && data.feedback_count === 0) { empty.style.display = 'block'; }
    }

    function setStatsPreset(type, button) {
        const today = fmtDate(new Date());
        const startEl = document.getElementById('stats-date-start');
        const endEl = document.getElementById('stats-date-end');
        if (type === 'all') {
            startEl.value = '2026-01-01';
            endEl.value = today;
        } else if (type === '7days') {
            const s = new Date(); s.setDate(s.getDate() - 6);
            startEl.value = fmtDate(s); endEl.value = today;
        } else if (type === '30days') {
            const s = new Date(); s.setDate(s.getDate() - 29);
            startEl.value = fmtDate(s); endEl.value = today;
        } else if (type === 'thisMonth') {
            const now = new Date();
            startEl.value = fmtDate(new Date(now.getFullYear(), now.getMonth(), 1));
            endEl.value = today;
        }
        document.querySelectorAll('#content-stats .btn-preset').forEach(b => b.classList.remove('active'));
        if (button) button.classList.add('active');
        loadStats();
    }

    function renderFeedbackStats(data) {
        const wrapper = document.getElementById('stats-feedback-wrapper');
        const tbody = document.getElementById('stats-feedback-body');
        const summaryEl = document.getElementById('stats-feedback-summary');
        const labels = {
            satisfied: '效果符合预期', high_ai_score: 'AI率仍高',
            content_disorder: '内容/结构错乱', meaning_changed: '原意改变',
            details_lost: '标题/数据/术语丢失', other: '其他问题'
        };
        const issueSummary = Object.entries(data.feedback_issue_counts || {})
            .map(([key, count]) => `${labels[key] || key} ${count}`)
            .join(' · ');
        summaryEl.textContent = issueSummary || '暂无分类数据';
        const rows = data.recent_feedback || [];
        tbody.innerHTML = rows.map(item => {
            const external = item.external_score == null
                ? escapeHtml(item.detector_platform || '-')
                : `${escapeHtml(item.detector_platform || '未填写平台')} ${item.external_score}%`;
            const screenshot = item.screenshot_url
                ? `<a href="${escapeHtml(item.screenshot_url)}" target="_blank" rel="noopener">查看</a>`
                : '-';
            return `<tr>
                <td>${formatTime(item.updated_at)}</td>
                <td style="font-family:monospace;font-size:0.75rem;">${escapeHtml(item.order_id)}</td>
                <td>${escapeHtml(item.user_email || '-')}</td>
                <td>${escapeHtml(labels[item.issue_type] || item.issue_type)}</td>
                <td>${external}</td>
                <td style="max-width:280px;white-space:normal;">${escapeHtml(item.comment || '-')}</td>
                <td>${item.contact_allowed ? '是' : '否'}</td>
                <td>${screenshot}</td>
            </tr>`;
        }).join('');
        wrapper.style.display = rows.length ? 'block' : 'none';
    }

    function renderStatsError(msg) {
        document.getElementById('stats-loading').style.display = 'none';
        const banner = document.getElementById('stats-error-banner');
        banner.textContent = msg;
        banner.style.display = 'block';
    }

    /* ========== ACTIVATION CODES ========== */
    async function loadActivationCodes() {
        try {
            const resp = await fetch('/admin/api/activation-codes');
            const data = await resp.json();
            if (data.error) { showError(data.error); return; }

            // Stats
            document.getElementById('ac-total').textContent = data.stats.total;
            document.getElementById('ac-used').textContent = data.stats.used;
            document.getElementById('ac-unused').textContent = data.stats.unused;
            document.getElementById('ac-words').textContent = data.stats.total_redeemed_words;
            document.getElementById('ac-count-badge').textContent = data.stats.total + ' 条';

            // Table
            const tbody = document.getElementById('ac-tbody');
            if (data.codes.length === 0) {
                tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:40px;color:#94a3b8;">暂无兑换码</td></tr>';
                return;
            }
            tbody.innerHTML = data.codes.map(c => {
                const statusHtml = c.status === 'redeemed'
                    ? '<span class="badge badge-paid">已使用</span>'
                    : '<span class="badge badge-pending">未使用</span>';
                return `<tr>
                    <td style="font-family:monospace;font-weight:600;">${escapeHtml(c.code)}</td>
                    <td>${c.word_quota}</td>
                    <td>${statusHtml}</td>
                    <td>${escapeHtml(c.redeemed_by_email || '-')}</td>
                    <td style="font-size:0.78rem;color:#64748b;">${formatTime(c.created_at)}</td>
                    <td style="font-size:0.78rem;color:#64748b;">${c.redeemed_at ? formatTime(c.redeemed_at) : '-'}</td>
                </tr>`;
            }).join('');
        } catch (e) {
            showError('加载兑换码失败: ' + e.message);
        }
    }

    async function generateCodes() {
        const count = parseInt(document.getElementById('gen-count').value) || 10;
        const wordQuota = parseInt(document.getElementById('gen-words').value) || 2000;
        const btn = event.target;
        const resultEl = document.getElementById('gen-result');

        btn.disabled = true;
        btn.textContent = '生成中...';
        resultEl.textContent = '';

        try {
            const resp = await fetch('/admin/api/activation-codes', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ count, word_quota: wordQuota })
            });
            const data = await resp.json();
            if (data.error) { showError(data.error); return; }

            resultEl.textContent = `✅ 已生成 ${data.count} 个兑换码，每码 ${data.word_quota} 词`;
            // Show first few codes in result
            const codes = data.codes.slice(0, 3).map(c => c.code).join(', ');
            if (data.count > 3) {
                resultEl.textContent += `（${codes}... 等 ${data.count} 个）`;
            } else {
                resultEl.textContent += `（${codes}）`;
            }
            loadActivationCodes();
        } catch (e) {
            showError('生成失败: ' + e.message);
        } finally {
            btn.disabled = false;
            btn.textContent = '生成兑换码';
        }
    }

    /* ========== USERS ========== */
    async function loadUsers() {
        const search = document.getElementById('user-search').value.trim();
        const resultEl = document.getElementById('users-result');
        resultEl.textContent = '加载中...';

        try {
            const url = '/admin/api/users' + (search ? `?search=${encodeURIComponent(search)}` : '');
            const resp = await fetch(url);
            const data = await resp.json();
            if (data.error) { showError(data.error); resultEl.textContent = ''; return; }

            // Stats
            document.getElementById('u-total').textContent = data.stats.total;
            document.getElementById('u-with-balance').textContent = data.stats.with_balance || 0;
            document.getElementById('u-total-balance').textContent = (data.stats.total_balance || 0).toLocaleString();
            document.getElementById('u-total-spent').textContent = (data.stats.total_spent || 0).toLocaleString();
            document.getElementById('users-count-badge').textContent = data.users.length + ' 条';
            resultEl.textContent = `共 ${data.users.length} 个用户`;

            // Table
            const tbody = document.getElementById('users-tbody');
            if (data.users.length === 0) {
                tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;padding:40px;color:#94a3b8;">暂无用户</td></tr>';
                return;
            }
            tbody.innerHTML = data.users.map(u => {
                const balance = u.word_balance || 0;
                const balanceColor = balance > 0 ? '#059669' : '#94a3b8';
                return `<tr>
                    <td>${u.id}</td>
                    <td style="font-family:monospace;font-size:0.85rem;">${escapeHtml(u.email)}</td>
                    <td style="font-weight:600;color:${balanceColor};">${balance.toLocaleString()}</td>
                    <td style="color:#4f46e5;">+${(u.total_recharged || 0).toLocaleString()}</td>
                    <td style="color:#ca8a04;">-${(u.total_spent || 0).toLocaleString()}</td>
                    <td>${u.order_count}</td>
                    <td>${u.paid_count}</td>
                    <td style="font-size:0.78rem;color:#64748b;">${formatTime(u.created_at)}</td>
                    <td style="font-size:0.78rem;color:#64748b;">${u.last_login_at ? formatTime(u.last_login_at) : '<span style="color:#cbd5e1;">从未登录</span>'}</td>
                </tr>`;
            }).join('');
        } catch (e) {
            showError('加载用户失败: ' + e.message);
            document.getElementById('users-result').textContent = '';
        }
    }

    /* ========== TRENDS ========== */
    const TREND_METRICS = {
        new_users:   { label: '每日用户数', color: '#8b5cf6', axis: 'left' },
        orders:      { label: '订单量', color: '#2563eb', axis: 'left' },
        paid_orders: { label: '付费订单量', color: '#16a34a', axis: 'left' },
        revenue:     { label: '营收额(¥)', color: '#f59e0b', axis: 'right' }
    };
    let trendData = null;

    function getSelectedMetrics() {
        return Object.keys(TREND_METRICS).filter(
            k => document.getElementById('metric-' + k).checked
        );
    }

    function initTrendDates() {
        const now = new Date();
        const start = new Date(now);
        start.setDate(start.getDate() - 6);
        document.getElementById('trend-date-start').value = fmtDate(start);
        document.getElementById('trend-date-end').value = fmtDate(now);
        const firstPreset = document.querySelector('#content-trends .btn-preset');
        if (firstPreset) firstPreset.classList.add('active');
    }

    function setTrendPreset(type) {
        const now = new Date();
        let start;
        switch (type) {
            case '7days':
                start = new Date(now); start.setDate(start.getDate() - 6); break;
            case '30days':
                start = new Date(now); start.setDate(start.getDate() - 29); break;
            case '90days':
                start = new Date(now); start.setDate(start.getDate() - 89); break;
            case 'thisMonth':
                start = new Date(now.getFullYear(), now.getMonth(), 1); break;
        }
        document.getElementById('trend-date-start').value = fmtDate(start);
        document.getElementById('trend-date-end').value = fmtDate(now);
        document.querySelectorAll('#content-trends .btn-preset').forEach(b => b.classList.remove('active'));
        event.target.classList.add('active');
        loadTrends();
    }

    async function loadTrends() {
        const start = document.getElementById('trend-date-start').value;
        const end = document.getElementById('trend-date-end').value;
        if (!start || !end) return;

        const errEl = document.getElementById('trend-error-banner');
        errEl.style.display = 'none';
        document.getElementById('trend-loading').style.display = 'block';
        document.getElementById('trend-chart-card').style.display = 'none';
        document.getElementById('trend-summary').style.display = 'none';
        document.getElementById('trend-empty').style.display = 'none';

        try {
            const resp = await fetch(
                '/admin/api/trends?start=' + encodeURIComponent(start) + '&end=' + encodeURIComponent(end)
            );
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.error || '请求失败');
            trendData = data;

            document.getElementById('ts-users').textContent = data.totals.new_users.toLocaleString();
            document.getElementById('ts-orders').textContent = data.totals.orders.toLocaleString();
            document.getElementById('ts-paid').textContent = data.totals.paid_orders.toLocaleString();
            document.getElementById('ts-revenue').textContent = data.totals.revenue.toFixed(2);
            document.getElementById('trend-summary').style.display = 'grid';

            if (!data.days.length) {
                document.getElementById('trend-empty').style.display = 'block';
            } else {
                document.getElementById('trend-chart-card').style.display = 'block';
                renderTrendChart();
            }
        } catch (e) {
            errEl.textContent = e.message;
            errEl.style.display = 'block';
        } finally {
            document.getElementById('trend-loading').style.display = 'none';
        }
    }

    function toggleMetric(key) {
        const cb = document.getElementById('metric-' + key);
        cb.checked = !cb.checked;
        renderTrendChart();
    }

    function fmtAxisNum(v) {
        v = Number(v) || 0;
        if (v >= 10000) return (v / 10000).toFixed(v % 10000 ? 1 : 0) + 'w';
        if (v >= 1000) return (v / 1000).toFixed(v % 1000 ? 1 : 0) + 'k';
        return String(Math.round(v * 100) / 100);
    }

    function renderTrendChart() {
        const data = trendData;
        if (!data) return;
        const metrics = getSelectedMetrics();

        // Legend (click to toggle, synced with checkboxes)
        document.getElementById('trend-legend').innerHTML = Object.keys(TREND_METRICS).map(k => {
            const m = TREND_METRICS[k];
            const on = metrics.includes(k);
            return '<span class="legend-item' + (on ? '' : ' off') + '" onclick="toggleMetric(\\'' + k + '\\')">'
                + '<span class="dot" style="background:' + m.color + ';"></span>' + m.label + '</span>';
        }).join('');

        const svg = document.getElementById('trend-svg');
        const tooltip = document.getElementById('trend-tooltip');
        tooltip.style.display = 'none';

        if (!metrics.length) {
            svg.innerHTML = '<text x="480" y="190" text-anchor="middle" fill="#94a3b8" font-size="14">请至少选择一个指标</text>';
            return;
        }

        const days = data.days;
        const n = days.length;
        const W = 960, H = 380, padL = 60, padR = 64, padT = 24, padB = 42;
        const innerW = W - padL - padR, innerH = H - padT - padB;

        // Round axis max up to a nice number
        function niceMax(v) {
            if (v <= 0) return 4;
            const p = Math.pow(10, Math.floor(Math.log10(v)));
            for (const mul of [1, 2, 4, 5, 10]) { if (mul * p >= v) return mul * p; }
            return 10 * p;
        }
        const leftKeys = metrics.filter(k => TREND_METRICS[k].axis === 'left');
        const rightKeys = metrics.filter(k => TREND_METRICS[k].axis === 'right');
        const maxOf = keys => Math.max(...keys.flatMap(k => data.series[k]), 0);
        const leftMax = niceMax(maxOf(leftKeys));
        const rightMax = niceMax(maxOf(rightKeys));

        const xAt = i => n === 1 ? padL + innerW / 2 : padL + innerW * i / (n - 1);
        const yAt = (v, max) => padT + innerH * (1 - v / max);

        let g = '';
        // Gridlines + dual y-axis labels
        const TICKS = 4;
        for (let t = 0; t <= TICKS; t++) {
            const frac = t / TICKS;
            const y = padT + innerH * (1 - frac);
            g += '<line x1="' + padL + '" y1="' + y + '" x2="' + (W - padR) + '" y2="' + y + '" stroke="#eef2f7" stroke-width="1"/>';
            g += '<text x="' + (padL - 8) + '" y="' + (y + 4) + '" text-anchor="end" fill="#94a3b8" font-size="11">' + fmtAxisNum(leftMax * frac) + '</text>';
            if (rightKeys.length) {
                g += '<text x="' + (W - padR + 8) + '" y="' + (y + 4) + '" text-anchor="start" fill="#94a3b8" font-size="11">' + fmtAxisNum(rightMax * frac) + '</text>';
            }
        }
        g += '<line x1="' + padL + '" y1="' + (padT + innerH) + '" x2="' + (W - padR) + '" y2="' + (padT + innerH) + '" stroke="#cbd5e1" stroke-width="1"/>';

        // X labels (thin out on long ranges)
        const step = Math.max(1, Math.ceil(n / 8));
        for (let i = 0; i < n; i += step) {
            g += '<text x="' + xAt(i) + '" y="' + (H - 14) + '" text-anchor="middle" fill="#94a3b8" font-size="11">' + days[i].slice(5) + '</text>';
        }
        if ((n - 1) % step !== 0) {
            g += '<text x="' + xAt(n - 1) + '" y="' + (H - 14) + '" text-anchor="middle" fill="#94a3b8" font-size="11">' + days[n - 1].slice(5) + '</text>';
        }

        // Series lines + dots
        for (const k of metrics) {
            const m = TREND_METRICS[k];
            const vals = data.series[k];
            const max = m.axis === 'left' ? leftMax : rightMax;
            const pts = vals.map((v, i) => [xAt(i), yAt(v, max)]);
            if (n > 1) {
                g += '<polyline fill="none" stroke="' + m.color + '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" points="'
                    + pts.map(p => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ') + '"/>';
            }
            g += pts.map(p => '<circle cx="' + p[0].toFixed(1) + '" cy="' + p[1].toFixed(1) + '" r="2.5" fill="' + m.color + '"/>').join('');
        }

        // Hover guide line
        g += '<line id="hover-line" x1="0" y1="' + padT + '" x2="0" y2="' + (padT + innerH) + '" stroke="#94a3b8" stroke-width="1" stroke-dasharray="4 3" visibility="hidden"/>';

        svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
        svg.innerHTML = g;

        svg.onmousemove = function(e) {
            const rect = svg.getBoundingClientRect();
            const px = (e.clientX - rect.left) / rect.width * W;
            let idx = Math.round((px - padL) / innerW * (n - 1));
            idx = Math.max(0, Math.min(n - 1, idx));
            const gx = xAt(idx);
            const hl = document.getElementById('hover-line');
            hl.setAttribute('x1', gx);
            hl.setAttribute('x2', gx);
            hl.setAttribute('visibility', 'visible');

            let html = '<div style="font-weight:700;margin-bottom:4px;">' + days[idx] + '</div>';
            for (const k of metrics) {
                const m = TREND_METRICS[k];
                const v = data.series[k][idx];
                const shown = k === 'revenue'
                    ? '¥' + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
                    : Number(v).toLocaleString();
                html += '<div><span class="dot" style="background:' + m.color + ';margin-right:6px;"></span>' + m.label + '：' + shown + '</div>';
            }
            tooltip.innerHTML = html;
            tooltip.style.display = 'block';

            const wrapRect = tooltip.parentElement.getBoundingClientRect();
            let tx = e.clientX - wrapRect.left + 14;
            if (tx + 200 > wrapRect.width) tx = tx - 214;
            tooltip.style.left = tx + 'px';
            tooltip.style.top = (e.clientY - wrapRect.top - 10) + 'px';
        };
        svg.onmouseleave = function() {
            tooltip.style.display = 'none';
            const hl = document.getElementById('hover-line');
            if (hl) hl.setAttribute('visibility', 'hidden');
        };
    }

    initTrendDates();
    </script>
</body>
</html>"""


# ============================================================
#  Main
# ============================================================
if __name__ == '__main__':
    print(f"\n  🔐 Admin dashboard → http://127.0.0.1:{ADMIN_PORT}/admin")
    print(f"  📁 Database: {DB_PATH}")
    print(f"  🔑 Login:  http://127.0.0.1:{ADMIN_PORT}/admin/login\n")
    admin_app.run(host='0.0.0.0', port=ADMIN_PORT, debug=True)
