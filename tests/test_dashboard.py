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


def test_csv_import_previews_duplicates_and_is_atomic(database):
    import io
    from csv_import import parse_csv
    with database() as c:
        aid=account(c,1);iid=instrument(c,'VAS')
        trade(c,1,aid,iid,'buy','2026-09-20',2,10)
        csv_bytes=(f'date,type,account,symbol,quantity,price,amount,fee\n'
                   f'2026-09-20,buy,AUD cash,VAS,2,10,0,0\n'
                   f'2026-09-22,buy,AUD cash,VAS,1,12,0,0\n').encode()
        assert len(parse_csv(csv_bytes))==2
        preview, inserts=dashboard.prepare_import(c,1,csv_bytes)
        assert [r['status'] for r in preview]==['duplicate','ready']
        assert len(inserts)==1
        c.execute('''INSERT INTO transactions(portfolio_id,account_id,target_account_id,instrument_id,type,
          occurred_at,quantity,price,amount,target_amount,fee,tax,note,fx_rate,franking_credit)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',inserts[0])
        again, none=dashboard.prepare_import(c,1,csv_bytes)
        assert [r['status'] for r in again]==['duplicate','duplicate']
        assert none==[]


def test_financial_year_fifo_split_and_dividend(database):
    from reports import financial_year
    with database() as c:
        aid=account(c,1);iid=instrument(c,'VAS')
        trade(c,1,aid,iid,'buy','2025-01-01',10,10,2)
        trade(c,1,aid,iid,'buy','2025-06-01',10,20,2)
        trade(c,1,aid,iid,'split','2025-07-01',20)
        trade(c,1,aid,iid,'sell','2026-03-01',25,15,5)
        cash_tx(c,1,aid,'dividend','2026-04-01',10,iid=iid)
        c.execute("UPDATE transactions SET franking_credit=3 WHERE type='dividend'")
        result=financial_year(c,1,2026)
    assert [round(s['quantity'],2) for s in result['sales']]==[20,5]
    assert result['sales'][0]['cost_aud']==pytest.approx(102)
    assert result['sales'][0]['discount_eligible'] is True
    assert result['sales'][1]['discount_eligible'] is False
    assert result['totals']['dividend_aud']==10
    assert result['totals']['franking_credit_aud']==3


def test_return_metrics_need_contributions_and_valid_opening():
    from reports import cashflow_returns
    values=[{'day':'2025-01-01','value':100,'invested':100},
            {'day':'2026-01-01','value':110,'invested':100}]
    result=cashflow_returns(values)
    assert result['twr_pct']==pytest.approx(10)
    assert result['xirr_pct']==pytest.approx(10,abs=.1)
    assert cashflow_returns([{'day':'2026-01-01','value':110,'invested':0}])['xirr_pct'] is None


def test_import_endpoints_require_preview_and_portfolio_scope(database):
    import io
    with database() as c:
        account(c,1)
        instrument(c,'VAS')
    client=dashboard.app.test_client()
    csrf=login(client,'adam',os.environ['ADMIN_PASSWORD'])
    header={'X-CSRF-Token':csrf}
    raw=b'date,type,account,symbol,quantity,price\n2026-09-20,buy,AUD cash,VAS,2,10\n'
    def upload(endpoint, blob=raw, token=None):
        data={'file':(io.BytesIO(blob),'trades.csv'),'portfolio_id':'1'}
        if token:data['token']=token
        return client.post(endpoint,data=data,headers=header)
    preview=upload('/api/import/preview')
    assert preview.status_code==200 and preview.json['ready']==1
    assert upload('/api/import/commit',token='bad').status_code==400
    assert upload('/api/import/commit',raw+b'\n',preview.json['token']).status_code==400
    committed=upload('/api/import/commit',token=preview.json['token'])
    assert committed.json['imported']==1
    assert upload('/api/import/preview').json['duplicates']==1
    viewer=dashboard.app.test_client();login(viewer,'brother',os.environ['VIEWER_PASSWORD'])
    assert viewer.get('/api/financial-year?end_year=2026&portfolio_id=1').json['sales']==[]
    assert viewer.get('/api/import/template.csv').status_code==403


def test_reinvestment_is_atomic_editable_and_keeps_dividend_income(database):
    with database() as c:
        aid=account(c,1)
        iid=instrument(c,'VAS')
        cash_tx(c,1,aid,'deposit','2026-01-01',100)
        trade(c,1,aid,iid,'buy','2026-01-01',2,10)
        price(c,iid,'2026-09-29',12)
    client=dashboard.app.test_client()
    csrf=login(client,'adam',os.environ['ADMIN_PASSWORD'])
    headers={'X-CSRF-Token':csrf}
    payload={'portfolio_id':1,'account_id':aid,'instrument_id':iid,'dividend_day':'2026-09-20',
             'buy_day':'2026-09-22','amount':10,'tax':1,'franking_credit':2,
             'quantity':1,'price':8,'fee':0,'note':'DRP'}
    response=client.post('/api/reinvestments',json=payload,headers=headers)
    assert response.status_code==200,response.json
    rid=response.json['id']
    with database() as c:
        result=dashboard.calculate(c,1)
        assert result['holdings'][0]['quantity']==3
        assert result['cash'][0]['balance']==pytest.approx(81)
        from reports import financial_year
        report=financial_year(c,1,2027)
        assert report['totals']['dividend_aud']==pytest.approx(10)
        assert report['totals']['franking_credit_aud']==pytest.approx(2)
    assert client.put(f"/api/transactions/{response.json['buy_transaction_id']}",json=payload,headers=headers).status_code==400
    payload['quantity']=2
    assert client.put(f'/api/reinvestments/{rid}',json=payload,headers=headers).status_code==200
    with database() as c:
        result=dashboard.calculate(c,1)
        assert result['holdings'][0]['quantity']==4
        assert result['cash'][0]['balance']==pytest.approx(73)
    assert client.delete(f'/api/reinvestments/{rid}?portfolio_id=1',headers=headers).status_code==200
    with database() as c:
        assert dashboard.calculate(c,1)['holdings'][0]['quantity']==2
        assert c.execute('SELECT COUNT(*) FROM reinvestments').fetchone()[0]==0


def test_reinvestment_rejects_wrong_portfolio_and_invalid_amounts(database):
    with database() as c:
        aid=account(c,1);iid=instrument(c,'VAS')
    admin=dashboard.app.test_client();csrf=login(admin,'adam',os.environ['ADMIN_PASSWORD'])
    payload={'portfolio_id':1,'account_id':aid,'instrument_id':iid,'dividend_day':'2026-09-20',
             'buy_day':'2026-09-20','amount':2,'tax':3,'quantity':1,'price':2}
    assert admin.post('/api/reinvestments',json=payload,headers={'X-CSRF-Token':csrf}).status_code==400
    viewer=dashboard.app.test_client();vcsrf=login(viewer,'brother',os.environ['VIEWER_PASSWORD'])
    assert viewer.post('/api/reinvestments',json=payload,headers={'X-CSRF-Token':vcsrf}).status_code==403
    assert viewer.get('/api/reinvestments?portfolio_id=1').json==[]


def test_docker_build_includes_runtime_modules(tmp_path):
    """Reproduce Dockerfile COPY layout without requiring a Docker daemon."""
    import shutil
    project=Path(__file__).resolve().parents[1]
    lines=project.joinpath('Dockerfile').read_text().splitlines()
    copy=next(line for line in lines if line.startswith('COPY app.py '))
    sources=copy.split()[1:-1]
    for source in sources:
        shutil.copy(project/source,tmp_path/source)
    env={**os.environ,'DATA_DIR':str(tmp_path/'data'),'ADMIN_PASSWORD':'test-admin-secret-123',
         'VIEWER_PASSWORD':'test-viewer-secret-123'}
    result=subprocess.run([sys.executable,'-c',
        "import app; c=app.app.test_client(); c.post('/api/login', json={'username':'adam','password':'test-admin-secret-123'}); assert c.get('/api/dashboard').status_code==200; assert c.get('/api/financial-year?end_year=2027').status_code==200"],
        cwd=tmp_path,env=env,text=True,capture_output=True)
    assert result.returncode==0,result.stderr
