from test_dashboard import database, account, instrument, trade, price
import app as dashboard
import pytest


def client_for(role='admin',pid=1):
    client=dashboard.app.test_client()
    with client.session_transaction() as s:
        s.update(user_id=1 if role=='admin' else 2,role=role,portfolio_id=pid,session_version=0,csrf='test')
    return client


def test_history_averages_sold_holdings_and_portfolio_privacy(database):
    with database() as c:
        a=account(c,1); b=account(c,2); iid=instrument(c,'QUAL')
        trade(c,1,a,iid,'buy','2026-09-20',10,10,2)
        trade(c,1,a,iid,'buy','2026-09-21',10,20,2)
        trade(c,1,a,iid,'sell','2026-09-22',20,25,2)
        trade(c,2,b,iid,'buy','2026-09-19',2,9)
        price(c,iid,'2026-09-29',26)
    r=client_for().get('/api/decisions').json
    assert len(r['events'])==3
    assert r['events'][0]['kind']=='sell'
    assert r['events'][0]['quantity_after']==0
    assert r['events'][0]['realized']==pytest.approx(194)
    assert r['events'][1]['average_after']==pytest.approx(15.2)
    assert len(r['holdings'])==1
    viewer=client_for('viewer',2)
    data=viewer.get('/api/decisions?portfolio_id=1').json
    assert len(data['events'])==1
    assert data['events'][0]['portfolio_id']==2
    assert viewer.post('/api/decisions/reviews',json={},headers={'X-CSRF-Token':'test'}).status_code==403


def test_notes_review_research_and_deletion_cascade(database):
    with database() as c:
        a=account(c,1); iid=instrument(c,'BGBL'); peer=instrument(c,'QUAL')
        trade(c,1,a,iid,'buy','2026-09-20',10,10)
        tid=c.execute('SELECT id FROM transactions').fetchone()['id']
    client=client_for();headers={'X-CSRF-Token':'test'}
    assert client.put(f'/api/decisions/transactions/{tid}',json={'reason':'Long term','basis':'average','target_price':20},headers=headers).status_code==200
    assert client.post('/api/decisions/reviews',json={'instrument_id':iid,'day':'2026-09-22','kind':'hold','reason':'Still valid'},headers=headers).status_code==200
    assert client.put(f'/api/decisions/research/{iid}',json={'description':'Global equities','peers':[peer],'reviewed_on':'2026-09-29'},headers=headers).status_code==200
    data=client.get('/api/decisions').json
    assert data['events'][0]['kind']=='hold'
    assert data['events'][1]['notes']['target_price']==20
    assert data['holdings'][0]['peers'][0]['symbol']=='QUAL'
    assert client_for('viewer',2).get('/api/decisions').json['holdings']==[]
    assert client.put(f'/api/decisions/transactions/{tid}',json={'portfolio_id':2,'reason':'wrong portfolio'},headers=headers).status_code==404
    assert client.put(f'/api/decisions/transactions/{tid}',json={'target_price':'nan'},headers=headers).status_code==400
    with database() as c:
        c.execute('DELETE FROM transactions WHERE id=?',(tid,))
        assert c.execute('SELECT COUNT(*) FROM decision_notes').fetchone()[0]==0
    assert len(client.get('/api/decisions').json['events'])==1


def test_existing_database_upgrade_keeps_transactions(database):
    with database() as c:
        a=account(c,1); iid=instrument(c,'BGBL')
        trade(c,1,a,iid,'buy','2026-09-20',10,10)
        for table in ('holding_research','decision_notes','decision_reviews'):
            c.execute(f'DROP TABLE {table}')
    dashboard.init_db()
    data=client_for().get('/api/decisions').json
    assert len(data['events'])==1
    assert data['events'][0]['quantity']==10
