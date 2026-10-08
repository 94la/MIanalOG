"""Revision-based chart patches on a stable UTC time grid."""
ARRAYS = ('liquidity', 'known', 'coverage', 'cvd')
SERIES = ('candles', 'prices')


def delta(previous, current):
    if (previous.get('empty') or current.get('empty') or
            any(previous.get(k)!=current.get(k) for k in ('mode','low','high','rows','price_step','column_ms'))):
        return None
    width=current.get('column_ms')
    if not width: return None
    distance=current['start_ms']-previous['start_ms']
    if distance<0 or distance%width: return None
    drop=distance//width
    if drop>=previous['columns']: return None
    metadata={k:v for k,v in current.items() if k not in ARRAYS+SERIES}
    patch={}
    for name in ARRAYS:
        old=previous[name][drop:]
        patch[name]=[[i,value] for i,value in enumerate(current[name]) if i>=len(old) or value!=old[i]]
    series={}
    for name in SERIES:
        stamp=lambda row: row['time']*1000 if name=='candles' else row[0]
        old={stamp(row):row for row in previous.get(name,[])}
        new={stamp(row):row for row in current.get(name,[])}
        series[name]={'remove':[t for t in old if t not in new],
                      'upsert':[row for t,row in new.items() if old.get(t)!=row]}
    return {'type':'delta','base':previous['revision'],'drop':drop,'metadata':metadata,'patch':patch,'series':series}
