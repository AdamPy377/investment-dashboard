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
        ''')
        for pid, label in [(1, os.getenv('ADMIN_USERNAME', 'Adam').title()),
                           (2, os.getenv('VIEWER_USERNAME', 'Brother').title())]:
            c.execute('INSERT OR IGNORE INTO portfolios(id,name) VALUES(?,?)', (pid, label))
        for username, password, role, pid in [
            (os.getenv('ADMIN_USERNAME', 'adam'), os.getenv('ADMIN_PASSWORD'), 'admin', 1),
            (os.getenv('VIEWER_USERNAME', 'brother'), os.getenv('VIEWER_PASSWORD'), 'viewer', 2),
        ]:
            if password and len(password) >= 12:
                c.execute('INSERT OR IGNORE INTO users(username,password_hash,role,portfolio_id) VALUES(?,?,?,?)',
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
        return jsonify(authenticated=False, configured=bool(os.getenv('ADMIN_PASSWORD') and os.getenv('VIEWER_PASSWORD')))
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
                   portfolio_id=user['portfolio_id'], csrf=secrets.token_urlsafe(32))
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
        c.execute('UPDATE users SET password_hash=? WHERE id=?', (generate_password_hash(replacement), user['id']))
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


@app.post('/api/instruments')
@auth(admin=True)
def add_instrument():
    d = request.get_json() or {}
    symbol = str(d.get('symbol', '')).strip().upper()
    exchange = str(d.get('exchange', '')).strip().upper()
    name = str(d.get('name', '')).strip()[:150]
    currency = 'AUD' if exchange == 'AU' else 'USD'
    if not re.fullmatch(r'[A-Z0-9.\-]{1,20}', symbol) or exchange not in ('AU', 'US') or not name:
        return problem('Enter a name, ticker, and AU or US exchange')
    with db() as c:
        c.execute('INSERT OR IGNORE INTO instruments(symbol,name,exchange,currency) VALUES(?,?,?,?)',
                  (symbol, name, exchange, currency))
    return jsonify(ok=True)


TYPES = {'buy', 'sell', 'deposit', 'withdrawal', 'dividend', 'interest', 'fee', 'transfer', 'fx', 'split'}


def validate_transaction(c, d, pid):
    kind = d.get('type')
    if kind not in TYPES:
        raise ValueError('Choose a transaction type')
    account = get_owned(c, 'accounts', d.get('account_id'), pid)
    if not account:
        raise ValueError('Choose an account in this portfolio')
    target = get_owned(c, 'accounts', d.get('target_account_id'), pid) if d.get('target_account_id') else None
    instrument = c.execute('SELECT * FROM instruments WHERE id=?', (d.get('instrument_id'),)).fetchone() if d.get('instrument_id') else None
    quantity = num(d.get('quantity', 0), 'Quantity')
    price = num(d.get('price', 0), 'Price')
    amount = num(d.get('amount', 0), 'Amount')
    target_amount = num(d.get('target_amount', 0), 'Received amount')
    fee = num(d.get('fee', 0), 'Fee')
    tax = num(d.get('tax', 0), 'Tax withheld')
    day = iso_day(d.get('occurred_at'))
    if kind in ('buy', 'sell') and (not instrument or instrument['currency'] != account['currency'] or quantity <= 0 or price <= 0):
        raise ValueError('Buy and sell need a holding, matching currency, quantity and price')
    if kind == 'split' and (not instrument or instrument['currency'] != account['currency'] or quantity <= 0):
        raise ValueError('Split needs a holding and the additional number of shares')
    if kind in ('deposit', 'withdrawal', 'dividend', 'interest', 'fee') and amount <= 0:
        raise ValueError('Enter an amount greater than zero')
    if kind == 'dividend' and not instrument:
        raise ValueError('Choose a holding for the dividend')
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
            target_amount, fee, tax, str(d.get('note', '')).strip()[:500])


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
                occurred_at,quantity,price,amount,target_amount,fee,tax,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''', values)
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
            values = validate_transaction(c, request.get_json() or {}, pid)
            c.execute('''UPDATE transactions SET portfolio_id=?,account_id=?,target_account_id=?,instrument_id=?,type=?,
                occurred_at=?,quantity=?,price=?,amount=?,target_amount=?,fee=?,tax=?,note=? WHERE id=?''', values + (tid,))
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


def fetch_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'PersonalPortfolio/1.0'}), timeout=15) as response:
        return json.load(response)


