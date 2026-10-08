import {mergeUpdate} from './updates.js';
import {Charts,SERIES,money,utc} from './chart.js';

const $=id=>document.getElementById(id);
// Input capabilities choose the client; viewport size only controls layout.
const clientQueries={fine:matchMedia('(pointer: fine)'),hover:matchMedia('(hover: hover)'),coarse:matchMedia('(pointer: coarse)')};
function updateClient(){
  const desktop=clientQueries.fine.matches&&clientQueries.hover.matches;
  const client=desktop?'desktop':clientQueries.coarse.matches||navigator.maxTouchPoints>0?'touch':'desktop';
  document.documentElement.dataset.client=client;
  updateInput(client==='touch'?'touch':'mouse');
}
function updateInput(input){
  document.documentElement.dataset.input=input;
  $('client-label').textContent=input==='touch'?'Сенсорное управление':'Мышь и клавиатура';
  document.querySelector('.chart-hint').textContent=input==='touch'
    ?'Удержание — объёмы и лупа · два пальца — масштаб · свайп — время · правая шкала — цена'
    :'Колесо — масштаб · перетаскивание — время · правая шкала — цена · + / − / 0 — масштаб и сброс';
}
for(const query of Object.values(clientQueries))query.addEventListener('change',updateClient);
document.addEventListener('pointerdown',event=>updateInput(event.pointerType==='touch'?'touch':'mouse'),{passive:true});
document.addEventListener('pointermove',event=>{if(event.pointerType==='mouse'&&document.documentElement.dataset.input!=='mouse')updateInput('mouse');},{passive:true});
updateClient();

let mode='1d',filterType='usdt',data=null,busy=false,sequence=0,connectionFailed=false;
const priceSettings={'1d':{step:200,range:5},'1w':{step:200,range:10}};
const filterSettings={'1d':{type:'usdt',volume:5,lower:67,upper:99},'1w':{type:'usdt',volume:5,lower:67,upper:99}};
let pendingPrice=false,accessTimer=null,pendingResume=false,hiddenAt=0;
let serverTime=Date.now(),clockSync=performance.now();
const serverNow=()=>serverTime+performance.now()-clockSync;
const charts=new Charts($('heatmap'),$('cvd'),(low,high)=>{
  $('scale-low').textContent=money(low,2);$('scale-high').textContent=money(high,2);
});

