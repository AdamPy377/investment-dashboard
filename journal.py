"""Portfolio-scoped decision history; prices are local quotes, never predictions."""
import json
from flask import request, jsonify


def register_journal(app, env):
    db, auth, pid_fn = env['db'], env['auth'], env['portfolio_id']
    rows, problem, num, iso_day = (env[k] for k in ('rows', 'problem', 'num', 'iso_day'))

    def permitted(c, pid, iid):
        return c.execute('SELECT 1 FROM transactions WHERE portfolio_id=? AND instrument_id=?', (pid, iid)).fetchone() or c.execute('SELECT 1 FROM decision_reviews WHERE portfolio_id=? AND instrument_id=?', (pid, iid)).fetchone()

    def clean(d, research=False):
        fields = ('description','role','risks','entry_points','alternatives','sources','allocation') if research else ('reason','expectation','risks','exit_rule','replacement','lesson','status','basis','horizon')
        result = {key: str(d.get(key) or '').strip()[:10000] for key in fields}
        if research:
            result['peers'] = list(dict.fromkeys(int(x) for x in d.get('peers', [])))[:5]
            result['reviewed_on'] = iso_day(d['reviewed_on']) if d.get('reviewed_on') else ''
        else:
            for key in ('target_price','stop_price','exit_fee','expected_income'):
                result[key] = num(d[key], key) if d.get(key) not in (None, '') else None
            result['target_day'] = iso_day(d['target_day']) if d.get('target_day') else ''
            if result['basis'] not in ('event','average',''): raise ValueError('Choose event or average cost')
            if result['status'] not in ('','open','hold','achieved','changed','closed'): raise ValueError('Choose a valid status')
        return result

    @app.get('/api/decisions')
    @auth()
    def decisions():
        pid = pid_fn()
        with db() as c:
            tx = rows(c, '''SELECT t.*,i.symbol,i.name,i.currency,a.name account_name,
                r.id reinvestment_id FROM transactions t JOIN instruments i ON i.id=t.instrument_id
                JOIN accounts a ON a.id=t.account_id LEFT JOIN reinvestments r ON r.buy_transaction_id=t.id
                WHERE t.portfolio_id=? ORDER BY t.occurred_at,t.id''', (pid,))
            notes = {n['transaction_id']:json.loads(n['data']) for n in rows(c, 'SELECT * FROM decision_notes WHERE portfolio_id=?', (pid,))}
            profiles = {n['instrument_id']:json.loads(n['data']) for n in rows(c, 'SELECT * FROM holding_research WHERE portfolio_id=?', (pid,))}
            reviews = rows(c, 'SELECT * FROM decision_reviews WHERE portfolio_id=?', (pid,))
            iids = set(t['instrument_id'] for t in tx) | set(r['instrument_id'] for r in reviews)
            holdings = []
            for iid in sorted(iids):
                h = dict(c.execute('SELECT * FROM instruments WHERE id=?', (iid,)).fetchone())
                h['research'] = profiles.get(iid, {})
                h['prices'] = rows(c, 'SELECT * FROM prices WHERE instrument_id=? AND day<=? ORDER BY day', (iid, env['local_today']().isoformat()))
                h['peers'] = []
                for peer in h['research'].get('peers', []):
                    item = c.execute('SELECT * FROM instruments WHERE id=?', (peer,)).fetchone()
                    if item:
                        item = dict(item)
                        item['prices'] = rows(c, 'SELECT * FROM prices WHERE instrument_id=? AND day<=? ORDER BY day', (peer, env['local_today']().isoformat()))
                        h['peers'].append(item)
                holdings.append(h)
            pools, events = {}, []
            for t in tx:
                key = (t['instrument_id'],t['account_id'])
                qty, cost = pools.get(key, (0,0))
                avg_before = cost/qty if qty else None
                realized = None
                if t['type']=='buy': qty += t['quantity']; cost += t['quantity']*t['price']+t['fee']
                elif t['type']=='sell':
                    allocated = (avg_before or 0)*t['quantity']
                    realized = t['quantity']*t['price']-t['fee']-allocated
                    qty -= t['quantity']; cost -= allocated
                elif t['type']=='split': qty += t['quantity']
                pools[key] = (qty,cost)
                total_qty = sum(q for (i,a),(q,cost_) in pools.items() if i==t['instrument_id'])
                total_cost = sum(cost_ for (i,a),(q,cost_) in pools.items() if i==t['instrument_id'])
                events.append({**t,'key':f"tx-{t['id']}",'day':t['occurred_at'],
                    'kind':'reinvest' if t['reinvestment_id'] else t['type'],
                    'notes':notes.get(t['id'], {}),'quantity_after':total_qty,
                    'average_after':total_cost/total_qty if total_qty>1e-8 else None,
                    'average_before':avg_before,'realized':realized})
            for r in reviews:
                h = next(h for h in holdings if h['id']==r['instrument_id'])
                events.append({**r,'key':f"review-{r['id']}",'symbol':h['symbol'],'name':h['name'],
                    'currency':h['currency'],'notes':json.loads(r['data'])})
            events.sort(key=lambda e:(e['day'], e['id'], e['key']), reverse=True)
            return jsonify(events=events, holdings=holdings)

    @app.put('/api/decisions/transactions/<int:tid>')
    @auth(admin=True)
    def save_note(tid):
        try:
            pid = pid_fn(); data = clean(request.get_json() or {})
            with db() as c:
                if not c.execute('SELECT 1 FROM transactions WHERE id=? AND portfolio_id=? AND instrument_id IS NOT NULL',(tid,pid)).fetchone(): return problem('Holding event not found',404)
                c.execute('INSERT OR REPLACE INTO decision_notes VALUES(?,?,?)',(pid,tid,json.dumps(data)))
            return jsonify(ok=True)
        except (ValueError,TypeError): return problem('Check note fields, prices and dates')

    @app.route('/api/decisions/reviews', methods=['POST'])
    @app.route('/api/decisions/reviews/<int:rid>', methods=['PUT','DELETE'])
    @auth(admin=True)
    def review(rid=None):
        pid = pid_fn()
        try:
            with db() as c:
                if rid and not c.execute('SELECT 1 FROM decision_reviews WHERE id=? AND portfolio_id=?',(rid,pid)).fetchone(): return problem('Review not found',404)
                if request.method=='DELETE':
                    c.execute('DELETE FROM decision_reviews WHERE id=? AND portfolio_id=?',(rid,pid)); return jsonify(ok=True)
                d=request.get_json() or {}; iid=int(d['instrument_id']); day=iso_day(d['day']); kind=d.get('kind','review')
                if not c.execute('SELECT 1 FROM instruments WHERE id=?',(iid,)).fetchone(): return problem('Holding not found',404)
                if kind not in ('review','hold','plan_change'): raise ValueError()
                data=json.dumps(clean(d))
                if rid: c.execute('UPDATE decision_reviews SET instrument_id=?,day=?,kind=?,data=? WHERE id=? AND portfolio_id=?',(iid,day,kind,data,rid,pid))
                else: c.execute('INSERT INTO decision_reviews(portfolio_id,instrument_id,day,kind,data) VALUES(?,?,?,?,?)',(pid,iid,day,kind,data))
            return jsonify(ok=True)
        except (ValueError,TypeError,KeyError): return problem('Check holding, review date and fields')

    @app.put('/api/decisions/research/<int:iid>')
    @auth(admin=True)
    def research(iid):
        try:
            pid=pid_fn(); d=clean(request.get_json() or {}, True)
            with db() as c:
                if not permitted(c,pid,iid): return problem('Record a holding event or review first',404)
                for peer in d['peers']:
                    if peer==iid or not c.execute('SELECT 1 FROM instruments WHERE id=?',(peer,)).fetchone(): raise ValueError()
                c.execute('INSERT OR REPLACE INTO holding_research VALUES(?,?,?)',(pid,iid,json.dumps(d)))
            return jsonify(ok=True)
        except (ValueError,TypeError): return problem('Check research date and comparison holdings')
