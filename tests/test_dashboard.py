"""Regression checks for portfolio math and two-account access controls."""
import os
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

import pytest

os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='dashboard-tests-'))
os.environ.setdefault('ADMIN_PASSWORD', 'test-admin-secret-123')
os.environ.setdefault('VIEWER_PASSWORD', 'test-viewer-secret-123')
import app as dashboard


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, 'DATA', tmp_path)
    monkeypatch.setattr(dashboard, 'DB', tmp_path / 'portfolio.sqlite3')
    monkeypatch.setattr(dashboard, 'local_today', lambda: date(2026, 9, 29))
    monkeypatch.setattr(dashboard, 'closed_market_day', lambda exchange, now=None: '2026-09-29')
    dashboard.init_db()
    return dashboard.db


def account(c, pid, currency='AUD'):
    return c.execute('INSERT INTO accounts(portfolio_id,name,broker,currency,kind) VALUES(?,?,?,?,?)',
                     (pid, f'{currency} cash', 'Stake', currency, 'broker')).lastrowid


def instrument(c, symbol, exchange='AU'):
    return c.execute('INSERT INTO instruments(symbol,name,exchange,currency) VALUES(?,?,?,?)',
                     (symbol, symbol, exchange, 'AUD' if exchange == 'AU' else 'USD')).lastrowid


def trade(c, pid, account_id, iid, kind, day, qty, price=0, fee=0):
    c.execute('''INSERT INTO transactions(portfolio_id,account_id,instrument_id,type,occurred_at,quantity,price,fee)
        VALUES(?,?,?,?,?,?,?,?)''', (pid, account_id, iid, kind, day, qty, price, fee))


def cash_tx(c, pid, aid, kind, day, amount, fx=0, iid=None):
    c.execute('''INSERT INTO transactions(portfolio_id,account_id,instrument_id,type,occurred_at,amount,fx_rate)
        VALUES(?,?,?,?,?,?,?)''', (pid, aid, iid, kind, day, amount, fx))


def price(c, iid, day, close):
    c.execute('INSERT INTO prices VALUES(?,?,?,?,?)', (iid, day, close, 'manual', day))


def test_session_move_includes_usd_fx_cash_and_ignores_cash_flows(database):
    with database() as c:
        aud, usd = account(c, 1), account(c, 1, 'USD')
        vas, amd = instrument(c, 'VAS'), instrument(c, 'AMD', 'US')
        cash_tx(c, 1, aud, 'deposit', '2026-09-20', 1000)
        cash_tx(c, 1, usd, 'deposit', '2026-09-20', 100, 1.4)
        trade(c, 1, aud, vas, 'buy', '2026-09-20', 10, 90, 5)
        trade(c, 1, usd, amd, 'buy', '2026-09-20', 2, 40)
        cash_tx(c, 1, usd, 'dividend', '2026-09-28', 5, iid=amd)
        cash_tx(c, 1, usd, 'fee', '2026-09-28', 2)
        for iid, day, close in [(vas, '2026-09-28', 100), (vas, '2026-09-29', 105),
                                (amd, '2026-09-25', 50), (amd, '2026-09-28', 60)]:
            price(c, iid, day, close)
        c.execute("INSERT INTO fx VALUES('2026-09-25',1.4,'manual')")
        c.execute("INSERT INTO fx VALUES('2026-09-28',1.5,'manual')")
        result = dashboard.calculate(c, 1)
    # 10 VAS × $5 + 2 AMD × $10 × 1.5 + (23 cash + 2 × $50) × 0.1 FX.
    assert result['session_move'] == pytest.approx(92.3)
    assert result['value'] == pytest.approx(1359.5)
    assert result['profit'] == pytest.approx(219.5)
    assert [h['symbol'] for h in result['stale_holdings']] == ['AMD']