def sync_market():
    result = {'updated': [], 'errors': []}
    key = os.getenv('EODHD_API_KEY', '').strip()
    with db() as c:
        earliest_usd = c.execute('''SELECT MIN(t.occurred_at) FROM transactions t JOIN accounts a ON a.id=t.account_id
            WHERE a.currency='USD' ''').fetchone()[0]
        if earliest_usd:
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
            except Exception as e:
                result['errors'].append('FX: ' + str(e)[:90])
        if not key:
            return result
        instruments = rows(c, '''SELECT i.*, MIN(t.occurred_at) first_day FROM instruments i
            JOIN transactions t ON t.instrument_id=i.id GROUP BY i.id''')
        for i in instruments:
            ticker = urllib.parse.quote(i['symbol'] + '.' + i['exchange'])
            params = {'api_token': key, 'fmt': 'json'}
            latest = c.execute('SELECT MAX(day) FROM prices WHERE instrument_id=? AND source=?',
                               (i['id'], 'EODHD daily')).fetchone()[0]
            params['from'] = max(i['first_day'], str(date.fromisoformat(latest) - timedelta(days=2))) if latest else i['first_day']
            try:
                historical = fetch_json(f'https://eodhd.com/api/eod/{ticker}?' + urllib.parse.urlencode(params))
                if not isinstance(historical, list):
                    raise ValueError('Historical feed returned an error')
                for p in historical:
                    c.execute('''INSERT INTO prices VALUES(?,?,?,?,?) ON CONFLICT(instrument_id,day) DO UPDATE SET
                        close=excluded.close,source=excluded.source,updated_at=excluded.updated_at
                        WHERE prices.source != 'manual' ''',
                              (i['id'], iso_day(p['date']), num(p['close'], 'Close', False),
                               'EODHD daily', datetime.utcnow().isoformat() + 'Z'))
                current = fetch_json(f'https://eodhd.com/api/real-time/{ticker}?' +
                                     urllib.parse.urlencode({'api_token': key, 'fmt': 'json'}))
                close = num(current.get('close'), 'Price', False)
                stamp = current.get('timestamp')
                zone = ZoneInfo('America/New_York' if i['exchange'] == 'US' else 'Australia/Sydney')
                price_day = datetime.fromtimestamp(int(stamp), zone).date().isoformat() if stamp else local_today().isoformat()
                c.execute('''INSERT INTO prices VALUES(?,?,?,?,?) ON CONFLICT(instrument_id,day) DO UPDATE SET
                    close=excluded.close, source=excluded.source, updated_at=excluded.updated_at
                    WHERE prices.source != 'manual' ''',
                    (i['id'], price_day, close, 'EODHD delayed', datetime.utcnow().isoformat() + 'Z'))
                result['updated'].append(i['symbol'])
            except Exception as e:
                result['errors'].append(f"{i['symbol']}: {str(e)[:90]}")
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
                if t['currency'] == 'USD' and rate is None:
                    missing_contribution_fx = True
                else:
                    contributed_aud += amount * (rate if t['currency'] == 'USD' else 1)
            elif kind == 'withdrawal':
                current_cash[aid] -= amount
                if t['currency'] == 'USD' and rate is None:
                    missing_contribution_fx = True
                else:
                    contributed_aud -= amount * (rate if t['currency'] == 'USD' else 1)
            elif kind == 'dividend':
                current_cash[aid] += amount - tax - fee
            elif kind == 'interest':
                current_cash[aid] += amount - tax
            elif kind == 'fee':
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
    for iid, qty in shares.items():
        if qty <= 0.00000001:
            continue
        inst = instruments[iid]
        quote = latest_quotes.get(iid)
        value = qty * quote['close'] if quote else None
        holdings.append({**inst, 'quantity': qty, 'cost': costs.get(iid, 0),
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
    for a in cash:
        a['holding_value'] = 0.0
        a['unpriced'] = False
        for (aid, iid), quantity in account_shares.items():
            if aid != a['id'] or quantity <= 0.00000001:
                continue
            quote = latest_quotes.get(iid)
            if quote:
                a['holding_value'] += quantity * quote['close']
            else:
                a['unpriced'] = True
        a['account_value'] = None if a['unpriced'] else a['balance'] + a['holding_value']
    current = series[-1]
    prev = series[-2] if len(series) > 1 else None
    day_change = (current['value'] - prev['value'] - (current['invested'] - prev['invested'])
                  if prev and None not in (current['value'], prev['value'], current['invested'], prev['invested']) else None)
    return {'holdings': holdings, 'cash': cash, 'series': series,
            'value': current['value'], 'invested': current['invested'],
            'profit': current['value'] - current['invested'] if current['value'] is not None and current['invested'] is not None else None,
            'day_change': day_change, 'fx_rate': rate,
            'fx_day': fx[fx_i - 1]['day'] if fx_i else None,
            'unpriced': current['unpriced']}


@app.get('/api/dashboard')
@auth()
def dashboard():
    with db() as c:
        return jsonify(calculate(c, portfolio_id()))


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
@auth()
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
        with db() as c:
            if not get_owned(c, 'accounts', d.get('account_id'), pid):
                return problem('Account not found')
            c.execute('INSERT INTO reconciliations(portfolio_id,account_id,day,reported_value,note) VALUES(?,?,?,?,?)',
                      (pid, d['account_id'], day, reported, str(d.get('note', ''))[:300]))
        return jsonify(ok=True)
    except ValueError as e:
        return problem(str(e))


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
