const arrays=['liquidity','known','coverage','cvd'];
export function mergeUpdate(previous, update){
  if(update.type==='unchanged'){
    if(previous?.revision!==update.revision)throw new Error('Revision mismatch');
    return previous;
  }
  if(update.type!=='delta')return update;
  if(!previous||previous.revision!==update.base)throw new Error('Revision mismatch');
  const next={...update.metadata};
  for(const name of arrays){
    const rows=previous[name].slice(update.drop);rows.length=next.columns;
    for(const [index,value] of update.patch[name])rows[index]=value;
    next[name]=rows;
  }
  for(const name of ['candles','prices']){
    const stamp=row=>name==='candles'?row.time*1000:row[0];
    const removed=new Set(update.series[name].remove);
    const values=new Map(previous[name].filter(row=>!removed.has(stamp(row))).map(row=>[stamp(row),row]));
    for(const row of update.series[name].upsert)values.set(stamp(row),row);
    next[name]=[...values.entries()].sort((a,b)=>a[0]-b[0]).map(([,row])=>row);
  }
  return next;
}
