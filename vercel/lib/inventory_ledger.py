"""FIFO inventory including transfers, interest and loan repayments.

Unknown opening/deposit basis stays unknown until its lots are consumed; it is
never silently replaced by market price. Quantity reconciles against each ledger
balance, allowing only API rounding of one millionth of a base unit.
"""
from collections import deque
from decimal import Decimal


D=lambda x:Decimal(str(x))
TOL=D('0.000001')

class FIFOInventory:
    def __init__(self):self.lots=deque()
    @property
    def qty(self):return sum((q for q,c in self.lots),D(0))
    @property
    def known(self):return all(c is not None for q,c in self.lots if q>D('0.00000001'))
    @property
    def average(self):
        if not self.known or self.qty<=0:return 0.0
        return float(sum((c or D(0) for q,c in self.lots),D(0))/self.qty)
    def add(self,qty,total_cost):
        qty=D(qty)
        if qty>0:self.lots.append((qty,None if total_cost is None else D(total_cost)))
    def remove(self,qty):
        remaining=D(qty)
        if remaining>self.qty+TOL:raise ValueError('Inventory debit exceeds reconciled balance')
        while remaining>0 and self.lots:
            q,c=self.lots.popleft();take=min(q,remaining);remaining-=take
            if q>take:self.lots.appendleft((q-take,None if c is None else c*(q-take)/q))
    def align(self,quantity):
        target=D(quantity);delta=target-self.qty
        if abs(delta)>TOL:raise ValueError('Transaction ledger has missing/out-of-order balance changes')
        if delta<0:self.remove(-delta)
        elif delta>0:
            if self.lots:
                q,c=self.lots.pop();self.lots.append((q+delta,None if c is None else c*(q+delta)/q))
            else:self.add(delta,None)

def inventory_from_transactions(rows,fills=()):
    unique={r['id']:r for r in rows}
    rows=sorted(unique.values(),key=lambda r:int(r['transactionTime']))
    book=FIFOInventory()
    if not rows:return book
    ordered=[];balance=None
    # Ledger timestamps can precede posting by seconds (interest versus trades).
    # Reorder only within a bounded 10-second window using balance continuity.
    groups=[]
    for row in rows:
        ts=int(row['transactionTime'])
        if not groups or ts-int(groups[-1][0]['transactionTime'])>10000:
            groups.append([])
        groups[-1].append(row)
    for group in groups:
        ts=int(group[0]['transactionTime'])
        pending=list(group)
        starts=[balance] if balance is not None else [D(r['cashBalance'])-D(r['change']) for r in pending]
        chain=None
        for opening in starts:
            left=list(pending);trial=[];current=opening
            while left:
                matches=[r for r in left if abs(D(r['cashBalance'])-D(r['change'])-current)<=TOL]
                if not matches:break
                row=min(matches,key=lambda r:abs(D(r['change'])))
                trial.append(row);left.remove(row);current=D(row['cashBalance'])
            if not left:
                chain=trial
                if balance is None:book.add(max(D(0),opening),None)
                balance=current
                break
        if chain is None:raise ValueError('Transaction ledger has a balance-chain gap at '+str(ts))
        ordered.extend(chain)
    by_id={f['execId']:f for f in fills}
    for row in ordered:
        change=D(row['change'])
        before=D(row['cashBalance'])-change
        book.align(max(D(0),before))
        if change>0:
            cost=None
            if row['type']=='TRADE' and row['side']=='Buy' and D(row.get('tradePrice') or 0)>0:
                cost=D(row.get('cashFlow') or row['qty'])*D(row['tradePrice'])
                fill=by_id.get(row.get('tradeId'),{})
                if fill.get('feeCurrency')=='USDT':cost+=D(fill.get('execFee') or 0)
            credited=max(D(0),D(row['cashBalance']))-max(D(0),before)
            if cost is not None:cost=cost*credited/change
            book.add(credited,cost)
        elif change<0:book.remove(min(-change,book.qty))
        book.align(max(D(0),D(row['cashBalance'])))
    return book
