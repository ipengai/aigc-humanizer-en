#!/usr/bin/env python3
"""
Data models for AI Humanizer.
SQLite database operations using sqlite3 module.
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from werkzeug.security import generate_password_hash, check_password_hash
from config import PROJ_ROOT, SIGNUP_BONUS_WORDS

DB_DIR = os.path.join(PROJ_ROOT, 'instance')
DB_PATH = os.path.join(DB_DIR, 'aigc_humanizer.db')


def get_connection():
    """Get a new SQLite database connection."""
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Initialize database and create all tables."""
    conn = get_connection()
    try:
        User.init_table(conn)
        Order.init_table(conn)
        ActivationCode.init_table(conn)
        BalanceTransaction.init_table(conn)
        RewriteFeedback.init_table(conn)
    finally:
        conn.close()


class User:
    """User model — class methods for database operations."""

    @classmethod
    def init_table(cls, conn):
        """Create the users table if it does not exist."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()

        # Add columns to existing table (backward compatibility)
        cursor = conn.execute("PRAGMA table_info(users)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'word_balance' not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN word_balance INTEGER DEFAULT 0")
        if 'last_login_at' not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN last_login_at TEXT")
        conn.commit()

    @classmethod
    def create(cls, conn, email, password):
        """Create a new user. Password is hashed via werkzeug.security.
        注册即赠送 SIGNUP_BONUS_WORDS 词数余额。
        """
        password_hash = generate_password_hash(password, method='pbkdf2:sha256')
        created_at = datetime.now(timezone.utc).isoformat()
        cursor = conn.execute(
            "INSERT INTO users (email, password_hash, created_at, word_balance) "
            "VALUES (?, ?, ?, ?)",
            (email, password_hash, created_at, SIGNUP_BONUS_WORDS)
        )
        conn.commit()
        return cls.get_by_id(conn, cursor.lastrowid)

    @classmethod
    def get_by_email(cls, conn, email):
        """Look up a user by email. Returns dict or None."""
        cursor = conn.execute("SELECT * FROM users WHERE email = ?", (email,))
        row = cursor.fetchone()
        return dict(row) if row else None

    @classmethod
    def get_by_id(cls, conn, user_id):
        """Look up a user by primary key. Returns dict or None."""
        cursor = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    @classmethod
    def verify_password(cls, conn, email, password):
        """Verify password for a given email. Returns user dict or None."""
        cursor = conn.execute("SELECT * FROM users WHERE email = ?", (email,))
        row = cursor.fetchone()
        if row and check_password_hash(row['password_hash'], password):
            return dict(row)
        return None

    # ========== Word balance methods ==========

    @classmethod
    def get_balance(cls, conn, user_id):
        """Get user's current word balance. Returns int."""
        row = conn.execute(
            "SELECT word_balance FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return row['word_balance'] if row else 0

    @classmethod
    def add_balance(cls, conn, user_id, words):
        """Add word balance to a user's account."""
        conn.execute(
            "UPDATE users SET word_balance = word_balance + ? WHERE id = ?",
            (words, user_id)
        )

    @classmethod
    def deduct_balance(cls, conn, user_id, words):
        """Deduct balance without committing; the caller owns the transaction."""
        cursor = conn.execute(
            "UPDATE users SET word_balance = word_balance - ? WHERE id = ? AND word_balance >= ?",
            (words, user_id, words)
        )
        if cursor.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT word_balance FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return row['word_balance'] if row else 0


class Order:
    """Order model — class methods for database operations."""

    @classmethod
    def init_table(cls, conn):
        """Create the orders table if it does not exist. Add payment columns if missing."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                order_id TEXT UNIQUE NOT NULL,
                original_text TEXT NOT NULL,
                paragraphs TEXT,
                rewritten_text TEXT,
                original_format TEXT DEFAULT 'txt',
                original_filename TEXT,
                word_count INTEGER,
                price REAL,
                mode TEXT DEFAULT 'low',
                detector_backend TEXT,
                humanizer_backend TEXT,
                rewrite_method TEXT,
                rewrite_provider TEXT,
                rewrite_model TEXT,
                humanizer_primary TEXT,
                humanizer_fallback TEXT,
                fallback_used INTEGER DEFAULT 0,
                fallback_block_count INTEGER DEFAULT 0,
                rewrite_block_count INTEGER,
                rewrite_pipeline_version TEXT,
                original_score REAL,
                rewritten_score REAL,
                input_type TEXT,
                traffic_source TEXT,
                utm_source TEXT,
                utm_medium TEXT,
                utm_campaign TEXT,
                referrer_domain TEXT,
                original_paragraph_count INTEGER,
                rewritten_paragraph_count INTEGER,
                original_table_count INTEGER,
                original_reference_count INTEGER,
                protected_paragraph_count INTEGER,
                rewritten_word_count INTEGER,
                word_count_change_ratio REAL,
                original_heading_count INTEGER,
                rewritten_heading_count INTEGER,
                heading_count_changed INTEGER DEFAULT 0,
                processing_duration_ms INTEGER,
                failure_stage TEXT,
                failure_code TEXT,
                completed_at TEXT,
                status TEXT DEFAULT 'pending',
                payment_status TEXT DEFAULT 'pending',
                alipay_trade_no TEXT,
                alipay_amount REAL,
                alipay_qr_code TEXT,
                paid_at TEXT,
                source_file_key TEXT,
                output_file_key TEXT,
                document_status TEXT DEFAULT 'not_applicable',
                document_error TEXT,
                document_updated_at TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        conn.commit()

        # Add payment columns to existing table (backward compatibility)
        cursor = conn.execute("PRAGMA table_info(orders)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'payment_status' not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN payment_status TEXT DEFAULT 'pending'")
        if 'alipay_trade_no' not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN alipay_trade_no TEXT")
        if 'alipay_amount' not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN alipay_amount REAL")
        if 'alipay_qr_code' not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN alipay_qr_code TEXT")
        if 'paid_at' not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN paid_at TEXT")
        if 'recharge_words' not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN recharge_words INTEGER DEFAULT 0")
        if 'balance_words_used' not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN balance_words_used INTEGER DEFAULT 0")
        if 'balance_after' not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN balance_after INTEGER")
        if 'paragraphs' not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN paragraphs TEXT")
        if 'rewritten_paragraphs' not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN rewritten_paragraphs TEXT")
        if 'progress_stage' not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN progress_stage TEXT")
        if 'progress_block' not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN progress_block INTEGER")
        if 'progress_total_blocks' not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN progress_total_blocks INTEGER")
        if 'progress_message' not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN progress_message TEXT")
        if 'progress_updated_at' not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN progress_updated_at TEXT")
        if 'source_file_key' not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN source_file_key TEXT")
        if 'output_file_key' not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN output_file_key TEXT")
        if 'document_status' not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN document_status TEXT DEFAULT 'not_applicable'")
        if 'document_error' not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN document_error TEXT")
        if 'document_updated_at' not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN document_updated_at TEXT")
        if 'detector_backend' not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN detector_backend TEXT")
        if 'humanizer_backend' not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN humanizer_backend TEXT")
        if 'rewritten_word_count' not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN rewritten_word_count INTEGER")
        if 'word_count_change_ratio' not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN word_count_change_ratio REAL")
        if 'original_heading_count' not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN original_heading_count INTEGER")
        if 'rewritten_heading_count' not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN rewritten_heading_count INTEGER")
        if 'heading_count_changed' not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN heading_count_changed INTEGER DEFAULT 0")
        if 'completed_at' not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN completed_at TEXT")
        analysis_columns = {
            'rewrite_method': 'TEXT',
            'rewrite_provider': 'TEXT',
            'rewrite_model': 'TEXT',
            'humanizer_primary': 'TEXT',
            'humanizer_fallback': 'TEXT',
            'fallback_used': 'INTEGER DEFAULT 0',
            'fallback_block_count': 'INTEGER DEFAULT 0',
            'rewrite_block_count': 'INTEGER',
            'rewrite_pipeline_version': 'TEXT',
            'input_type': 'TEXT',
            'traffic_source': 'TEXT',
            'utm_source': 'TEXT',
            'utm_medium': 'TEXT',
            'utm_campaign': 'TEXT',
            'referrer_domain': 'TEXT',
            'original_paragraph_count': 'INTEGER',
            'rewritten_paragraph_count': 'INTEGER',
            'original_table_count': 'INTEGER',
            'original_reference_count': 'INTEGER',
            'protected_paragraph_count': 'INTEGER',
            'processing_duration_ms': 'INTEGER',
            'failure_stage': 'TEXT',
            'failure_code': 'TEXT',
        }
        for name, column_type in analysis_columns.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE orders ADD COLUMN {name} {column_type}")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_alipay_trade_no "
            "ON orders(alipay_trade_no) WHERE alipay_trade_no IS NOT NULL"
        )
        conn.commit()



    @classmethod
    def _apply_input_analysis_context(cls, conn, order_id, paragraphs,
                                      original_filename, analysis_context=None):
        """Persist input, structure, and acquisition dimensions at order creation."""
        analysis_context = analysis_context or {}
        paragraph_items = [
            item for item in (paragraphs or [])
            if isinstance(item, dict) and item.get('text')
        ]
        original_paragraph_count = len(paragraph_items)
        original_heading_count = sum(
            1 for item in paragraph_items if item.get('is_heading')
        )
        original_reference_count = sum(
            1 for item in paragraph_items if item.get('is_reference')
        )
        original_table_count = sum(
            1 for item in (paragraphs or [])
            if isinstance(item, dict) and 'table' in item
        )
        input_type = analysis_context.get('input_type') or (
            'upload' if original_filename else 'paste'
        )
        conn.execute(
            """UPDATE orders
               SET input_type = ?, traffic_source = ?, utm_source = ?,
                   utm_medium = ?, utm_campaign = ?, referrer_domain = ?,
                   original_paragraph_count = ?, original_heading_count = ?,
                   original_table_count = ?, original_reference_count = ?
               WHERE order_id = ?""",
            (
                input_type,
                analysis_context.get('traffic_source') or 'direct',
                analysis_context.get('utm_source'),
                analysis_context.get('utm_medium'),
                analysis_context.get('utm_campaign'),
                analysis_context.get('referrer_domain'),
                original_paragraph_count,
                original_heading_count,
                original_table_count,
                original_reference_count,
                order_id,
            )
        )

    @classmethod
    def create_balance_order(cls, conn, user_id, order_id, original_text, rewritten_text,
                              original_format, original_filename, word_count, price, mode,
                              original_score, rewritten_score, rewritten_paragraphs=None):
        """Create a balance-deducted rewrite order record (payment_status='balance').

        rewritten_paragraphs: 可选，改写后的结构化段落列表（list[dict]，含
        text/is_heading/heading_level/style），JSON 序列化后存入
        orders.rewritten_paragraphs，供下载 Word 时重建标题格式。
        """
        import json
        created_at = datetime.now(timezone.utc).isoformat()
        expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        rewritten_paragraphs_json = json.dumps(
            rewritten_paragraphs, ensure_ascii=False
        ) if rewritten_paragraphs else None
        conn.execute(
            """INSERT INTO orders
               (user_id, order_id, original_text, rewritten_text,
                original_format, original_filename, word_count, price, mode,
                original_score, rewritten_score, status, payment_status,
                balance_words_used, balance_after, rewritten_paragraphs,
                created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', 'balance', ?, ?, ?, ?, ?)""",
            (user_id, order_id, original_text, rewritten_text,
             original_format, original_filename, word_count, price, mode,
             original_score, rewritten_score, word_count,
             User.get_balance(conn, user_id), rewritten_paragraphs_json,
             created_at, expires_at)
        )
        conn.commit()

    @classmethod
    def create_processing_order(cls, conn, user_id, order_id, original_text,
                                original_format, original_filename, word_count,
                                price, mode, paragraphs=None, source_file_key=None,
                                analysis_context=None):
        """Create a balance-deducted order in 'processing' status (async rewrite).

        与 create_balance_order 的区别：直接改写现改为异步（后台线程改写），
        订单先以 status='processing' 入库，改写完成后由 update_result 置为 'completed'。
        """
        import json
        created_at = datetime.now(timezone.utc).isoformat()
        expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        paragraphs_json = json.dumps(paragraphs, ensure_ascii=False) if paragraphs else None
        conn.execute(
            """INSERT INTO orders
               (user_id, order_id, original_text, paragraphs, rewritten_text,
                original_format, original_filename, word_count, price, mode,
                original_score, rewritten_score, status, payment_status,
                balance_words_used, balance_after, source_file_key,
                document_status, created_at, expires_at)
               VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, NULL, 'processing', 'balance', ?, ?, ?, ?, ?, ?)""",
            (user_id, order_id, original_text, paragraphs_json,
             original_format, original_filename, word_count, price, mode,
             word_count, User.get_balance(conn, user_id), source_file_key,
             'pending' if source_file_key else 'not_applicable', created_at, expires_at)
        )
        cls._apply_input_analysis_context(
            conn, order_id, paragraphs, original_filename, analysis_context
        )
        conn.commit()
        return cls.get_by_order_id(conn, order_id)

    @classmethod
    def get_by_user_id(cls, conn, user_id, page=1, per_page=10,
                       payment_status=None, history_only=False):
        """Get paginated orders for a user. Returns (orders_list, total_count)."""
        where = "user_id = ?"
        params = [user_id]
        if payment_status:
            where += " AND payment_status = ?"
            params.append(payment_status)
        if history_only:
            where += " AND status IN ('completed', 'processing', 'failed', 'awaiting_balance')"

        count_row = conn.execute(
            f"SELECT COUNT(*) as total FROM orders WHERE {where}", params
        ).fetchone()
        total = count_row['total'] if count_row else 0

        offset = (page - 1) * per_page
        cursor = conn.execute(
            f"SELECT * FROM orders WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [per_page, offset]
        )
        orders = [dict(row) for row in cursor.fetchall()]
        return orders, total

    @classmethod
    def get_by_order_id(cls, conn, order_id):
        """Look up an order by order_id. Returns dict or None."""
        cursor = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    @classmethod
    def update_progress(cls, conn, order_id, stage, block=None,
                        total_blocks=None, message="", updated_at=None):
        """Persist rewrite progress so every web worker sees the same state."""
        conn.execute(
            """UPDATE orders
               SET progress_stage = ?, progress_block = ?,
                   progress_total_blocks = ?, progress_message = ?,
                   progress_updated_at = ?
               WHERE order_id = ?""",
            (stage, block, total_blocks, message, updated_at, order_id)
        )
        conn.commit()

    @classmethod
    def update_rewrite_plan(cls, conn, order_id, rewrite_metadata):
        """Persist the planned rewrite route before execution can fail."""
        rewrite_metadata = rewrite_metadata or {}
        conn.execute(
            """UPDATE orders
               SET rewrite_method = ?, rewrite_provider = ?, rewrite_model = ?,
                   humanizer_primary = ?, humanizer_fallback = ?,
                   rewrite_pipeline_version = ?
               WHERE order_id = ?""",
            (
                rewrite_metadata.get('rewrite_method'),
                rewrite_metadata.get('rewrite_provider'),
                rewrite_metadata.get('rewrite_model'),
                rewrite_metadata.get('humanizer_primary'),
                rewrite_metadata.get('humanizer_fallback'),
                rewrite_metadata.get('rewrite_pipeline_version'),
                order_id,
            )
        )
        conn.commit()

    @classmethod
    def get_progress(cls, conn, order_id):
        row = conn.execute(
            """SELECT progress_stage, progress_block, progress_total_blocks,
                      progress_message, progress_updated_at
               FROM orders WHERE order_id = ?""",
            (order_id,)
        ).fetchone()
        if not row or not row['progress_stage']:
            return None
        return {
            "stage": row['progress_stage'],
            "block": row['progress_block'],
            "total_blocks": row['progress_total_blocks'],
            "message": row['progress_message'] or "",
            "updated_at": row['progress_updated_at'],
        }

    # ========== Payment-related methods ==========

    @classmethod
    def create_payment_record(cls, conn, user_id, order_id, original_text,
                               original_format, original_filename, word_count,
                               price, mode, recharge_words, balance_words_used,
                               paragraphs=None, source_file_key=None,
                               analysis_context=None):
        """Create a pending auto-recharge order tied to a rewrite task.

        paragraphs: 可选的段落结构（list[dict]，含 style/is_heading/is_reference
        等），JSON 序列化后存入 orders.paragraphs，供异步后台改写读取。
        """
        import json
        created_at = datetime.now(timezone.utc).isoformat()
        expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        paragraphs_json = json.dumps(paragraphs, ensure_ascii=False) if paragraphs else None
        conn.execute(
            """INSERT INTO orders
               (user_id, order_id, original_text, paragraphs, rewritten_text,
                original_format, original_filename, word_count, price, mode,
                original_score, rewritten_score, status, payment_status,
                alipay_amount, recharge_words, balance_words_used,
                source_file_key, document_status, created_at, expires_at)
               VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, NULL, 'pending', 'pending', ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, order_id, original_text, paragraphs_json,
             original_format, original_filename,
             word_count, price, mode, price, recharge_words, balance_words_used,
             source_file_key, 'pending' if source_file_key else 'not_applicable',
             created_at, expires_at)
        )
        cls._apply_input_analysis_context(
            conn, order_id, paragraphs, original_filename, analysis_context
        )
        conn.commit()
        return cls.get_by_order_id(conn, order_id)

    @classmethod
    def save_qr_code(cls, conn, order_id, qr_code):
        """Save the Alipay QR code string for an order."""
        conn.execute(
            "UPDATE orders SET alipay_qr_code = ? WHERE order_id = ?",
            (qr_code, order_id)
        )
        conn.commit()

    @classmethod
    def mark_paid(cls, conn, order_id, alipay_trade_no, paid_at):
        """Mark order as paid after Alipay notification.

        The WHERE payment_status = 'pending' guard makes this idempotent:
        if two callers race (e.g. webhook + polling), the second UPDATE
        affects zero rows and is a safe no-op.
        """
        conn.execute(
            """UPDATE orders
               SET payment_status = 'paid',
                   alipay_trade_no = ?,
                   paid_at = ?,
                   status = 'processing'
               WHERE order_id = ? AND payment_status = 'pending'""",
            (alipay_trade_no, paid_at, order_id)
        )
        conn.commit()

    @classmethod
    def update_result(cls, conn, order_id, rewritten_text, rewritten_score, original_score=None,
                      rewritten_paragraphs=None, detector_backend=None,
                      humanizer_backend=None, rewrite_metadata=None):
        """Update order with rewrite result (called after humanization completes)."""
        import json
        rewrite_metadata = rewrite_metadata or {}
        paragraphs_json = json.dumps(
            rewritten_paragraphs, ensure_ascii=False
        ) if rewritten_paragraphs else None

        existing = conn.execute(
            """SELECT word_count, paragraphs, created_at, original_heading_count
               FROM orders WHERE order_id = ?""",
            (order_id,)
        ).fetchone()
        original_word_count = int(existing['word_count'] or 0) if existing else 0
        rewritten_word_count = len((rewritten_text or '').split())
        word_count_change_ratio = (
            (rewritten_word_count - original_word_count) / original_word_count
            if original_word_count else None
        )

        original_paragraphs = []
        if existing and existing['paragraphs']:
            try:
                loaded = json.loads(existing['paragraphs'])
                original_paragraphs = loaded if isinstance(loaded, list) else []
            except (TypeError, ValueError):
                original_paragraphs = []
        rewritten_items = rewritten_paragraphs if isinstance(rewritten_paragraphs, list) else []
        stored_heading_count = existing['original_heading_count'] if existing else None
        original_heading_count = (
            stored_heading_count if stored_heading_count is not None else
            sum(
                1 for item in original_paragraphs
                if isinstance(item, dict) and item.get('is_heading')
            )
        )
        rewritten_heading_count = sum(
            1 for item in rewritten_items
            if isinstance(item, dict) and item.get('is_heading')
        )
        rewritten_paragraph_count = (
            sum(
                1 for item in rewritten_items
                if isinstance(item, dict) and item.get('text')
            )
            if rewritten_items else
            len([
                value for value in (rewritten_text or '').split('\n\n')
                if value.strip()
            ])
        )
        protected_paragraph_count = sum(
            1 for item in rewritten_items
            if isinstance(item, dict) and item.get('was_rewritten') is False
        )
        heading_count_changed = int(original_heading_count != rewritten_heading_count)
        completed_dt = datetime.now(timezone.utc)
        completed_at = completed_dt.isoformat()
        processing_duration_ms = None
        if existing and existing['created_at']:
            try:
                created_dt = datetime.fromisoformat(existing['created_at'])
                processing_duration_ms = max(
                    0, round((completed_dt - created_dt).total_seconds() * 1000)
                )
            except (TypeError, ValueError):
                processing_duration_ms = None

        conn.execute(
            """UPDATE orders
               SET rewritten_text = ?, rewritten_score = ?,
                   original_score = COALESCE(?, original_score),
                   rewritten_paragraphs = ?, detector_backend = ?,
                   humanizer_backend = ?, rewrite_method = ?,
                   rewrite_provider = ?, rewrite_model = ?,
                   humanizer_primary = ?, humanizer_fallback = ?,
                   fallback_used = ?, fallback_block_count = ?,
                   rewrite_block_count = ?, rewrite_pipeline_version = ?,
                   rewritten_word_count = ?, word_count_change_ratio = ?,
                   original_heading_count = ?, rewritten_heading_count = ?,
                   heading_count_changed = ?, rewritten_paragraph_count = ?,
                   protected_paragraph_count = ?, processing_duration_ms = ?,
                   completed_at = ?, failure_stage = NULL, failure_code = NULL,
                   status = 'completed'
               WHERE order_id = ?""",
            (
                rewritten_text,
                rewritten_score,
                original_score,
                paragraphs_json,
                detector_backend,
                rewrite_metadata.get('humanizer_backend') or humanizer_backend,
                rewrite_metadata.get('rewrite_method'),
                rewrite_metadata.get('rewrite_provider'),
                rewrite_metadata.get('rewrite_model'),
                rewrite_metadata.get('humanizer_primary'),
                rewrite_metadata.get('humanizer_fallback'),
                int(bool(rewrite_metadata.get('fallback_used'))),
                int(rewrite_metadata.get('fallback_block_count') or 0),
                rewrite_metadata.get('rewrite_block_count'),
                rewrite_metadata.get('rewrite_pipeline_version'),
                rewritten_word_count,
                word_count_change_ratio,
                original_heading_count,
                rewritten_heading_count,
                heading_count_changed,
                rewritten_paragraph_count,
                protected_paragraph_count,
                processing_duration_ms,
                completed_at,
                order_id,
            )
        )
        conn.commit()

    @classmethod
    def mark_failed(cls, conn, order_id, failure_stage=None, failure_code=None):
        """Mark order as failed when background rewrite encounters an error."""
        conn.execute(
            """UPDATE orders
               SET status = 'failed', failure_stage = ?, failure_code = ?
               WHERE order_id = ?""",
            (failure_stage, failure_code, order_id)
        )
        conn.commit()

    @classmethod
    def expire_old_orders(cls, conn, max_age_minutes=10):
        """Mark orders as expired if payment pending for too long.
        10 分钟 = 与支付宝 timeout_express 和前端 QR 过期时间保持一致（P6）"""
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)).isoformat()
        conn.execute(
            """UPDATE orders
               SET payment_status = 'expired', status = 'expired'
               WHERE payment_status = 'pending' AND created_at < ?""",
            (cutoff,)
        )
        conn.commit()


class RewriteFeedback:
    """User-reported result quality for a completed rewrite order."""

    @classmethod
    def init_table(cls, conn):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rewrite_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                order_id TEXT UNIQUE NOT NULL,
                issue_type TEXT NOT NULL,
                issue_types TEXT,
                detector_platform TEXT,
                external_score REAL,
                comment TEXT,
                contact_allowed INTEGER DEFAULT 0,
                screenshot_file_key TEXT,
                status TEXT DEFAULT 'new',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (order_id) REFERENCES orders(order_id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rewrite_feedback_created_at "
            "ON rewrite_feedback(created_at)"
        )
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(rewrite_feedback)").fetchall()
        }
        if 'issue_types' not in columns:
            conn.execute("ALTER TABLE rewrite_feedback ADD COLUMN issue_types TEXT")
        conn.commit()

    @classmethod
    def upsert(cls, conn, user_id, order_id, issue_types,
               detector_platform=None, external_score=None, comment=None,
               contact_allowed=False, screenshot_file_key=None):
        import json

        if isinstance(issue_types, str):
            issue_types = [issue_types]
        issue_types = list(dict.fromkeys(issue_types or []))
        if not issue_types:
            raise ValueError('At least one feedback issue type is required')
        issue_type = issue_types[0]
        issue_types_json = json.dumps(issue_types, ensure_ascii=False)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO rewrite_feedback
               (user_id, order_id, issue_type, issue_types, detector_platform,
                external_score, comment, contact_allowed, screenshot_file_key,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(order_id) DO UPDATE SET
                   issue_type = excluded.issue_type,
                   issue_types = excluded.issue_types,
                   detector_platform = excluded.detector_platform,
                   external_score = excluded.external_score,
                   comment = excluded.comment,
                   contact_allowed = excluded.contact_allowed,
                   screenshot_file_key = COALESCE(
                       excluded.screenshot_file_key,
                       rewrite_feedback.screenshot_file_key
                   ),
                   updated_at = excluded.updated_at""",
            (user_id, order_id, issue_type, issue_types_json, detector_platform,
             external_score, comment, int(bool(contact_allowed)),
             screenshot_file_key, now, now)
        )
        conn.commit()
        return cls.get_by_order_id(conn, order_id)

    @classmethod
    def get_by_order_id(cls, conn, order_id):
        row = conn.execute(
            "SELECT * FROM rewrite_feedback WHERE order_id = ?", (order_id,)
        ).fetchone()
        return dict(row) if row else None

    @classmethod
    def get_order_ids_with_feedback(cls, conn, order_ids):
        """批量返回已有反馈的 order_id 集合。

        订单列表只需判断「是否已有反馈」，逐单调用 get_by_order_id 会造成
        N+1 查询；此处用一条 IN 查询代替，列表页固定两次数据库往返。
        """
        order_ids = list(dict.fromkeys(order_ids or []))
        if not order_ids:
            return set()
        placeholders = ','.join('?' for _ in order_ids)
        rows = conn.execute(
            f"SELECT order_id FROM rewrite_feedback "
            f"WHERE order_id IN ({placeholders})",
            tuple(order_ids),
        ).fetchall()
        return {row[0] for row in rows}

    @classmethod
    def get_issue_types(cls, feedback):
        """Return a normalized list for both legacy single-choice and new rows."""
        import json

        if not feedback:
            return []
        raw = feedback.get('issue_types')
        if raw:
            try:
                values = json.loads(raw)
                if isinstance(values, list):
                    return [value for value in values if isinstance(value, str)]
            except (TypeError, ValueError):
                pass
        legacy = feedback.get('issue_type')
        return [legacy] if legacy else []

class ActivationCode:
    """Activation/recharge code model for Xianyu channel."""

    @classmethod
    def init_table(cls, conn):
        """Create the activation_codes table if it does not exist."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS activation_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                word_quota INTEGER NOT NULL,
                status TEXT DEFAULT 'unused',
                created_at TEXT NOT NULL,
                redeemed_by INTEGER REFERENCES users(id),
                redeemed_at TEXT
            )
        """)
        conn.commit()

    @classmethod
    def generate(cls, conn, code, word_quota):
        """Insert a new unredeemed activation code. Returns the record dict."""
        created_at = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO activation_codes (code, word_quota, created_at) VALUES (?, ?, ?)",
            (code, word_quota, created_at)
        )
        conn.commit()
        cursor = conn.execute("SELECT * FROM activation_codes WHERE code = ?", (code,))
        return dict(cursor.fetchone())

    @classmethod
    def get_by_code(cls, conn, code):
        """Look up an activation code. Returns dict or None."""
        cursor = conn.execute("SELECT * FROM activation_codes WHERE code = ?", (code,))
        row = cursor.fetchone()
        return dict(row) if row else None

    @classmethod
    def redeem(cls, conn, code, user_id):
        """Redeem an activation code for a user. Returns (success, message)."""
        try:
            ac = cls.get_by_code(conn, code)
            if not ac:
                return False, "兑换码不存在"
            if ac['status'] != 'unused':
                return False, "该兑换码已被使用"
            now = datetime.now(timezone.utc).isoformat()
            cursor = conn.execute(
                "UPDATE activation_codes SET status = 'redeemed', redeemed_by = ?, redeemed_at = ? WHERE code = ? AND status = 'unused'",
                (user_id, now, code)
            )
            if cursor.rowcount == 0:
                conn.rollback()
                return False, "该兑换码已被使用"
            User.add_balance(conn, user_id, ac['word_quota'])
            balance_after = User.get_balance(conn, user_id)
            BalanceTransaction.create(
                conn, user_id, 'activation_recharge', ac['word_quota'], balance_after,
                reference_id=code, description='兑换码充值'
            )
            conn.commit()
            return True, f"兑换成功！已添加 {ac['word_quota']} 词到你的账户"
        except Exception:
            conn.rollback()
            raise

    @classmethod
    def list_all(cls, conn, limit=50, offset=0):
        """List all activation codes. Returns (list, total_count)."""
        count_row = conn.execute("SELECT COUNT(*) as total FROM activation_codes").fetchone()
        total = count_row['total'] if count_row else 0
        cursor = conn.execute(
            "SELECT * FROM activation_codes ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset)
        )
        return [dict(row) for row in cursor.fetchall()], total

    @classmethod
    def stats(cls, conn):
        """Get activation code stats. Returns dict."""
        total = conn.execute("SELECT COUNT(*) as c FROM activation_codes").fetchone()['c']
        used = conn.execute("SELECT COUNT(*) as c FROM activation_codes WHERE status = 'redeemed'").fetchone()['c']
        unused = conn.execute("SELECT COUNT(*) as c FROM activation_codes WHERE status = 'unused'").fetchone()['c']
        total_words = conn.execute(
            "SELECT COALESCE(SUM(word_quota), 0) as s FROM activation_codes WHERE status = 'redeemed'"
        ).fetchone()['s']
        return {
            'total': total, 'used': used, 'unused': unused, 'total_redeemed_words': total_words
        }


class BalanceTransaction:
    """Immutable word-balance ledger for recharge, consumption and refunds."""

    @classmethod
    def init_table(cls, conn):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS balance_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                transaction_type TEXT NOT NULL,
                words INTEGER NOT NULL,
                balance_after INTEGER NOT NULL,
                order_id TEXT,
                reference_id TEXT,
                description TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_balance_transactions_user "
            "ON balance_transactions(user_id, created_at DESC)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_balance_transactions_order_type "
            "ON balance_transactions(order_id, transaction_type) WHERE order_id IS NOT NULL"
        )
        conn.commit()

    @classmethod
    def create(cls, conn, user_id, transaction_type, words, balance_after,
               order_id=None, reference_id=None, description=None):
        conn.execute(
            """INSERT INTO balance_transactions
               (user_id, transaction_type, words, balance_after, order_id,
                reference_id, description, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, transaction_type, words, balance_after, order_id,
             reference_id, description, datetime.now(timezone.utc).isoformat())
        )
