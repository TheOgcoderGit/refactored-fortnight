"""Forwarding units, never money. UTC accounting; one unit per source dispatch.

Reserved rows surviving a crash are retained for operator reconciliation: a send
may have reached Telegram even if the acknowledgement was lost. Never blindly
refund/replay an ambiguous send. No promise of exactly-once network delivery.
"""
import uuid
from database.db import get_connection
from services import plan_service


def balance(user_id):
    conn = get_connection()
    try:
        return conn.execute('SELECT COALESCE(SUM(units),0) FROM forward_credit_ledger WHERE user_id=?', (user_id,)).fetchone()[0]
    finally:
        conn.close()


def adjust(user_id, units, reference, reason, actor_id=None):
    """Trusted service entry point. Caller must authorize admin/payment provider."""
    if type(units) is not int or not units or not reference or not reason:
        raise ValueError('An integer nonzero adjustment, unique reference and reason are required.')
    conn = get_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        existing = conn.execute('SELECT * FROM forward_credit_ledger WHERE reference=?', (reference,)).fetchone()
        if existing:
            if (existing['user_id'], existing['units'], existing['reason']) != (user_id, units, reason):
                raise ValueError('Idempotency reference reused with different adjustment.')
            conn.rollback()
            return False
        current = conn.execute('SELECT COALESCE(SUM(units),0) FROM forward_credit_ledger WHERE user_id=?', (user_id,)).fetchone()[0]
        if current + units < 0:
            raise ValueError('Insufficient forwarding credits.')
        conn.execute('INSERT INTO forward_credit_ledger(user_id,units,reference,reason,actor_id) VALUES(?,?,?,?,?)',
                     (user_id,units,reference,reason,actor_id))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reserve(user_id, project_id, reference=None):
    reference = reference or uuid.uuid4().hex
    conn = get_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        owner = conn.execute('SELECT p.user_id,u.status FROM projects p JOIN users u ON p.user_id=u.telegram_id WHERE p.id=?', (project_id,)).fetchone()
        if not owner or owner['user_id'] != user_id or owner['status'] != 'active':
            raise PermissionError('Project unavailable.')
        prior = conn.execute('SELECT * FROM forward_reservations WHERE reference=?', (reference,)).fetchone()
        if prior:
            if (prior['user_id'],prior['project_id']) != (user_id,project_id):
                raise PermissionError('Reservation unavailable.')
            if prior['status'] != 'released':
                conn.rollback()
                return reference if prior['status'] == 'committed' else None
        limits = plan_service.get_entitlements(user_id)
        day = conn.execute("SELECT date('now')").fetchone()[0]
        total = conn.execute('''SELECT COALESCE(SUM(d.forward_count),0) FROM daily_usage d
            JOIN projects p ON p.id=d.project_id WHERE p.user_id=? AND d.usage_date=?''', (user_id,day)).fetchone()[0]
        # Deleting a project must not reset consumed daily allowance.
        total += conn.execute('''SELECT COUNT(*) FROM forward_reservations WHERE user_id=? AND usage_date=?
            AND project_id IS NULL AND kind='daily' AND status != 'released' ''', (user_id,day)).fetchone()[0]
        row = conn.execute('SELECT forward_count FROM daily_usage WHERE project_id=? AND usage_date=?', (project_id,day)).fetchone()
        project_used = row[0] if row else 0
        allowed = ((limits['daily_forward_limit'] is None or total < limits['daily_forward_limit']) and
                   (limits['per_project_daily_forward_limit'] is None or project_used < limits['per_project_daily_forward_limit']))
        kind = 'daily' if allowed else 'credit'
        if kind == 'credit':
            available = conn.execute('SELECT COALESCE(SUM(units),0) FROM forward_credit_ledger WHERE user_id=?', (user_id,)).fetchone()[0]
            if available < 1:
                conn.rollback()
                return None
            conn.execute('INSERT INTO forward_credit_ledger(user_id,units,reference,reason) VALUES(?,-1,?,?)',
                         (user_id,'consume:'+reference+':'+uuid.uuid4().hex,'forward reservation'))
        else:
            conn.execute('''INSERT INTO daily_usage(project_id,usage_date,forward_count) VALUES(?,?,1)
                ON CONFLICT(project_id,usage_date) DO UPDATE SET forward_count=forward_count+1''', (project_id,day))
        conn.execute('''INSERT INTO forward_reservations(reference,user_id,project_id,usage_date,kind,status)
            VALUES(?,?,?,?,?,'reserved') ON CONFLICT(reference) DO UPDATE SET
            usage_date=excluded.usage_date,kind=excluded.kind,status='reserved',created_at=CURRENT_TIMESTAMP''',
                     (reference,user_id,project_id,day,kind))
        conn.commit()
        return reference
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def finish(reference, success):
    conn = get_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM forward_reservations WHERE reference=?', (reference,)).fetchone()
        if not row or row['status'] != 'reserved':
            conn.rollback()
            return False
        if not success:
            if row['kind'] == 'daily':
                conn.execute('UPDATE daily_usage SET forward_count=MAX(0,forward_count-1) WHERE project_id=? AND usage_date=?',
                             (row['project_id'],row['usage_date']))
            else:
                conn.execute('INSERT INTO forward_credit_ledger(user_id,units,reference,reason) VALUES(?,1,?,?)',
                             (row['user_id'],'release:'+reference+':'+uuid.uuid4().hex,'failed forward rollback'))
        conn.execute('UPDATE forward_reservations SET status=? WHERE reference=?', ('committed' if success else 'released',reference))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
