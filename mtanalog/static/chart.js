// Keep archive cohort indexes stable; the third flag hides a group from the UI.
export const SERIES = [
  ['Все исполнения','#a4b0aa'],['< $100','#465e57',true],['$100–1k','#e6ac58'],
  ['$1k–10k','#55c9c0'],['$10k–100k','#bbef1f'],['$100k–1M','#b99aff'],
  ['$1M–10M','#ff0055'],['≥ $10M','#bbef1f',true]
];
export const money = (value, digits=1) => {
  if (value == null || !Number.isFinite(value)) return '—';
  const abs=Math.abs(value);
  return abs>=1e6 ? `${(value/1e6).toFixed(digits)}M` : abs>=1e3 ? `${(value/1e3).toFixed(digits)}k` : value.toFixed(digits);
};
export const utc = value => new Date(value).toISOString().replace('T',' ').slice(0,19);

export function percentile(sorted,p){
  if(!sorted.length)return 0;
  const index=(sorted.length-1)*p/100,lower=Math.floor(index),fraction=index-lower;
  return sorted[lower]+((sorted[lower+1]??sorted[lower])-sorted[lower])*fraction;
}

export function anchoredRange(view,factor,anchor=.5,target=anchor,minSize=1/48){
  anchor=Math.max(0,Math.min(1,anchor));target=Math.max(0,Math.min(1,target));
  const size=Math.min(1,Math.max(minSize,(view[1]-view[0])*factor));
  const point=view[0]+(view[1]-view[0])*anchor;
  const start=Math.max(0,Math.min(1-size,point-size*target));
  return [start,start+size];
}

export function inspectionPoint(x,y,p,offset){
  return {x:Math.max(p.x,Math.min(p.x+p.w,x+offset.x)),y:Math.max(p.y,Math.min(p.y+p.h,y+offset.y))};
}

export const CHART_THEME={background:'#101914',bull:'#bbef1f',bear:'#ff0055',bullWick:'#d5fa77',bearWick:'#ff6296',outline:'#071410'};
const stops=[[0,[57,73,75]],[.5,[126,146,149]],[1,[221,231,231]]];
function heatColor(value){
  value=Math.max(0,Math.min(1,value));
  for(let i=1;i<stops.length;i++){
    if(value<=stops[i][0]){
      const [a,ca]=stops[i-1],[b,cb]=stops[i],q=(value-a)/(b-a);
      return ca.map((v,j)=>Math.round(v+(cb[j]-v)*q));
    }
  }
  return stops.at(-1)[1];
}

export function aggregateCandles(candles, minutes=1){
  const groups=new Map(),width=minutes*60;
  for(const candle of candles){
    const time=Math.floor(candle.time/width)*width,old=groups.get(time);
    if(old){old.high=Math.max(old.high,candle.high);old.low=Math.min(old.low,candle.low);old.close=candle.close;}
    else groups.set(time,{...candle,time});
  }
  return [...groups.values()];
}