function setConnection(text,offline=false){
  $('connection').classList.toggle('offline',offline);$('connection').querySelector('span').textContent=text;
}
function selected(attribute,value){
  document.querySelectorAll(`[${attribute}]`).forEach(button=>{
    const active=button.getAttribute(attribute)===value;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));
  });
}
let filterFrame=null;
function scheduleFilter(){
  const lower=Number($('percentile-low').value),upper=Number($('percentile-high').value);
  $('percentile-low-value').textContent=lower+'%';$('percentile-high-value').textContent=upper+'%';
  filterSettings[mode]={type:filterType,volume:Math.max(0,Math.min(100,Number($('volume-input').value)||0)),lower,upper};
  if(filterFrame!==null)return;
  filterFrame=requestAnimationFrame(()=>{filterFrame=null;filter();});
}
function filter(){
  const minimum=Math.max(0,Math.min(100,Number($('volume-input').value)||0))*1e6;
  const lower=Number($('percentile-low').value),upper=Number($('percentile-high').value);
  $('percentile-low-value').textContent=lower+'%';$('percentile-high-value').textContent=upper+'%';
  filterSettings[mode]={type:filterType,volume:minimum/1e6,lower,upper};
  charts.setFilter({type:filterType,minimum,lower,upper});
}
function restoreFilter(){
  const setting=filterSettings[mode];filterType=setting.type;
  selected('data-filter',filterType);
  $('volume-input').value=setting.volume;$('volume-slider').value=setting.volume;
  $('percentile-low').value=setting.lower;$('percentile-high').value=setting.upper;
  $('usdt-controls').hidden=filterType!=='usdt';$('percentile-controls').hidden=filterType!=='percentile';
}
function renderStats(){
  if(!data||data.empty)return;
  const latest=data.candles?.at(-1)?.close??data.last_price;
  $('last-price').textContent=latest.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
  const change=data.candles?.length?(latest/data.candles[0].open-1)*100:data.price_change_pct;
  $('price-change').textContent=(change>=0?'+':'')+change.toFixed(2)+'%';
  $('price-change').classList.toggle('negative',change<0);
  $('price-range').textContent='±'+data.price_margin_pct+'%';
  document.querySelector(`[data-period="${mode}"] span`).textContent='±'+data.price_margin_pct+'%';
  $('range-info').textContent=(mode==='1d'?'Последние 24 часа':'Последние 7 дней')+(data.candles?.length?' · OHLC':' · свечи загружаются');
  $('history-target').textContent=mode==='1d'?'24 часа':'7 дней';
  const hours=(data.end_ms-data.start_ms)/3600000,target=mode==='1d'?24:168;
  $('history-duration').textContent=hours<24?hours.toFixed(1)+' ч':(hours/24).toFixed(1)+' д';
  $('history-progress').style.width=Math.min(100,hours/target*100)+'%';
  $('history-note').textContent=hours<target-.1?'Показан доступный участок. История продолжает накапливаться.':'Полное временное окно доступно; пропуски отмечены на карте.';
  $('updated-at').textContent='Обновлено '+utc(data.generated_ms).slice(11)+' UTC';
  renderFlow();
  checkFreshness();
}
function checkFreshness(){
  if(!data||data.empty)return;
  if(connectionFailed){setConnection('Нет соединения',true);return;}
  const stale=serverNow()-data.collector.heartbeat_ms>30000||data.collector.state!=='collecting';
  const delayed=(data.collector.processing_lag_ms||0)>15000;
  setConnection(stale?'Сборщик неактивен':delayed?'Обработка отстаёт':'Live · 15 сек.',stale||delayed);
}
async function load(reset=false,full=false){
  if(busy||charts.touches.size)return;
  const current=++sequence,requestedMode=mode;const params={mode:requestedMode,...priceSettings[requestedMode]};
  if(!reset&&!full&&data?.revision&&!data.empty)params.since=data.revision;
  busy=true;$('refresh').disabled=true;$('apply-price').disabled=true;
  if(!data||reset)$('loading').hidden=false;
  try{
    const response=await fetch('/api/chart?'+new URLSearchParams(params),{cache:'no-store',signal:AbortSignal.timeout(25000)});
    if(response.status===401){window.location.replace('/');return;}
    const responseTime=Date.parse(response.headers.get('Date'));
    if(Number.isFinite(responseTime)){serverTime=responseTime;clockSync=performance.now();}
    const expires=Number(response.headers.get('X-Access-Expires'));
    if(expires){clearTimeout(accessTimer);accessTimer=setTimeout(()=>window.location.replace('/'),Math.max(0,expires*1000-serverNow()));}
    const result=await response.json();
    if(!response.ok)throw new Error(result.error||'Не удалось обновить график.');
    if(current!==sequence||requestedMode!==mode||charts.touches.size)return;
    if(result.type==='unchanged'){connectionFailed=false;checkFreshness();return;}
    try{data=mergeUpdate(data,result);}catch{data=null;pendingPrice=true;return;}
    connectionFailed=false;$('data-error').hidden=true;
    if(data.empty){charts.setData(data,true);$('data-error').textContent='Пока нет данных для этого периода. Сборщик наполняет архив.';$('data-error').hidden=false;setConnection('Ожидаем данные',true);return;}
    charts.setData(data,reset,result);filter();renderStats();
  }catch(error){
    connectionFailed=true;
    setConnection('Нет соединения',true);
    $('data-error').hidden=false;$('data-error').textContent=data?'Обновление не удалось. Последние данные сохранены на экране; повторим через 15 секунд.':'Сервис временно недоступен. Повторим запрос через 15 секунд.';
  }finally{busy=false;$('refresh').disabled=false;$('apply-price').disabled=false;$('loading').hidden=true;if(pendingPrice){pendingPrice=false;pendingResume=false;load(true);}else if(pendingResume){pendingResume=false;load(false,true);}}
}
document.querySelectorAll('[data-period]').forEach(button=>button.addEventListener('click',async()=>{
  if(busy)return;mode=button.dataset.period;restoreFilter();selected('data-period',mode);$('price-step').value=priceSettings[mode].step;$('price-margin').value=priceSettings[mode].range;syncPriceSliders();await load(true);
}));
document.querySelectorAll('[data-filter]').forEach(button=>button.addEventListener('click',()=>{
  filterType=button.dataset.filter;selected('data-filter',filterType);
  $('usdt-controls').hidden=filterType!=='usdt';$('percentile-controls').hidden=filterType!=='percentile';filter();
}));
$('volume-input').addEventListener('input',()=>{$('volume-slider').value=$('volume-input').value;scheduleFilter();});
$('volume-slider').addEventListener('input',()=>{$('volume-input').value=$('volume-slider').value;scheduleFilter();});
$('percentile-low').addEventListener('input',()=>{
  if(Number($('percentile-low').value)>=Number($('percentile-high').value))$('percentile-high').value=Number($('percentile-low').value)+1;scheduleFilter();
});
$('percentile-high').addEventListener('input',()=>{
  if(Number($('percentile-high').value)<=Number($('percentile-low').value))$('percentile-low').value=Number($('percentile-high').value)-1;scheduleFilter();
});
document.querySelectorAll('[data-cvd]').forEach(button=>button.addEventListener('click',()=>{
  charts.normalized=button.dataset.cvd==='normalized';selected('data-cvd',button.dataset.cvd);charts.draw();
}));
SERIES.forEach(([label,color,hidden],i)=>{
  if(hidden)return;
  const button=document.createElement('button'),line=document.createElement('span');line.style.setProperty('--series-color',color);if(i===0)line.classList.add('series-total');
  button.append(line,document.createTextNode(label));button.classList.toggle('off',!charts.visible[i]);button.setAttribute('aria-pressed',String(charts.visible[i]));
  button.addEventListener('click',()=>{charts.visible[i]=!charts.visible[i];button.classList.toggle('off',!charts.visible[i]);button.setAttribute('aria-pressed',String(charts.visible[i]));charts.draw();});$('cvd-legend').append(button);
});
$('reset-view').addEventListener('click',()=>charts.reset());$('refresh').addEventListener('click',()=>load());
$('zoom-in').addEventListener('click',()=>charts.zoom(.65,charts.anchor()));
$('zoom-out').addEventListener('click',()=>charts.zoom(1.5,charts.anchor()));
function resumePage(force=false){
  charts.resume();
  const full=force||(hiddenAt&&Date.now()-hiddenAt>60000);hiddenAt=0;
  if(busy)pendingResume=true;else load(false,Boolean(full));
}
document.addEventListener('visibilitychange',()=>{if(document.hidden){hiddenAt=Date.now();charts.cancelHold();}else resumePage();});
window.addEventListener('pageshow',event=>{if(event.persisted)resumePage(true);});
setInterval(()=>{if(!document.hidden)load();},15000);
setInterval(()=>{checkFreshness();renderFlow();},5000);

