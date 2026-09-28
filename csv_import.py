"""Strict CSV intake for the dashboard ledger. No broker-specific column guesses."""
import csv
import io
from collections import Counter
from decimal import Decimal, InvalidOperation
from hashlib import sha256

FIELDS = ('account_id','target_account_id','instrument_id','type','occurred_at','quantity','price','amount','target_amount','fee','tax','fx_rate','franking_credit')
ALIAS = {'date':'occurred_at','trade date':'occurred_at','transaction date':'occurred_at',
         'activity':'type','transaction type':'type','ticker':'symbol','security':'symbol',
         'units':'quantity','shares':'quantity','price per share':'price','brokerage':'fee',
         'fees':'fee','tax withheld':'tax','franking credits':'franking_credit',
         'cash account':'account','destination account':'target_account',
         'received amount':'target_amount','exchange rate':'fx_rate'}
NUMBERS = ('quantity','price','amount','target_amount','fee','tax','fx_rate','franking_credit')


def decimal_str(value):
    raw = str(value or '0').strip().replace(',', '').replace('$', '')
    try:
        x = Decimal(raw or '0')
        if not x.is_finite() or x < 0:
            raise ValueError()
        return str(x.normalize())
    except (InvalidOperation, ValueError):
        raise ValueError(f'Invalid nonnegative number: {str(value)[:30]}')


def parse_csv(blob):
    if len(blob) > 2 * 1024 * 1024:
        raise ValueError('CSV is limited to 2 MB per import')
    try:
        content = blob.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ValueError('Save the CSV as UTF-8')
    reader = csv.DictReader(io.StringIO(content, newline=''))
    if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
        raise ValueError('CSV needs a header row with unique column names')
    keys = {k: ALIAS.get(k.strip().lower(), k.strip().lower().replace(' ', '_')) for k in reader.fieldnames}
    if len(set(keys.values())) != len(keys):
        raise ValueError('CSV has duplicate mapped columns')
    if not {'type', 'occurred_at'} <= set(keys.values()):
        raise ValueError('CSV needs type and date columns; use the downloadable template')
    result = []
    for n, raw in enumerate(reader, 2):
        if n > 1001:
            raise ValueError('Import up to 1,000 transactions at a time')
        if None in raw:
            raise ValueError(f'Row {n} has more cells than the header')
        if not any(str(v or '').strip() for v in raw.values()):
            continue
        row = {keys[k]: str(v or '').strip() for k, v in raw.items()}
        row['_line'] = n
        result.append(row)
    if not result:
        raise ValueError('CSV contains no transactions')
    return result


def canonical(values):
    # Ignore notes and source IDs: a second copy of a transaction must not be posted again.
    return tuple(str(values[k]) if k not in NUMBERS else decimal_str(values[k]) for k in FIELDS)


def existing_signatures(c, pid):
    existing = Counter()
    for row in c.execute('SELECT * FROM transactions WHERE portfolio_id=?', (pid,)):
        existing[canonical(dict(row))] += 1
    return existing


def resolve_row(c, raw, pid, accounts, instruments, validate):
    if raw.get('portfolio_id') and str(raw['portfolio_id']) != str(pid):
        raise ValueError('Export belongs to a different portfolio')
    def lookup(kind, id_col, name_col, choices, label):
        value = raw.get(id_col)
        if value:
            try:
                item = choices[int(value)]
            except (ValueError, KeyError):
                raise ValueError(f'Unknown {label} ID {value}; select the matching portfolio or use the template')
            return item
        name = raw.get(name_col, '').strip().casefold()
        if not name:
            return None
        matches = [x for x in choices.values() if x[kind].casefold() == name]
        if len(matches) != 1:
            raise ValueError(f'{label} "{name}" is missing or ambiguous; create it first')
        return matches[0]
    account = lookup('name', 'account_id', 'account', accounts, 'cash account')
    target = lookup('name', 'target_account_id', 'target_account', accounts, 'destination account')
    iid = raw.get('instrument_id')
    if iid:
        try:
            inst = instruments[int(iid)]
        except (ValueError, KeyError):
            raise ValueError(f'Unknown holding ID {iid}; create the holding first')
    else:
        symbol = (raw.get('symbol') or raw.get('ticker') or '').strip().upper()
        matches = [x for x in instruments.values() if x['symbol'].upper() == symbol] if symbol else []
        if len(matches) > 1 and account:
            matches = [x for x in matches if x['currency'] == account['currency']]
        if len(matches) > 1:
            raise ValueError(f'Ticker {symbol} is ambiguous; use instrument_id')
        inst = matches[0] if matches else None
        if symbol and not inst:
            raise ValueError(f'Holding {symbol} not found; add it first')
    d = {k: raw.get(k, '') for k in NUMBERS}
    d.update(type=raw.get('type', '').strip().lower().replace(' ', '_'),
             occurred_at=raw.get('occurred_at'), account_id=account['id'] if account else None,
             target_account_id=target['id'] if target else None,
             instrument_id=inst['id'] if inst else None, note=raw.get('note', ''))
    for k in NUMBERS:
        d[k] = decimal_str(d[k])
    values = validate(c, d, pid)
    return d, values