export class Charts {
  constructor(heat,cvd,onScale){
    this.heat=heat;this.cvd=cvd;this.onScale=onScale;this.data=null;
    this.view=[0,1];this.priceView=[0,1];this.touches=new Map();this.filter={type:'usdt',minimum:5e6,lower:67,upper:99};
    this.normalized=false;this.visible=SERIES.map(([, ,hidden])=>!hidden);
    this.pointer=null;this.drag=null;this.left=16;this.right=68;
    this.observer=new ResizeObserver(()=>this.draw());this.observer.observe(heat);this.observer.observe(cvd);
    this.lenses=new Map();
    for(const canvas of [heat,cvd]){
      const lens=document.createElement('canvas');lens.className='chart-loupe';lens.hidden=true;lens.setAttribute('aria-hidden','true');
      canvas.parentElement.append(lens);this.lenses.set(canvas,lens);
      canvas.addEventListener('contextrestored',()=>this.recoverCanvas());
      canvas.addEventListener('contextmenu',event=>event.preventDefault());
      canvas.addEventListener('pointermove',event=>{
        this.pendingMove={event,canvas};
        if(this.moveFrame)return;
        this.moveFrame=requestAnimationFrame(()=>{this.moveFrame=null;const move=this.pendingMove;this.pendingMove=null;if(move)this.move(move.event,move.canvas);});
      });
      canvas.addEventListener('pointerleave',()=>{if(!this.drag&&!this.inspection){this.pendingMove=null;this.pointer=null;this.draw();this.hideTips();}});
      canvas.addEventListener('pointerdown',event=>this.down(event,canvas));
      for(const type of ['pointerup','pointercancel','lostpointercapture'])canvas.addEventListener(type,event=>this.up(event,canvas));
      canvas.addEventListener('wheel',event=>{
        if(!this.data)return;event.preventDefault();
        const box=canvas.getBoundingClientRect(),fraction=(event.clientX-box.left-this.left)/(box.width-this.left-this.right);
        if(canvas===this.heat&&event.clientX-box.left>=box.width-this.right)this.zoomPrice(event.deltaY>0?1.25:.8);
        else if(Math.abs(event.deltaX)>Math.abs(event.deltaY)||event.shiftKey)this.pan((event.shiftKey?event.deltaY:event.deltaX)/600);
        else this.zoom(Math.exp(Math.max(-400,Math.min(400,event.deltaY))*.002),fraction);
      },{passive:false});
      canvas.addEventListener('dblclick',()=>this.reset());
    }
  }
  setData(data,reset=false,update=null){this.data=data;this.volumeData=null;this.cvdCache=null;this.candleLookup=new Map((data.candles??[]).map(c=>[c.time,c]));if(reset){this.view=[0,1];this.priceView=[0,1];}if(data.empty){this.pointer=null;this.hideTips();this.surface(this.heat);this.surface(this.cvd);return;}this.buildImage(update);this.draw();}
  setFilter(filter){if(JSON.stringify(filter)===JSON.stringify(this.filter))return;this.filter=filter;this.buildImage();this.draw();}
  recoverCanvas(){
    if(this.recoveryFrame)return;
    this.recoveryFrame=requestAnimationFrame(()=>{this.recoveryFrame=null;this.resume();});
  }
  resume(){
    this.cancelHold();cancelAnimationFrame(this.moveFrame);cancelAnimationFrame(this.candleFrame);
    this.pendingMove=null;this.moveFrame=null;this.candleFrame=null;
    for(const [id,point] of this.touches){try{if(point.canvas.hasPointerCapture(id))point.canvas.releasePointerCapture(id);}catch{}}
    this.touches.clear();this.drag=null;this.inspection=false;this.pointer=null;this.hideTips();
    this.heatLayer=null;this.cvdLayer=null;this.candleBitmap=null;this.candleSignature=null;this.candleDataSignature=null;this.engineSize=null;
    if(this.engine){this.engine.remove();this.engine=null;this.candleSeries=null;}
    this.engineHost?.remove();this.engineHost=null;
    this.buildImage();this.draw();
  }
  reset(){this.inspection=false;this.cancelHold();this.view=[0,1];this.priceView=[0,1];this.pointer=null;this.hideTips();this.draw();}
  anchor(){
    const box=this.heat.getBoundingClientRect();
    return this.pointer?Math.max(0,Math.min(1,(this.pointer.x-this.left)/(box.width-this.left-this.right))):1;
  }
  zoom(factor,anchor=1){this.view=anchoredRange(this.view,factor,anchor);this.hideTips();this.draw();}
  pan(fraction){
    const size=this.view[1]-this.view[0],start=Math.max(0,Math.min(1-size,this.view[0]+size*fraction));
    this.view=[start,start+size];this.pointer=null;this.inspection=false;this.hideTips();this.draw();
  }
  zoomPrice(factor){this.priceView=anchoredRange(this.priceView,factor,.5,.5,1/100);this.hideTips();this.draw();}
  priceBounds(){const d=this.data;return this.priceView.map(v=>d.low+v*(d.high-d.low));}
  down(event,canvas){
    if(event.button!==0||!this.data||this.data.empty)return;
    event.preventDefault();if(event.pointerType==='mouse'&&canvas.focus)canvas.focus({preventScroll:true});canvas.setPointerCapture(event.pointerId);
    this.touches.set(event.pointerId,{x:event.clientX,y:event.clientY,canvas});
    this.inspection=false;this.cancelHold();this.beginGesture(canvas);this.pointer=null;this.hideTips();this.draw();
    const box=canvas.getBoundingClientRect();
    if(event.pointerType==='touch'&&this.touches.size===1&&this.drag.kind==='pan'
        &&event.clientY-box.top>=8&&event.clientY-box.top<=box.height-35
        &&event.clientX-box.left>=this.left){
      const id=event.pointerId;
      this.holdTimer=setTimeout(()=>{
        this.holdTimer=null;
        if(this.touches.size!==1||!this.touches.has(id)||this.drag?.kind!=='pan')return;
        this.drag.kind='inspect';this.inspection=true;
        this.drag.offset={x:event.clientX-box.left>box.width/2?-48:48,y:event.clientY-box.top>110?-90:90};
        const point=this.touches.get(id);
        this.move({pointerId:id,clientX:point.x,clientY:point.y},canvas);
      },550);
    }
  }
  cancelHold(){clearTimeout(this.holdTimer);this.holdTimer=null;}
  beginGesture(canvas){
    const points=[...this.touches.values()].filter(p=>p.canvas===canvas),box=canvas.getBoundingClientRect();
    if(!points.length){this.drag=null;return;}
    const first=points[0],second=points[1];
    this.drag={canvas,view:[...this.view],price:[...this.priceView],x:first.x,y:first.y,width:box.width-this.left-this.right,height:box.height-43};
    if(second){
      this.drag.kind='pinch';this.drag.distance=Math.max(1,Math.hypot(second.x-first.x,second.y-first.y));
      this.drag.midX=(first.x+second.x)/2;this.drag.midY=(first.y+second.y)/2;
    }else this.drag.kind=canvas===this.heat&&first.x-box.left>=box.width-this.right?'price':'pan';
  }
  up(event,canvas){
    if(this.pendingMove?.event.pointerId===event.pointerId){const move=this.pendingMove;this.pendingMove=null;this.move(move.event,move.canvas);}
    if(!this.touches.has(event.pointerId))return;
    this.cancelHold();this.touches.delete(event.pointerId);this.beginGesture(canvas);
    if(canvas.style)canvas.style.cursor='crosshair';
    if(event.type==='pointercancel'){this.inspection=false;this.pointer=null;this.hideTips();this.draw();}
  }
  gesture(event,canvas){
    if(!this.touches.has(event.pointerId)||!this.drag||this.drag.canvas!==canvas)return false;
    this.touches.set(event.pointerId,{x:event.clientX,y:event.clientY,canvas});
    const g=this.drag,points=[...this.touches.values()].filter(p=>p.canvas===canvas),box=canvas.getBoundingClientRect();
    if(g.kind==='inspect')return false;
    if(Math.hypot(event.clientX-g.x,event.clientY-g.y)>8)this.cancelHold();
    if(g.kind==='pinch'&&points.length>=2){
      const [a,b]=points,midX=(a.x+b.x)/2,midY=(a.y+b.y)/2,factor=g.distance/Math.max(1,Math.hypot(a.x-b.x,a.y-b.y));
      this.view=anchoredRange(g.view,factor,(g.midX-box.left-this.left)/g.width,(midX-box.left-this.left)/g.width);
      if(canvas===this.heat)this.priceView=anchoredRange(g.price,factor,1-(g.midY-box.top-8)/g.height,1-(midY-box.top-8)/g.height,1/100);
    }else if(g.kind==='price'){
      this.priceView=anchoredRange(g.price,Math.exp((event.clientY-g.y)/g.height*3),.5,.5,1/100);
    }else{
      const size=g.view[1]-g.view[0],delta=(event.clientX-g.x)/g.width*size;
      const a=Math.max(0,Math.min(1-size,g.view[0]-delta));this.view=[a,a+size];
    }
    this.pointer=null;this.hideTips();this.draw();return true;
  }
  buildImage(update=null){
    if(!this.data||this.data.empty)return;
    const d=this.data;
    if(this.volumeData!==d){this.volumeData=d;this.volumes=d.liquidity.flatMap(col=>col?col.map(pair=>pair[1]):[]).filter(v=>v>0).sort((a,b)=>a-b);}
    const volumes=this.volumes;
    let minimum=this.filter.type==='percentile'?percentile(volumes,this.filter.lower):this.filter.minimum;
    let maximum=this.filter.type==='percentile'?percentile(volumes,this.filter.upper):percentile(volumes.filter(v=>v>=minimum),99.5);
    if(this.filter.type==='usdt'){
      maximum=Math.max(minimum*2,maximum,1);
      const magnitude=10**Math.floor(Math.log10(maximum));maximum=Math.ceil(maximum/(magnitude/2))*magnitude/2;
    }
    if(maximum<=minimum)maximum=minimum+Math.max(1,minimum*.01);
    const incremental=update?.type==='delta'&&this.image&&this.image.width===d.columns&&this.image.height===d.rows
      &&this.imageLow===d.low&&this.imageStep===d.price_step&&this.minimum===minimum&&this.maximum===maximum;
    this.minimum=minimum;this.maximum=maximum;this.onScale(minimum,maximum);
    if(!incremental){this.image=document.createElement('canvas');this.image.width=d.columns;this.image.height=d.rows;const image=this.image;image.addEventListener('contextrestored',()=>{if(this.image===image)this.recoverCanvas();});}
    const ctx=this.image.getContext('2d');
    if(incremental&&update.drop){
      const copy=document.createElement('canvas');copy.width=d.columns;copy.height=d.rows;copy.getContext('2d').drawImage(this.image,0,0);
      ctx.clearRect(0,0,d.columns,d.rows);ctx.drawImage(copy,update.drop,0,d.columns-update.drop,d.rows,0,0,d.columns-update.drop,d.rows);
    }
    const columns=incremental?update.patch.liquidity.map(([index])=>index):Array.from({length:d.columns},(_,index)=>index);
    for(const c of columns){
      const col=d.liquidity[c],values=new Map(col??[]),pixels=ctx.createImageData(1,d.rows);
      for(let row=0;row<d.rows;row++){
        const value=values.get(row)??0,color=col===null?[49,59,54]:value<minimum||value===0?[16,25,20]:heatColor((value-minimum)/(maximum-minimum));
        const offset=(d.rows-1-row)*4;
        pixels.data[offset]=color[0];pixels.data[offset+1]=color[1];pixels.data[offset+2]=color[2];pixels.data[offset+3]=255;
      }
      ctx.putImageData(pixels,c,0);
    }
    this.imageLow=d.low;this.imageStep=d.price_step;this.imageVersion=(this.imageVersion??0)+1;
  }
  surface(canvas){
    const box=canvas.getBoundingClientRect(),ratio=window.devicePixelRatio||1;
    if(canvas.width!==Math.round(box.width*ratio)||canvas.height!==Math.round(box.height*ratio)){
      canvas.width=Math.round(box.width*ratio);canvas.height=Math.round(box.height*ratio);
    }
    const ctx=canvas.getContext('2d');ctx.setTransform(ratio,0,0,ratio,0,0);ctx.clearRect(0,0,box.width,box.height);
    const plot={x:this.left,y:8,w:box.width-this.left-this.right,h:box.height-43};
    ctx.font='11px Montserrat, sans-serif';
    return {ctx,plot,box};
  }
  timeBounds(){const d=this.data;return this.view.map(v=>d.start_ms+v*(d.end_ms-d.start_ms));}
  x(time,p){const [a,b]=this.timeBounds();return p.x+(time-a)/(b-a)*p.w;}
  draw(){
    if(!this.data||this.data.empty)return;
    this.drawHeat();this.drawCvd();this.drawLoupe();
  }
  grid(ctx,p,low,high,normalized=false){
    ctx.lineWidth=1;ctx.textAlign='left';ctx.fillStyle='#b3c1b9';
    for(let i=0;i<=5;i++){
      const y=p.y+p.h*i/5,value=high-(high-low)*i/5;
      ctx.strokeStyle='#a5bca51b';ctx.beginPath();ctx.moveTo(p.x,y);ctx.lineTo(p.x+p.w,y);ctx.stroke();
      ctx.fillText(normalized?value.toFixed(2):money(value),p.x+p.w+10,y+3);
    }
    const [a,b]=this.timeBounds();ctx.textAlign='center';
    const count=p.w<450?3:5;
    for(let i=0;i<=count;i++){
      const x=p.x+p.w*i/count,time=a+(b-a)*i/count,date=new Date(time);
      ctx.strokeStyle='#a5bca510';ctx.beginPath();ctx.moveTo(x,p.y);ctx.lineTo(x,p.y+p.h);ctx.stroke();
      ctx.fillStyle='#b3c1b9';
      ctx.fillText(date.toISOString().slice(11,16),x,p.y+p.h+17);
      ctx.font='9px Montserrat, sans-serif';ctx.fillText(date.toISOString().slice(5,10),x,p.y+p.h+29);ctx.font='11px Montserrat, sans-serif';
    }
  }
  clip(ctx,p){ctx.save();ctx.beginPath();ctx.rect(p.x,p.y,p.w,p.h);ctx.clip();}
  cachedLayer(name,key,ctx,box){
    const layer=this[name];
    if(!layer||layer.key!==key)return false;
    if(!layer.canvas.width||!layer.canvas.height)return false;ctx.drawImage(layer.canvas,0,0,box.width,box.height);return true;
  }
  saveLayer(name,key,source){
    if(!this[name]){const canvas=document.createElement('canvas');canvas.addEventListener('contextrestored',()=>this.recoverCanvas());this[name]={canvas};}
    const layer=this[name];
    if(layer.canvas.width!==source.width||layer.canvas.height!==source.height){layer.canvas.width=source.width;layer.canvas.height=source.height;}
    const ctx=layer.canvas.getContext('2d');ctx.clearRect(0,0,source.width,source.height);ctx.drawImage(source,0,0);layer.key=key;
  }
  drawHeat(){
    const size=this.heat.getBoundingClientRect();if(!this.data||this.data.empty||size.width<=this.left+this.right||size.height<=43)return;
    const {ctx,plot:p,box}=this.surface(this.heat),d=this.data,[low,high]=this.priceBounds();
    const key=[d.generated_ms,this.imageVersion,...this.view,...this.priceView,box.width,box.height,window.devicePixelRatio,this.candleBitmapVersion??0].join(':');
    if(this.cachedLayer('heatLayer',key,ctx,box)){this.clip(ctx,p);this.drawCrosshair(ctx,p,this.heat);ctx.restore();return;}
    ctx.fillStyle=CHART_THEME.background;ctx.fillRect(p.x,p.y,p.w,p.h);
    ctx.imageSmoothingEnabled=false;
    ctx.drawImage(this.image,this.view[0]*d.columns,(1-this.priceView[1])*d.rows,(this.view[1]-this.view[0])*d.columns,(this.priceView[1]-this.priceView[0])*d.rows,p.x,p.y,p.w,p.h);
    this.grid(ctx,p,low,high);
    this.clip(ctx,p);
    for(const [kind,begin,end] of d.gaps){
      if(kind!=='depth')continue;
      const x=this.x(begin,p),right=this.x(end??d.end_ms,p);ctx.fillStyle='#94a49a4c';ctx.fillRect(x,p.y,Math.max(1,right-x),p.h);
    }
    const y=price=>p.y+(high-price)/(high-low)*p.h;
    for(let side=0;side<2;side++){
      ctx.strokeStyle='#9caf9f70';ctx.lineWidth=.7;ctx.setLineDash([4,4]);ctx.beginPath();let previous=false;
      d.known.forEach((bounds,c)=>{
        if(!bounds){previous=false;return;}
        const x=this.x(d.start_ms+(c+.5)*(d.end_ms-d.start_ms)/d.columns,p);
        if(previous)ctx.lineTo(x,y(bounds[side]));else ctx.moveTo(x,y(bounds[side]));previous=true;
      });ctx.stroke();ctx.setLineDash([]);
    }
    this.drawCandles(ctx,p);
    this.saveLayer('heatLayer',key,this.heat);
    this.drawCrosshair(ctx,p,this.heat);
    ctx.restore();
  }
  drawCandles(ctx,p){
    if(!window.LightweightCharts||!this.data.candles?.length)return;
    if(!this.engine){
      const host=document.createElement('div');host.className='candle-engine';this.engineHost=host;this.heat.parentElement.append(host);
      const L=window.LightweightCharts;
      this.engine=L.createChart(host,{width:Math.max(1,Math.round(p.w)),height:Math.max(1,Math.round(p.h)),
        layout:{background:{type:'solid',color:'transparent'},textColor:'#b3c1b9',attributionLogo:false},
        grid:{vertLines:{visible:false},horzLines:{visible:false}},
        rightPriceScale:{visible:false,scaleMargins:{top:0,bottom:0}},leftPriceScale:{visible:false},
        timeScale:{visible:false,minBarSpacing:.001,fixLeftEdge:false,fixRightEdge:false},
        handleScroll:false,handleScale:false,crosshair:{vertLine:{visible:false},horzLine:{visible:false}}});
      this.candleSeries=this.engine.addSeries(L.CandlestickSeries,{upColor:CHART_THEME.bull,downColor:CHART_THEME.bear,borderVisible:false,wickUpColor:CHART_THEME.bullWick,wickDownColor:CHART_THEME.bearWick,priceLineVisible:false,lastValueVisible:false,
        autoscaleInfoProvider:()=>({priceRange:{minValue:this.priceBounds()[0],maxValue:this.priceBounds()[1]}})});
    }
    const [a,b]=this.timeBounds(),minutes=(b-a)/60000;
    const interval=minutes/p.w>12?60:minutes/p.w>3?15:minutes/p.w>.8?5:1;
    const signature=[this.data.generated_ms,interval,...this.view,...this.priceView,p.w,p.h].join(':');
    if(signature!==this.candleSignature){
      this.candleSignature=signature;
      const step=interval*60;
      const first=Math.floor(this.data.start_ms/1000/step)*step,last=Math.floor(this.data.end_ms/1000/step)*step;
      const dataSignature=[this.data.generated_ms,interval].join(':');
      if(this.candleDataSignature!==dataSignature){
        this.candleDataSignature=dataSignature;
        const candles=aggregateCandles(this.data.candles,interval),values=new Map(candles.map(c=>[c.time,c])),series=[];
        for(let time=first;time<=last;time+=step)series.push(values.get(time)??{time});
        this.candleSeries.setData(series);
      }
      const size=[Math.max(1,Math.round(p.w)),Math.max(1,Math.round(p.h))];
      if(size.join(':')!==this.engineSize){this.engineSize=size.join(':');this.engine.resize(...size);}
      this.engine.timeScale().setVisibleLogicalRange({from:(a/1000-first)/step-.5,to:(b/1000-first)/step-.5});
      this.engine.priceScale('right').applyOptions({autoScale:true,scaleMargins:{top:0,bottom:0}});
      cancelAnimationFrame(this.candleFrame);
      this.candleFrame=requestAnimationFrame(()=>{this.candleFrame=null;const bitmap=this.engine.takeScreenshot();if(!bitmap.width||!bitmap.height)return;const outlined=document.createElement('canvas');outlined.width=bitmap.width;outlined.height=bitmap.height;const outline=outlined.getContext('2d');outline.shadowColor=CHART_THEME.outline;outline.shadowBlur=3*(window.devicePixelRatio||1);outline.drawImage(bitmap,0,0);this.candleBitmap=outlined;this.candleBitmapVersion=(this.candleBitmapVersion??0)+1;this.drawHeat();this.drawLoupe();});
    }
    if(this.candleBitmap?.width&&this.candleBitmap?.height)ctx.drawImage(this.candleBitmap,p.x,p.y,p.w,p.h);
  }
  cvdValues(){
    if(this.cvdCache?.data===this.data&&this.cvdCache.normalized===this.normalized)return this.cvdCache.rows;
    const rows=this.data.cvd.map(row=>row?[...row]:null);
    if(this.normalized){
      SERIES.forEach((_,i)=>{
        const values=rows.filter(Boolean).map(row=>row[i]);
        if(!values.length)return;
        const min=Math.min(...values),max=Math.max(...values);
        for(const row of rows)if(row)row[i]=max>min?(row[i]-min)/(max-min):.5;
      });
    }
    this.cvdCache={data:this.data,normalized:this.normalized,rows};
    return rows;
  }
  drawCvd(){
    const size=this.cvd.getBoundingClientRect();if(!this.data||this.data.empty||size.width<=this.left+this.right||size.height<=43)return;
    const {ctx,plot:p,box}=this.surface(this.cvd),d=this.data;
    const key=[d.generated_ms,...this.view,this.normalized,this.visible.join(','),box.width,box.height,window.devicePixelRatio].join(':');
    if(this.cachedLayer('cvdLayer',key,ctx,box)){this.clip(ctx,p);this.drawCrosshair(ctx,p,this.cvd);ctx.restore();return;}
    const rows=this.cvdValues();
    let low=0,high=1;
    if(!this.normalized){
      const vals=rows.filter(Boolean).flatMap(row=>row.filter((_,i)=>this.visible[i]));
      low=Math.min(0,...vals);high=Math.max(0,...vals);const margin=Math.max(1,(high-low)*.08);low-=margin;high+=margin;
    }
    this.grid(ctx,p,low,high,this.normalized);this.clip(ctx,p);
    SERIES.forEach(([,color],i)=>{
      if(!this.visible[i])return;
      ctx.strokeStyle=color;ctx.lineWidth=i===0?1.4:1.3;ctx.setLineDash(i===0?[5,4]:[]);ctx.beginPath();let previous=false;
      rows.forEach((row,c)=>{
        if(!row){previous=false;return;}
        const x=this.x(d.start_ms+(c+.5)*(d.end_ms-d.start_ms)/d.columns,p),y=p.y+(high-row[i])/(high-low)*p.h;
        if(previous)ctx.lineTo(x,y);else ctx.moveTo(x,y);previous=true;
      });ctx.stroke();ctx.setLineDash([]);
    });
    for(const [kind,begin,end] of d.gaps){if(kind==='cvd'){ctx.fillStyle='#94a49a4c';ctx.fillRect(this.x(begin,p),p.y,this.x(end??d.end_ms,p)-this.x(begin,p),p.h);}}
    this.saveLayer('cvdLayer',key,this.cvd);
    this.drawCrosshair(ctx,p,this.cvd);
    ctx.restore();
  }
  drawCrosshair(ctx,p,canvas){
    if(!this.pointer)return;
    const {x,y}=this.pointer;ctx.strokeStyle='#d1e3d5b0';ctx.lineWidth=1;ctx.setLineDash([3,4]);ctx.beginPath();
    ctx.moveTo(x,p.y);ctx.lineTo(x,p.y+p.h);
    if(this.pointer.canvas===canvas){ctx.moveTo(p.x,y);ctx.lineTo(p.x+p.w,y);}
    ctx.stroke();ctx.setLineDash([]);
  }
  drawLoupe(){
    if(!this.lenses)return;
    for(const [canvas,lens] of this.lenses){
      const show=this.pointer?.touch&&this.pointer.canvas===canvas;
      lens.hidden=!show;if(!show)continue;
      const {x,y}=this.pointer,ratio=window.devicePixelRatio||1,size=88,zoom=2.5;
      if(lens.width!==Math.round(size*ratio)){lens.width=Math.round(size*ratio);lens.height=Math.round(size*ratio);}
      lens.style.left=(x-size/2)+'px';lens.style.top=(y-size/2)+'px';
      const ctx=lens.getContext('2d');ctx.setTransform(ratio,0,0,ratio,0,0);
      ctx.fillStyle=CHART_THEME.background;ctx.fillRect(0,0,size,size);ctx.imageSmoothingEnabled=false;
      const sourceSize=size/zoom;
      ctx.drawImage(canvas,(x-sourceSize/2)*ratio,(y-sourceSize/2)*ratio,sourceSize*ratio,sourceSize*ratio,0,0,size,size);
      ctx.strokeStyle='#e3eee2';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(size/2,8);ctx.lineTo(size/2,size-8);ctx.moveTo(8,size/2);ctx.lineTo(size-8,size/2);ctx.stroke();
      ctx.fillStyle='#7eafd4';ctx.beginPath();ctx.arc(size/2,size/2,2.5,0,Math.PI*2);ctx.fill();
      lens.dataset.centerX=x;lens.dataset.centerY=y;
    }
  }
  hideTips(){document.querySelectorAll('.chart-tooltip').forEach(t=>t.hidden=true);if(this.lenses)for(const lens of this.lenses.values())lens.hidden=true;}
  move(event,canvas){
    if(!this.data||this.data.empty)return;
    const box=canvas.getBoundingClientRect(),p={x:this.left,y:8,w:box.width-this.left-this.right,h:box.height-43};let x=event.clientX-box.left,y=event.clientY-box.top;
    if(canvas.style&&event.pointerType==='mouse')canvas.style.cursor=this.drag?.kind==='pan'?'grabbing':x>p.x+p.w?'ns-resize':'crosshair';
    if(this.gesture(event,canvas))return;
    if(x<p.x||x>p.x+p.w||y<p.y||y>p.y+p.h){this.pointer=null;this.hideTips();this.draw();return;}
    const touch=this.drag?.kind==='inspect';
    const finger={x,y};
    if(touch)({x,y}=inspectionPoint(x,y,p,this.drag.offset));
    this.pointer={x,y,canvas,touch};const [a,b]=this.timeBounds(),time=a+(x-p.x)/p.w*(b-a),d=this.data;
    const col=Math.min(d.columns-1,Math.max(0,Math.floor((time-d.start_ms)/(d.end_ms-d.start_ms)*d.columns)));
    this.hideTips();const tip=canvas.parentElement.querySelector('.chart-tooltip');
    tip.replaceChildren();const date=document.createElement('small');date.textContent=utc(time)+' UTC';tip.append(date);
    const add=(label,value)=>{const row=document.createElement('div');row.className='tip-row';const l=document.createElement('span'),v=document.createElement('strong');l.textContent=label;v.textContent=value;row.append(l,v);tip.append(row);};
    if(canvas===this.heat){
      const [low,high]=this.priceBounds(),price=high-(y-p.y)/p.h*(high-low),row=Math.max(0,Math.min(d.rows-1,Math.floor((price-d.low)/d.price_step)));
      const volume=d.liquidity[col]?.find(([r])=>r===row)?.[1]??0;
      const missing=d.liquidity[col]===null||d.gaps.some(([kind,a,b])=>kind==='depth'&&time>=a&&time<(b??d.end_ms));
      add('Цена',`${(d.low+row*d.price_step).toLocaleString('en-US')}–${(d.low+(row+1)*d.price_step).toLocaleString('en-US')}`);
      add('Объём',missing?'Нет данных':money(volume,2)+' USDT');
      const candle=this.candleLookup?.get(Math.floor(time/60000)*60);
      if(candle){add('O / C',`${candle.open.toFixed(2)} / ${candle.close.toFixed(2)}`);add('H / L',`${candle.high.toFixed(2)} / ${candle.low.toFixed(2)}`);}
      if(!missing&&volume<this.minimum)add('Фильтр','Ниже порога');
      if(d.known[col]&&(price<d.known[col][0]||price>d.known[col][1]))add('Книга','Частично наблюдаемая');
    }else{
      const row=this.cvdValues()[col];SERIES.forEach(([label],i)=>{if(this.visible[i])add(label,row?(this.normalized?row[i].toFixed(3):money(row[i],2)+' USDT'):'Нет данных');});
    }
    tip.hidden=false;tip.style.left=Math.max(0,Math.min(box.width-tip.offsetWidth-6,x+14))+'px';tip.style.top=Math.max(0,Math.min(box.height-tip.offsetHeight-6,y+12))+'px';
    if(touch){
      const width=tip.offsetWidth,height=tip.offsetHeight;
      const candidates=[[6,6],[box.width-width-6,6],[6,box.height-height-6],[box.width-width-6,box.height-height-6]];
      const distance=(point,left,top)=>Math.hypot(Math.max(left-point.x,0,point.x-left-width),Math.max(top-point.y,0,point.y-top-height));
      candidates.sort((a,b)=>Math.min(distance(finger,b[0],b[1])-25,distance({x,y},b[0],b[1])-48)-Math.min(distance(finger,a[0],a[1])-25,distance({x,y},a[0],a[1])-48));
      tip.style.left=Math.max(0,candidates[0][0])+'px';tip.style.top=Math.max(0,candidates[0][1])+'px';
    }
    this.draw();
  }
}