// Discard obsolete one-time link fragments from previously shared URLs.
if(new URLSearchParams(location.hash.slice(1)).has('login'))history.replaceState(null,'',location.pathname+location.search);
load(true);

$('apply-price').addEventListener('click',()=>{
  const step=Number($('price-step').value),range=Number($('price-margin').value);
  const valid=Number.isInteger(step)&&step>=25&&step<=2000&&step%25===0&&Number.isFinite(range)&&range>=.5&&range<=20&&Number.isInteger(range*2);
  $('price-settings-error').hidden=valid;
  if(!valid){$('price-settings-error').textContent='Шаг: 25–2000 USDT, кратно 25. Диапазон: 0.5–20%, шаг 0.5%.';return;}
  priceSettings[mode]={step,range};if(busy)pendingPrice=true;else load(true);
});

for(const canvas of [$('heatmap'),$('cvd')])canvas.addEventListener('keydown',event=>{
  if(event.ctrlKey||event.metaKey||event.altKey)return;
  if(event.key==='+'||event.key==='=')charts.zoom(.8,charts.anchor());
  else if(event.key==='-')charts.zoom(1.25,charts.anchor());
  else if(event.key==='0'||event.key==='Escape')charts.reset();
  else if(event.key==='ArrowLeft')charts.pan(-.15);
  else if(event.key==='ArrowRight')charts.pan(.15);
  else return;
  event.preventDefault();
});

function syncPriceSliders(){
  for(const id of ['price-step','price-margin']){
    const input=$(id),slider=$(id+'-slider');
    if(input.value!==''&&input.validity.valid)slider.value=input.value;
  }
}
for(const id of ['price-step','price-margin']){
  $(id+'-slider').addEventListener('input',()=>{$(id).value=$(id+'-slider').value;});
  $(id).addEventListener('input',syncPriceSliders);
}

for(const id of ['price-step','price-margin']){
  const input=$(id);let previous=input.value;
  input.addEventListener('focus',()=>{previous=input.value;input.value='';});
  input.addEventListener('blur',()=>{if(input.value==='')input.value=previous;syncPriceSliders();});
}

document.fonts.ready.then(()=>charts.draw());

let flowMinutes=5;
function renderFlow(){
  const flow=data?.flow?.[String(flowMinutes)];
  const stale=!data?.market_updated_ms||serverNow()-data.market_updated_ms>90000;
  const available=flow?.available;
  $('flow-buy').hidden=$('flow-sell').hidden=!available;
  $('flow-empty').hidden=Boolean(available);$('flow-empty').textContent='Недостаточно данных для полного интервала';
  if(available){
    $('flow-buy-pct').textContent=flow.buy_pct.toFixed(1)+'%';$('flow-sell-pct').textContent=(100-flow.buy_pct).toFixed(1)+'%';
    $('flow-buy-value').textContent=money(flow.buy,2)+' USDT';$('flow-sell-value').textContent=money(flow.sell,2)+' USDT';
  }
  document.querySelector('.flow-track').classList.toggle('unavailable',!available);
  $('flow-buy-bar').style.width=available?flow.buy_pct+'%':'0%';
  $('flow-caption').textContent=available?`Дельта ${flow.delta>=0?'+':''}${money(flow.delta,2)} USDT · до ${utc(flow.end_ms)} UTC · завершённые минуты${stale?' · данные устарели':''}`:'Binance Spot · агрессивные исполнения · завершённые минуты';
}
document.querySelectorAll('[data-flow]').forEach(button=>button.addEventListener('click',()=>{flowMinutes=Number(button.dataset.flow);selected('data-flow',String(flowMinutes));renderFlow();}));

function updateClock(){const now=serverNow();$('utc-clock').textContent=utc(now).slice(11)+' UTC';$('utc-clock').dateTime=new Date(now).toISOString();}
updateClock();setInterval(()=>{if(!document.hidden)updateClock();},1000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)updateClock();});
