"""Portfolio returns and illustrative per-parcel Australian financial-year schedule."""
from datetime import date
from decimal import Decimal
from collections import defaultdict


def cashflow_returns(series):
    """Flows are at day-end; valuation is at day-end. XIRR uses actual dated flows."""
    if not series or series[-1]['value'] is None or series[-1]['invested'] is None:
        return {'xirr_pct': None, 'twr_pct': None}
    flows = []
    previous_contribution = 0
    twr = 1.0
    previous_value = None
    valid_twr = True
    for point in series:
        invested = point['invested']
        if invested is None:
            valid_twr = False
            continue
        delta = invested - previous_contribution
        previous_contribution = invested
        if abs(delta) > .00001:
            flows.append((date.fromisoformat(point['day']), -delta))
        value = point['value']
        if value is None:
            valid_twr = False
            previous_value = None
            continue
        if previous_value is not None:
            if previous_value <= 0:
                valid_twr = False
            else:
                factor = (value - delta) / previous_value
                if factor <= 0:
                    valid_twr = False
                else:
                    twr *= factor
        elif abs(value-delta) > .005 and flows:
            # Missing starting cash / prices prevents a trustworthy linked return.
            valid_twr = False
        previous_value = value
    flows.append((date.fromisoformat(series[-1]['day']), series[-1]['value']))
    if not any(amount < -0.00001 for _, amount in flows[:-1]):
        return {'xirr_pct': None, 'twr_pct': None}
    xirr = None
    if any(x < -0.00001 for _, x in flows) and any(x > .00001 for _, x in flows):
        start = min(day for day, _ in flows)
        def npv(rate):
            return sum(amount/(1+rate)**((day-start).days/365.25) for day, amount in flows)
        lo, hi = -.999, 1.0
        try:
            while npv(lo)*npv(hi)>0 and hi < 1e6:
                hi = hi*2+1
            if npv(lo)*npv(hi)<=0:
                for _ in range(90):
                    mid=(lo+hi)/2
                    if npv(lo)*npv(mid)>0:lo=mid
                    else:hi=mid
                xirr=(lo+hi)/2*100
        except (OverflowError, ZeroDivisionError):
            pass
    return {'xirr_pct': round(xirr, 2) if xirr is not None else None,
            'twr_pct': round((twr-1)*100,2) if valid_twr and flows else None}


def financial_year(c, pid, end_year):
    start, end = date(end_year-1,7,1),date(end_year,6,30)
    transactions = [dict(x) for x in c.execute('''SELECT t.*,a.currency,a.name account_name,i.symbol FROM transactions t
        JOIN accounts a ON a.id=t.account_id LEFT JOIN instruments i ON i.id=t.instrument_id
        WHERE t.portfolio_id=? AND t.occurred_at<=? ORDER BY t.occurred_at,t.id''',(pid,end.isoformat()))]
    fx = [(row['day'], Decimal(str(row['usd_aud']))) for row in c.execute('SELECT * FROM fx WHERE day<=? ORDER BY day',(end.isoformat(),))]
    lots = defaultdict(list)
    sales, income, issues = [], [], []
    totals = defaultdict(Decimal)
    def amt(value):return Decimal(str(value or 0))
    def rate(day,currency):
        if currency=='AUD':return Decimal('1')
        available=[r for when,r in fx if when<=day]
        if not available:
            issues.append(f'Missing USD/AUD rate on {day}; USD amounts cannot be valued in AUD')
            return None
        return available[-1]
    for tx in transactions:
        day=tx['occurred_at'];kind=tx['type'];currency=tx['currency'];r=rate(day,currency)
        key=(tx['account_id'],tx['instrument_id'])
        quantity=amt(tx['quantity'])
        in_year=start.isoformat()<=day<=end.isoformat()
        if kind=='buy':
            if r is None:
                issues.append(f"Buy {tx['symbol']} on {day} has no AUD cost base")
            lots[key].append({'day':day,'qty':quantity,'basis':(quantity*amt(tx['price'])+amt(tx['fee']))*r if r else None,'id':tx['id']})
        elif kind=='split':
            pool=lots[key];owned=sum((lot['qty'] for lot in pool),Decimal(0))
            if owned<=0:
                issues.append(f"Split {tx['symbol']} on {day} has no earlier parcel")
            else:
                for lot in pool:
                    lot['qty']+=quantity*lot['qty']/owned
        elif kind=='sell':
            remaining=quantity
            proceeds=(quantity*amt(tx['price'])-amt(tx['fee']))*r if r else None
            while remaining>Decimal('0.000000001') and lots[key]:
                lot=lots[key][0];taken=min(remaining,lot['qty'])
                basis=lot['basis']*taken/lot['qty'] if lot['basis'] is not None else None
                part=proceeds*taken/quantity if proceeds is not None else None
                gain=part-basis if part is not None and basis is not None else None
                anniversary=date.fromisoformat(lot['day'])
                try:anniversary=anniversary.replace(year=anniversary.year+1)
                except ValueError:anniversary=anniversary.replace(year=anniversary.year+1,day=28)
                eligible=date.fromisoformat(day)>anniversary
                if in_year:
                    sales.append({'symbol':tx['symbol'],'account_id':tx['account_id'],'account_name':tx['account_name'],'buy_id':lot['id'],
                                  'sell_id':tx['id'],'acquired':lot['day'],'sold':day,'quantity':float(taken),
                                  'cost_aud':float(basis) if basis is not None else None,
                                  'proceeds_aud':float(part) if part is not None else None,
                                  'gain_aud':float(gain) if gain is not None else None,
                                  'discount_eligible':eligible})
                    if gain is not None:
                        totals['realised_gain_aud']+=gain
                        if eligible and gain>0:totals['discount_eligible_gain_aud']+=gain
                lot['qty']-=taken
                if lot['basis'] is not None:lot['basis']-=basis
                if lot['qty']<Decimal('0.000000001'):lots[key].pop(0)
                remaining-=taken
            if remaining>Decimal('0.000000001'):
                issues.append(f"Sale {tx['symbol']} on {day} exceeds earlier parcels; check imported trade dates")
        elif in_year and kind in ('dividend','interest'):
            gross=amt(tx['amount']);withheld=amt(tx['tax']);credits=amt(tx['franking_credit'])
            income.append({'day':day,'type':kind,'symbol':tx['symbol'],'currency':currency,
                           'gross':float(gross),'tax_withheld':float(withheld),'franking_credit':float(credits),
                           'gross_aud':float(gross*r) if r else None,
                           'withheld_aud':float(withheld*r) if r else None,
                           'franking_aud':float(credits*r) if r else None})
            if r is not None:
                totals[kind+'_aud']+=gross*r
                totals['withheld_aud']+=withheld*r
                totals['franking_credit_aud']+=credits*r
        elif in_year and kind=='fee' and r is not None:
            totals['other_fees_aud']+=amt(tx['amount'])*r
    if any(t['type'] in ('adjustment_in','adjustment_out') for t in transactions):
        issues.append('Cash corrections exist; they are not classified as deposits or taxable income')
    if not any(t['type']=='deposit' for t in transactions):
        issues.append('No deposits recorded; transaction history may be incomplete')
    # No automatic discount deduction: losses, AMMA adjustments, and chosen parcels require review.
    return {'year':f'{end_year-1}–{str(end_year)[-2:]}','start':start.isoformat(),'end':end.isoformat(),
            'method':'FIFO per broker cash account; illustrative only','totals':{k:round(float(v),2) for k,v in totals.items()},
            'sales':sales,'income':income,'issues':sorted(set(issues))}
