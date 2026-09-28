import json
import csv
import io
import os
import re
import secrets
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, abort, jsonify, request, send_file, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


DATA = Path(os.getenv('DATA_DIR', '/data'))
DATA.mkdir(parents=True, exist_ok=True)
(DATA / 'documents').mkdir(exist_ok=True)
DB = DATA / 'portfolio.sqlite3'
app = Flask(__name__, static_folder=None)
app.config.update(
    SECRET_KEY=os.environ.get('SECRET_KEY') or secrets.token_hex(32),
    MAX_CONTENT_LENGTH=20 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Strict',
    SESSION_COOKIE_SECURE=os.getenv('COOKIE_SECURE') == '1',
)
ALLOWED_EXT = {'pdf', 'png', 'jpg', 'jpeg', 'webp', 'csv'}
LOGIN_ATTEMPTS = {}
MELBOURNE = ZoneInfo('Australia/Melbourne')


def local_today():
    return datetime.now(MELBOURNE).date()


def db():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA journal_mode=WAL')
    return c


def init_db():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS portfolios(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, role TEXT NOT NULL, portfolio_id INTEGER NOT NULL REFERENCES portfolios(id));
        CREATE TABLE IF NOT EXISTS accounts(id INTEGER PRIMARY KEY, portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
            name TEXT NOT NULL, broker TEXT NOT NULL, currency TEXT NOT NULL, kind TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS instruments(id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, name TEXT NOT NULL,
            exchange TEXT NOT NULL, currency TEXT NOT NULL, UNIQUE(symbol, exchange));
        CREATE TABLE IF NOT EXISTS transactions(id INTEGER PRIMARY KEY, portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
            account_id INTEGER NOT NULL REFERENCES accounts(id), target_account_id INTEGER REFERENCES accounts(id),
            instrument_id INTEGER REFERENCES instruments(id), type TEXT NOT NULL, occurred_at TEXT NOT NULL,
            quantity REAL NOT NULL DEFAULT 0, price REAL NOT NULL DEFAULT 0, amount REAL NOT NULL DEFAULT 0,
            target_amount REAL NOT NULL DEFAULT 0, fee REAL NOT NULL DEFAULT 0, tax REAL NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS tx_port_date ON transactions(portfolio_id, occurred_at);
        CREATE TABLE IF NOT EXISTS prices(instrument_id INTEGER NOT NULL REFERENCES instruments(id),
            day TEXT NOT NULL, close REAL NOT NULL, source TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(instrument_id, day));
        CREATE TABLE IF NOT EXISTS fx(day TEXT PRIMARY KEY, usd_aud REAL NOT NULL, source TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fx_sync_attempts(day TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS yahoo_fetches(instrument_id INTEGER NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
            market_day TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, last_attempt TEXT NOT NULL,
            success INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(instrument_id,market_day));
        CREATE TABLE IF NOT EXISTS yahoo_backfills(instrument_id INTEGER PRIMARY KEY REFERENCES instruments(id) ON DELETE CASCADE,
            first_buy TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS documents(id INTEGER PRIMARY KEY, portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
            instrument_id INTEGER REFERENCES instruments(id), account_id INTEGER REFERENCES accounts(id),
            tax_year TEXT NOT NULL DEFAULT '', title TEXT NOT NULL, original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL UNIQUE, uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS reconciliations(id INTEGER PRIMARY KEY, portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
            account_id INTEGER NOT NULL REFERENCES accounts(id), day TEXT NOT NULL, reported_value REAL NOT NULL,
            note TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS transaction_audit(id INTEGER PRIMARY KEY, portfolio_id INTEGER NOT NULL,
            transaction_id INTEGER NOT NULL, action TEXT NOT NULL, actor TEXT NOT NULL,
            before_json TEXT, after_json TEXT, changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS reinvestments(id INTEGER PRIMARY KEY,
            portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
            dividend_transaction_id INTEGER NOT NULL UNIQUE REFERENCES transactions(id),
            buy_transaction_id INTEGER NOT NULL UNIQUE REFERENCES transactions(id));
        ''')
        if 'fx_rate' not in {column['name'] for column in c.execute('PRAGMA table_info(transactions)')}:
            c.execute('ALTER TABLE transactions ADD COLUMN fx_rate REAL NOT NULL DEFAULT 0')
        if 'franking_credit' not in {column['name'] for column in c.execute('PRAGMA table_info(transactions)')}:
            c.execute('ALTER TABLE transactions ADD COLUMN franking_credit REAL NOT NULL DEFAULT 0')
        instrument_columns = {column['name'] for column in c.execute('PRAGMA table_info(instruments)')}
        if 'tv_symbol' not in instrument_columns:
            c.execute("ALTER TABLE instruments ADD COLUMN tv_symbol TEXT NOT NULL DEFAULT ''")
        if 'yahoo_symbol' not in instrument_columns:
            c.execute("ALTER TABLE instruments ADD COLUMN yahoo_symbol TEXT NOT NULL DEFAULT ''")
        recon_columns = {column['name'] for column in c.execute('PRAGMA table_info(reconciliations)')}
        if 'balance_scope' not in recon_columns:
            c.execute("ALTER TABLE reconciliations ADD COLUMN balance_scope TEXT NOT NULL DEFAULT 'cash'")
        if 'adjustment_transaction_id' not in recon_columns:
            c.execute('ALTER TABLE reconciliations ADD COLUMN adjustment_transaction_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL')
        if 'session_version' not in {column['name'] for column in c.execute('PRAGMA table_info(users)')}:
            c.execute('ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0')
        # Older versions treated broker cash accounts as cash plus holdings.
        # The account is cash only; retain the reported figure and linked adjustment
        # so the owner can review and rematch an existing check to cash.
        c.execute("UPDATE reconciliations SET balance_scope='cash' WHERE balance_scope!='cash'")
        for pid, label in [(1, os.getenv('ADMIN_USERNAME', 'Adam').title()),
                           (2, os.getenv('VIEWER_USERNAME', 'Brother').title())]:
            c.execute('INSERT OR IGNORE INTO portfolios(id,name) VALUES(?,?)', (pid, label))
        for username, password, role, pid in [
            (os.getenv('ADMIN_USERNAME', 'adam'), os.getenv('ADMIN_PASSWORD'), 'admin', 1),
            (os.getenv('VIEWER_USERNAME', 'brother'), os.getenv('VIEWER_PASSWORD'), 'viewer', 2),
        ]:
            if c.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone():
                continue
            if not password or len(password) < 12:
                raise RuntimeError(f'{role.upper()}_PASSWORD must be at least 12 characters to create the account')
            c.execute('INSERT INTO users(username,password_hash,role,portfolio_id) VALUES(?,?,?,?)',
                      (username, generate_password_hash(password), role, pid))


init_db()


def rowdict(row):
    return dict(row) if row else None


def rows(c, sql, params=()):
    return [dict(x) for x in c.execute(sql, params)]


def problem(message, status=400):
    return jsonify(error=message), status


def auth(admin=False):
    def deco(fn):
        @wraps(fn)
        def inner(*args, **kwargs):
            if not session.get('user_id'):
                return problem('Please log in', 401)
            with db() as c:
                user = c.execute('SELECT session_version FROM users WHERE id=?', (session['user_id'],)).fetchone()
            if not user or session.get('session_version', 0) != user['session_version']:
                session.clear()
                return problem('Session expired. Please log in again.', 401)
            if admin and session.get('role') != 'admin':
                return problem('Admin access required', 403)
            if request.method not in ('GET', 'HEAD') and request.headers.get('X-CSRF-Token') != session.get('csrf'):
                return problem('Invalid session token', 403)
            return fn(*args, **kwargs)
        return inner
    return deco


def portfolio_id():
    if session['role'] == 'viewer':
        return session['portfolio_id']
    try:
        pid = int(request.args.get('portfolio_id') or request.form.get('portfolio_id') or
                  (request.get_json(silent=True) or {}).get('portfolio_id') or session['portfolio_id'])
    except (ValueError, TypeError):
        abort(400)
    if pid not in (1, 2):
        abort(404)
    return pid


def get_owned(c, table, id_, pid):
    if not id_:
        return None
    return c.execute(f'SELECT * FROM {table} WHERE id=? AND portfolio_id=?', (id_, pid)).fetchone()


def num(value, name, allow_zero=True):
    try:
        n = Decimal(str(value))
        if not n.is_finite() or n < 0 or (not allow_zero and n == 0):
            raise ValueError()
        return float(n)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f'{name} must be a valid positive number')


def iso_day(value):
    return date.fromisoformat(str(value)).isoformat()


@app.errorhandler(413)
def too_large(_):
    return problem('Document exceeds 20 MB', 413)


@app.errorhandler(404)
def not_found(_):
    return problem('Not found', 404)


@app.get('/')
def home():
    return send_from_directory('static', 'index.html')


@app.get('/api/health')
def health():
    try:
        with db() as c:
            c.execute('SELECT 1').fetchone()
        return jsonify(ok=True)
    except sqlite3.Error:
        return problem('Database unavailable', 503)


@app.get('/static/<path:path>')
def static(path):
    return send_from_directory('static', path)


@app.get('/api/auth')
def auth_state():
    if not session.get('user_id'):
        with db() as c:
            configured = c.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 2
        return jsonify(authenticated=False, configured=configured)
    with db() as c:
        user = c.execute('SELECT session_version FROM users WHERE id=?', (session['user_id'],)).fetchone()
    if not user or session.get('session_version', 0) != user['session_version']:
        session.clear()
        return jsonify(authenticated=False, configured=True)
    return jsonify(authenticated=True, username=session['username'], role=session['role'],
                   portfolio_id=session['portfolio_id'], csrf=session['csrf'])


@app.post('/api/login')
def login():
    ip = request.remote_addr or 'local'
    now = time.time()
    failures = [t for t in LOGIN_ATTEMPTS.get(ip, []) if now - t < 600]
    if len(failures) >= 8:
        return problem('Too many attempts. Try again in 10 minutes.', 429)
    payload = request.get_json(silent=True) or {}
    with db() as c:
        user = c.execute('SELECT * FROM users WHERE username=?', (str(payload.get('username', '')).strip(),)).fetchone()
    if not user or not check_password_hash(user['password_hash'], str(payload.get('password', ''))):
        failures.append(now)
        LOGIN_ATTEMPTS[ip] = failures
        return problem('Incorrect username or password', 401)
    LOGIN_ATTEMPTS.pop(ip, None)
    session.clear()
    session.update(user_id=user['id'], username=user['username'], role=user['role'],
                   portfolio_id=user['portfolio_id'], session_version=user['session_version'],
                   csrf=secrets.token_urlsafe(32))
    return auth_state()


@app.post('/api/logout')
@auth()
def logout():
    session.clear()
    return jsonify(ok=True)


@app.post('/api/change-password')
@auth()
def change_password():
    d = request.get_json() or {}
    current = str(d.get('current_password', ''))
    replacement = str(d.get('new_password', ''))
    if len(replacement) < 12:
        return problem('New password must contain at least 12 characters')
    with db() as c:
        user = c.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
        if not user or not check_password_hash(user['password_hash'], current):
            return problem('Current password is incorrect', 403)
        c.execute('UPDATE users SET password_hash=?,session_version=session_version+1 WHERE id=?',
                  (generate_password_hash(replacement), user['id']))
        session['session_version'] = user['session_version'] + 1
    return jsonify(ok=True)


@app.post('/api/admin/reset-viewer-password')
@auth(admin=True)
def reset_viewer_password():
    d = request.get_json() or {}
    password = str(d.get('new_password', ''))
    if not 12 <= len(password) <= 256:
        return problem('New password must contain 12–256 characters')
    with db() as c:
        admin = c.execute('SELECT password_hash FROM users WHERE id=?', (session['user_id'],)).fetchone()
        if not admin or not check_password_hash(admin['password_hash'], str(d.get('admin_password', ''))):
            return problem('Your admin password is incorrect', 403)
        viewer = c.execute("SELECT id FROM users WHERE role='viewer' AND portfolio_id=2").fetchone()
        if not viewer:
            return problem('Viewer account not found', 404)
        c.execute('UPDATE users SET password_hash=?,session_version=session_version+1 WHERE id=?',
                  (generate_password_hash(password), viewer['id']))
    return jsonify(ok=True)


@app.get('/api/setup')
@auth()
def setup():
    pid = portfolio_id()
    with db() as c:
        portfolios = rows(c, 'SELECT id,name FROM portfolios') if session['role'] == 'admin' else rows(c, 'SELECT id,name FROM portfolios WHERE id=?', (pid,))
        instruments = rows(c, 'SELECT * FROM instruments ORDER BY symbol') if session['role'] == 'admin' else rows(c, '''
            SELECT DISTINCT i.* FROM instruments i JOIN transactions t ON t.instrument_id=i.id
            WHERE t.portfolio_id=? ORDER BY i.symbol''', (pid,))
        return jsonify(portfolios=portfolios,
                       accounts=rows(c, 'SELECT * FROM accounts WHERE portfolio_id=? ORDER BY name', (pid,)),
                       instruments=instruments)


@app.put('/api/portfolios/<int:pid>')
@auth(admin=True)
def edit_portfolio(pid):
    if pid not in (1, 2):
        abort(404)
    name = str((request.get_json() or {}).get('name', '')).strip()[:80]
    if not name:
        return problem('Enter a portfolio name')
    with db() as c:
        c.execute('UPDATE portfolios SET name=? WHERE id=?', (name, pid))
    return jsonify(ok=True)


@app.post('/api/accounts')
@auth(admin=True)
def add_account():
    d = request.get_json() or {}
    pid = portfolio_id()
    name = str(d.get('name', '')).strip()[:80]
    broker = str(d.get('broker', '')).strip()[:80]
    currency = str(d.get('currency', '')).upper()
    kind = str(d.get('kind', 'broker'))
    if not name or currency not in ('AUD', 'USD') or kind not in ('broker', 'bank'):
        return problem('Enter an account name, currency and type')
    with db() as c:
        c.execute('INSERT INTO accounts(portfolio_id,name,broker,currency,kind) VALUES(?,?,?,?,?)',
                  (pid, name, broker, currency, kind))
    return jsonify(ok=True)


@app.put('/api/accounts/<int:aid>')
@auth(admin=True)
def edit_account(aid):
    d, pid = request.get_json() or {}, portfolio_id()
    name, broker = str(d.get('name', '')).strip()[:80], str(d.get('broker', '')).strip()[:80]
    currency, kind = str(d.get('currency', '')).upper(), str(d.get('kind', ''))
    if not name or currency not in ('AUD', 'USD') or kind not in ('broker', 'bank'):
        return problem('Enter an account name, currency and type')
    with db() as c:
        old = get_owned(c, 'accounts', aid, pid)
        if not old:
            abort(404)
        used = c.execute('SELECT 1 FROM transactions WHERE account_id=? OR target_account_id=? LIMIT 1', (aid, aid)).fetchone()
        if used and currency != old['currency']:
            return problem('Currency cannot change after transactions exist. Edit the transaction or add a new account.')
        c.execute('UPDATE accounts SET name=?,broker=?,currency=?,kind=? WHERE id=?', (name, broker, currency, kind, aid))
    return jsonify(ok=True)


@app.delete('/api/accounts/<int:aid>')
@auth(admin=True)
def delete_account(aid):
    pid = portfolio_id()
    with db() as c:
        if not get_owned(c, 'accounts', aid, pid):
            abort(404)
        for table, clause, params in [('transactions', 'account_id=? OR target_account_id=?', (aid, aid)),
                                       ('documents', 'account_id=?', (aid,)),
                                       ('reconciliations', 'account_id=?', (aid,))]:
            if c.execute(f'SELECT 1 FROM {table} WHERE {clause} LIMIT 1', params).fetchone():
                return problem(f'Account has {table}. Move or delete those records first.')
        c.execute('DELETE FROM accounts WHERE id=? AND portfolio_id=?', (aid, pid))
    return jsonify(ok=True)


@app.post('/api/instruments')
@auth(admin=True)
def add_instrument():
    d = request.get_json() or {}
    symbol = str(d.get('symbol', '')).strip().upper()
    exchange = str(d.get('exchange', '')).strip().upper()
    name = str(d.get('name', '')).strip()[:150]
    yahoo_symbol = str(d.get('yahoo_symbol', '')).strip().upper()
    tv_symbol = str(d.get('tv_symbol', '')).strip().upper()
    currency = 'AUD' if exchange == 'AU' else 'USD'
    if not re.fullmatch(r'[A-Z0-9.\-]{1,20}', symbol) or exchange not in ('AU', 'US') or not name:
        return problem('Enter a name, ticker, and AU or US exchange')
    if (yahoo_symbol and not re.fullmatch(r'[A-Z0-9.\-]{1,40}', yahoo_symbol)) or (tv_symbol and not re.fullmatch(r'[A-Z0-9.\-]+:[A-Z0-9.\-]+', tv_symbol)):
        return problem('Enter a valid provider symbol, such as ASX:VAS or NASDAQ:AMD')
    with db() as c:
        try:
            c.execute('INSERT INTO instruments(symbol,name,exchange,currency,yahoo_symbol,tv_symbol) VALUES(?,?,?,?,?,?)',
                      (symbol, name, exchange, currency, yahoo_symbol, tv_symbol))
        except sqlite3.IntegrityError:
            return problem('This ticker and exchange already exist. Edit the existing holding instead.')
    return jsonify(ok=True)


@app.put('/api/instruments/<int:iid>')
@auth(admin=True)
def edit_instrument(iid):
    d = request.get_json() or {}
    symbol = str(d.get('symbol', '')).strip().upper()
    exchange = str(d.get('exchange', '')).strip().upper()
    name = str(d.get('name', '')).strip()[:150]
    yahoo_symbol = str(d.get('yahoo_symbol', '')).strip().upper()
    tv_symbol = str(d.get('tv_symbol', '')).strip().upper()
    if not re.fullmatch(r'[A-Z0-9.\-]{1,20}', symbol) or exchange not in ('AU', 'US') or not name:
        return problem('Enter a name, ticker and AU or US exchange')
    if (yahoo_symbol and not re.fullmatch(r'[A-Z0-9.\-]{1,40}', yahoo_symbol)) or (tv_symbol and not re.fullmatch(r'[A-Z0-9.\-]+:[A-Z0-9.\-]+', tv_symbol)):
        return problem('Enter a valid provider symbol, such as ASX:VAS or NASDAQ:AMD')
    try:
        with db() as c:
            old = c.execute('SELECT * FROM instruments WHERE id=?', (iid,)).fetchone()
            if not old:
                abort(404)
            used = c.execute('SELECT 1 FROM transactions WHERE instrument_id=? LIMIT 1', (iid,)).fetchone()
            if used and exchange != old['exchange']:
                return problem('Exchange cannot change after transactions exist.')
            if symbol != old['symbol'] or exchange != old['exchange'] or yahoo_symbol != old['yahoo_symbol']:
                c.execute("DELETE FROM prices WHERE instrument_id=? AND source!='manual'", (iid,))
                c.execute('DELETE FROM yahoo_fetches WHERE instrument_id=?', (iid,))
                c.execute('DELETE FROM yahoo_backfills WHERE instrument_id=?', (iid,))
            c.execute('UPDATE instruments SET symbol=?,name=?,exchange=?,currency=?,yahoo_symbol=?,tv_symbol=? WHERE id=?',
                      (symbol, name, exchange, 'AUD' if exchange == 'AU' else 'USD', yahoo_symbol, tv_symbol, iid))
        return jsonify(ok=True)
    except sqlite3.IntegrityError:
        return problem('A holding with this ticker and exchange already exists')


@app.delete('/api/instruments/<int:iid>')
@auth(admin=True)
def delete_instrument(iid):
    with db() as c:
        if not c.execute('SELECT 1 FROM instruments WHERE id=?', (iid,)).fetchone():
            abort(404)
        if c.execute('SELECT 1 FROM transactions WHERE instrument_id=? LIMIT 1', (iid,)).fetchone():
            return problem('Holding has transactions. Delete or correct those transactions first.')
        if c.execute('SELECT 1 FROM documents WHERE instrument_id=? LIMIT 1', (iid,)).fetchone():
            return problem('Holding has documents. Reassign or delete them first.')
        c.execute('DELETE FROM prices WHERE instrument_id=?', (iid,))
        c.execute('DELETE FROM instruments WHERE id=?', (iid,))
    return jsonify(ok=True)


TYPES = {'buy', 'sell', 'deposit', 'withdrawal', 'dividend', 'interest', 'fee', 'transfer', 'fx', 'split',
         'adjustment_in', 'adjustment_out'}


def validate_transaction(c, d, pid):
    kind = d.get('type')
    if kind not in TYPES:
        raise ValueError('Choose a transaction type')
    account = get_owned(c, 'accounts', d.get('account_id'), pid)
    if not account:
        raise ValueError('Choose an account in this portfolio')
    target = get_owned(c, 'accounts', d.get('target_account_id'), pid) if d.get('target_account_id') else None
    instrument = c.execute('SELECT * FROM instruments WHERE id=?', (d.get('instrument_id'),)).fetchone() if d.get('instrument_id') else None
    quantity = num(d.get('quantity') or 0, 'Quantity')
    price = num(d.get('price') or 0, 'Price')
    amount = num(d.get('amount') or 0, 'Amount')
    target_amount = num(d.get('target_amount') or 0, 'Received amount')
    fee = num(d.get('fee') or 0, 'Fee')
    tax = num(d.get('tax') or 0, 'Tax withheld')
    fx_rate = num(d.get('fx_rate') or 0, 'FX rate')
    franking_credit = num(d.get('franking_credit') or 0, 'Franking credit')
    day = iso_day(d.get('occurred_at'))
    if kind in ('buy', 'sell') and (not instrument or instrument['currency'] != account['currency'] or quantity <= 0 or price <= 0):
        raise ValueError('Buy and sell need a holding, matching currency, quantity and price')
    if kind == 'split' and (not instrument or instrument['currency'] != account['currency'] or quantity <= 0):
        raise ValueError('Split needs a holding and the additional number of shares')
    if kind in ('deposit', 'withdrawal', 'dividend', 'interest', 'fee', 'adjustment_in', 'adjustment_out') and amount <= 0:
        raise ValueError('Enter an amount greater than zero')
    if kind == 'dividend' and not instrument:
        raise ValueError('Choose a holding for the dividend')
    if franking_credit and (kind != 'dividend' or account['currency'] != 'AUD'):
        raise ValueError('Franking credits require an AUD dividend')
    if kind in ('transfer', 'fx'):
        if not target or target['id'] == account['id'] or amount <= 0:
            raise ValueError('Choose different source and destination accounts and enter the sent amount')
        if kind == 'transfer' and (target['currency'] != account['currency'] or target_amount not in (0, amount)):
            raise ValueError('A transfer needs accounts in the same currency')
        if kind == 'fx' and (target['currency'] == account['currency'] or target_amount <= 0):
            raise ValueError('Currency exchange needs a destination in another currency and a received amount')
        if kind == 'transfer':
            target_amount = amount
    else:
        target = None
    return (pid, account['id'], target['id'] if target else None,
            instrument['id'] if instrument else None, kind, day, quantity, price, amount,
            target_amount, fee, tax, str(d.get('note', '')).strip()[:500], fx_rate, franking_credit)


def check_positions(c, pid):
    balances = {}
    for t in c.execute('''SELECT account_id,instrument_id,type,quantity FROM transactions
        WHERE portfolio_id=? AND type IN ('buy','sell','split') ORDER BY occurred_at,id''', (pid,)):
        key = t['account_id'], t['instrument_id']
        delta = -t['quantity'] if t['type'] == 'sell' else t['quantity']
        balances[key] = balances.get(key, 0) + delta
        if balances[key] < -0.00000001:
            raise ValueError('A sale exceeds shares held in that account on that date')


def audit(c, pid, tid, action, before=None, after=None):
    c.execute('''INSERT INTO transaction_audit(portfolio_id,transaction_id,action,actor,before_json,after_json)
        VALUES(?,?,?,?,?,?)''', (pid, tid, action, session['username'],
                                 json.dumps(dict(before)) if before else None,
                                 json.dumps(dict(after)) if after else None))


@app.get('/api/transactions')
@auth()
def transactions():
    pid = portfolio_id()
    with db() as c:
        return jsonify(rows(c, '''SELECT t.*, a.name account_name, a.currency, i.symbol, i.name instrument_name,
            b.name target_account_name FROM transactions t
            JOIN accounts a ON a.id=t.account_id LEFT JOIN accounts b ON b.id=t.target_account_id
            LEFT JOIN instruments i ON i.id=t.instrument_id
            WHERE t.portfolio_id=? ORDER BY t.occurred_at DESC,t.id DESC''', (pid,)))


@app.post('/api/transactions')
@auth(admin=True)
def create_transaction():
    pid = portfolio_id()
    try:
        with db() as c:
            values = validate_transaction(c, request.get_json() or {}, pid)
            cur = c.execute('''INSERT INTO transactions(portfolio_id,account_id,target_account_id,instrument_id,type,
                occurred_at,quantity,price,amount,target_amount,fee,tax,note,fx_rate,franking_credit) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', values)
            check_positions(c, pid)
            audit(c, pid, cur.lastrowid, 'create', after=c.execute('SELECT * FROM transactions WHERE id=?', (cur.lastrowid,)).fetchone())
        return jsonify(ok=True)
    except (ValueError, sqlite3.IntegrityError) as e:
        return problem(str(e))


@app.put('/api/transactions/<int:tid>')
@auth(admin=True)
def update_transaction(tid):
    pid = portfolio_id()
    try:
        with db() as c:
            old = get_owned(c, 'transactions', tid, pid)
            if not old:
                return problem('Transaction not found', 404)
            if c.execute('SELECT 1 FROM reconciliations WHERE adjustment_transaction_id=?', (tid,)).fetchone():
                return problem('This cash correction belongs to a balance check. Edit and reconcile the check instead.')
            if c.execute('SELECT 1 FROM reinvestments WHERE dividend_transaction_id=? OR buy_transaction_id=?', (tid, tid)).fetchone():
                return problem('This transaction belongs to a dividend reinvestment. Edit the reinvestment together instead.')
            values = validate_transaction(c, request.get_json() or {}, pid)
            c.execute('''UPDATE transactions SET portfolio_id=?,account_id=?,target_account_id=?,instrument_id=?,type=?,
                occurred_at=?,quantity=?,price=?,amount=?,target_amount=?,fee=?,tax=?,note=?,fx_rate=?,franking_credit=? WHERE id=?''', values + (tid,))
            check_positions(c, pid)
            audit(c, pid, tid, 'update', before=old, after=c.execute('SELECT * FROM transactions WHERE id=?', (tid,)).fetchone())
        return jsonify(ok=True)
    except (ValueError, sqlite3.IntegrityError) as e:
        return problem(str(e))


@app.delete('/api/transactions/<int:tid>')
@auth(admin=True)
def delete_transaction(tid):
    pid = portfolio_id()
    try:
        with db() as c:
            old = get_owned(c, 'transactions', tid, pid)
            if not old:
                return problem('Transaction not found', 404)
            if c.execute('SELECT 1 FROM reconciliations WHERE adjustment_transaction_id=?', (tid,)).fetchone():
                return problem('This cash correction belongs to a balance check. Delete that check to remove the correction.')
            if c.execute('SELECT 1 FROM reinvestments WHERE dividend_transaction_id=? OR buy_transaction_id=?', (tid, tid)).fetchone():
                return problem('This transaction belongs to a dividend reinvestment. Delete the reinvestment together instead.')
            cur = c.execute('DELETE FROM transactions WHERE id=? AND portfolio_id=?', (tid, pid))
            check_positions(c, pid)
            audit(c, pid, tid, 'delete', before=old)
    except ValueError as e:
        return problem(str(e))
    return jsonify(ok=True)


@app.post('/api/prices')
@auth(admin=True)
def add_price():
    d = request.get_json() or {}
    try:
        price = num(d.get('close'), 'Price', False)
        day = iso_day(d.get('day'))
        with db() as c:
            if not c.execute('SELECT 1 FROM instruments WHERE id=?', (d.get('instrument_id'),)).fetchone():
                return problem('Holding not found')
            c.execute('''INSERT INTO prices VALUES(?,?,?,?,?) ON CONFLICT(instrument_id,day) DO UPDATE SET
                close=excluded.close,source=excluded.source,updated_at=excluded.updated_at''',
                (d['instrument_id'], day, price, 'manual', datetime.utcnow().isoformat() + 'Z'))
        return jsonify(ok=True)
    except (ValueError, TypeError) as e:
        return problem(str(e))


@app.get('/api/prices')
@auth(admin=True)
def list_prices():
    with db() as c:
        return jsonify(rows(c, '''SELECT p.*,i.symbol,i.currency FROM prices p JOIN instruments i ON i.id=p.instrument_id
            ORDER BY p.day DESC,i.symbol'''))


@app.delete('/api/prices/<int:iid>/<day>')
@auth(admin=True)
def delete_price(iid, day):
    try:
        day = iso_day(day)
    except ValueError:
        abort(404)
    with db() as c:
        result = c.execute('DELETE FROM prices WHERE instrument_id=? AND day=?', (iid, day))
        if not result.rowcount:
            abort(404)
    return jsonify(ok=True)


def fetch_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'PersonalPortfolio/1.0'}), timeout=15) as response:
        return json.load(response)


def closed_market_day(exchange, now=None):
    zone = ZoneInfo('America/New_York' if exchange == 'US' else 'Australia/Sydney')
    local = (now or datetime.now(ZoneInfo('UTC'))).astimezone(zone)
    # The extra time allows for the closing auction and end-of-day feed processing.
    day = local.date() if local.hour >= 17 else local.date() - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day.isoformat()


def fetch_yahoo_history(symbol, start, end):
    """Fetch unadjusted daily closes in the market's local trading dates."""
    import yfinance as yf
    frame = yf.Ticker(symbol).history(start=start, end=end, interval='1d',
                                      auto_adjust=False, actions=False, timeout=20, raise_errors=True)
    if frame.empty:
        raise ValueError('No daily prices returned; check the Yahoo Finance symbol')
    result = []
    for trading_day, row in frame.iterrows():
        day = trading_day.date().isoformat()
        if start <= day < end:
            try:
                result.append((day, num(row['Close'], 'Daily close', False)))
            except ValueError:
                continue
    if not result:
        raise ValueError('No valid daily closing prices returned')
    return result


def sync_yahoo(result):
    result['provider'] = 'Yahoo Finance'
    with db() as c:
        # Include sold holdings: their past prices are needed for the historical portfolio graph.
        instruments = rows(c, '''SELECT i.*, MIN(CASE WHEN t.type='buy' THEN t.occurred_at END) first_buy,
            MAX(t.occurred_at) last_trade,
            SUM(CASE WHEN t.type='sell' THEN -t.quantity ELSE t.quantity END) net_shares
            FROM instruments i JOIN transactions t ON t.instrument_id=i.id
            WHERE t.type IN ('buy','sell','split') GROUP BY i.id
            HAVING first_buy IS NOT NULL ORDER BY i.id''')
    for i in instruments:
        market_day = closed_market_day(i['exchange'])
        target_day = min(market_day, i['last_trade']) if i['net_shares'] <= 0.00000001 else market_day
        if i['first_buy'] > target_day:
            continue
        with db() as c:
            backfilled = c.execute('SELECT first_buy FROM yahoo_backfills WHERE instrument_id=?', (i['id'],)).fetchone()
            fetched = c.execute('SELECT * FROM yahoo_fetches WHERE instrument_id=? AND market_day=?',
                                (i['id'], target_day)).fetchone()
            latest = c.execute("SELECT MAX(day) FROM prices WHERE instrument_id=? AND source='Yahoo Finance daily'",
                               (i['id'],)).fetchone()[0]
        needs_backfill = not backfilled or i['first_buy'] < backfilled['first_buy']
        if i['net_shares'] <= 0.00000001 and not needs_backfill:
            continue
        if fetched and not (fetched['success'] and needs_backfill):
            if (fetched['success'] or fetched['attempts'] >= 4 or
                datetime.now(ZoneInfo('UTC')) - datetime.fromisoformat(fetched['last_attempt']) < timedelta(hours=2)):
                continue
        symbol = i['yahoo_symbol'] or (i['symbol'] + '.AX' if i['exchange'] == 'AU' else i['symbol'].replace('.', '-'))
        start = i['first_buy'] if needs_backfill or not latest else max(i['first_buy'],
                       (date.fromisoformat(latest) - timedelta(days=5)).isoformat())
        end = (date.fromisoformat(target_day) + timedelta(days=1)).isoformat()
        try:
            history = fetch_yahoo_history(symbol, start, end)
            newest = max(day for day, _ in history)
            stamp = datetime.now(ZoneInfo('UTC')).isoformat()
            with db() as c:
                for day, close in history:
                    c.execute('''INSERT INTO prices VALUES(?,?,?,?,?) ON CONFLICT(instrument_id,day) DO UPDATE SET
                        close=excluded.close,source=excluded.source,updated_at=excluded.updated_at
                        WHERE prices.source != 'manual' ''',
                        (i['id'], day, close, 'Yahoo Finance daily', stamp))
                if needs_backfill:
                    c.execute('''INSERT INTO yahoo_backfills(instrument_id,first_buy) VALUES(?,?)
                        ON CONFLICT(instrument_id) DO UPDATE SET first_buy=excluded.first_buy''',
                        (i['id'], i['first_buy']))
                success = newest >= target_day
                c.execute('''INSERT INTO yahoo_fetches VALUES(?,?,?,?,?) ON CONFLICT(instrument_id,market_day)
                    DO UPDATE SET attempts=yahoo_fetches.attempts+1,last_attempt=excluded.last_attempt,
                    success=excluded.success''', (i['id'], target_day, 1, stamp, int(success)))
            result['updated'].append(i['symbol'])
            if not success and i['net_shares'] > 0.00000001:
                result['deferred'].append(i['symbol'] + ': latest published close is ' + newest)
        except Exception as e:
            result['errors'].append(i['symbol'] + ': ' + str(e)[:140])
            stamp = datetime.now(ZoneInfo('UTC')).isoformat()
            with db() as c:
                c.execute('''INSERT INTO yahoo_fetches VALUES(?,?,?,?,0) ON CONFLICT(instrument_id,market_day)
                    DO UPDATE SET attempts=yahoo_fetches.attempts+1,last_attempt=excluded.last_attempt,success=0''',
                    (i['id'], target_day, 1, stamp))


def sync_market():
    result = {'updated': [], 'errors': [], 'deferred': [], 'provider': 'Yahoo Finance'}
    with db() as c:
        earliest_usd = c.execute('''SELECT MIN(t.occurred_at) FROM transactions t JOIN accounts a ON a.id=t.account_id
            WHERE a.currency='USD' ''').fetchone()[0]
        fx_day = local_today().isoformat()
        if earliest_usd and not c.execute('SELECT 1 FROM fx_sync_attempts WHERE day=?', (fx_day,)).fetchone():
            first_fx = c.execute('SELECT MIN(day) FROM fx').fetchone()[0]
            from_day = min(earliest_usd, first_fx) if first_fx else earliest_usd
            try:
                url = 'https://api.frankfurter.dev/v2/rates?' + urllib.parse.urlencode(
                    {'from': from_day, 'base': 'USD', 'quotes': 'AUD'})
                rates = fetch_json(url)
                for item in rates:
                    if item.get('quote') == 'AUD':
                        c.execute('''INSERT INTO fx VALUES(?,?,?) ON CONFLICT(day) DO NOTHING''',
                                  (iso_day(item['date']), num(item['rate'], 'Exchange rate', False), 'Frankfurter'))
                result['fx_updated'] = len(rates)
                c.execute('INSERT OR IGNORE INTO fx_sync_attempts(day) VALUES(?)', (fx_day,))
            except Exception as e:
                result['errors'].append('FX: ' + str(e)[:90])
    sync_yahoo(result)
    return result


@app.post('/api/refresh')
@auth(admin=True)
def refresh_prices():
    return jsonify(sync_market())


@app.post('/api/fx-rate')
@auth(admin=True)
def set_fx():
    d = request.get_json() or {}
    try:
        day = iso_day(d.get('day'))
        rate = num(d.get('usd_aud'), 'USD to AUD rate', False)
        with db() as c:
            c.execute('INSERT INTO fx VALUES(?,?,?) ON CONFLICT(day) DO UPDATE SET usd_aud=excluded.usd_aud,source=excluded.source',
                      (day, rate, 'manual'))
        return jsonify(ok=True)
    except ValueError as e:
        return problem(str(e))


@app.get('/api/fx-rates')
@auth(admin=True)
def list_fx():
    with db() as c:
        return jsonify(rows(c, 'SELECT * FROM fx ORDER BY day DESC'))


@app.delete('/api/fx-rate/<day>')
@auth(admin=True)
def delete_fx(day):
    try:
        day = iso_day(day)
    except ValueError:
        abort(404)
    with db() as c:
        result = c.execute('DELETE FROM fx WHERE day=?', (day,))
        if not result.rowcount:
            abort(404)
    return jsonify(ok=True)


@app.post('/api/fx-refresh')
@auth(admin=True)
def refresh_fx():
    try:
        data = fetch_json('https://api.frankfurter.dev/v2/rate/USD/AUD')
        rate = num(data['rate'], 'Exchange rate', False)
        day = iso_day(data['date'])
        with db() as c:
            c.execute('INSERT INTO fx VALUES(?,?,?) ON CONFLICT(day) DO UPDATE SET usd_aud=excluded.usd_aud,source=excluded.source',
                      (day, rate, 'Frankfurter'))
        return jsonify(day=day, rate=rate)
    except Exception as e:
        return problem('Could not fetch exchange rate: ' + str(e)[:100], 502)


def calculate(c, pid):
    txs = rows(c, '''SELECT t.*,a.currency,i.symbol,i.name instrument_name,i.currency instrument_currency
        FROM transactions t JOIN accounts a ON a.id=t.account_id
        LEFT JOIN instruments i ON i.id=t.instrument_id WHERE t.portfolio_id=?
        ORDER BY t.occurred_at,t.id''', (pid,))
    accounts = rows(c, 'SELECT * FROM accounts WHERE portfolio_id=?', (pid,))
    instruments = {i['id']: i for i in rows(c, 'SELECT * FROM instruments')}
    prices = rows(c, 'SELECT * FROM prices ORDER BY day')
    fx = rows(c, 'SELECT * FROM fx ORDER BY day')
    today = local_today()
    start = date.fromisoformat(txs[0]['occurred_at']) if txs else today
    start = min(start, today)
    # The graph is daily; no price before a first observed quote is invented.
    days = sorted({str(start + timedelta(days=n)) for n in range((today - start).days + 1)})
    current_cash = {a['id']: 0.0 for a in accounts}
    shares = {}
    account_shares = {}
    costs = {}
    account_costs = {}
    realized = {}
    contributed_aud = 0.0
    missing_contribution_fx = False
    price_history = {}
    for p in prices:
        price_history.setdefault(p['instrument_id'], []).append(p)
    series = []
    ti = 0
    latest_quotes = {}
    fx_i = 0
    rate = None
    quote_idx = {iid: 0 for iid in price_history}
    for day in days:
        while fx_i < len(fx) and fx[fx_i]['day'] <= day:
            rate = fx[fx_i]['usd_aud']
            fx_i += 1
        while ti < len(txs) and txs[ti]['occurred_at'] <= day:
            t = txs[ti]
            aid, iid, kind = t['account_id'], t['instrument_id'], t['type']
            q, p, amount, fee, tax = (t[x] for x in ('quantity', 'price', 'amount', 'fee', 'tax'))
            if kind == 'buy':
                current_cash[aid] -= q * p + fee
                shares[iid] = shares.get(iid, 0) + q
                account_shares[(aid, iid)] = account_shares.get((aid, iid), 0) + q
                account_costs[(aid, iid)] = account_costs.get((aid, iid), 0) + q * p + fee
                costs[iid] = costs.get(iid, 0) + q * p + fee
            elif kind == 'sell':
                held = shares.get(iid, 0)
                held_account = account_shares.get((aid, iid), 0)
                if held_account > 0:
                    basis = account_costs.get((aid, iid), 0) * min(q, held_account) / held_account
                    account_costs[(aid, iid)] = max(0, account_costs.get((aid, iid), 0) - basis)
                    costs[iid] = max(0, costs.get(iid, 0) - basis)
                    realized[iid] = realized.get(iid, 0) + q * p - fee - basis
                shares[iid] = held - q
                account_shares[(aid, iid)] = account_shares.get((aid, iid), 0) - q
                current_cash[aid] += q * p - fee
            elif kind == 'split':
                shares[iid] = shares.get(iid, 0) + q
                account_shares[(aid, iid)] = account_shares.get((aid, iid), 0) + q
            elif kind == 'deposit':
                current_cash[aid] += amount
                contribution_rate = t['fx_rate'] or rate
                if t['currency'] == 'USD' and contribution_rate is None:
                    missing_contribution_fx = True
                else:
                    contributed_aud += amount * (contribution_rate if t['currency'] == 'USD' else 1)
            elif kind == 'withdrawal':
                current_cash[aid] -= amount
                contribution_rate = t['fx_rate'] or rate
                if t['currency'] == 'USD' and contribution_rate is None:
                    missing_contribution_fx = True
                else:
                    contributed_aud -= amount * (contribution_rate if t['currency'] == 'USD' else 1)
            elif kind == 'dividend':
                current_cash[aid] += amount - tax - fee
            elif kind == 'interest':
                current_cash[aid] += amount - tax
            elif kind == 'fee':
                current_cash[aid] -= amount
            elif kind == 'adjustment_in':
                current_cash[aid] += amount
            elif kind == 'adjustment_out':
                current_cash[aid] -= amount
            elif kind in ('transfer', 'fx'):
                current_cash[aid] -= amount + fee
                current_cash[t['target_account_id']] += t['target_amount']
            ti += 1
        for iid, history in price_history.items():
            idx = quote_idx[iid]
            while idx < len(history) and history[idx]['day'] <= day:
                latest_quotes[iid] = history[idx]
                idx += 1
            quote_idx[iid] = idx
        values = {'AUD': sum(current_cash[a['id']] for a in accounts if a['currency'] == 'AUD'),
                  'USD': sum(current_cash[a['id']] for a in accounts if a['currency'] == 'USD')}
        unpriced = []
        for iid, qty in shares.items():
            if qty <= 0.00000001:
                continue
            inst = instruments[iid]
            quote = latest_quotes.get(iid)
            if quote:
                values[inst['currency']] += qty * quote['close']
            else:
                unpriced.append(inst['symbol'])
        total = values['AUD'] + values['USD'] * rate if rate else (values['AUD'] if not values['USD'] else None)
        if unpriced:
            total = None
        invested = None if missing_contribution_fx else contributed_aud
        series.append({'day': day, 'value': total, 'invested': invested, 'unpriced': unpriced})
    holdings = []
    account_by_id = {a['id']: a for a in accounts}
    for iid, qty in shares.items():
        if qty <= 0.00000001:
            continue
        inst = instruments[iid]
        quote = latest_quotes.get(iid)
        value = qty * quote['close'] if quote else None
        brokers = {}
        for (aid, holding_id), held in account_shares.items():
            if holding_id == iid and held > .00000001:
                label = account_by_id[aid]['broker'].strip() or account_by_id[aid]['name']
                brokers[label] = brokers.get(label, 0) + held
        holdings.append({**inst, 'quantity': qty, 'cost': costs.get(iid, 0),
                         'brokers': [{'name': name, 'quantity': held} for name, held in sorted(brokers.items())],
                         'avg_cost': costs.get(iid, 0) / qty if qty else 0,
                         'price': quote['close'] if quote else None,
                         'price_day': quote['day'] if quote else None,
                         'price_source': quote['source'] if quote else None,
                         'value': value, 'gain': value - costs.get(iid, 0) if value is not None else None,
                         'gain_pct': (value / costs[iid] - 1) * 100 if value is not None and costs.get(iid, 0) else None,
                         'realized': realized.get(iid, 0),
                         'prices': price_history.get(iid, [])})
    holdings.sort(key=lambda h: (h['value'] or 0) * (rate if h['currency'] == 'USD' and rate else 1), reverse=True)
    cash = [{**a, 'balance': current_cash[a['id']]} for a in accounts]
    current = series[-1]
    # Current positions at their last two published closes. Trades and cash transfers
    # are excluded; USD exposure includes the move between the last two FX fixes.
    session_move = 0.0
    prior_usd_exposure = sum(current_cash[a['id']] for a in accounts if a['currency'] == 'USD')
    missing_session_data = False
    stale_holdings = []
    for holding in holdings:
        history = [p for p in price_history.get(holding['id'], []) if p['day'] <= today.isoformat()]
        expected = closed_market_day(holding['exchange'])
        if not history or history[-1]['day'] < expected:
            stale_holdings.append({'symbol': holding['symbol'], 'price_day': holding['price_day'],
                                   'expected_day': expected})
        if len(history) < 2:
            missing_session_data = True
            continue
        prior, latest = history[-2]['close'], history[-1]['close']
        if holding['currency'] == 'USD':
            prior_usd_exposure += holding['quantity'] * prior
            if rate is None:
                missing_session_data = True
            else:
                session_move += holding['quantity'] * (latest - prior) * rate
        else:
            session_move += holding['quantity'] * (latest - prior)
    usd_accounts = any(a['currency'] == 'USD' for a in accounts)
    previous_rate = fx[fx_i - 2]['usd_aud'] if fx_i > 1 else None
    if (usd_accounts or any(h['currency'] == 'USD' for h in holdings)) and rate is not None:
        if previous_rate is None:
            missing_session_data = True
        else:
            session_move += prior_usd_exposure * (rate - previous_rate)
    session_move = None if missing_session_data or current['value'] is None else session_move
    from reports import cashflow_returns
    return {'holdings': holdings, 'cash': cash, 'series': series,
            'returns': cashflow_returns(series),
            'value': current['value'], 'invested': current['invested'],
            'profit': current['value'] - current['invested'] if current['value'] is not None and current['invested'] is not None else None,
            'session_move': session_move, 'stale_holdings': stale_holdings, 'fx_rate': rate,
            'fx_day': fx[fx_i - 1]['day'] if fx_i else None,
            'unpriced': current['unpriced']}


@app.get('/api/dashboard')
@auth()
def dashboard():
    with db() as c:
        return jsonify(calculate(c, portfolio_id()))


@app.get('/api/financial-year')
@auth()
def financial_year_report():
    from reports import financial_year
    try:
        end_year = int(request.args.get('end_year', ''))
        if end_year < 2000 or end_year > local_today().year + 1:
            raise ValueError()
    except (ValueError, TypeError):
        return problem('Choose a valid financial year')
    with db() as c:
        return jsonify(financial_year(c, portfolio_id(), end_year))


@app.get('/api/financial-year.csv')
@auth()
def financial_year_csv():
    from flask import Response
    from reports import financial_year
    try:
        end_year = int(request.args.get('end_year', ''))
        if end_year < 2000 or end_year > local_today().year + 1:
            raise ValueError()
    except (ValueError, TypeError):
        return problem('Choose a valid financial year')
    with db() as c:
        report = financial_year(c, portfolio_id(), end_year)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(('kind','day','symbol','account','purchase_date','quantity','proceeds_aud',
                     'cost_aud','gain_aud','discount_eligible','gross_income_aud',
                     'tax_withheld_aud','franking_credit_aud'))
    for s in report['sales']:
        writer.writerow(('sale',s['sold'],s['symbol'],s['account_name'],s['acquired'],s['quantity'],
                         s['proceeds_aud'],s['cost_aud'],s['gain_aud'],s['discount_eligible'],'','',''))
    for t in report['income']:
        writer.writerow((t['type'],t['day'],t['symbol'],'','','','','','','',
                         t['gross_aud'],t['withheld_aud'],t['franking_aud']))
    return Response(output.getvalue(),mimetype='text/csv',headers={
        'Content-Disposition':f'attachment; filename="portfolio-financial-year-{end_year}.csv"'})


@app.get('/api/documents')
@auth()
def documents():
    pid = portfolio_id()
    with db() as c:
        return jsonify(rows(c, '''SELECT d.id,d.portfolio_id,d.instrument_id,d.account_id,d.tax_year,
            d.title,d.original_name,d.uploaded_at,i.symbol,a.name account_name FROM documents d
            LEFT JOIN instruments i ON i.id=d.instrument_id LEFT JOIN accounts a ON a.id=d.account_id
            WHERE d.portfolio_id=? ORDER BY d.uploaded_at DESC,d.id DESC''', (pid,)))


@app.post('/api/documents')
@auth(admin=True)
def upload_document():
    pid = portfolio_id()
    file = request.files.get('file')
    if not file or not file.filename:
        return problem('Choose a document')
    filename = secure_filename(file.filename)[:150]
    extension = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if extension not in ALLOWED_EXT:
        return problem('Allowed files: PDF, images and CSV')
    title = request.form.get('title', '').strip()[:120] or filename
    year = request.form.get('tax_year', '').strip()[:12]
    try:
        iid = int(request.form['instrument_id']) if request.form.get('instrument_id') else None
        aid = int(request.form['account_id']) if request.form.get('account_id') else None
    except ValueError:
        return problem('Invalid holding or account')
    stored = secrets.token_hex(20) + '.' + extension
    with db() as c:
        if iid and not c.execute('SELECT 1 FROM instruments WHERE id=?', (iid,)).fetchone():
            return problem('Holding not found')
        if aid and not get_owned(c, 'accounts', aid, pid):
            return problem('Account not found')
        file.save(DATA / 'documents' / stored)
        try:
            c.execute('''INSERT INTO documents(portfolio_id,instrument_id,account_id,tax_year,title,original_name,stored_name)
                VALUES(?,?,?,?,?,?,?)''', (pid, iid, aid, year, title, filename, stored))
        except Exception:
            (DATA / 'documents' / stored).unlink(missing_ok=True)
            raise
    return jsonify(ok=True)


@app.get('/api/documents/<int:did>/file')
@auth()
def document_file(did):
    with db() as c:
        d = get_owned(c, 'documents', did, portfolio_id())
    if not d:
        abort(404)
    path = DATA / 'documents' / d['stored_name']
    if not path.is_file():
        abort(404)
    response = send_file(path, download_name=d['original_name'], as_attachment=False)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@app.put('/api/documents/<int:did>')
@auth(admin=True)
def edit_document(did):
    d, pid = request.get_json() or {}, portfolio_id()
    title = str(d.get('title', '')).strip()[:120]
    if not title:
        return problem('Enter a document title')
    try:
        iid = int(d['instrument_id']) if d.get('instrument_id') else None
        aid = int(d['account_id']) if d.get('account_id') else None
    except (ValueError, TypeError):
        return problem('Invalid holding or account')
    with db() as c:
        if not get_owned(c, 'documents', did, pid):
            abort(404)
        if iid and not c.execute('SELECT 1 FROM instruments WHERE id=?', (iid,)).fetchone():
            return problem('Holding not found')
        if aid and not get_owned(c, 'accounts', aid, pid):
            return problem('Account not found')
        c.execute('UPDATE documents SET title=?,tax_year=?,instrument_id=?,account_id=? WHERE id=? AND portfolio_id=?',
                  (title, str(d.get('tax_year', '')).strip()[:12], iid, aid, did, pid))
    return jsonify(ok=True)


@app.delete('/api/documents/<int:did>')
@auth(admin=True)
def delete_document(did):
    with db() as c:
        d = get_owned(c, 'documents', did, portfolio_id())
        if not d:
            abort(404)
        c.execute('DELETE FROM documents WHERE id=?', (did,))
    (DATA / 'documents' / d['stored_name']).unlink(missing_ok=True)
    return jsonify(ok=True)


@app.get('/api/reconciliations')
@auth(admin=True)
def reconciliations():
    pid = portfolio_id()
    with db() as c:
        return jsonify(rows(c, '''SELECT r.*,a.name account_name,a.currency FROM reconciliations r JOIN accounts a ON a.id=r.account_id
            WHERE r.portfolio_id=? ORDER BY r.day DESC,r.id DESC''', (pid,)))


@app.post('/api/reconciliations')
@auth(admin=True)
def add_reconciliation():
    d = request.get_json() or {}
    pid = portfolio_id()
    try:
        day = iso_day(d.get('day'))
        reported = num(d.get('reported_value'), 'Reported balance')
        apply_now = d.get('apply_now') is True
        if apply_now and day != local_today().isoformat():
            return problem('Only a check dated today can set the current balance')
        with db() as c:
            if not get_owned(c, 'accounts', d.get('account_id'), pid):
                return problem('Account not found')
            cur = c.execute("INSERT INTO reconciliations(portfolio_id,account_id,day,reported_value,note,balance_scope) VALUES(?,?,?,?,?,'cash')",
                            (pid, d['account_id'], day, reported, str(d.get('note', ''))[:300]))
            delta = match_balance(c, c.execute('SELECT * FROM reconciliations WHERE id=?', (cur.lastrowid,)).fetchone(), pid) if apply_now else None
        return jsonify(ok=True, applied=apply_now, cash_change=delta)
    except ValueError as e:
        return problem(str(e))


@app.put('/api/reconciliations/<int:rid>')
@auth(admin=True)
def edit_reconciliation(rid):
    d, pid = request.get_json() or {}, portfolio_id()
    try:
        day = iso_day(d.get('day'))
        reported = num(d.get('reported_value'), 'Reported balance')
        apply_now = d.get('apply_now') is True
        if apply_now and day != local_today().isoformat():
            return problem('Only a check dated today can set the current balance')
        with db() as c:
            old = get_owned(c, 'reconciliations', rid, pid)
            if not old or not get_owned(c, 'accounts', d.get('account_id'), pid):
                abort(404)
            if old['adjustment_transaction_id'] and (str(old['account_id']) != str(d['account_id']) or old['day'] != day):
                remove_check_adjustment(c, old, pid)
            c.execute("UPDATE reconciliations SET account_id=?,day=?,reported_value=?,note=?,balance_scope='cash' WHERE id=? AND portfolio_id=?",
                      (d['account_id'], day, reported, str(d.get('note', '')).strip()[:300], rid, pid))
            delta = match_balance(c, c.execute('SELECT * FROM reconciliations WHERE id=?', (rid,)).fetchone(), pid) if apply_now else None
        return jsonify(ok=True, applied=apply_now, cash_change=delta)
    except ValueError as e:
        return problem(str(e))


def remove_check_adjustment(c, check, pid):
    tid = check['adjustment_transaction_id']
    if tid:
        old = get_owned(c, 'transactions', tid, pid)
        c.execute('UPDATE reconciliations SET adjustment_transaction_id=NULL WHERE id=?', (check['id'],))
        if old:
            c.execute('DELETE FROM transactions WHERE id=?', (tid,))
            audit(c, pid, tid, 'delete', before=old)


def match_balance(c, check, pid, expected=None):
    if check['day'] != local_today().isoformat():
        raise ValueError('Only a check dated today can set the current balance')
    account = next((a for a in calculate(c, pid)['cash'] if a['id'] == check['account_id']), None)
    if not account:
        raise ValueError('Account not found')
    observed = account['balance']
    if expected is not None and abs(observed - expected) > .005:
        raise ValueError('Account value has changed. Reload and review the difference before matching')
    old = (get_owned(c, 'transactions', check['adjustment_transaction_id'], pid)
           if check['adjustment_transaction_id'] else None)
    if old and old['account_id'] != check['account_id']:
        raise ValueError('Linked cash correction belongs to another account')
    previous = (old['amount'] if old['type'] == 'adjustment_in' else -old['amount']) if old else 0
    required = round(check['reported_value'] - (observed - previous), 2)
    delta = round(required - previous, 2)
    if abs(required) < .005:
        if old:
            remove_check_adjustment(c, check, pid)
    else:
        kind = 'adjustment_in' if required > 0 else 'adjustment_out'
        note = f'Balance check #{check["id"]}: cash balance {check["reported_value"]:.2f}'
        if old:
            if old['type'] != kind or abs(old['amount'] - abs(required)) >= .005 or old['note'] != note:
                c.execute('UPDATE transactions SET type=?,amount=?,note=? WHERE id=?', (kind, abs(required), note, old['id']))
                audit(c, pid, old['id'], 'update', before=old,
                      after=c.execute('SELECT * FROM transactions WHERE id=?', (old['id'],)).fetchone())
        else:
            cur = c.execute('''INSERT INTO transactions(portfolio_id,account_id,type,occurred_at,amount,note)
                VALUES(?,?,?,?,?,?)''', (pid, check['account_id'], kind, check['day'], abs(required), note))
            c.execute('UPDATE reconciliations SET adjustment_transaction_id=? WHERE id=?', (cur.lastrowid, check['id']))
            audit(c, pid, cur.lastrowid, 'create', after=c.execute('SELECT * FROM transactions WHERE id=?', (cur.lastrowid,)).fetchone())
    return delta


@app.post('/api/reconciliations/<int:rid>/match')
@auth(admin=True)
def match_reconciliation(rid):
    pid = portfolio_id()
    d = request.get_json() or {}
    try:
        expected = float(d['expected_cash_balance'])
        if not Decimal(str(expected)).is_finite():
            raise ValueError()
    except (KeyError, ValueError, TypeError, OverflowError):
        return problem('Reload the balance check and try again')
    try:
        with db() as c:
            check = get_owned(c, 'reconciliations', rid, pid)
            if not check:
                abort(404)
            delta = match_balance(c, check, pid, expected)
        return jsonify(ok=True, amount=delta)
    except ValueError as e:
        return problem(str(e), 409)


@app.delete('/api/reconciliations/<int:rid>')
@auth(admin=True)
def delete_reconciliation(rid):
    with db() as c:
        pid = portfolio_id()
        check = get_owned(c, 'reconciliations', rid, pid)
        if not check:
            abort(404)
        remove_check_adjustment(c, check, pid)
        c.execute('DELETE FROM reconciliations WHERE id=? AND portfolio_id=?', (rid, pid))
    return jsonify(ok=True)


@app.get('/api/export/transactions.csv')
@auth(admin=True)
def export_transactions():
    import csv
    import io
    from flask import Response
    pid = portfolio_id()
    with db() as c:
        data = rows(c, 'SELECT * FROM transactions WHERE portfolio_id=? ORDER BY occurred_at,id', (pid,))
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(data[0]) if data else ['No transactions'])
    writer.writeheader()
    writer.writerows(data)
    return Response(output.getvalue(), mimetype='text/csv', headers={'Content-Disposition': f'attachment; filename="portfolio-{pid}-transactions.csv"'})


@app.get('/api/import/template.csv')
@auth(admin=True)
def import_template():
    from flask import Response
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(('date','type','account','symbol','quantity','price','amount','fee','tax',
                     'franking_credit','target_account','target_amount','fx_rate','note'))
    return Response(output.getvalue(), mimetype='text/csv', headers={
        'Content-Disposition': 'attachment; filename="portfolio-import-template.csv"'})


def prepare_import(c, pid, blob):
    from csv_import import canonical, existing_signatures, parse_csv, resolve_row
    incoming = parse_csv(blob)
    accounts = {a['id']: a for a in rows(c, 'SELECT * FROM accounts WHERE portfolio_id=?', (pid,))}
    instruments = {i['id']: i for i in rows(c, 'SELECT * FROM instruments')}
    existing = existing_signatures(c, pid)
    preview = []
    inserts = []
    for raw in incoming:
        line = raw['_line']
        try:
            d, values = resolve_row(c, raw, pid, accounts, instruments, validate_transaction)
            signature = canonical(dict(zip(('portfolio_id','account_id','target_account_id','instrument_id',
                'type','occurred_at','quantity','price','amount','target_amount','fee','tax','note','fx_rate',
                'franking_credit'), values)))
            if existing[signature]:
                existing[signature] -= 1
                status, detail = 'duplicate', 'Already present in this portfolio'
            elif d['type'] in ('adjustment_in', 'adjustment_out'):
                status, detail = 'review', 'Cash correction excluded; restore the original database or enter missing deposits instead'
            else:
                status, detail = 'ready', ''
                inserts.append(values)
            summary = f"{d['occurred_at']} · {d['type']} · {accounts[d['account_id']]['name']}"
            if d['instrument_id']:
                summary += ' · ' + instruments[d['instrument_id']]['symbol']
        except (ValueError, TypeError) as exc:
            status, detail = 'error', str(exc)
            summary = f"Row {line} · {raw.get('type', '?')}"
        preview.append({'line': line, 'summary': summary, 'status': status, 'detail': detail})
    return preview, inserts


@app.post('/api/import/preview')
@auth(admin=True)
def preview_import():
    from itsdangerous import URLSafeTimedSerializer
    pid = portfolio_id()
    upload = request.files.get('file')
    if not upload or not upload.filename.lower().endswith('.csv'):
        return problem('Select a CSV file')
    blob = upload.read()
    try:
        with db() as c:
            preview, inserts = prepare_import(c, pid, blob)
    except ValueError as exc:
        return problem(str(exc))
    token = URLSafeTimedSerializer(app.secret_key, salt='transaction-import').dumps(
        {'digest': __import__('hashlib').sha256(blob).hexdigest(), 'pid': pid, 'uid': session['user_id']})
    return jsonify(rows=preview, ready=len(inserts), duplicates=sum(x['status']=='duplicate' for x in preview),
                   errors=sum(x['status']=='error' for x in preview), token=token)


@app.post('/api/import/commit')
@auth(admin=True)
def commit_import():
    from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
    pid = portfolio_id()
    upload = request.files.get('file')
    token = request.form.get('token', '')
    if not upload or not upload.filename.lower().endswith('.csv'):
        return problem('Select the previewed CSV file')
    blob = upload.read()
    try:
        signed = URLSafeTimedSerializer(app.secret_key, salt='transaction-import').loads(token, max_age=1800)
    except (BadSignature, SignatureExpired):
        return problem('Preview expired; preview the CSV again')
    if (signed.get('digest') != __import__('hashlib').sha256(blob).hexdigest() or
            signed.get('pid') != pid or signed.get('uid') != session['user_id']):
        return problem('File or portfolio changed; preview again')
    try:
        with db() as c:
            preview, inserts = prepare_import(c, pid, blob)
            if any(x['status']=='error' for x in preview):
                raise ValueError('Some rows cannot be imported; fix them and preview again')
            for values in inserts:
                cur = c.execute('''INSERT INTO transactions(portfolio_id,account_id,target_account_id,instrument_id,type,
                    occurred_at,quantity,price,amount,target_amount,fee,tax,note,fx_rate,franking_credit)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', values)
                audit(c, pid, cur.lastrowid, 'import',
                      after=c.execute('SELECT * FROM transactions WHERE id=?', (cur.lastrowid,)).fetchone())
            check_positions(c, pid)
        return jsonify(imported=len(inserts), skipped=len(preview)-len(inserts))
    except (ValueError, sqlite3.IntegrityError) as exc:
        return problem(str(exc))


@app.get('/api/reinvestments')
@auth()
def list_reinvestments():
    pid = portfolio_id()
    with db() as c:
        return jsonify(rows(c, '''SELECT r.id,r.dividend_transaction_id,r.buy_transaction_id,
            d.occurred_at dividend_day,b.occurred_at buy_day,d.account_id,d.instrument_id,
            a.name account_name,a.currency,i.symbol,d.amount,d.tax,d.franking_credit,
            b.quantity,b.price,b.fee,d.note FROM reinvestments r
            JOIN transactions d ON d.id=r.dividend_transaction_id
            JOIN transactions b ON b.id=r.buy_transaction_id
            JOIN accounts a ON a.id=d.account_id JOIN instruments i ON i.id=d.instrument_id
            WHERE r.portfolio_id=? ORDER BY d.occurred_at DESC,r.id DESC''',(pid,)))


def reinvestment_values(c, pid, payload):
    dividend_day = iso_day(payload.get('dividend_day'))
    buy_day = iso_day(payload.get('buy_day') or dividend_day)
    if buy_day < dividend_day:
        raise ValueError('Share allotment cannot be before the dividend payment date')
    shared={'account_id':payload.get('account_id'),'instrument_id':payload.get('instrument_id'),
            'note':str(payload.get('note','')).strip()[:500]}
    dividend=validate_transaction(c,{**shared,'type':'dividend','occurred_at':dividend_day,
        'amount':payload.get('amount'),'tax':payload.get('tax'),
        'franking_credit':payload.get('franking_credit')},pid)
    if Decimal(str(dividend[8])) < Decimal(str(dividend[11])):
        raise ValueError('Tax withheld cannot exceed the gross dividend')
    buy=validate_transaction(c,{**shared,'type':'buy','occurred_at':buy_day,
        'quantity':payload.get('quantity'),'price':payload.get('price'),
        'fee':payload.get('fee')},pid)
    # The income is assessable even when reinvested. Each share allotment is a new cost parcel.
    return dividend,buy


TX_COLUMNS='portfolio_id,account_id,target_account_id,instrument_id,type,occurred_at,quantity,price,amount,target_amount,fee,tax,note,fx_rate,franking_credit'
TX_SET=','.join(f'{column}=?' for column in TX_COLUMNS.split(','))


@app.post('/api/reinvestments')
@auth(admin=True)
def add_reinvestment():
    pid=portfolio_id()
    try:
        with db() as c:
            dividend,buy=reinvestment_values(c,pid,request.get_json() or {})
            first=c.execute(f'INSERT INTO transactions({TX_COLUMNS}) VALUES({",".join(["?"]*15)})',dividend).lastrowid
            second=c.execute(f'INSERT INTO transactions({TX_COLUMNS}) VALUES({",".join(["?"]*15)})',buy).lastrowid
            check_positions(c,pid)
            rid=c.execute('INSERT INTO reinvestments(portfolio_id,dividend_transaction_id,buy_transaction_id) VALUES(?,?,?)',
                          (pid,first,second)).lastrowid
            for tid in (first,second):
                audit(c,pid,tid,'create',after=c.execute('SELECT * FROM transactions WHERE id=?',(tid,)).fetchone())
        return jsonify(ok=True,id=rid,dividend_transaction_id=first,buy_transaction_id=second)
    except (ValueError,sqlite3.IntegrityError) as exc:
        return problem(str(exc))


@app.put('/api/reinvestments/<int:rid>')
@auth(admin=True)
def edit_reinvestment(rid):
    pid=portfolio_id()
    try:
        with db() as c:
            group=c.execute('SELECT * FROM reinvestments WHERE id=? AND portfolio_id=?',(rid,pid)).fetchone()
            if not group:return problem('Reinvestment not found',404)
            dividend,buy=reinvestment_values(c,pid,request.get_json() or {})
            for tid,values in ((group['dividend_transaction_id'],dividend),(group['buy_transaction_id'],buy)):
                old=c.execute('SELECT * FROM transactions WHERE id=?',(tid,)).fetchone()
                c.execute(f'UPDATE transactions SET {TX_SET} WHERE id=?',values+(tid,))
                audit(c,pid,tid,'update',before=old,after=c.execute('SELECT * FROM transactions WHERE id=?',(tid,)).fetchone())
            check_positions(c,pid)
        return jsonify(ok=True)
    except (ValueError,sqlite3.IntegrityError) as exc:
        return problem(str(exc))


@app.delete('/api/reinvestments/<int:rid>')
@auth(admin=True)
def delete_reinvestment(rid):
    pid=portfolio_id()
    try:
        with db() as c:
            group=c.execute('SELECT * FROM reinvestments WHERE id=? AND portfolio_id=?',(rid,pid)).fetchone()
            if not group:return problem('Reinvestment not found',404)
            before=[c.execute('SELECT * FROM transactions WHERE id=?',(group[key],)).fetchone()
                    for key in ('dividend_transaction_id','buy_transaction_id')]
            c.execute('DELETE FROM reinvestments WHERE id=?',(rid,))
            for tx in before:
                c.execute('DELETE FROM transactions WHERE id=?',(tx['id'],))
                audit(c,pid,tx['id'],'delete',before=tx)
            check_positions(c,pid)
        return jsonify(ok=True)
    except ValueError as exc:
        return problem(str(exc))