def test_sales_splits_and_dividends_do_not_create_fake_contributions(database):
    with database() as c:
        aid = account(c, 1)
        iid = instrument(c, 'VAS')
        cash_tx(c, 1, aid, 'deposit', '2026-09-20', 300)
        trade(c, 1, aid, iid, 'buy', '2026-09-20', 10, 10)
        trade(c, 1, aid, iid, 'sell', '2026-09-21', 4, 15, 1)
        trade(c, 1, aid, iid, 'split', '2026-09-22', 6)
        cash_tx(c, 1, aid, 'dividend', '2026-09-24', 3, iid=iid)
        price(c, iid, '2026-09-28', 5)
        price(c, iid, '2026-09-29', 6)
        result = dashboard.calculate(c, 1)
    holding = result['holdings'][0]
    assert holding['quantity'] == 12
    assert holding['cost'] == pytest.approx(60)
    assert holding['realized'] == pytest.approx(19)
    assert result['invested'] == pytest.approx(300)
    assert result['session_move'] == pytest.approx(12)
    assert result['value'] == pytest.approx(334)
    assert result['profit'] == pytest.approx(34)


def test_missing_price_does_not_invent_a_historical_value(database):
    with database() as c:
        aid = account(c, 1)
        iid = instrument(c, 'VAS')
        cash_tx(c, 1, aid, 'deposit', '2026-09-27', 100)
        trade(c, 1, aid, iid, 'buy', '2026-09-27', 1, 50)
        price(c, iid, '2026-09-29', 55)
        result = dashboard.calculate(c, 1)
    assert result['series'][0]['value'] is None
    assert result['value'] == pytest.approx(105)
    assert result['session_move'] is None


def login(client, username, password):
    response = client.post('/api/login', json={'username': username, 'password': password})
    assert response.status_code == 200
    return response.json['csrf']


def test_viewer_only_first_four_sections_and_admin_reset_revokes_sessions(database):
    admin, viewer = dashboard.app.test_client(), dashboard.app.test_client()
    admin_csrf = login(admin, 'adam', os.environ['ADMIN_PASSWORD'])
    viewer_csrf = login(viewer, 'brother', os.environ['VIEWER_PASSWORD'])
    assert viewer.get('/api/dashboard?portfolio_id=1').status_code == 200
    assert viewer.get('/api/reconciliations').status_code == 403
    assert viewer.get('/api/setup').json['portfolios'] == [{'id': 2, 'name': 'Brother'}]
    assert viewer.post('/api/admin/reset-viewer-password', json={}, headers={'X-CSRF-Token': viewer_csrf}).status_code == 403
    assert admin.post('/api/admin/reset-viewer-password', json={
        'admin_password': 'wrong', 'new_password': 'replacement-pass-123'},
        headers={'X-CSRF-Token': admin_csrf}).status_code == 403
    assert viewer.get('/api/dashboard').status_code == 200
    reset = admin.post('/api/admin/reset-viewer-password', json={
        'admin_password': os.environ['ADMIN_PASSWORD'], 'new_password': 'replacement-pass-123'},
        headers={'X-CSRF-Token': admin_csrf})
    assert reset.status_code == 200
    assert viewer.get('/api/dashboard').status_code == 401
    assert viewer.post('/api/login', json={'username': 'brother', 'password': os.environ['VIEWER_PASSWORD']}).status_code == 401
    login(viewer, 'brother', 'replacement-pass-123')
    assert viewer.get('/api/dashboard').status_code == 200


def test_short_initial_password_fails_at_startup(tmp_path):
    env = {**os.environ, 'DATA_DIR': str(tmp_path), 'ADMIN_PASSWORD': 'short',
           'VIEWER_PASSWORD': 'test-viewer-secret-123'}
    run = subprocess.run([sys.executable, '-c', 'import app'], cwd=Path(__file__).resolve().parents[1],
                         env=env, text=True, capture_output=True)
    assert run.returncode != 0
    assert 'ADMIN_PASSWORD must be at least 12 characters' in run.stderr
