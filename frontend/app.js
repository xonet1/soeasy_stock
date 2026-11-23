/* app.js — 股票分析工作台前端逻辑 */
const API = '';
let stockChart = null, equityChart = null, corrChart = null, accountsCorrChart = null;
let currentStock = null, currentBars = null;
let activeIndicators = new Set(['kline','macd','kdj','rsi','vol']);
let screenFilters = new Set();
let signalTypes = new Set(['macd_golden','ma_golden','breakout20','volume_burst']);
let sentParts = new Set(['news','notice','research']);
let sentCurrent = null, sentFilter = 'all', sentDistChart = null, sentTrendChart = null;
let sentBusy = false, sentTimer = null;
let chainCache = {};  // code -> chain data，避免重复加载
let holderCache = {}; // code -> holders data，避免重复加载
let holderTab = 'top';
let deepCache = {};   // code -> deep analysis data
let deepChart = null, deepBusy = false, deepTimer = null;
let relCache = {}, policyCache = {}, synthCache = {};
let relChart = null, synthRadar = null;
let synthBusy = false, synthTimer = null;
let holdings = [];
let accounts = ['默认账户'];
let dataVersion = null;
let watchlistCache = null;
let _holdingsSyncTimer = null;
let _lastPortfolioReq = null;
let portfolioReady = null;  // 持仓从服务端加密文件加载的 Promise，供个股页持仓透视等待

// 持仓与账户名录只存服务端加密文件，浏览器不再保留副本。
function saveHoldings(){ persistPortfolio(); }
function loadAccountsFrom(rows){
  const list=['默认账户'];
  (rows||[]).forEach(a=>{ a=String(a||'').trim(); if(a && !list.includes(a))list.push(a); });
  (holdings||[]).forEach(h=>{ const a=String(h.account||'').trim(); if(a && !list.includes(a))list.push(a); });
  return list;
}
function saveAccounts(list){
  accounts=loadAccountsFrom(list);
  persistPortfolio();
}
function persistPortfolio(){
  clearTimeout(_holdingsSyncTimer);
  _holdingsSyncTimer=setTimeout(()=>{
    fetch('/api/holdings/sync',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({holdings,accounts})}).catch(()=>{});
  },400);
}
// 一次性迁移：旧版本存在浏览器 localStorage 的持仓，写入加密文件成功后清除。
async function migrateLegacyHoldings(){
  let legacy=[], legacyAccounts=[];
  try{
    legacy=JSON.parse(localStorage.getItem('stock_holdings')||'[]');
    legacyAccounts=JSON.parse(localStorage.getItem('stock_accounts')||'[]');
  }catch(e){ return false; }
  if(!Array.isArray(legacy)||!legacy.length)return false;
  try{
    const r=await fetch('/api/holdings/sync',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({holdings:legacy,accounts:legacyAccounts})});
    if(!r.ok)return false;
  }catch(e){ return false; }
  localStorage.removeItem('stock_holdings');
  localStorage.removeItem('stock_accounts');
  return true;
}
async function loadPortfolio(){
  try{
    let d=await api('/api/holdings');
    let rows=Array.isArray(d.holdings)?d.holdings:[];
    let migrated=false;
    if(!rows.length && await migrateLegacyHoldings()){
      migrated=true;
      d=await api('/api/holdings');
      rows=Array.isArray(d.holdings)?d.holdings:[];
    }
    holdings=rows;
    accounts=loadAccountsFrom(d.accounts);
    renderHoldings(); renderAccountPanel();
    if(migrated && rows.length) toast('已从浏览器迁移 '+rows.length+' 条持仓到加密文件');
  }catch(e){
    // 解密失败或接口异常时保留当前空列表，不触发保存，避免覆盖密文。
    console.warn('读取加密持仓失败：'+e.message);
  }
}
function fmt(n, d=2){ return n==null?'—':Number(n).toFixed(d); }
// 价格精度：场内基金(ETF/LOF)按 0.001 报价，股票按 0.01。与后端 price_divisor 保持一致。
const FUND_PREFIXES=['15','16','18','50','51','52','56','58'];
function isFund(code){ return FUND_PREFIXES.some(p=>String(code||'').startsWith(p)); }
function fmtPrice(n, code){ if(n==null) return '—'; return Number(n).toFixed(isFund(code)?3:2); }
function pctCls(n){ return n>0?'up':(n<0?'down':''); }
function pct(n){ return n==null?'—':(n>0?'+':'')+Number(n).toFixed(2)+'%'; }
function esc(s){ return s==null?'':String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
// 页面内浮层提示：取代浏览器原生 alert（原生弹窗位置由浏览器决定，常出现在窗口最上方）
let _toastTimer=null;
function toast(msg,kind){
  const box=document.getElementById('toastBox'); if(!box)return;
  box.textContent=msg;
  box.className='toast show '+(kind==='warn'?'warn':kind==='error'?'error':'success');
  clearTimeout(_toastTimer);
  _toastTimer=setTimeout(()=>{ box.className='toast'; },2800);
}
async function api(path, opts={}){
  const r = await fetch(API+path, opts);
  const j = await r.json();
  if(!j.ok) throw new Error(j.error||'请求失败');
  return j.data;
}

/* ---------- 标签切换 ---------- */
document.querySelectorAll('.tab').forEach(t=>{
  t.addEventListener('click',()=>{
    document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
    document.querySelectorAll('.page').forEach(x=>x.classList.remove('active'));
    t.classList.add('active');
    document.getElementById('page-'+t.dataset.page).classList.add('active');
    if(t.dataset.page==='map') initMapPage();   // 地图页懒加载（首次进入才请求）
    if(t.dataset.page==='controller') initControllerPage();
    if(t.dataset.page==='equity') initEquityPage();
  });
});

/* ---------- 状态 ---------- */
function updateStatusText(s, updated){
  const dot=document.getElementById('liveDot');
  dot.classList.remove('err');
  if(updated) dot.style.background='#e8912d';
  else dot.style.background='';
  document.getElementById('statusText').textContent =
    (updated?'数据已更新 · ':'已连接 · ')+`${s.total} 只标的 · 数据至 ${s.latest_date?fmtDate(s.latest_date):'—'}`;
}
(async ()=>{
  try{
    const s = await api('/api/status');
    dataVersion = s.version;
    updateStatusText(s, false);
  }catch(e){
    document.getElementById('statusText').textContent = '数据源未连接';
    document.getElementById('liveDot').classList.add('err');
  }
})();
function fmtDate(s){ if(!s) return '—'; s=String(s); return s.slice(0,4)+'-'+s.slice(4,6)+'-'+s.slice(6,8); }

// 每 20 秒检测客户端数据是否更新（mtime 变化），自动提示刷新
setInterval(async()=>{
  try{
    const d=await api('/api/data-version');
    if(dataVersion && d.version!==dataVersion){
      dataVersion=d.version;
      watchlistCache=null;  // 数据更新，清空自选股缓存
      const s=await api('/api/status');
      updateStatusText(s, true);
      setTimeout(()=>{ document.getElementById('liveDot').style.background=''; document.getElementById('statusText').textContent=`已连接 · ${s.total} 只标的 · 数据至 ${s.latest_date?fmtDate(s.latest_date):'—'}`; }, 8000);
    }
  }catch(e){}
}, 20000);

/* ---------- 搜索框通用逻辑 ---------- */
function bindSearch(inputId, resultId, onSelect){
  const input = document.getElementById(inputId);
  const box = document.getElementById(resultId);
  let timer=null;
  input.addEventListener('input',()=>{
    clearTimeout(timer);
    const q=input.value.trim();
    if(!q){ box.classList.remove('show'); return; }
    timer=setTimeout(async()=>{
      try{
        const list=await api('/api/search?q='+encodeURIComponent(q));
        box.innerHTML='';
        if(!list.length){ box.innerHTML='<div style="color:#9aa3ad">无结果</div>'; }
        list.forEach(it=>{
          const d=document.createElement('div');
          d.textContent=`${it.code}  ${it.name}`+(it.has_data?'':'（无数据）');
          d.addEventListener('click',()=>{ input.value=it.code; box.classList.remove('show'); onSelect(it); });
          box.appendChild(d);
        });
        box.classList.add('show');
      }catch(e){}
    },200);
  });
  input.addEventListener('blur',()=>setTimeout(()=>box.classList.remove('show'),200));
}
bindSearch('stockSearch','stockResults',()=>{});

/* ---------- 个股技术分析 ---------- */
document.getElementById('stockLoad').addEventListener('click', loadStock);
document.getElementById('stockSearch').addEventListener('keydown',e=>{ if(e.key==='Enter') loadStock(); });

// 从持仓/组合明细直接进入个股分析：设置代码 → 切页 → 加载
function openStock(code){
  const c=String(code||'').trim();
  if(!c) return;
  document.getElementById('stockSearch').value=c;
  document.querySelector('.tab[data-page="stock"]').click();
  loadStock();
  window.scrollTo({top:0,behavior:'smooth'});
}

async function loadStock(){
  const code=(document.getElementById('stockSearch').value||'').trim();
  if(!code){ alert('请输入股票代码'); return; }
  const days=document.getElementById('stockDays').value;
  document.getElementById('stockEmpty').style.display='none';
  document.getElementById('stockContent').style.display='none';
  // 重置舆情区，避免切换股票时残留上一只的结果
  if(sentTimer){ clearInterval(sentTimer); sentTimer=null; }
  sentBusy=false;
  document.getElementById('sentContent').style.display='none';
  document.getElementById('sentStatus').textContent='';
  document.getElementById('sentProgress').classList.remove('show');
  sentCurrent=null;
  // 产业链区重置
  document.getElementById('chainContent').style.display='none';
  document.getElementById('chainEmpty').style.display='block';
  // 股东区重置
  document.getElementById('holderContent').style.display='none';
  document.getElementById('holderEmpty').style.display='block';
  if(deepTimer){ clearInterval(deepTimer); deepTimer=null; }
  deepBusy=false;
  document.getElementById('deepContent').style.display='none';
  document.getElementById('deepProgress').style.display='none';
  // 多维分析区重置
  if(synthTimer){ clearInterval(synthTimer); synthTimer=null; }
  synthBusy=false;
  ['synth','rel','policy'].forEach(p=>{
    document.getElementById(p+'Content').style.display='none';
    document.getElementById(p+'Empty').style.display='block';
    const pg=document.getElementById(p+'Progress'); if(pg) pg.style.display='none';
  });
  // 持仓透视重置
  document.getElementById('holdingContent').style.display='none';
  document.getElementById('holdingEmpty').style.display='block';
  document.getElementById('holdingChip').textContent='';
  const old=document.getElementById('stockLoading');
  if(old) old.remove();
  const loading=document.createElement('div'); loading.className='card loading'; loading.id='stockLoading';
  loading.innerHTML='<span class="spinner"></span>正在分析 '+code+' …';
  document.getElementById('page-stock').insertBefore(loading, document.getElementById('stockContent'));
  try{
    const d=await api(`/api/stock/${encodeURIComponent(code)}?days=${days}`);
    currentStock=d; currentBars=d.bars;
    renderStock(d);
    renderHoldingView(d);  // 持仓透视（异步，不阻塞）
    // 舆情已合并入个股页：分析时自动加载（异步，不阻塞技术图表）
    loadSentiment(false);
  }catch(e){ alert('加载失败：'+e.message); }
  finally{ loading.remove(); }
}

function renderStock(d){
  document.getElementById('stockContent').style.display='block';
  document.getElementById('stockTitle').textContent=`${d.name}  ${d.code}`;
  const last=d.bars[d.bars.length-1];
  document.getElementById('stockMeta').textContent=`数据截止 ${last.date} · 收盘 ¥${fmtPrice(last.close,d.code)} · 共 ${d.bars.length} 根K线`;
  const ind=d.indicators;
  const sigBuy=d.signals.filter(s=>['macd_golden','ma_golden','kdj_oversold','boll_lower','breakout20','volume_burst'].includes(s.type)).length;
  const sigSell=d.signals.filter(s=>['macd_death','ma_death','kdj_overbought','boll_upper'].includes(s.type)).length;
  const sigChip=document.getElementById('signalSummary');
  sigChip.textContent=`偏多信号 ${sigBuy} · 偏空信号 ${sigSell}`;
  sigChip.className='chip'+(sigBuy>sigSell?' up':(sigSell>sigBuy?' down':''));

  const metrics=[
    ['最新收盘','¥'+fmtPrice(ind.close,d.code),''],
    ['MA20',fmtPrice(ind.ma20,d.code),ind.close>ind.ma20?'up':'down'],
    ['MACD 柱',fmt(ind.macd,isFund(d.code)?3:2),ind.macd>0?'up':'down'],
    ['RSI(6)',fmt(ind.rsi6,1),ind.rsi6>70?'up':(ind.rsi6<30?'down':'')],
    ['KDJ-J',fmt(ind.j,1),''],
    ['距布林上轨',ind.boll_up?pct((ind.close/ind.boll_up-1)*100):'—',''],
    ['5日均量',ind.vma5?fmt(ind.vma5/10000,1)+'万股':'—',''],
    ['ATR(14)',fmt(ind.atr,isFund(d.code)?3:2),''],
  ];
  document.getElementById('stockMetrics').innerHTML=metrics.map(m=>
    `<div class="metric"><div class="k">${m[0]}</div><div class="v ${m[2]}">${m[1]}</div></div>`).join('');
  renderStockStats(d);

  document.querySelectorAll('[data-ind]').forEach(c=>{
    c.classList.toggle('on', activeIndicators.has(c.dataset.ind));
  });
  renderMainChart();
  renderSignalList(d.signals);
  renderVolumeDaily(d.volume_daily||{});
  loadModels(d.code, d.is_fund);
  loadPredict(d.code);
  loadFinReport(d.code, d.is_fund);
  // 投资决策引擎：多维信号融合（异步，不阻塞图表）
  loadDecision(d.code);
  // 模块折叠 + 锚点导航（首次分析时构建，DOM 已静态存在）
  try{ buildStockModules(); }catch(e){ console.warn('模块折叠初始化失败',e); }
}

/* ---------- 近 8 年财报分析 ---------- */
let finSeq=0, finChart=null, divChart=null;
async function loadFinReport(code, isFund, refresh){
  const seq=++finSeq;
  const body=document.getElementById('finBody');
  if(isFund){
    body.innerHTML='<div class="empty" style="padding:18px">ETF/基金无个股财报；基金规模与跟踪误差不在本模块范围。</div>';
    return;
  }
  body.innerHTML='<div class="sub" style="padding:14px 0">正在拉取年报数据…</div>';
  try{
    const d=await api('/api/finreport/'+code+(refresh?'?refresh=1':''));
    if(seq!==finSeq) return;
    renderFinReport(d);
  }catch(e){
    if(seq!==finSeq) return;
    body.innerHTML='<div class="empty" style="padding:16px">财报分析失败：'+e.message+'</div>';
  }
}

function renderFinReport(d){
  const body=document.getElementById('finBody');
  if(!d.ok){
    body.innerHTML='<div class="empty" style="padding:16px">'+(d.is_fund?d.message:('不可用：'+(d.error||'未知原因')))+'</div>';
    return;
  }
  const m=d.metrics;
  const vColor=d.score>=75?'var(--up)':(d.score>=60?'var(--up)':(d.score>=45?'var(--warn,#d97706)':'var(--down)'));
  const vCls=d.score>=60?'buy':(d.score>=45?'info':'sell');
  const kv=[
    ['营收 CAGR', m.rev_cagr==null?'—':(m.rev_cagr>0?'+':'')+m.rev_cagr+'%'+(m.rev_cagr_approx?'*':''), m.rev_cagr>0?'up':'down'],
    ['净利 CAGR', m.np_cagr==null?'—':(m.np_cagr>0?'+':'')+m.np_cagr+'%'+(m.np_cagr_approx?'*':''), m.np_cagr>0?'up':'down'],
    ['ROE 均值', m.roe_avg==null?'—':m.roe_avg+'%', m.roe_avg>=10?'up':(m.roe_avg<5?'down':'')],
    ['现金流含金量', m.cash_ratio==null?'—':m.cash_ratio+' 倍', m.cash_ratio>=0.8?'up':(m.cash_ratio<0.4?'down':'')],
    ['盈利年 / 增长年', m.profit_years+'/'+m.years+' · '+m.np_up_years+'/'+m.years, m.profit_years>=7?'up':''],
    ['累计每股分红', m.div_total>0?fmt(m.div_total,2)+' 元 ('+m.div_years+'年)':'无分红', m.div_total>0?'up':''],
    ['平均负债率', m.debt_avg==null?'—':m.debt_avg+'%', m.debt_avg<50?'up':(m.debt_avg>65?'down':'')],
    ['ROE 区间', (m.roe_min==null?'—':m.roe_min+'%')+' ~ '+(m.roe_max==null?'—':m.roe_max+'%'), ''],
  ];
  // 趋势图
  const chartHtml='<div id="finChart" style="width:100%;height:300px;margin-top:14px"></div>';
  // 年报表
  const rows=d.rows.slice().reverse();
  const tableHtml='<div style="overflow-x:auto"><table><thead><tr>'+
    ['年份','营收(亿)','同比','净利(亿)','同比','净利率','毛利率','ROE','负债率','EPS','每股经营现金流','每股分红'].map(h=>`<th>${h}</th>`).join('')+
    '</tr></thead><tbody>'+rows.map(r=>`<tr>
      <td class="code">${r.year}</td>
      <td>${r.rev!=null?fmt(r.rev,1):'—'}</td>
      <td class="${(r.rev_g||0)>0?'up':((r.rev_g||0)<0?'down':'')}">${r.rev_g!=null?pct(r.rev_g):'—'}</td>
      <td>${r.np!=null?fmt(r.np,1):'—'}</td>
      <td class="${(r.np_g||0)>0?'up':((r.np_g||0)<0?'down':'')}">${r.np_g!=null?pct(r.np_g):'—'}</td>
      <td>${r.net_rate!=null?fmt(r.net_rate,1)+'%':'—'}</td>
      <td>${r.gm!=null?fmt(r.gm,1)+'%':'—'}</td>
      <td class="${(r.roe||0)>=10?'up':((r.roe||0)<5?'down':'')}">${r.roe!=null?fmt(r.roe,1)+'%':'—'}</td>
      <td>${r.debt!=null?fmt(r.debt,1)+'%':'—'}</td>
      <td>${r.eps!=null?fmt(r.eps,2):'—'}</td>
      <td>${r.ocf_ps!=null?fmt(r.ocf_ps,2):'—'}</td>
      <td class="${r.dps>0?'up':''}" title="${esc(r.div_plan||'')}">${r.dps>0?fmt(r.dps,2):'—'}</td>
    </tr>`).join('')+'</tbody></table></div>';

  // 分红区块（年报 + 中期/三季报全部派息，一年多次按年度合计）
  const dv=d.dividend||{};
  const ds=dv.summary||{};
  const drows=dv.rows||[];
  const devents=dv.events||[];
  const multiYears=drows.filter(r=>(r.count||0)>=2).length;
  const divHtml=!drows.length?'':`
  <div style="margin-top:18px;border-top:1px solid var(--border);padding-top:14px">
    <div class="row" style="justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px">
      <div><b style="font-size:14px">💰 分红情况（近 ${drows.length} 年 · 含中期分红）</b>
        ${multiYears?`<span class="tag info" style="margin-left:8px">${multiYears} 年一年两次派息</span>`:''}
        <span class="tag ${ds.streak>=drows.length&&drows.length>=3?'buy':(ds.streak>=3?'info':'sell')}" style="margin-left:8px">${esc(ds.stability||'—')}</span></div>
      <div class="sub" style="font-size:11px">${dv.available?'年报/中报/三季报全部派息按年度合计，明细见下方「历次派息」':'分红方案暂不可得（网络异常）'}</div>
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px;margin-top:10px">
      <div class="metric"><div class="k">连续分红</div><div class="v ${ds.streak>=3?'up':'down'}">${ds.streak||0} 年</div></div>
      <div class="metric"><div class="k">派息率均值</div><div class="v">${ds.payout_avg!=null?fmt(ds.payout_avg,1)+'%':'—'}</div></div>
      <div class="metric"><div class="k">最新年度股息率</div><div class="v ${(ds.yield_latest||0)>=2?'up':''}">${ds.yield_latest!=null?fmt(ds.yield_latest,2)+'%':'—'}</div></div>
      <div class="metric"><div class="k">股息率均值</div><div class="v">${ds.yield_avg!=null?fmt(ds.yield_avg,2)+'%':'—'}</div></div>
      <div class="metric"><div class="k">分红总额累计</div><div class="v">${ds.div_amount_total!=null?fmt(ds.div_amount_total,1)+'亿':'—'}</div></div>
      <div class="metric"><div class="k">分红增长年数</div><div class="v">${ds.dps_growing_years||0}/${drows.length}</div></div>
    </div>
    <div id="divChart" style="width:100%;height:220px;margin-top:10px"></div>
    <div style="overflow-x:auto;margin-top:6px"><table><thead><tr>
      ${['年份','次数','每股分红(元)','同比','派息率','股息率','年末/最新价','现金分红(亿)','分配方案（逐次）'].map(h=>`<th>${h}</th>`).join('')}
    </tr></thead><tbody>${drows.slice().reverse().map(r=>`<tr>
      <td class="code">${r.year}</td>
      <td>${(r.count||1)>1?`<span class="tag buy">${r.count} 次</span>`:(r.partial?'<span class="tag info">仅中期</span>':'1')}</td>
      <td class="${r.dps>0?'up':''}">${r.dps>0?fmt(r.dps,2):'—'}</td>
      <td class="${(r.dps_g||0)>0?'up':((r.dps_g||0)<0?'down':'')}">${r.dps_g!=null?pct(r.dps_g):'—'}</td>
      <td>${r.payout!=null?fmt(r.payout,1)+'%':'—'}</td>
      <td class="${(r.div_yield||0)>=2?'up':''}">${r.div_yield!=null?fmt(r.div_yield,2)+'%':'—'}</td>
      <td>${r.year_end_price!=null?fmt(r.year_end_price,2):'—'}</td>
      <td>${r.div_amount!=null?fmt(r.div_amount,2):'—'}</td>
      <td class="sub" style="font-size:11px;line-height:1.5">${(r.plans&&r.plans.length)?r.plans.map((p,i)=>`<div>${esc((r.kinds&&r.kinds[i])||'')}：${esc(p.length>26?p.slice(0,26)+'…':p)}</div>`).join(''):(r.plan?esc(r.plan.length>26?r.plan.slice(0,26)+'…':r.plan):'—')}</td>
    </tr>`).join('')}</tbody></table></div>
    ${devents.length?`<details style="margin-top:12px">
      <summary style="cursor:pointer;font-size:13px;font-weight:600;color:var(--text)">📋 历次派息明细（共 ${devents.length} 次，含 2026 中期 / 一年多次）</summary>
      <div style="overflow-x:auto;margin-top:8px"><table><thead><tr>
        ${['报告期','类型','每股派息(元)','分配方案'].map(h=>`<th>${h}</th>`).join('')}
      </tr></thead><tbody>${devents.map(e=>`<tr>
        <td class="code">${esc(e.period)}</td>
        <td><span class="tag ${e.kind==='年度'?'buy':'info'}">${esc(e.kind||'—')}</span></td>
        <td class="up">${fmt(e.dps,4)}</td>
        <td class="sub" style="font-size:11px" title="${esc(e.plan||'')}">${esc(e.plan||'')}</td>
      </tr>`).join('')}</tbody></table></div>
    </details>`:''}
    <div class="sub" style="font-size:11px;margin-top:8px">${esc(dv.note||'')}</div>
  </div>`;

  body.innerHTML=`
  <div class="row" style="gap:14px;align-items:stretch;flex-wrap:wrap">
    <div style="flex:0 0 220px;display:flex;flex-direction:column;justify-content:center;text-align:center;background:var(--surface-2);border-radius:10px;padding:14px">
      <div class="sub">长期财务健康分</div>
      <div style="font-size:38px;font-weight:700;color:${vColor};margin:4px 0">${d.score}<span style="font-size:14px;color:var(--hint)">/100</span></div>
      <div><span class="tag ${vCls}">${d.verdict}</span></div>
      <div class="sub" style="font-size:11px;margin-top:6px">${d.note}</div>
      <div class="sub" style="font-size:10px;margin-top:6px;text-align:left">${d.parts.map(p=>`${p.name} ${p.score}/${p.max}`).join('<br>')}</div>
    </div>
    <div style="flex:1 1 420px;min-width:300px;display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px;align-content:start">
      ${kv.map(x=>`<div class="metric"><div class="k">${x[0]}</div><div class="v ${x[2]||''}">${x[1]}</div></div>`).join('')}
    </div>
  </div>
  ${chartHtml}
  <div style="margin-top:12px">${tableHtml}</div>
  ${divHtml}
  <div class="sub" style="font-size:11px;margin-top:8px">${d.disclaimer}${(m.rev_cagr_approx||m.np_cagr_approx)?' *含负值年份，用均值同比近似 CAGR。':''}</div>`;

  // ECharts：营收/净利柱 + ROE 线（双轴）
  if(finChart){ finChart.dispose(); }
  finChart=echarts.init(document.getElementById('finChart'));
  const years=d.rows.map(r=>r.year);
  finChart.setOption({
    tooltip:{trigger:'axis',backgroundColor:'#fff',borderColor:'#e5e8ec',textStyle:{color:'#1f2733',fontSize:12}},
    legend:{data:['营收(亿)','净利(亿)','ROE%'],textStyle:{fontSize:11,color:'#6b7480'},top:0},
    grid:{left:8,right:8,top:30,bottom:24,containLabel:true},
    xAxis:{type:'category',data:years,axisLabel:{fontSize:11,color:'#4a5568'}},
    yAxis:[
      {type:'value',name:'亿元',axisLabel:{fontSize:10,color:'#9aa5b4'},splitLine:{lineStyle:{color:'#eef1f5'}},nameTextStyle:{fontSize:10,color:'#9aa5b4'}},
      {type:'value',name:'ROE%',axisLabel:{fontSize:10,color:'#9aa5b4'},splitLine:{show:false},nameTextStyle:{fontSize:10,color:'#9aa5b4'}},
    ],
    series:[
      {name:'营收(亿)',type:'bar',data:d.rows.map(r=>r.rev),itemStyle:{color:'#60a5fa',borderRadius:[2,2,0,0]},barMaxWidth:26},
      {name:'净利(亿)',type:'bar',data:d.rows.map(r=>r.np),itemStyle:{color:'#2563eb',borderRadius:[2,2,0,0]},barMaxWidth:26},
      {name:'ROE%',type:'line',yAxisIndex:1,data:d.rows.map(r=>r.roe),lineStyle:{color:'#d23b3b',width:2},itemStyle:{color:'#d23b3b'},symbolSize:5,smooth:true},
    ],
  },true);
  window.addEventListener('resize',()=>finChart.resize());

  // 分红图：每股分红柱 + 派息率线（双轴）
  if(dv.rows&&dv.rows.length){
    const el=document.getElementById('divChart');
    if(!divChart) divChart=echarts.init(el);
    const dyears=dv.rows.map(r=>r.year);
    divChart.setOption({
      tooltip:{trigger:'axis',backgroundColor:'#fff',borderColor:'#e5e8ec',textStyle:{color:'#1f2733',fontSize:12}},
      legend:{data:['每股分红(元)','派息率%','股息率%'],textStyle:{fontSize:11,color:'#6b7480'},top:0},
      grid:{left:8,right:8,top:30,bottom:24,containLabel:true},
      xAxis:{type:'category',data:dyears,axisLabel:{fontSize:11,color:'#4a5568'}},
      yAxis:[
        {type:'value',name:'元/股',axisLabel:{fontSize:10,color:'#9aa5b4'},splitLine:{lineStyle:{color:'#eef1f5'}},nameTextStyle:{fontSize:10,color:'#9aa5b4'}},
        {type:'value',name:'%',axisLabel:{fontSize:10,color:'#9aa5b4'},splitLine:{show:false},nameTextStyle:{fontSize:10,color:'#9aa5b4'}},
      ],
      series:[
        {name:'每股分红(元)',type:'bar',data:dv.rows.map(r=>r.dps||0),itemStyle:{color:'#16a34a',borderRadius:[2,2,0,0]},barMaxWidth:26},
        {name:'派息率%',type:'line',yAxisIndex:1,data:dv.rows.map(r=>r.payout),lineStyle:{color:'#d97706',width:2},itemStyle:{color:'#d97706'},symbolSize:5,smooth:true},
        {name:'股息率%',type:'line',yAxisIndex:1,data:dv.rows.map(r=>r.div_yield),lineStyle:{color:'#0891b2',width:2,type:'dashed'},itemStyle:{color:'#0891b2'},symbolSize:4,smooth:true},
      ],
    },true);
    window.addEventListener('resize',()=>divChart.resize());
  }
}

/* ---------- 下一交易日概率评级 ---------- */
let predictSeq=0;
async function loadPredict(code){
  const seq=++predictSeq;
  const body=document.getElementById('predictBody');
  body.innerHTML='<div class="sub" style="padding:14px 0">正在统计该股历史相似条件…</div>';
  try{
    const d=await api('/api/predict/'+code);
    if(seq!==predictSeq) return;
    renderPredict(d);
  }catch(e){
    if(seq!==predictSeq) return;
    body.innerHTML='<div class="empty" style="padding:16px">概率评级失败：'+e.message+'</div>';
  }
}

function condChip(k, v){
  const names={trend:'MA20趋势',pos:'收盘vs MA20',vol:'量能状态',macd:'MACD动能',rsi:'RSI6',chg:'当日涨跌'};
  const vals={vol:['明显缩量','缩量','平量','放量','明显放量'],chg:['跌>3%','跌0~3%','涨0~1.5%','涨1.5~3%','涨>3%'],
              rsi:['<30超卖','30~50','50~70','>70超买']};
  let vv=v;
  if(k==='vol'&&vals.vol[v]!=null) vv=vals.vol[v];
  if(k==='chg'&&vals.chg[v]!=null) vv=vals.chg[v];
  if(k==='rsi'&&vals.rsi[v]!=null) vv=vals.rsi[v];
  if(k==='macd'){ vv={'多扩':'多头·柱扩大','多敛':'多头·柱收敛','空扩':'空头·柱扩大','空敛':'空头·柱收敛'}[v]||v; }
  return `<span class="chip on" style="font-size:11px" title="${names[k]||k}">${names[k]||k}: ${vv}</span>`;
}

function renderPredict(d){
  const body=document.getElementById('predictBody');
  if(!d.ok){
    body.innerHTML='<div class="empty" style="padding:16px">'+(d.error||'不可用')+'</div>';
    return;
  }
  const pu=d.p_up, pd=d.p_down;
  const upColor=pu>=62?'var(--up)':(pu>=45?'var(--warn,#d97706)':'var(--down)');
  const gradeCls=d.grade==='A'||d.grade==='B'?'buy':(d.grade==='C'?'info':'sell');
  const condHtml=Object.entries(d.conditions).map(([k,v])=>condChip(k,v)).join('');
  const st=d.stats;
  const statsHtml=st?`
    <table style="margin-top:8px"><tbody>
      <tr><td>相似样本</td><td><b>${st.n}</b> 次（${d.match_level}）</td><td>次日胜率</td><td class="${st.win_rate>=50?'up':'down'}"><b>${st.win_rate}%</b></td></tr>
      <tr><td>次日平均</td><td class="${st.avg>0?'up':'down'}"><b>${st.avg>0?'+':''}${st.avg}%</b></td><td>中位数</td><td class="${st.median>0?'up':'down'}"><b>${st.median>0?'+':''}${st.median}%</b></td></tr>
      <tr><td>最好 / 最差</td><td><b style="color:var(--up)">+${st.best}%</b> / <b style="color:var(--down)">${st.worst}%</b></td><td>3 日累计均值</td><td class="${st.avg3>0?'up':'down'}"><b>${st.avg3>0?'+':''}${st.avg3}%</b></td></tr>
    </tbody></table>`:'<div class="sub" style="font-size:12px">历史相似样本不足，仅用研判评分映射。</div>';
  body.innerHTML=`
  <div class="row" style="gap:18px;align-items:center;flex-wrap:wrap">
    <div style="flex:0 0 200px;text-align:center">
      <div style="font-size:13px;color:var(--muted)">截至 ${d.as_of} 收盘</div>
      <div style="margin:6px 0"><span style="font-size:30px;font-weight:700;color:var(--up)">买 ${pu}%</span>
        <span style="color:var(--hint);margin:0 6px">·</span>
        <span style="font-size:20px;font-weight:600;color:var(--down)">卖 ${pd}%</span></div>
      <div><span class="tag ${gradeCls}" style="font-size:14px">评级 ${d.grade} · ${d.label}</span>
        <span title="买入侧星级" style="color:var(--warn,#d97706);font-size:13px">${'★'.repeat(d.stars_buy)}${'☆'.repeat(5-d.stars_buy)}</span></div>
      ${d.low_confidence?'<div class="sub" style="font-size:11px;color:var(--warn,#d97706);margin-top:4px">⚠ 样本较少或维度已放宽，置信度偏低</div>':''}
    </div>
    <div style="flex:1 1 340px;min-width:300px">
      <div style="height:16px;display:flex;border-radius:8px;overflow:hidden;margin-bottom:6px">
        <div style="width:${pu}%;background:var(--up);opacity:.85" title="上涨概率 ${pu}%"></div>
        <div style="width:${pd}%;background:var(--down);opacity:.85" title="下跌概率 ${pd}%"></div>
      </div>
      <div class="row" style="justify-content:space-between;font-size:11px;color:var(--hint)"><span>← 下跌 ${pd}%</span><span>上涨 ${pu}% →</span></div>
      <div class="row" style="gap:6px;margin-top:10px;flex-wrap:wrap">${condHtml}</div>
      <div class="sub" style="font-size:11px;margin-top:8px">
        概率构成：经验统计 ${d.p_empirical==null?'—':d.p_empirical+'%'}（权重65%）+ 研判评分映射 ${d.p_verdict}%（研判分 ${d.verdict_score}，权重35%）→ 融合 ${pu}%（${d.basis}）
      </div>
    </div>
  </div>
  <div style="margin-top:12px">
    <div class="sub" style="font-weight:600;font-size:12px">历史相似条件下的次日表现</div>
    ${statsHtml}
  </div>
  <div class="sub" style="font-size:11px;margin-top:10px">${d.note}</div>`;
}

/* ---------- 多模型购买参考 ---------- */
let modelsSeq=0;
async function loadModels(code, isFund){
  const seq=++modelsSeq;
  const body=document.getElementById('modelsBody');
  if(isFund){
    body.innerHTML='<div class="empty" style="padding:18px">ETF/基金无个股财务模型；技术维度请参考上方量能研判与信号模块。</div>';
    return;
  }
  body.innerHTML='<div class="sub" style="padding:14px 0">正在计算 9 个经典模型…（财务数据来自东财公开报表，技术数据来自本地日线）</div>';
  try{
    const d=await api('/api/models/'+code);
    if(seq!==modelsSeq) return;
    renderModels(d);
  }catch(e){
    if(seq!==modelsSeq) return;
    body.innerHTML='<div class="empty" style="padding:16px">模型计算失败：'+e.message+'</div>';
  }
}

function scoreBar(label, val, max, note){
  const pctv=val==null?0:Math.min(100,Math.round(val/max*100));
  const color=val==null?'var(--hint)':(pctv>=60?'var(--up)':(pctv>=40?'var(--warn,#d97706)':'var(--down)'));
  return `<div style="margin-bottom:10px" title="${note||''}">
    <div class="row" style="justify-content:space-between;font-size:12px;margin-bottom:3px">
      <span>${label}</span><b style="color:${color}">${val==null?'不可得':val+' / '+max}</b>
    </div>
    <div style="height:6px;background:var(--surface-2);border-radius:3px;overflow:hidden">
      <div style="height:100%;width:${pctv}%;background:${color};border-radius:3px"></div>
    </div></div>`;
}

function renderModels(d){
  const body=document.getElementById('modelsBody');
  if(!d.ok){
    body.innerHTML='<div class="empty" style="padding:16px">'+(d.is_fund?d.message:('模型不可用：'+(d.error||'未知原因')))+'</div>';
    return;
  }
  const sc=d.scores, T=d.total, V=d.verdict;
  const vColor=T>=70?'var(--up)':(T>=55?'var(--up)':(T>=40?'var(--warn,#d97706)':(T>=25?'var(--down)':'var(--down)')));
  // 雷达图
  const radarEl=`<div id="modelsRadar" style="width:100%;height:260px"></div>`;
  // 各模型明细
  const fs=d.fscore, gr=d.graham, pg=d.peg, zr=d.zrisk, qu=d.quality, te=d.technical||{};
  const fsItems=(fs.items||[]).map(it=>{
    const icon=it.pass===null?'—':(it.pass?'✓':'✗');
    const color=it.pass===null?'var(--hint)':(it.pass?'var(--up)':'var(--down)');
    return `<div style="display:flex;justify-content:space-between;font-size:12px;padding:4px 0;border-bottom:1px dashed var(--surface-2)">
      <span title="${it.note||''}">${it.name}${it.val!=null?` <span style="color:var(--hint)">(${Array.isArray(it.val)?it.val.join('→'):it.val})</span>`:''}</span>
      <b style="color:${color}">${icon}</b></div>`;
  }).join('');
  const gv=(te.granville||{}).fired||[];
  const gvHtml=gv.length?gv.map(f=>`<div style="font-size:12px;margin-bottom:4px"><span class="tag ${f[0].startsWith('买')?'buy':'sell'}">${f[0]}</span> ${f[1]}</div>`).join(''):'<div class="sub" style="font-size:12px">当前无格兰维尔法则触发（中性）</div>';
  const zParts=(zr.parts||[]).map(p=>`<div style="display:flex;justify-content:space-between;font-size:12px;padding:3px 0"><span title="${p.note||''}">${p.name}</span><b>${p.val}${p.unit||''} <span style="color:${p.score>=2?'var(--up)':(p.score>=1?'var(--warn,#d97706)':'var(--down)')}">${'●'.repeat(p.score)}${'○'.repeat(2-p.score)}</span></b></div>`).join('');
  const quParts=(qu.parts||[]).map(p=>`<div style="display:flex;justify-content:space-between;font-size:12px;padding:3px 0"><span>${p.name}</span><b>${p.val==null?'—':p.val+(p.unit||'')}</b></div>`).join('');

  body.innerHTML=`
  <div class="row" style="gap:16px;align-items:stretch;flex-wrap:wrap">
    <div style="flex:1 1 300px;min-width:280px">
      ${radarEl}
      <div style="text-align:center;margin-top:-6px">
        <span style="font-size:34px;font-weight:700;color:${vColor}">${T}</span>
        <span class="tag ${T>=55?'buy':(T>=40?'info':'sell')}" style="font-size:13px;margin-left:8px;vertical-align:middle">${V}</span>
      </div>
      <div class="sub" style="text-align:center;font-size:11px;margin-top:2px">综合分 0~100（基本面 45% + 估值 20% + 技术 25% + 风险 10%）</div>
    </div>
    <div style="flex:1.2 1 320px;min-width:300px">
      <div class="sub" style="font-weight:600;margin-bottom:8px">五维评分</div>
      ${scoreBar('基本面质量（ROE/毛利/负债/现金流/增速）',sc.quality,100,'质量五维加权')}
      ${scoreBar('Piotroski F-Score（财务健康）',sc.fscore,100,fs.note)}
      ${scoreBar('估值（格雷厄姆安全边际+PEG）',sc.valuation,100,gr?('格雷厄姆数字 '+gr.graham+' vs 现价 '+gr.price+'，安全边际 '+gr.margin+'%'):'')}
      ${scoreBar('技术趋势（海龟+格兰维尔+动量）',sc.technical,100)}
      ${scoreBar('财务风险（Z-Score 思想简化版）',sc.risk,100,'分项越足风险越低')}
      <div class="sub" style="font-size:11px;margin-top:6px">${d.disclaimer}</div>
    </div>
  </div>
  <div class="grid g2" style="margin-top:16px;gap:14px">
    <div>
      <div class="sub" style="font-weight:600;margin-bottom:6px">F-Score ${fs.score}/${fs.of} 明细 <span class="src-tag real">真实数据</span></div>
      ${fsItems}
    </div>
    <div>
      <div class="sub" style="font-weight:600;margin-bottom:6px">关键估值指标</div>
      <div style="display:flex;justify-content:space-between;font-size:12px;padding:4px 0"><span>格雷厄姆数字 √(22.5×EPS×BPS)</span><b>${gr?gr.graham:'不可得'}</b></div>
      <div style="display:flex;justify-content:space-between;font-size:12px;padding:4px 0"><span>　vs 现价 ${gr?gr.price:''} 安全边际</span><b style="color:${gr&&gr.margin>0?'var(--up)':'var(--down)'}">${gr?gr.margin+'%':'—'}</b></div>
      <div style="display:flex;justify-content:space-between;font-size:12px;padding:4px 0" title="${pg?pg.note:''}"><span>PEG（PE ${pg?pg.pe:'—'} ÷ 增速 ${pg?pg.growth+'%':'—'}）</span><b>${pg?pg.peg:':不可得'}</b></div>
      ${pg&&pg.reason?`<div class="sub" style="font-size:11px;color:var(--warn,#d97706)">${pg.reason}</div>`:''}
      <div class="sub" style="font-weight:600;margin:10px 0 6px">财务风险分项（Z-Score 思想）</div>
      ${zParts}
      <div class="sub" style="font-weight:600;margin:10px 0 6px">质量五维</div>
      ${quParts}
    </div>
  </div>
  <div class="grid g2" style="margin-top:14px;gap:14px">
    <div>
      <div class="sub" style="font-weight:600;margin-bottom:6px">海龟法则（唐奇安通道）</div>
      <div style="font-size:12px;line-height:1.7">
        <div>状态：<b>${te.turtle?te.turtle.state:'—'}</b></div>
        ${te.turtle?`<div style="color:var(--hint)">20 日高 ${te.turtle.hi20} · 10 日低 ${te.turtle.lo10} · ATR ${te.turtle.atr}</div><div>${te.turtle.action}</div>`:''}
      </div>
    </div>
    <div>
      <div class="sub" style="font-weight:600;margin-bottom:6px">格兰维尔八大法则（vs MA20，乖离 ${te.granville?te.granville.dist+'%':''}）</div>
      ${gvHtml}
    </div>
  </div>
  <div class="row" style="margin-top:12px;gap:14px;flex-wrap:wrap">
    <div style="flex:1 1 200px;font-size:12px">${te.momentum?`<span class="sub">12-1月动量：</span><b style="color:${te.momentum.mom>0?'var(--up)':'var(--down)'}">${te.momentum.mom>0?'+':''}${te.momentum.mom}%</b> <span class="sub">（${te.momentum.window}日窗口）</span>`:'<span class="sub">动量：样本不足</span>'}</div>
    <div style="flex:1 1 200px;font-size:12px">${te.volatility?`<span class="sub">年化波动率：</span><b>${te.volatility.vol}%</b> <span class="sub">${te.volatility.vol_hist?('（历史均值 '+te.volatility.vol_hist+'%）'):''}</span>`:''}</div>
  </div>`;

  // 雷达图
  const radar=echarts.init(document.getElementById('modelsRadar'));
  const rInd=[
    {name:'质量',max:100},{name:'财务健康',max:100},{name:'估值',max:100},
    {name:'趋势',max:100},{name:'风险防护',max:100}];
  const rVal=[sc.quality,sc.fscore,sc.valuation,sc.technical,sc.risk].map(v=>v==null?0:v);
  radar.setOption({
    radar:{indicator:rInd,radius:'68%',splitNumber:4,
      axisName:{color:'#6b7480',fontSize:11},
      splitLine:{lineStyle:{color:'#e5e8ec'}},
      splitArea:{areaStyle:{color:['#fafbfd','#f3f5f8']}},
      axisLine:{lineStyle:{color:'#e5e8ec'}}},
    series:[{type:'radar',data:[{value:rVal,name:'模型评分'}],
      areaStyle:{color:'rgba(37,99,235,.18)'},
      lineStyle:{color:'#2563eb',width:2},
      itemStyle:{color:'#2563eb'},symbolSize:4}],
    tooltip:{backgroundColor:'#fff',borderColor:'#e5e8ec',textStyle:{color:'#1f2733',fontSize:12}},
  });
  window.addEventListener('resize',()=>radar.resize());
}

/* ---------- 量能分析 · 每日综合研判 ---------- */
function volStateTag(state){
  if(state==='明显放量') return '<span class="tag buy" title="量比≥1.8">明显放量</span>';
  if(state==='放量') return '<span class="tag buy" style="opacity:.75" title="量比1.3~1.8">放量</span>';
  if(state==='缩量') return '<span class="tag sell" style="opacity:.75" title="量比0.6~0.9">缩量</span>';
  if(state==='明显缩量') return '<span class="tag sell" title="量比<0.6">明显缩量</span>';
  if(state==='平量') return '<span class="tag info">平量</span>';
  return '<span class="tag info">—</span>';
}
function verdictTag(label,code){
  const cls=code>=2?'buy':(code===1?'buy':(code===-2?'sell':(code===-1?'sell':'info')));
  const op=(code===2||code===-2)?'':' style="opacity:.8"';
  return `<span class="tag ${cls}"${op}>${label}</span>`;
}
function renderVolumeDaily(vd){
  const latest=document.getElementById('volumeLatest');
  const table=document.getElementById('volumeTable');
  const vs=(vd.verdicts||[]);
  if(!vs.length){ latest.innerHTML='<div class="empty" style="padding:16px">数据不足，无法研判</div>'; table.innerHTML=''; return; }
  // 最新研判卡
  const cur=vs[vs.length-1];
  const partsHtml=(cur.parts||[]).map(p=>{
    const cls=p.delta>0?'up':(p.delta<0?'down':'');
    const sign=p.delta>0?`+${p.delta}`:(p.delta||'0');
    return `<div class="metric"><div class="k">${p.dim} <span class="${cls}" style="font-size:10px">${sign}</span></div><div class="v" style="font-size:11px;font-weight:400;white-space:normal;line-height:1.5">${p.text}</div></div>`;
  }).join('');
  latest.innerHTML=
    `<div class="row" style="align-items:center;gap:10px;margin-bottom:10px">
       <span style="font-size:12px;color:var(--muted)">${cur.date} 综合研判</span>
       ${verdictTag(cur.verdict,cur.verdict_code)}
       <b style="font-size:15px" class="${cur.score>0?'up':(cur.score<0?'down':'')}">${cur.score>0?'+':''}${cur.score}</b>
       ${volStateTag(cur.volume.state)}
       <span class="sub" style="font-size:11px">量比 ${cur.volume.vr??'—'} · ${cur.volume.pattern}（当日涨跌 ${pct(cur.volume.chg)}）</span>
     </div>
     <div class="grid g4">${partsHtml}</div>
     <div class="chain-note" style="margin-top:10px">综合结论：${cur.summary}</div>`;
  // 近 10 日研判表
  const rows=vs.slice().reverse().map(v=>`<tr>
    <td style="white-space:nowrap">${v.date}</td>
    <td>${volStateTag(v.volume.state)} <span class="sub" style="font-size:11px">量比${v.volume.vr??'—'}</span></td>
    <td style="white-space:nowrap">${v.volume.pattern}</td>
    <td class="${(v.volume.chg||0)>0?'up':((v.volume.chg||0)<0?'down':'')}">${pct(v.volume.chg)}</td>
    <td>${verdictTag(v.verdict,v.verdict_code)} <b style="font-size:12px" class="${v.score>0?'up':(v.score<0?'down':'')}">${v.score>0?'+':''}${v.score}</b></td>
    <td class="sig-exp" title="${(v.summary||'').replace(/"/g,'&quot;')}">${v.summary||''}</td>
  </tr>`).join('');
  table.innerHTML='<table><thead><tr><th>日期</th><th>量能</th><th>量价关系</th><th>涨跌</th><th>综合研判</th><th style="min-width:280px">研判结论</th></tr></thead><tbody>'+rows+'</tbody></table>'+
    '<div class="sub" style="font-size:11px;margin-top:8px">评分口径：趋势(±25)/位置(±10)/MACD动能(±10~15)/KDJ(±10)/RSI(±5~10)/布林(±10)/量价配合(±10)，合计 -100~+100；≥40 强偏多，≥15 偏多，-15~15 中性，≤-15 偏空，≤-40 强偏空。规则化参考，非投资建议。</div>';
}

// 行情统计卡：全部由本地日线数据计算（振幅/量比/成交额/52周/分位/区间涨幅/连涨跌/波动率/缺口/扩展指标）
function renderStockStats(d){
  const s=d.stats||{};
  const ind=d.indicators||{};
  const code=d.code;
  const amt=v=>v==null?'—':(Math.abs(v)>=1e8?fmt(v/1e8,2)+'亿':(Math.abs(v)>=1e4?fmt(v/1e4,1)+'万':fmt(v,0)));
  const dirs=s.streak_dir>0?'连涨':(s.streak_dir<0?'连跌':'横盘');
  const metrics=[
    ['当日振幅',s.amplitude!=null?fmt(s.amplitude,2)+'%':'—',''],
    ['5日平均振幅',s.amplitude5!=null?fmt(s.amplitude5,2)+'%':'—',''],
    ['量比(量/5日均量)',s.volume_ratio!=null?fmt(s.volume_ratio,2):'—',s.volume_ratio>1.5?'up':''],
    ['当日成交额',amt(s.amount_today),''],
    ['5日平均成交额',amt(s.amount5),''],
    ['BIAS(6)',ind.bias6!=null?fmt(ind.bias6,2)+'%':'—',ind.bias6>0?'up':(ind.bias6<0?'down':'')],
    ['WR(14)',ind.wr14!=null?fmt(ind.wr14,1):'—',ind.wr14<20?'up':(ind.wr14>80?'down':'')],
    ['ROC(12)',ind.roc12!=null?fmt(ind.roc12,2)+'%':'—',ind.roc12>0?'up':(ind.roc12<0?'down':'')],
    ['52周最高',s.w52_high!=null?fmtPrice(s.w52_high,code):'—',''],
    ['距52周高点',s.drawdown_from_high!=null?pct(s.drawdown_from_high):'—',s.drawdown_from_high!=null&&s.drawdown_from_high>-10?'up':'down'],
    ['52周最低',s.w52_low!=null?fmtPrice(s.w52_low,code):'—',''],
    ['距52周低点',s.gain_from_low!=null?pct(s.gain_from_low):'—',s.gain_from_low>0?'up':''],
    ['价格分位(250日)',s.price_percentile!=null?fmt(s.price_percentile,1)+'%':'—',s.price_percentile>80?'up':(s.price_percentile<20?'down':'')],
    ['近5日涨幅',s.chg5!=null?pct(s.chg5):'—',s.chg5>=0?'up':'down'],
    ['近20日涨幅',s.chg20!=null?pct(s.chg20):'—',s.chg20>=0?'up':'down'],
    ['近60日涨幅',s.chg60!=null?pct(s.chg60):'—',s.chg60>=0?'up':'down'],
    ['近250日涨幅',s.chg250!=null?pct(s.chg250):'—',s.chg250>=0?'up':'down'],
    ['年初至今',s.ytd!=null?pct(s.ytd):'—',s.ytd>=0?'up':'down'],
    [dirs,`${s.streak||0} 天`,s.streak_dir>0?'up':(s.streak_dir<0?'down':'')],
    ['近60日上涨占比',s.up_ratio_60!=null?fmt(s.up_ratio_60,1)+'%':'—',''],
    ['年化波动(60日)',s.volatility_annual!=null?fmt(s.volatility_annual,1)+'%':'—',''],
    ['OBV 能量潮',ind.obv!=null?amt(ind.obv)+'股':'—',''],
  ];
  document.getElementById('stockStats').innerHTML=metrics.map(m=>
    `<div class="metric"><div class="k">${m[0]}</div><div class="v ${m[2]}">${m[1]}</div></div>`).join('');
  // 未回补跳空缺口
  const gaps=(s.gaps||[]);
  const gel=document.getElementById('stockGaps');
  if(gaps.length){
    gel.innerHTML='<div class="row" style="gap:8px;flex-wrap:wrap">'+gaps.map(g=>
      `<span class="tag ${g.type==='up'?'buy':'sell'}">${g.type==='up'?'↑ 向上缺口':'↓ 向下缺口'} ${g.date} ${pct(g.gap_pct)} · 未回补</span>`).join('')+'</div>';
  }else{
    gel.innerHTML='<div class="sub">近 20 个交易日无未回补跳空缺口</div>';
  }
}

function renderMainChart(){
  if(!currentStock) return;
  if(!stockChart) stockChart=echarts.init(document.getElementById('mainChart'));
  const bars=currentBars;
  const dates=bars.map(b=>b.date);
  const ohlc=bars.map(b=>[b.open,b.close,b.low,b.high]);
  const vols=bars.map(b=>({value:b.volume, itemStyle:{color:b.close>=b.open?'#d64545':'#1a9c6b'}}));
  const show=(k)=>activeIndicators.has(k);

  const grids=[]; const xAxes=[]; const yAxes=[]; const series=[];
  // 主图 K 线
  grids.push({left:56,right:20,top:40,height: show('kline')?'52%':'0%'});
  xAxes.push({type:'category',data:dates,gridIndex:0,axisLabel:{show:false},axisLine:{lineStyle:{color:'#e5e8ec'}}});
  const priceDigits=isFund(currentStock.code)?3:2;
  yAxes.push({scale:true,gridIndex:0,splitLine:{lineStyle:{color:'#f0f2f5'}},axisLabel:{color:'#6b7480',formatter:v=>Number(v).toFixed(priceDigits)}});
  if(show('kline')){
    series.push({name:'K线',type:'candlestick',data:ohlc,xAxisIndex:0,yAxisIndex:0,
      itemStyle:{color:'#d64545',color0:'#1a9c6b',borderColor:'#d64545',borderColor0:'#1a9c6b'}});
    [['ma5','#e8912d'],['ma10','#2563eb'],['ma20','#8b5cf6'],['ma60','#6b7480']].forEach(([k,c])=>{
      if(bars[0][k]!=null) series.push({name:k.toUpperCase(),type:'line',data:bars.map(b=>b[k]),xAxisIndex:0,yAxisIndex:0,
        smooth:true,symbol:'none',lineStyle:{width:1.2,color:c}});
    });
  }
  const subInds=[['macd','MACD'],['kdj','KDJ'],['rsi','RSI'],['vol','成交量'],['amount','成交额']].filter(x=>show(x[0]));
  const subTop=show('kline')?'60%':'8%';
  const subH=subInds.length? Math.min(28, 88/(subInds.length))/100 : 0;
  subInds.forEach((ind,idx)=>{
    const gi=idx+1;
    grids.push({left:56,right:20,top:`calc(${subTop} + ${idx} * ${subH*100}% + 10px)`,height:`calc(${subH*100}% - 8px)`});
    xAxes.push({type:'category',data:dates,gridIndex:gi,axisLabel:{show:idx===subInds.length-1,color:'#9aa3ad',fontSize:10},axisLine:{lineStyle:{color:'#e5e8ec'}}});
    yAxes.push({scale:true,gridIndex:gi,splitLine:{show:false},axisLabel:{color:'#9aa3ad',fontSize:10}});
    if(ind[0]==='macd'){
      series.push({name:'DIF',type:'line',data:bars.map(b=>b.dif),xAxisIndex:gi,yAxisIndex:gi,symbol:'none',lineStyle:{width:1.2,color:'#e8912d'}});
      series.push({name:'DEA',type:'line',data:bars.map(b=>b.dea),xAxisIndex:gi,yAxisIndex:gi,symbol:'none',lineStyle:{width:1.2,color:'#2563eb'}});
      series.push({name:'MACD',type:'bar',data:bars.map(b=>({value:b.macd,itemStyle:{color:b.macd>=0?'#d64545':'#1a9c6b'}})),xAxisIndex:gi,yAxisIndex:gi});
    }else if(ind[0]==='kdj'){
      [['k','#e8912d'],['d','#2563eb'],['j','#8b5cf6']].forEach(([k,c])=>{
        series.push({name:k.toUpperCase(),type:'line',data:bars.map(b=>b[k]),xAxisIndex:gi,yAxisIndex:gi,symbol:'none',lineStyle:{width:1.1,color:c}});
      });
    }else if(ind[0]==='rsi'){
      [['rsi6','#e8912d'],['rsi12','#2563eb']].forEach(([k,c])=>{
        if(bars[0][k]!=null) series.push({name:k.toUpperCase(),type:'line',data:bars.map(b=>b[k]),xAxisIndex:gi,yAxisIndex:gi,symbol:'none',lineStyle:{width:1.1,color:c}});
      });
    }else if(ind[0]==='vol'){
      series.push({name:'成交量',type:'bar',data:vols,xAxisIndex:gi,yAxisIndex:gi});
    }else if(ind[0]==='amount'){
      // 成交额副图（单位亿元），红涨绿跌按当日收盘相对前一日
      const amtData=bars.map((b,i)=>({value:b.amount!=null?+(b.amount/1e8).toFixed(2):null,
        itemStyle:{color:i>0&&b.close<bars[i-1].close?'#1a9c6b':'#d64545'}}));
      series.push({name:'成交额(亿)',type:'bar',data:amtData,xAxisIndex:gi,yAxisIndex:gi});
      yAxes[gi]=yAxes[gi]||{};
      yAxes[gi].axisLabel={color:'#9aa3ad',fontSize:10,formatter:v=>Number(v).toFixed(1)};
    }
  });
  const legendNames=[...new Set(series.map(s=>s.name))];
  stockChart.setOption({
    animation:false,
    legend:{data:legendNames,top:6,textStyle:{color:'#6b7480',fontSize:11},itemWidth:14,itemHeight:8},
    tooltip:{trigger:'axis',axisPointer:{type:'cross'},backgroundColor:'#fff',borderColor:'#e5e8ec',textStyle:{color:'#1f2733',fontSize:12}},
    axisPointer:{link:[{xAxisIndex:'all'}]},
    grid:grids,xAxis:xAxes,yAxis:yAxes,series:series,
    dataZoom:[{type:'inside',xAxisIndex:xAxes.map((_,i)=>i),start:50,end:100},
              {type:'slider',xAxisIndex:xAxes.map((_,i)=>i),bottom:4,height:18,start:50,end:100,borderColor:'#e5e8ec'}],
  },true);
}
document.querySelectorAll('[data-ind]').forEach(c=>{
  c.addEventListener('click',()=>{
    if(activeIndicators.has(c.dataset.ind)) activeIndicators.delete(c.dataset.ind);
    else activeIndicators.add(c.dataset.ind);
    c.classList.toggle('on');
    renderMainChart();
  });
});
window.addEventListener('resize',()=>{ if(stockChart)stockChart.resize(); if(equityChart)equityChart.resize(); if(corrChart)corrChart.resize(); if(accountsCorrChart)accountsCorrChart.resize(); });

function renderSignalList(signals){
  const el=document.getElementById('signalList');
  if(!signals.length){ el.innerHTML='<div class="empty">近期无信号</div>'; return; }
  const buySet=['macd_golden','ma_golden','kdj_oversold','boll_lower','breakout20','volume_burst'];
  el.innerHTML='<table><thead><tr><th>日期</th><th>信号</th><th>方向</th><th style="min-width:220px">信号解释</th><th style="min-width:320px">买卖审慎分析</th></tr></thead><tbody>'+
    signals.slice().reverse().map(s=>`<tr><td style="white-space:nowrap">${s.date}</td><td style="white-space:nowrap">${s.label}</td><td><span class="tag ${buySet.includes(s.type)?'buy':'sell'}">${buySet.includes(s.type)?'偏多':'偏空'}</span></td><td class="sig-exp">${s.detail||''}</td><td class="sig-cau">${s.caution||''}</td></tr>`).join('')+
    '</tbody></table>';
}

/* ---------- 持仓组合 ---------- */
const DEFAULT_ACCOUNT='默认账户';
function acctOf(h){ return String(h.account||'').trim()||DEFAULT_ACCOUNT; }
function holdKey(a,c){ return a+'\u0001'+c; }
let holdSelected=new Set();

/** 持仓或账户变更后的统一提交：持久化 -> 同步后端 -> 重渲染 -> 如已有分析结果则重算 */
function commitPortfolio(silent){
  const used=new Set(holdings.map(acctOf));
  used.forEach(a=>{ if(!accounts.includes(a))accounts.push(a); });
  saveAccounts(accounts);
  saveHoldings();
  const valid=new Set(holdings.map(h=>holdKey(acctOf(h),h.code)));
  holdSelected=new Set([...holdSelected].filter(k=>valid.has(k)));
  renderHoldings(); renderAccountPanel();
  if(!silent && _lastPortfolioReq && document.getElementById('portContent').style.display!=='none') rerunPortfolio();
}
async function rerunPortfolio(){
  try{
    const d=await api('/api/portfolio',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(_lastPortfolioReq)});
    renderPortfolio(d);
  }catch(e){ /* 编辑过程中的后台重算失败不打断操作 */ }
}

/* ---- 通用编辑弹窗 ---- */
let _modalOnOk=null;
function openEditor({title,sub,body,okText,onOk}){
  const mask=document.getElementById('acctMask');
  document.getElementById('acctTitle').textContent=title;
  document.getElementById('acctSub').textContent=sub||'';
  document.getElementById('acctBody').innerHTML=body;
  document.getElementById('acctOk').textContent=okText||'保存';
  _modalOnOk=onOk; mask.classList.add('show'); mask.style.display='flex';
  const first=mask.querySelector('#acctBody input,#acctBody select');
  if(first)setTimeout(()=>first.focus(),0);
}
function closeEditor(){
  const mask=document.getElementById('acctMask');
  mask.classList.remove('show'); mask.style.display='none'; _modalOnOk=null;
}
// 回调返回 false 表示校验未通过，保留对话框；其余情况统一关闭。
function submitEditor(){
  const fn=_modalOnOk; if(!fn)return;
  const result=fn();
  if(result!==false)closeEditor();
}

/* ---- 账户名录 ---- */
function renderAccountPanel(){
  const el=document.getElementById('accountPanel'); if(!el)return;
  const counts={};
  holdings.forEach(h=>{ const a=acctOf(h); counts[a]=(counts[a]||0)+1; });
  el.innerHTML='<div class="acct-bar"><span class="acct-title">账户</span>'+accounts.map(a=>{
    const locked=a===DEFAULT_ACCOUNT;
    return '<span class="acct-chip" data-acct="'+esc(a)+'"><b>'+esc(a)+'</b><small>'+(counts[a]||0)+' 只</small>'+
      (locked?'<span class="acct-lock" title="默认账户不可编辑">固定</span>':
        '<button class="acct-mini" data-act="rename" title="重命名">编辑</button><button class="acct-mini danger" data-act="delete" title="删除账户">删除</button>')+
      '</span>';
  }).join('')+'<button class="acct-mini add" id="acctAdd">+ 新增账户</button></div>';
  el.querySelectorAll('.acct-chip').forEach(chip=>{
    const name=chip.getAttribute('data-acct');
    chip.querySelectorAll('button[data-act]').forEach(btn=>{
      btn.addEventListener('click',()=>{ btn.getAttribute('data-act')==='rename'?openAccountEditor(name):deleteAccountUI(name); });
    });
  });
  document.getElementById('acctAdd').addEventListener('click',()=>openAccountEditor(null));
  const all=document.getElementById('acctListAll');
  if(all)all.innerHTML=accounts.map(a=>'<option value="'+esc(a)+'">').join('');
}
function openAccountEditor(oldName){
  const isNew=!oldName;
  openEditor({
    title:isNew?'新增账户':'编辑账户名称',
    sub:'账户用于区分不同券商的持仓，同一股票可属于多个账户',
    body:'<div class="field"><label>账户名称</label><input id="acctName" value="'+(oldName?esc(oldName):'')+'" placeholder="例如：华泰普通 / 中信融资"></div>'+
      (isNew?'':'<div class="sub" style="margin-top:10px">重命名会同步更新该账户下全部持仓，组合分析结果自动重算。</div>'),
    okText:isNew?'添加账户':'保存',
    onOk:()=>{
      const name=(document.getElementById('acctName').value||'').trim();
      if(!name){ toast('请输入账户名称','warn'); return false; }
      if(name===DEFAULT_ACCOUNT){ toast('默认账户为保留名称，不可新增或重命名为此名称','warn'); return false; }
      if(name!==oldName && accounts.includes(name)){ toast('账户名称已存在，请使用其他名称','warn'); return false; }
      let msg;
      if(isNew){ saveAccounts([...accounts,name]); msg='已新增账户：'+name; }
      else{
        const old=oldName; let n=0;
        holdings.forEach(h=>{ if(acctOf(h)===old){ h.account=name; n++; } });
        saveAccounts(accounts.map(a=>a===old?name:a));
        msg='账户已重命名：'+old+' → '+name+'，同步更新 '+n+' 条持仓';
      }
      commitPortfolio(); toast(msg); return true;
    }
  });
}
function deleteAccountUI(name){
  if(name===DEFAULT_ACCOUNT){ toast('默认账户不可删除','warn'); return; }
  const rows=holdings.filter(h=>acctOf(h)===name);
  const others=accounts.filter(a=>a!==name);
  openEditor({
    title:'删除账户', sub:'账户「'+name+'」下有 '+rows.length+' 条持仓，请选择处理方式',
    body:'<div class="field"><label>处理方式</label><select id="delMode">'+
           '<option value="move">转移到其他账户（保留持仓）</option>'+
           '<option value="drop">同时删除该账户下全部持仓</option></select></div>'+
         '<div class="field" id="delTargetWrap" style="margin-top:10px"><label>接收账户</label><select id="delTarget">'+
           others.map(a=>'<option value="'+esc(a)+'">'+esc(a)+'</option>').join('')+'</select></div>',
    okText:'确认删除',
    onOk:()=>{
      const mode=document.getElementById('delMode').value;
      let msg;
      if(mode==='drop'){
        holdings=holdings.filter(h=>acctOf(h)!==name);
        saveAccounts(accounts.filter(a=>a!==name));
        msg='已删除账户「'+name+'」及其 '+rows.length+' 条持仓';
      }else{
        const target=(document.getElementById('delTarget').value||'').trim();
        if(!target){ toast('请选择接收账户','warn'); return false; }
        const toMove=holdings.filter(h=>acctOf(h)===name);
        holdings=holdings.filter(h=>acctOf(h)!==name);
        let merged=0;
        toMove.forEach(h=>{
          const existed=holdings.some(x=>acctOf(x)===target && x.code===h.code);
          if(existed)merged++;
          mergeInto(target,h.code,Number(h.shares)||0,Number(h.cost)||0,h.name);
        });
        saveAccounts(accounts.filter(a=>a!==name));
        msg='账户「'+name+'」已删除，'+toMove.length+' 条持仓转入「'+target+'」'+(merged?'（合并同股 '+merged+' 条）':'');
      }
      commitPortfolio(); toast(msg); return true;
    }
  });
  const mode=document.getElementById('delMode'), wrap=document.getElementById('delTargetWrap');
  mode.addEventListener('change',()=>{ wrap.style.display=mode.value==='move'?'':'none'; });
}

/* ---- 股票与账户对应关系 ---- */
function mergeInto(target,code,shares,cost,name){
  const hit=holdings.find(h=>acctOf(h)===target && h.code===code);
  if(hit){
    const total=(Number(hit.shares)||0)+shares;
    hit.cost=total>0?((Number(hit.cost)||0)*(Number(hit.shares)||0)+cost*shares)/total:cost;
    hit.shares=total;
    if(!hit.name && name)hit.name=name;
  }else holdings.push({code,name:name||'',cost,shares,account:target});
}
function openTransfer(indices){
  const rows=indices.map(i=>holdings[i]).filter(Boolean);
  if(!rows.length)return;
  const multi=rows.length>1;
  const opts=accounts.map(a=>'<option value="'+esc(a)+'">'+esc(a)+'</option>').join('');
  const body='<div class="sub" style="margin-bottom:12px">'+rows.slice(0,6).map(h=>esc(acctOf(h))+' / '+esc(h.code)+(rows.length>6?' …':'')).join('、')+'</div>'+
    '<div class="field"><label>目标账户</label><select id="tfAccount">'+opts+'</select></div>'+
    (multi?'<div class="sub" style="margin-top:10px">批量转移按整行迁移；目标账户已有同一股票时自动合并股数并按加权平均成本计价。</div>':
      '<div class="field" style="margin-top:10px"><label>转移股数</label><input id="tfShares" type="number" step="1" min="1" value="'+Number(rows[0].shares||0)+'"></div>'+
      '<div class="sub" style="margin-top:8px">填 0 表示全部转移；部分转移按原成本拆分，原账户保留剩余股数。</div>');
  openEditor({
    title:multi?'批量调整股票所属账户':'调整股票所属账户',
    sub:'同一股票可在不同账户分别持有，也可以拆分股数',
    body, okText:'转移',
    onOk:()=>{
      const target=(document.getElementById('tfAccount').value||'').trim();
      if(!target){ toast('请选择目标账户','warn'); return false; }
      let moved=0, merged=0, skipped=0;
      if(multi){
        [...rows].forEach(h=>{
          if(acctOf(h)===target){ skipped++; return; }
          const idx=holdings.indexOf(h); if(idx<0)return;
          const sh=Number(h.shares)||0, cost=Number(h.cost)||0;
          if(holdings.some(x=>x!==h && acctOf(x)===target && x.code===h.code))merged++;
          holdings.splice(idx,1); mergeInto(target,h.code,sh,cost,h.name); moved++;
        });
      }else{
        const h=rows[0], cur=Number(h.shares)||0;
        let n=parseFloat(document.getElementById('tfShares').value);
        if(!n)n=cur;
        if(!(n>0)||n>cur){ toast('转移股数须大于 0 且不超过当前股数','warn'); return false; }
        if(acctOf(h)===target){ toast('该持仓已在目标账户，请选择其他账户','warn'); return false; }
        if(holdings.some(x=>x!==h && acctOf(x)===target && x.code===h.code))merged++;
        const cost=Number(h.cost)||0;
        if(n>=cur){ const idx=holdings.indexOf(h); if(idx>=0)holdings.splice(idx,1); }
        else h.shares=cur-n;
        mergeInto(target,h.code,n,cost,h.name); moved++;
      }
      if(!moved){ toast('所选持仓已全部在目标账户，无需调整','warn'); return false; }
      commitPortfolio();
      toast('已转移 '+moved+' 条'+(merged?'，合并同股 '+merged+' 条':'')+(skipped?'，跳过已在目标账户的 '+skipped+' 条':''));
      return true;
    }
  });
}
function openTransferSelected(){
  const idx=holdings.map((h,i)=>holdSelected.has(holdKey(acctOf(h),h.code))?i:-1).filter(i=>i>=0);
  if(!idx.length){ toast('请先勾选要调整的持仓','warn'); return; }
  openTransfer(idx);
}

function renderHoldings(){
  const el=document.getElementById('holdingsList');
  if(!holdings.length){ el.innerHTML='<div class="empty" style="padding:20px">暂无持仓，请在下方添加或从客户端导入自选股</div>'; return; }
  const accts=[...new Set(holdings.map(acctOf))];
  el.innerHTML='<div class="acct-tools"><label><input type="checkbox" id="holdAll"> 全选</label><span class="sub">已选 <b id="holdSelCount">'+holdSelected.size+'</b> 行</span>'+
      '<button class="btn-ghost" onclick="openTransferSelected()">批量调整账户</button></div>'+
    '<table><thead><tr><th style="width:30px"></th><th>账户</th><th>代码</th><th>名称</th><th>成本价</th><th>股数</th><th>市值占比</th><th></th></tr></thead><tbody>'+
    holdings.map((h,i)=>{ const k=holdKey(acctOf(h),h.code); return `<tr>
      <td><input type="checkbox" class="hold-chk" data-key="${esc(k)}" ${holdSelected.has(k)?'checked':''}></td>
      <td><input list="acctList" value="${esc(acctOf(h))}" style="width:105px;padding:5px 7px" onchange="editHolding(${i},'account',this.value)"></td>
      <td class="code">${h.code}</td><td><a href="javascript:void(0)" class="name-link" onclick="openStock('${esc(h.code)}')" title="查看个股分析">${esc(h.name||h.code)}</a></td>
      <td><input type="number" step="any" value="${h.cost}" style="width:85px;padding:5px 7px" onchange="editHolding(${i},'cost',this.value)"></td>
      <td><input type="number" step="1" value="${h.shares}" style="width:75px;padding:5px 7px" onchange="editHolding(${i},'shares',this.value)"></td>
      <td class="code" id="hw${i}">—</td>
      <td class="row-acts"><button class="btn-ghost" onclick="openTransfer([${i}])" title="调整该股票所属账户或拆分股数">转移</button><button class="btn-danger" onclick="removeHolding(${i})">删除</button></td></tr>`; }).join('')+
    '</tbody></table><datalist id="acctList">'+accts.map(a=>`<option value="${esc(a)}">`).join('')+'</datalist>';
  el.querySelectorAll('.hold-chk').forEach(chk=>{
    chk.addEventListener('change',()=>{
      if(chk.checked)holdSelected.add(chk.getAttribute('data-key')); else holdSelected.delete(chk.getAttribute('data-key'));
      const c=document.getElementById('holdSelCount'); if(c)c.textContent=holdSelected.size;
    });
  });
  const all=document.getElementById('holdAll');
  if(all){ all.checked=holdSelected.size===holdings.length; all.addEventListener('change',()=>{
    holdSelected=new Set(all.checked?holdings.map(h=>holdKey(acctOf(h),h.code)):[]); renderHoldings();
  }); }
}
function removeHolding(i){ if(!holdings[i])return; holdings.splice(i,1); commitPortfolio(); }
function editHolding(i,key,val){
  if(!holdings[i])return;
  const old=acctOf(holdings[i]);
  holdings[i][key]=(key==='account')?(String(val||'').trim()||DEFAULT_ACCOUNT):(parseFloat(val)||0);
  if(key==='account' && old!==acctOf(holdings[i])){
    const a=acctOf(holdings[i]); if(!accounts.includes(a))accounts.push(a);
  }
  commitPortfolio();
}

bindSearch('holdCode','holdResults',(it)=>{ document.getElementById('holdCost').focus(); });
document.getElementById('holdAdd').addEventListener('click',()=>{
  const code=(document.getElementById('holdCode').value||'').trim();
  const cost=parseFloat(document.getElementById('holdCost').value);
  const shares=parseInt(document.getElementById('holdShares').value);
  if(!code || !cost || !shares){ toast('请填写代码、成本价和股数','warn'); return; }
  const account=(document.getElementById('holdAccount').value||'').trim()||DEFAULT_ACCOUNT;
  holdings.push({code, cost, shares, name:'', account});
  commitPortfolio();
  document.getElementById('holdCode').value=''; document.getElementById('holdCost').value=''; document.getElementById('holdShares').value='';
  // 异步补充名称（同一代码分属多账户时按账户定位，避免改错行）
  api('/api/search?q='+encodeURIComponent(code)).then(list=>{
    const hit=list.find(x=>x.code===code);
    if(hit){ const h=holdings.find(x=>x.code===code && acctOf(x)===account); if(h){h.name=hit.name;commitPortfolio();} }
  }).catch(()=>{});
});

document.getElementById('portAnalyze').addEventListener('click',async()=>{
  if(!holdings.length){ toast('请先添加持仓','warn'); return; }
  const days=document.getElementById('portDays').value;
  const btn=document.getElementById('portAnalyze'); btn.disabled=true; btn.textContent='分析中…';
  try{
    _lastPortfolioReq={holdings,days};
    const d=await api('/api/portfolio',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(_lastPortfolioReq)});
    renderPortfolio(d);
  }catch(e){ toast('分析失败：'+e.message,'error'); }
  finally{ btn.disabled=false; btn.textContent='分析组合'; }
});

function renderPortfolio(d){
  document.getElementById('portEmpty').style.display='none';
  document.getElementById('portContent').style.display='block';
  const s=d.summary;
  const metrics=[
    ['持仓市值','¥'+fmt(s.total_value),''],
    ['累计盈亏',(s.total_pnl>=0?'+':'')+'¥'+fmt(s.total_pnl),pctCls(s.total_pnl)],
    ['累计盈亏%',pct(s.total_pnl_pct),pctCls(s.total_pnl_pct)],
    ['区间收益',pct(s.interval_return),pctCls(s.interval_return)],
    ['区间年化',pct(s.annual_return),pctCls(s.annual_return)],
    ['年化波动',fmt(s.annual_vol,2)+'%',''],
    ['最大回撤',fmt(s.max_drawdown,2)+'%','down'],
    ['夏普比率',fmt(s.sharpe,3),''],
  ];
  document.getElementById('portMetrics').innerHTML=metrics.map(m=>`<div class="metric"><div class="k">${m[0]}</div><div class="v ${m[2]}">${m[1]}</div></div>`).join('');
  const conc=d.concentration||{};
  const risk=[
    ['Beta',fmt(s.beta,3),''],['年化 Alpha',pct(s.alpha_annual),pctCls(s.alpha_annual)],
    ['跟踪误差',fmt(s.tracking_error,2)+'%',''],['信息比率',fmt(s.information_ratio,3),''],
    ['Sortino',fmt(s.sortino,3),''],['VaR 95% 单日',fmt(s.var95_daily,2)+'%','down'],
    ['CVaR 95% 单日',fmt(s.cvar95_daily,2)+'%','down'],['日胜率',fmt(s.win_rate,1)+'%',''],
    ['HHI 集中度',fmt(conc.hhi,3),''],['有效持仓数',fmt(conc.effective_n,1),''],
    ['Top1 权重',fmt(conc.top1_pct,2)+'%',''],['最差单日',pct(s.worst_day),'down'],
  ];
  document.getElementById('portRisk').innerHTML=risk.map(m=>`<div class="metric"><div class="k">${m[0]}</div><div class="v ${m[2]}">${m[1]}</div></div>`).join('');
  const rn=document.getElementById('riskNote');
  if(rn) rn.textContent=(d.risk_note||'')+(s.start_date?` 样本区间 ${s.start_date} ~ ${s.end_date}，共 ${s.observations||'—'} 个交易日。`:'');
  // 净值口径说明 + 组合与指数的区间表现对比
  const noteEl=document.getElementById('curveNote');
  if(noteEl){
    let note=d.note||'';
    if(s.interval_return!=null && s.benchmark_return!=null){
      note+=` 本区间组合 ${pct(s.interval_return)}，${s.benchmark_name||'基准指数'} ${pct(s.benchmark_return)}。`;
    }
    noteEl.textContent=note;
  }

  // 代码-名称核对警示：填错代码时明确提示（如 688525佰维存储 vs 688825长鑫科技）
  const oldWarn=document.getElementById('codeWarnBox'); if(oldWarn) oldWarn.remove();
  if(noteEl && d.warnings && d.warnings.length){
    const warnBox=document.createElement('div');
    warnBox.id='codeWarnBox';
    warnBox.style.cssText='margin-top:8px;padding:8px 12px;background:#fff7ed;border:1px solid #fed7aa;border-radius:6px;color:#9a3412;font-size:12px;line-height:1.7';
    warnBox.innerHTML='⚠️ <b>请核对持仓代码</b><br/>'+d.warnings.map(w=>'· '+w).join('<br/>');
    noteEl.parentNode.insertBefore(warnBox,noteEl.nextSibling);
  }
  const acctCard=document.getElementById('acctCard');
  if(acctCard){
    if((d.accounts||[]).length){
      acctCard.style.display='block';
      document.getElementById('portAccounts').innerHTML='<table><thead><tr><th>账户</th><th>持仓数</th><th>市值</th><th>组合权重</th><th>成本</th><th>累计盈亏</th><th>盈亏%</th><th>区间收益</th><th>净值口径</th></tr></thead><tbody>'+
        d.accounts.map(a=>`<tr><td><b>${esc(a.name)}</b></td><td>${a.position_count}</td><td>${fmt(a.total_value)}</td><td>${fmt(a.weight_pct,2)}%</td><td>${a.total_cost==null?'—':fmt(a.total_cost)}</td><td class="${pctCls(a.total_pnl)}">${a.total_pnl==null?'—':(a.total_pnl>=0?'+':'')+fmt(a.total_pnl)}</td><td class="${pctCls(a.total_pnl_pct)}">${pct(a.total_pnl_pct)}</td><td class="${pctCls(a.interval_return)}">${pct(a.interval_return)}</td><td>${a.nav_base==='cost'?'成本':'区间起点'}</td></tr>`).join('')+
        '</tbody></table>';
    } else acctCard.style.display='none';
  }
  const ov=d.overlap||{};
  const ovCard=document.getElementById('overlapCard');
  if(ovCard){
    if(ov.position_count){
      ovCard.style.display='block';
      document.getElementById('portOverlap').innerHTML=
        `<div class="sub" style="margin-bottom:8px">重复持有 <b>${ov.position_count}</b> 只，合计市值 <b>¥${fmt(ov.total_value)}</b>（占组合 <b>${fmt(ov.weight_pct,2)}%</b>）；HHI ${fmt(conc.hhi,3)} · 有效持仓 ${fmt(conc.effective_n,1)} 只 · Top3 ${fmt(conc.top3_pct,1)}%</div>`+
        '<table><thead><tr><th>代码</th><th>名称</th><th>涉及账户</th><th>合并股数</th><th>合并市值</th><th>组合权重</th></tr></thead><tbody>'+
        ov.items.map(x=>`<tr><td class="code">${x.code}</td><td><a href="javascript:void(0)" class="name-link" onclick="openStock('${esc(x.code)}')" title="查看个股分析">${esc(x.name)}</a></td><td>${x.accounts.map(a=>esc(a.account)+' '+a.shares+'股').join(' / ')}</td><td>${fmt(x.total_shares)}</td><td>${fmt(x.total_value)}</td><td>${fmt(x.weight_pct,2)}%</td></tr>`).join('')+
        '</tbody></table>';
    } else ovCard.style.display='none';
  }
  document.getElementById('portTable').innerHTML='<table><thead><tr><th>账户</th><th>代码</th><th>名称</th><th>成本</th><th>现价</th><th>股数</th><th>市值</th><th>组合权重</th><th>盈亏</th><th>盈亏%</th></tr></thead><tbody>'+
    d.details.map(h=>`<tr${h.name_mismatch?' style="background:#fff7ed"':''}><td>${esc(h.account)}</td><td class="code">${h.code}</td><td${h.name_mismatch?' style="color:#c2410c;font-weight:600"':''}><a href="javascript:void(0)" class="name-link" onclick="openStock('${esc(h.code)}')" title="查看个股分析">${esc(h.name)}</a>${h.name_mismatch?' ⚠':''}</td><td>${fmtPrice(h.cost,h.code)}</td><td>${fmtPrice(h.last,h.code)}</td><td>${h.shares}</td><td>${fmt(h.market_value)}</td><td>${fmt(h.weight_pct,2)}%</td><td class="${pctCls(h.pnl)}">${h.pnl==null?'—':(h.pnl>=0?'+':'')+fmt(h.pnl)}</td><td class="${pctCls(h.pnl_pct)}">${pct(h.pnl_pct)}</td></tr>`).join('')+
    '</tbody></table>';
  holdings.forEach((h,i)=>{
    const cell=document.getElementById('hw'+i);
    if(cell){ const row=(d.details||[]).find(x=>x.code===String(h.code).padStart(6,'0')&&x.account===acctOf(h)); cell.textContent=row&&row.weight_pct!=null?fmt(row.weight_pct,2)+'%':'—'; }
  });

  if(!equityChart) equityChart=echarts.init(document.getElementById('equityChart'));
  const curve=d.curve;
  equityChart.setOption({
    animation:false,
    legend:{data:['组合净值','基准指数'],top:4,textStyle:{color:'#6b7480',fontSize:11}},
    tooltip:{trigger:'axis',backgroundColor:'#fff',borderColor:'#e5e8ec',textStyle:{color:'#1f2733'}},
    grid:{left:50,right:16,top:36,bottom:28},
    xAxis:{type:'category',data:curve.map(x=>x.date),axisLabel:{color:'#9aa3ad',fontSize:10}},
    yAxis:{scale:true,splitLine:{lineStyle:{color:'#f0f2f5'}},axisLabel:{color:'#6b7480'}},
    series:[
      {name:'组合净值',type:'line',data:curve.map(x=>x.value),symbol:'none',smooth:true,lineStyle:{width:2,color:'#2563eb'},areaStyle:{color:'rgba(37,99,235,.08)'}},
      {name:'基准指数',type:'line',data:curve.map(x=>x.benchmark),symbol:'none',smooth:true,lineStyle:{width:1.5,color:'#9aa3ad',type:'dashed'}},
    ]
  },true);

  if(!corrChart) corrChart=echarts.init(document.getElementById('corrChart'));
  const c=d.correlation;
  const data=[];
  c.values.forEach((row,i)=>row.forEach((v,j)=>data.push([j,i,v])));
  corrChart.setOption({
    animation:false,
    tooltip:{position:'top',backgroundColor:'#fff',borderColor:'#e5e8ec',textStyle:{color:'#1f2733'},formatter:p=>`${c.labels[p.data[1]]} × ${c.labels[p.data[0]]}<br/>相关系数 ${fmt(p.data[2],3)}`},
    grid:{left:80,right:20,top:16,bottom:70},
    xAxis:{type:'category',data:c.labels,splitArea:{show:true},axisLabel:{color:'#6b7480',fontSize:10,rotate:30}},
    yAxis:{type:'category',data:c.labels,splitArea:{show:true},axisLabel:{color:'#6b7480',fontSize:10}},
    visualMap:{min:-1,max:1,calculable:false,orient:'horizontal',left:'center',bottom:0,inRange:{color:['#2563eb','#f5f6f8','#d64545']},textStyle:{fontSize:10,color:'#6b7480'}},
    series:[{type:'heatmap',data:data,label:{show:true,fontSize:10,formatter:p=>p.data[2].toFixed(2)},itemStyle:{borderColor:'#fff',borderWidth:1}}]
  },true);

  const ac=document.getElementById('acctCorrCard');
  if(ac){
    if(d.accounts_corr && (d.accounts_corr.labels||[]).length>1){
      ac.style.display='block';
      if(!accountsCorrChart) accountsCorrChart=echarts.init(document.getElementById('accountsCorrChart'));
      const cc=d.accounts_corr, cd=[];
      cc.values.forEach((row,i)=>row.forEach((v,j)=>cd.push([j,i,v])));
      accountsCorrChart.setOption({
        animation:false,
        tooltip:{position:'top',backgroundColor:'#fff',borderColor:'#e5e8ec',textStyle:{color:'#1f2733'},formatter:p=>`${cc.labels[p.data[1]]} × ${cc.labels[p.data[0]]}<br/>相关系数 ${fmt(p.data[2],3)}`},
        grid:{left:100,right:20,top:16,bottom:70},
        xAxis:{type:'category',data:cc.labels,splitArea:{show:true},axisLabel:{color:'#6b7480',fontSize:10,rotate:20}},
        yAxis:{type:'category',data:cc.labels,splitArea:{show:true},axisLabel:{color:'#6b7480',fontSize:10}},
        visualMap:{min:-1,max:1,calculable:false,orient:'horizontal',left:'center',bottom:0,inRange:{color:['#2563eb','#f5f6f8','#d64545']},textStyle:{fontSize:10,color:'#6b7480'}},
        series:[{type:'heatmap',data:cd,label:{show:true,fontSize:11,formatter:p=>p.data[2].toFixed(2)},itemStyle:{borderColor:'#fff',borderWidth:1}}]
      },true);
    } else ac.style.display='none';
  }
}
renderHoldings();
renderAccountPanel();
portfolioReady = loadPortfolio();
document.getElementById('acctClose').addEventListener('click',closeEditor);
document.getElementById('acctCancel').addEventListener('click',closeEditor);
document.getElementById('acctOk').addEventListener('click',submitEditor);
document.getElementById('acctMask').addEventListener('click',e=>{ if(e.target.id==='acctMask')closeEditor(); });
document.addEventListener('keydown',e=>{ if(e.key==='Escape' && document.getElementById('acctMask').classList.contains('show'))closeEditor(); });

/* ---------- 动态读取客户端自选股 ---------- */
let watchSelected = new Set();
document.getElementById('importWatchlist').addEventListener('click',async()=>{
  const box=document.getElementById('watchlistBox');
  const btn=document.getElementById('importWatchlist'); btn.disabled=true; btn.textContent='读取中…';
  try{
    const d=await api('/api/watchlist');
    watchlistCache=d.items;
    dataVersion=d.version;
    renderWatchlist(d.items);
    box.style.display='block';
  }catch(e){ toast('读取自选股失败：'+e.message,'error'); }
  finally{ btn.disabled=false; btn.textContent='从客户端导入自选股'; }
});
function renderWatchlist(items){
  const el=document.getElementById('watchlistItems');
  document.getElementById('watchCount').textContent=`（${items.length} 只）`;
  if(!items.length){ el.innerHTML='<div class="empty" style="padding:16px">客户端自选股为空</div>'; return; }
  watchSelected=new Set(items.map(x=>x.code));
  el.innerHTML='<table><thead><tr><th style="width:32px"></th><th>代码</th><th>名称</th><th>市场</th></tr></thead><tbody>'+
    items.map(x=>`<tr><td><input type="checkbox" class="watch-chk" value="${x.code}" checked></td><td class="code">${x.code}</td><td>${esc(x.name)}</td><td>${esc(x.market.toUpperCase())}</td></tr>`).join('')+
    '</tbody></table>';
  el.querySelectorAll('.watch-chk').forEach(chk=>{
    chk.addEventListener('change',()=>{ if(chk.checked)watchSelected.add(chk.value);else watchSelected.delete(chk.value); });
  });
}
document.getElementById('importSelected').addEventListener('click',()=>{
  if(!watchlistCache||!watchSelected.size){ toast('请先选择要导入的自选股','warn'); return; }
  let added=0;
  watchlistCache.forEach(x=>{
    if(watchSelected.has(x.code) && !holdings.some(h=>h.code===x.code)){
      holdings.push({code:x.code, name:x.name, cost:0, shares:0, account:DEFAULT_ACCOUNT}); added++;
    }
  });
  saveHoldings(); renderHoldings();
  toast(`已导入 ${added} 只自选股。成本价和股数为 0，请编辑后分析。`);
});

/* ---------- 通用异步任务轮询 ---------- */
async function runJob({url, body, btn, progressEl, barEl, statusEl, resultEl, countEl, renderFn}){
  btn.disabled=true; const orig=btn.textContent; btn.textContent='扫描中…';
  progressEl.classList.add('show'); barEl.style.width='0%';
  statusEl.textContent='正在提交任务…';
  try{
    const d=await api(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const jid=d.job_id;
    statusEl.textContent='正在扫描全市场（约 1-2 分钟）…';
    let lastP=0;
    const timer=setInterval(async()=>{
      try{
        const j=await api('/api/job/'+jid);
        if(j.total>0){ const p=Math.round(j.progress/j.total*100); if(p!==lastP){barEl.style.width=p+'%';lastP=p;} }
        statusEl.textContent=`已扫描 ${j.progress} / ${j.total} …`;
        if(j.status==='done'){
          clearInterval(timer);
          statusEl.textContent=`扫描完成 · 共扫描 ${j.result.meta.scanned} 只股票`;
          barEl.style.width='100%';
          countEl.textContent=`${j.result.items.length} 条结果`;
          renderFn(j.result.items, j.labels);
          btn.disabled=false; btn.textContent=orig;
        }else if(j.status==='error'){
          clearInterval(timer); statusEl.textContent='扫描失败：'+j.error;
          btn.disabled=false; btn.textContent=orig;
        }
      }catch(e){ clearInterval(timer); statusEl.textContent='轮询失败：'+e.message; btn.disabled=false; btn.textContent=orig; }
    },1000);
  }catch(e){ statusEl.textContent='提交失败：'+e.message; btn.disabled=false; btn.textContent=orig; progressEl.classList.remove('show'); }
}

/* ---------- 全市场选股 ---------- */
document.querySelectorAll('#screenFilters .chip').forEach(c=>{
  c.addEventListener('click',()=>{ c.classList.toggle('on'); if(c.classList.contains('on'))screenFilters.add(c.dataset.f);else screenFilters.delete(c.dataset.f); });
});
document.getElementById('screenRun').addEventListener('click',function(){
  if(!screenFilters.size){ alert('请至少选择一个选股条件'); return; }
  runJob({
    url:'/api/screen', body:{filters:[...screenFilters],limit:parseInt(document.getElementById('screenLimit').value)}, btn:this,
    progressEl:document.getElementById('screenProgress'), barEl:document.getElementById('screenBar'),
    statusEl:document.getElementById('screenStatus'), resultEl:document.getElementById('screenResult'),
    countEl:document.getElementById('screenCount'), renderFn:renderScreenResult
  });
});
function renderScreenResult(items,labels){
  const el=document.getElementById('screenResult');
  if(!items.length){ el.innerHTML='<div class="empty">没有符合条件的股票</div>'; return; }
  el.innerHTML='<table><thead><tr><th>代码</th><th>名称</th><th>收盘价</th><th>涨跌幅</th><th>MA5</th><th>RSI6</th><th>日期</th></tr></thead><tbody>'+
    items.map(x=>`<tr><td class="code">${x.code}</td><td>${esc(x.name)}</td><td>${fmtPrice(x.close,x.code)}</td><td class="${pctCls(x.change)}">${pct(x.change)}</td><td>${fmtPrice(x.ma5,x.code)}</td><td>${fmt(x.rsi6,1)}</td><td>${x.date}</td></tr>`).join('')+
    '</tbody></table>';
}

/* ---------- 买卖信号 ---------- */
document.querySelectorAll('#signalTypes .chip').forEach(c=>{
  c.addEventListener('click',()=>{ c.classList.toggle('on'); if(c.classList.contains('on'))signalTypes.add(c.dataset.s);else signalTypes.delete(c.dataset.s); });
});
document.getElementById('signalRun').addEventListener('click',function(){
  if(!signalTypes.size){ alert('请至少选择一个信号类型'); return; }
  runJob({
    url:'/api/signals', body:{types:[...signalTypes],limit:parseInt(document.getElementById('signalLimit').value)}, btn:this,
    progressEl:document.getElementById('signalProgress'), barEl:document.getElementById('signalBar'),
    statusEl:document.getElementById('signalStatus'), resultEl:document.getElementById('signalResult'),
    countEl:document.getElementById('signalCount'), renderFn:renderSignalResult
  });
});
function renderSignalResult(items,labels){
  const el=document.getElementById('signalResult');
  if(!items.length){ el.innerHTML='<div class="empty">暂无信号</div>'; return; }
  const buySet=['macd_golden','ma_golden','kdj_oversold','boll_lower','breakout20','volume_burst'];
  el.innerHTML='<table><thead><tr><th>代码</th><th>名称</th><th>信号</th><th>方向</th><th>收盘价</th><th>涨跌幅</th><th>日期</th></tr></thead><tbody>'+
    items.map(x=>`<tr><td class="code">${x.code}</td><td>${x.name}</td><td title="${(x.detail||'').replace(/"/g,'&quot;')} ${x.caution?'|| '+x.caution.replace(/"/g,'&quot;'):''}"><span class="tag info">${x.label}</span></td><td><span class="tag ${buySet.includes(x.signal)?'buy':'sell'}">${buySet.includes(x.signal)?'偏多':'偏空'}</span></td><td>${fmtPrice(x.close,x.code)}</td><td class="${pctCls(x.change)}">${pct(x.change)}</td><td>${x.date}</td></tr>`).join('')+
    '</tbody></table>';
}

/* ---------- 个股舆情 ---------- */
document.querySelectorAll('#sentParts .chip').forEach(c=>{
  c.addEventListener('click',()=>{
    c.classList.toggle('on');
    if(c.classList.contains('on')) sentParts.add(c.dataset.p); else sentParts.delete(c.dataset.p);
    if(!sentParts.size){ c.classList.add('on'); sentParts.add(c.dataset.p); alert('至少保留一类数据来源'); }
  });
});
document.querySelectorAll('#sentFilter .chip').forEach(c=>{
  c.addEventListener('click',()=>{
    document.querySelectorAll('#sentFilter .chip').forEach(x=>x.classList.remove('on'));
    c.classList.add('on'); sentFilter=c.dataset.f;
    if(sentCurrent) renderSentList(sentCurrent);
  });
});
function sentLabel(label){ return label==='positive'?'偏多':(label==='negative'?'偏空':'中性'); }
function sentScoreColor(score){ return score>=60?'var(--up)':(score<=40?'var(--down)':'var(--muted)'); }

async function loadSentiment(refresh){
  // 复用当前个股分析的股票代码，无需单独搜索
  if(!currentStock || !currentStock.code){ return; }
  const code=currentStock.code;
  if(sentBusy && !refresh){ return; }  // 防重入：正在加载时跳过自动触发
  if(sentTimer){ clearInterval(sentTimer); sentTimer=null; }
  const btn=document.getElementById('sentRefresh');
  const prog=document.getElementById('sentProgress'), bar=document.getElementById('sentBar');
  const status=document.getElementById('sentStatus');
  sentBusy=true; btn.disabled=true;
  prog.classList.add('show'); bar.style.width='0%';
  document.getElementById('sentContent').style.display='none';
  status.textContent='正在提交舆情任务…';
  try{
    const d=await api('/api/sentiment',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({code,parts:[...sentParts],refresh:!!refresh})});
    const jid=d.job_id;
    let lastP=0;
    sentTimer=setInterval(async()=>{
      try{
        const j=await api('/api/job/'+jid);
        if(j.total>0){ const p=Math.round(j.progress/j.total*100); if(p!==lastP){bar.style.width=p+'%';lastP=p;} }
        status.textContent=(j.message||'处理中…')+(j.total?` (${j.progress}/${j.total})`:'');
        if(j.status==='done'){
          clearInterval(sentTimer); sentTimer=null; bar.style.width='100%';
          status.textContent='获取完成 · '+j.result.as_of;
          sentCurrent=j.result; renderSentiment(j.result);
          sentBusy=false; btn.disabled=false; prog.classList.remove('show');
        }else if(j.status==='error'){
          clearInterval(sentTimer); sentTimer=null; status.textContent='获取失败：'+j.error;
          sentBusy=false; btn.disabled=false; prog.classList.remove('show');
        }
      }catch(e){
        clearInterval(sentTimer); sentTimer=null; status.textContent='轮询失败：'+e.message;
        sentBusy=false; btn.disabled=false; prog.classList.remove('show');
      }
    },1200);
  }catch(e){
    status.textContent='提交失败：'+e.message;
    sentBusy=false; btn.disabled=false; prog.classList.remove('show');
  }
}
document.getElementById('sentRefresh').addEventListener('click',()=>loadSentiment(true));
document.getElementById('modelsRefresh').addEventListener('click',()=>{
  const el=document.getElementById('stockSearch');
  const code=el&&el.value.trim();
  if(code){ loadModels(code.replace(/\D/g,'').slice(-6), isFund(code)); }
});
document.getElementById('predictRefresh').addEventListener('click',()=>{
  const el=document.getElementById('stockSearch');
  const code=el&&el.value.trim();
  if(code){ loadPredict(code.replace(/\D/g,'').slice(-6)); }
});
document.getElementById('finRefresh').addEventListener('click',()=>{
  const el=document.getElementById('stockSearch');
  const code=el&&el.value.trim();
  if(code){ loadFinReport(code.replace(/\D/g,'').slice(-6), isFund(code), true); }
});
window.addEventListener('resize',()=>{ if(sentDistChart)sentDistChart.resize(); if(sentTrendChart)sentTrendChart.resize(); });

function renderSentiment(d){
  document.getElementById('sentContent').style.display='block';
  const staleParts=[];
  if(d.by_source) Object.keys(d.by_source).forEach(k=>{ if(d.by_source[k].stale) staleParts.push(d.by_source[k].label); });
  document.getElementById('sentMeta').textContent=`${d.name||d.code} ${d.code} · 分析时间 ${d.as_of} · 共 ${d.total} 条`+(staleParts.length?` · ${staleParts.join('/')}使用离线缓存`:'');
  const chip=document.getElementById('sentOverall');
  chip.textContent='综合情绪：'+sentLabel(d.overall_label)+(d.overall_score!=null?' '+d.overall_score:'');
  chip.className='chip '+(d.overall_label==='positive'?'up':(d.overall_label==='negative'?'down':''));

  const c=d.counts||{};
  const metrics=[
    ['综合情绪分', d.overall_score!=null?d.overall_score:'—', d.overall_score!=null?sentScoreColor(d.overall_score):''],
    ['正面条目', c.positive||0, 'up'],
    ['中性条目', c.neutral||0, ''],
    ['负面条目', c.negative||0, 'down'],
  ];
  document.getElementById('sentMetrics').innerHTML=metrics.map(m=>
    `<div class="metric"><div class="k">${m[0]}</div><div class="v ${m[2]}">${m[1]}</div></div>`).join('');

  renderSentDist(d);
  renderSentTrend(d);

  const mini=(items,color)=>{
    if(!items||!items.length) return '<div class="empty" style="padding:16px">无</div>';
    return items.map(r=>`<div class="sent-mini">
      <span class="tag ${r.label}">${r.score}</span>
      ${r.url?`<a href="${r.url}" target="_blank" rel="noopener">${r.title||'（无标题）'}</a>`:(r.title||'（无标题）')}
      <div style="color:var(--hint);font-size:11px">${r.source} · ${r.date||''}${r.org?' · '+r.org:''}${r.rating?' · 评级:'+r.rating:''}</div>
    </div>`).join('');
  };
  document.getElementById('sentTopPos').innerHTML=mini(d.top_positive,'up');
  document.getElementById('sentTopNeg').innerHTML=mini(d.top_negative,'down');
  renderSentHolderEvents(d);
  renderSentList(d);
}

// 股东事件 × 舆情关联：把"谁新进/增持"与相关报道、股东画像放在一起解读
function renderSentHolderEvents(d){
  const box=document.getElementById('sentHolderEvents');
  const meta=document.getElementById('sentHolderMeta');
  const link=d.holder_events||{};
  const evs=link.events||[];
  const cov=link.coverage||{};
  if(!evs.length){
    box.innerHTML='<div class="sub" style="font-size:11px">'+esc(link.note||'本期无重点股东变动事件')+'</div>';
    meta.textContent='';
    return;
  }
  meta.textContent='共 '+evs.length+' 项 · '+(cov.with_direct_coverage||0)+' 项有直接报道 · 报告期 '+(link.report_date||'—');
  const toneColor=e.media_tone==='positive'?'var(--up)':(e.media_tone==='negative'?'var(--down)':'var(--muted)');
  box.innerHTML=evs.map(e=>`<div style="padding:9px 0;border-bottom:1px solid var(--border)">
    <div class="row" style="gap:7px;flex-wrap:wrap;align-items:center">
      <b style="font-size:12px">${esc(e.name)}</b>
      ${(e.tags||[]).map(t=>`<span class="tag ${(t==='新进'||t==='跨界持股')?'buy':'info'}">${esc(t)}</span>`).join('')}
      <span class="sub" style="font-size:11px">持股 ${fmt(e.ratio,2)}% · ${esc(e.change||'—')}</span>
    </div>
    <div style="font-size:12px;margin-top:5px;color:var(--muted)">${esc(e.reading||'')}</div>
    <div style="font-size:12px;margin-top:5px;color:${toneColor}">📰 ${esc(e.media_summary||'')}</div>
    ${(e.related_items||[]).length?`<div style="margin-top:5px">${e.related_items.map(r=>`<div style="font-size:11px;padding:2px 0">· ${r.url?`<a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.title)}</a>`:esc(r.title)} <span class="sub">${esc(r.source)} ${esc(r.date)}</span></div>`).join('')}</div>`:''}
    ${(e.context_items||[]).length?`<details style="margin-top:5px"><summary class="sub" style="font-size:11px;cursor:pointer">背景资讯 ${e.context_count} 条（未点名该股东）</summary>${e.context_items.map(r=>`<div style="font-size:11px;padding:2px 0">· ${esc(r.title)} <span class="sub">${esc(r.date)} · ${esc(r.relation||'')}</span></div>`).join('')}</details>`:''}
  </div>`).join('')+`<div class="sub" style="font-size:10px;margin-top:8px">${esc(link.note||'')}</div>`;
}

function renderSentDist(d){
  if(!sentDistChart) sentDistChart=echarts.init(document.getElementById('sentDistChart'));
  const kinds=Object.keys(d.by_source||{});
  const labelMap={news:'新闻',notice:'公告',research:'研报'};
  const pos=kinds.map(k=>d.by_source[k].counts.positive);
  const neu=kinds.map(k=>d.by_source[k].counts.neutral);
  const neg=kinds.map(k=>d.by_source[k].counts.negative);
  sentDistChart.setOption({
    animation:false,
    legend:{data:['正面','中性','负面'],top:2,textStyle:{color:'#6b7480',fontSize:11}},
    tooltip:{trigger:'axis',axisPointer:{type:'shadow'},backgroundColor:'#fff',borderColor:'#e5e8ec',textStyle:{color:'#1f2733'}},
    grid:{left:40,right:16,top:34,bottom:24},
    xAxis:{type:'category',data:kinds.map(k=>labelMap[k]||k),axisLabel:{color:'#6b7480'}},
    yAxis:{type:'value',splitLine:{lineStyle:{color:'#f0f2f5'}},axisLabel:{color:'#9aa3ad'}},
    series:[
      {name:'正面',type:'bar',stack:'t',data:pos,itemStyle:{color:'#d64545'}},
      {name:'中性',type:'bar',stack:'t',data:neu,itemStyle:{color:'#b7c0cc'}},
      {name:'负面',type:'bar',stack:'t',data:neg,itemStyle:{color:'#1a9c6b'}},
    ]
  },true);
}

function renderSentTrend(d){
  if(!sentTrendChart) sentTrendChart=echarts.init(document.getElementById('sentTrendChart'));
  const t=d.trend||[];
  sentTrendChart.setOption({
    animation:false,
    tooltip:{trigger:'axis',backgroundColor:'#fff',borderColor:'#e5e8ec',textStyle:{color:'#1f2733'}},
    grid:{left:40,right:16,top:20,bottom:40},
    xAxis:{type:'category',data:t.map(x=>x.date),axisLabel:{color:'#9aa3ad',fontSize:10,rotate:30}},
    yAxis:{type:'value',min:0,max:100,splitLine:{lineStyle:{color:'#f0f2f5'}},axisLabel:{color:'#9aa3ad'}},
    series:[{
      type:'line',data:t.map(x=>x.score),symbol:'size',symbolSize:6,smooth:true,
      lineStyle:{width:2,color:'#2563eb'},itemStyle:{color:'#2563eb'},
      markLine:{silent:true,symbol:'none',label:{show:false},data:[{yAxis:50}],lineStyle:{color:'#b7c0cc',type:'dashed'}},
    }]
  },true);
}

// 舆情明细列表（后端 result.full_items 提供全量逐条数据）
function renderSentList(d){
  const el=document.getElementById('sentList');
  const items=(d.full_items||[]).filter(x=> sentFilter==='all' || x.label===sentFilter);
  if(!items.length){
    el.innerHTML='<div class="empty" style="padding:24px">该分类下暂无条目</div>'; return;
  }
  el.innerHTML='<table><thead><tr><th>日期</th><th>来源</th><th>标题</th><th>情绪</th><th>评分</th></tr></thead><tbody>'+
    items.map(r=>`<tr>
      <td style="color:var(--muted);font-size:12px">${r.date||''}</td>
      <td>${r.source}${r.rating?'<br/><span class="code" style="font-size:10px">'+r.rating+'</span>':''}</td>
      <td style="white-space:normal;max-width:560px">${r.url?`<a href="${r.url}" target="_blank" rel="noopener" style="color:var(--text);text-decoration:none">${r.title||'（无标题）'}</a>`:(r.title||'（无标题）')}<div style="color:var(--hint);font-size:11px">${r.org||''}</div></td>
      <td><span class="tag ${r.label}">${sentLabel(r.label)}</span></td>
      <td><span style="color:${sentScoreColor(r.score)};font-weight:600">${r.score}</span><span class="score-bar"><i style="width:${r.score}%;background:${sentScoreColor(r.score)}"></i></span></td>
    </tr>`).join('')+'</tbody></table>';
}

/* ---------- 个股上下游产业链穿透 ---------- */
document.getElementById('chainLoad').addEventListener('click',function(){
  if(!currentStock || !currentStock.code){ alert('请先分析一只股票'); return; }
  const btn=this, code=currentStock.code;
  if(chainCache[code]){ renderChain(chainCache[code]); return; }
  btn.disabled=true; const orig=btn.textContent; btn.textContent='加载中…';
  api('/api/chain/'+code).then(d=>{
    chainCache[code]=d; renderChain(d);
  }).catch(e=>{ alert('加载失败：'+e.message); })
  .finally(()=>{ btn.disabled=false; btn.textContent=orig; });
});

function srcTag(source){
  if(source==='REAL') return '<span class="src-tag real">真实</span>';
  if(source==='INDUSTRY_KB') return '<span class="src-tag kb">行业图谱</span>';
  return '<span class="src-tag infer">推断·需核实</span>';
}
function nodeCode(name){
  const m=String(name||'').match(/(\d{6})/); return m?m[1]:null;
}
function composerEl(){
  return document.getElementById('chainCompose');
}
function renderChain(d){
  document.getElementById('chainEmpty').style.display='none';
  document.getElementById('chainContent').style.display='block';
  if(d.is_fund){
    document.getElementById('chainGraph').innerHTML='<div class="empty" style="padding:30px">'+d.message+'</div>';
    document.getElementById('chainProfile').innerHTML='<div class="empty" style="padding:20px">—</div>';
    composerEl().innerHTML='<div class="empty" style="padding:20px">—</div>';
    document.getElementById('chainRelated').innerHTML='';
    document.getElementById('chainRelatedNote').textContent='';
    document.getElementById('chainContracts').innerHTML='';
    document.getElementById('chainHighlights').innerHTML='';
    document.getElementById('chainNotes').innerHTML='';
    document.getElementById('chainSources').innerHTML='';
    document.getElementById('chainKbLevel').textContent='';
    return;
  }
  const r=d.real||{}, inf=d.inference||{};
  // 公司业务档案（真实数据）
  const prof=[
    ['公司',r.company],['所属行业',r.industry],['上市日期',r.list_date],
    ['主营业务',r.main_business],['产品/业务',(r.products||[]).concat(r.product_types||[]).slice(0,6).join('、')],
    ['入选指数',(r.indexes||[]).slice(0,8).join('、')],
  ].filter(x=>x[1]);
  document.getElementById('chainProfile').innerHTML='<div class="chain-meta" style="font-size:12px">'+
    prof.map(([k,v],i)=>`<div style="padding:5px 0;${i?'border-top:1px solid var(--surface-2)':''}"><span style="color:var(--muted);display:inline-block;width:62px">${k}</span><span style="color:var(--text)">${v}</span></div>`).join('')+'</div>';
  // 主营构成（真实数据，横向条形）
  let composeHtml='';
  const groups=r.compose||{};
  for(const [g,items] of Object.entries(groups)){
    composeHtml+=`<div style="font-size:11px;color:var(--muted);margin:8px 0 5px;font-weight:600">${g}${r.compose_date?' · '+r.compose_date:''}</div>`;
    composeHtml+=items.slice(0,6).map(it=>`<div class="compose-row">
      <span class="cn" title="${it.name}">${it.name}</span>
      <span class="cbar"><i style="width:${Math.min(it.revenue_ratio||0,100)}%"></i></span>
      <span class="cv">${it.revenue_ratio!=null?fmt(it.revenue_ratio,1)+'%':'—'}${it.gross_margin!=null?' / 毛'+fmt(it.gross_margin,1)+'%':''}</span>
    </div>`).join('');
  }
  composerEl().innerHTML=composeHtml||'<div class="empty" style="padding:16px">暂无主营构成数据</div>';
  // 产业链关联公司（名单真实 / 方向规则映射）
  const rel=d.related||{};
  const relCols=[['上游供应商相关','up',rel.upstream||[]],['同链 · 同行业','mid',rel.peers||[]],['下游应用 · 客户相关','down',rel.downstream||[]]];
  let relHtml='';
  relCols.forEach(([title,cls,list])=>{
    relHtml+=`<div class="chain-col ${cls}"><div class="ch"><span>${title}</span><span style="font-weight:400;color:var(--hint)">${list.length}</span></div>`;
    if(!list.length){ relHtml+='<div style="font-size:11px;color:var(--hint);padding:8px 0">—</div>'; }
    list.forEach(c=>{
      relHtml+=`<div class="chain-node" data-code="${c.code}" title="${c.concept||''}（点击分析该股）">
        <div class="nm"><span>${c.name}</span><span class="gocode">${c.code}</span></div>
        <div class="role">同属：${c.concept||'—'}</div>
      </div>`;
    });
    relHtml+='</div>';
  });
  const relEl=document.getElementById('chainRelated');
  relEl.innerHTML=relHtml;
  document.getElementById('chainRelatedNote').textContent=rel.note||'';
  relEl.querySelectorAll('.chain-node[data-code]').forEach(el=>{
    el.addEventListener('click',()=>{
      const c=el.dataset.code;
      document.getElementById('stockSearch').value=c;
      loadStock();
      document.getElementById('page-stock').scrollIntoView({behavior:'smooth'});
    });
  });
  // 合同/中标/订单公告（真实披露）
  const ct=d.contracts||{};
  let ctHtml='';
  if(ct.items&&ct.items.length){
    ctHtml+=`<div style="font-size:11px;color:var(--muted);margin-bottom:6px">检出 <b style="color:var(--accent)">${ct.count}</b> 条（同期共 ${ct.total_announcements} 条公告，窗口 ${ct.window_days} 天）</div>`;
    ctHtml+=ct.items.map(it=>`<div class="policy-item"><div class="t"><a href="${it.url}" target="_blank" rel="noopener" style="color:var(--text);text-decoration:none">${esc(it.title)}</a></div><div class="m">${it.date} · 巨潮资讯网</div></div>`).join('');
  }else if(ct.total_announcements!=null){
    ctHtml+=`<div class="empty" style="padding:14px;font-size:12px">近 13 个月 ${ct.total_announcements} 条公告标题中未检出合同/中标/订单关键词。<br/>说明：多数订单不会单独发公告，可结合年报「重大合同」章节与调研纪要核实。</div>`;
  }else{
    ctHtml='<div class="empty" style="padding:14px;font-size:12px">公告数据加载失败，可稍后重试或点右上角重新加载。</div>';
  }
  document.getElementById('chainContracts').innerHTML=ctHtml;
  // 核心题材要点（真实）
  const hl=d.highlights||[];
  document.getElementById('chainHighlights').innerHTML=hl.length?
    hl.map(h=>`<div class="policy-item"><div class="t"><span class="tag info" style="margin-right:6px">${h.keyword||'要点'}</span>${h.content||''}</div></div>`).join('')
    :'<div class="empty" style="padding:14px">暂无核心题材数据</div>';
  // 推断注释
  const notes=inf.notes||[];
  document.getElementById('chainNotes').innerHTML=notes.map(n=>'<div class="chain-note">ℹ '+n+'</div>').join('');
  // 产业链图谱（上/中/下游三列）
  const nodes=inf.nodes||[];
  const stages=[['上游','up'],['中游','mid'],['下游','down']];
  const kbLabel={stock:'个股专属研究',industry:'行业通用图谱',basic:'基础环节定位'}[inf.kb_level]||'基础';
  document.getElementById('chainKbLevel').textContent='图谱来源：'+kbLabel;
  let graphHtml='';
  stages.forEach(([stage,cls])=>{
    const list=nodes.filter(n=>n.stage===stage);
    graphHtml+=`<div class="chain-col ${cls}"><div class="ch"><span>${stage}</span><span style="font-weight:400;color:var(--hint)">${list.length}</span></div>`;
    if(!list.length){ graphHtml+='<div style="font-size:11px;color:var(--hint);padding:8px 0">—</div>'; }
    list.forEach(n=>{
      const code=nodeCode(n.name);
      graphHtml+=`<div class="chain-node${n.id==='self'?' self':''}" ${code?'data-code="'+code+'" title="点击分析该股"':''}>
        <div class="nm"><span>${n.name}</span>${srcTag(n.source)}</div>
        <div class="role">${n.role||''}</div>
        ${n.detail?`<div class="detail">${n.detail}</div>`:''}
      </div>`;
    });
    graphHtml+='</div>';
  });
  const graphEl=document.getElementById('chainGraph');
  graphEl.innerHTML=graphHtml;
  // 节点点击 → 跳转分析该股（若有代码且本地有数据）
  graphEl.querySelectorAll('.chain-node[data-code]').forEach(el=>{
    el.addEventListener('click',()=>{
      const c=el.dataset.code;
      document.getElementById('stockSearch').value=c;
      loadStock();
      document.getElementById('page-stock').scrollIntoView({behavior:'smooth'});
    });
  });
  // 数据来源与免责
  const srcs=(inf.sources||[]);
  let srcHtml='<div class="sub" style="font-size:11px;line-height:1.8">';
  if(d.sources&&d.sources.length){
    srcHtml+='<b>真实数据来源：</b>'+d.sources.filter(s=>s.ok).map(s=>s.label+(s.stale?'(离线缓存)':'')).join(' · ')+'<br/>';
  }
  if(srcs.length){ srcHtml+='<b>推断研究来源：</b>'+srcs.map(s=>s.label+(s.type==='非一手·需核实'?'（需核实原文）':'')).join(' · ')+'<br/>'; }
  srcHtml+='<span style="color:var(--warn)">'+(inf.disclaimer||'')+'</span></div>';
  document.getElementById('chainSources').innerHTML=srcHtml;
}

// ===== 前十大股东 =====
function fmtShares(n){
  if(n===null||n===undefined) return '—';
  if(n>=1e8) return (n/1e8).toFixed(2)+'亿';
  if(n>=1e4) return (n/1e4).toFixed(2)+'万';
  return Number(n).toLocaleString('zh-CN');
}
function changeCell(x){
  // 增持红、减持绿（A股习惯）
  if(x.change===null||x.change===undefined){
    return '<span style="color:var(--muted)">'+(x.change_raw||'不变')+'</span>';
  }
  const up=x.change>0;
  const color=up?'var(--up)':'var(--down)';
  const sign=up?'+':'';
  return '<span style="color:'+color+';font-weight:600">'+sign+fmtShares(x.change)+'</span>'+
    (x.change_ratio?('<div class="sub" style="color:'+color+'">'+(x.change_ratio>0?'+':'')+fmt(x.change_ratio,2)+'%</div>'):'');
}
document.getElementById('holderLoad').addEventListener('click',async function(){
  if(!currentStock){ alert('请先选择一只股票'); return; }
  const code=currentStock.code;
  const btn=this; btn.disabled=true; btn.textContent='加载中…';
  document.getElementById('holderEmpty').style.display='none';
  try{
    let d=holderCache[code];
    if(!d){ d=await api('/api/holders/'+code); holderCache[code]=d; }
    renderHolders(d);
  }catch(e){
    document.getElementById('holderEmpty').style.display='block';
    document.getElementById('holderEmpty').innerHTML='<div class="empty">加载失败：'+e.message+'</div>';
  }finally{ btn.disabled=false; btn.textContent='刷新股东信息'; }
});
document.getElementById('holderTabs').addEventListener('click',function(e){
  const chip=e.target.closest('[data-h]'); if(!chip) return;
  holderTab=chip.dataset.h;
  this.querySelectorAll('[data-h]').forEach(c=>c.classList.toggle('on',c===chip));
  if(holderCache[currentStock&&currentStock.code]) renderHolders(holderCache[currentStock.code]);
});
function renderHolders(d){
  const box=document.getElementById('holderContent');
  if(d.is_fund){
    box.style.display='block';
    document.getElementById('holderMeta').textContent='';
    document.getElementById('holderMetrics').innerHTML='';
    document.getElementById('holderTable').innerHTML='<div class="empty" style="padding:30px">'+d.message+'</div>';
    document.getElementById('holderSources').innerHTML='';
    return;
  }
  box.style.display='block';
  const isFree=holderTab==='free';
  const list=isFree?(d.free_top||[]):(d.top||[]);
  const sum=(d.summary||{})[holderTab]||{};
  document.getElementById('holderMeta').innerHTML='报告期 <b>'+(d.report_date||'—')+'</b> · 共 '+list.length+' 位股东'+
    (d.sources&&d.sources[0]&&d.sources[0].stale?' · <span style="color:var(--warn)">离线缓存</span>':'');
  // 集中度指标卡
  const metrics=[
    {label:'CR5 集中度',value:sum.cr5!==null&&sum.cr5!==undefined?sum.cr5+'%':'—',sub:'前五大合计持股'},
    {label:'CR10 集中度',value:sum.cr10!==null&&sum.cr10!==undefined?sum.cr10+'%':'—',sub:'前十大合计持股'},
    {label:'增持 / 减持',value:(sum.increase||0)+' / '+(sum.decrease||0),sub:'本期变动家数'},
    {label:'股东性质',value:Object.keys(sum.nature_dist||{}).length||'—',sub:'类别数'}
  ];
  document.getElementById('holderMetrics').innerHTML=metrics.map(m=>
    '<div class="card" style="padding:14px"><div class="sub">'+m.label+'</div>'+
    '<div style="font-size:22px;font-weight:700;margin:4px 0">'+m.value+'</div>'+
    '<div class="sub" style="font-size:11px">'+m.sub+'</div></div>').join('');
  // 明细表
  if(!list.length){
    document.getElementById('holderTable').innerHTML='<div class="empty">该报告期暂无数据</div>';
  }else{
    const cols=isFree
      ? '<th>名次</th><th>股东名称</th><th>股东性质</th><th>持股数</th><th>占流通股</th><th>变动</th>'
      : '<th>名次</th><th>股东名称</th><th>股份类型</th><th>持股数</th><th>占总股本</th><th>变动</th>';
    document.getElementById('holderTable').innerHTML='<table><thead><tr>'+cols+'</tr></thead><tbody>'+
      list.map(x=>'<tr>'+
        '<td>'+(x.rank||'')+'</td>'+
        '<td><span class="look-link" data-look="'+esc(x.name||'')+'" title="穿透：主体、控制链与持股动机">'+esc(x.name||'—')+'<span class="lk">穿透</span></span></td>'+
        '<td><span class="tag neutral">'+(isFree?(x.nature||'—'):(x.share_type||'—'))+'</span></td>'+
        '<td>'+fmtShares(x.shares)+'</td>'+
        '<td style="font-weight:600">'+fmt(x.ratio,2)+'%</td>'+
        '<td>'+changeCell(x)+'</td>'+
      '</tr>').join('')+'</tbody></table>';
  }
  // 来源与免责
  const src=d.sources&&d.sources.find(s=>s.ok);
  document.getElementById('holderSources').innerHTML='<div class="sub" style="font-size:11px;line-height:1.8">'+
    '<b>数据来源：</b>'+(src?src.label:'—')+' · 报告期 '+d.report_date+'（披露存在滞后）<br/>'+
    '<span style="color:var(--warn)">'+(d.disclaimer||'')+'</span></div>';
}

// ===== 股东穿透：点名字看"它到底为什么持股" =====
const lookMask=document.getElementById('lookMask');
const lookBody=document.getElementById('lookBody');
let lookHolderName='';
function closeLookthrough(){ lookMask.classList.remove('show'); }
document.getElementById('lookClose').addEventListener('click',closeLookthrough);
lookMask.addEventListener('click',e=>{ if(e.target===lookMask) closeLookthrough(); });
document.addEventListener('keydown',e=>{ if(e.key==='Escape'&&lookMask.classList.contains('show')) closeLookthrough(); });
document.getElementById('holderTable').addEventListener('click',function(e){
  const el=e.target.closest('[data-look]');
  if(!el||!currentStock) return;
  const name=el.getAttribute('data-look');
  if(!name) return;
  openLookthrough(currentStock.code,name);
});
function openLookthrough(code,name){
  lookHolderName=name||'';
  lookMask.classList.add('show');
  document.getElementById('lookTitle').textContent=name;
  document.getElementById('lookSub').textContent=(currentStock&&currentStock.name?currentStock.name+' · ':'')+code+' · 主体与控制链穿透';
  lookBody.innerHTML='<div class="sub" style="padding:30px;text-align:center">正在整理公开证据与本地数据…</div>';
  api('/api/holder/lookthrough?code='+encodeURIComponent(code)+'&holder='+encodeURIComponent(name))
    .then(d=>renderLookthrough(d))
    .catch(e=>{ lookBody.innerHTML='<div class="chain-note">穿透失败：'+esc(e.message||e)+'</div>'; });
}
function renderLookthrough(d){
  if(!d||!d.ok){ lookBody.innerHTML='<div class="chain-note">'+esc((d&&d.error)||'暂无数据')+'</div>'; return; }
  const v=d.verdict||{}, ent=d.entity||{}, a=d.attraction||{};
  const lv=v.level==='inferred'?'buy':(v.level==='unverified'?'neutral':'info');
  let h='';
  // 核心结论
  h+='<div class="look-verdict"><div class="lb">持股动机结论 · '+esc(v.confidence||'')+'</div>'+
     '<div class="lv"><span class="tag '+lv+'">'+esc(v.label||'待核实')+'</span></div>'+
     '<div class="ld">'+esc(v.one_liner||'')+'</div></div>';
  // 主体穿透链
  h+='<div class="look-sec"><h4>主体穿透 · 谁在持股</h4><div class="look-chain">'+
     (d.chain||[]).map(n=>'<div class="node"><span class="dotc"></span><div>'+
       '<span class="nm">'+esc(n.name||'')+'</span><span class="rl">'+esc(n.role||'')+'</span>'+
       '<div class="nt">'+esc(n.note||'')+' · 来源：'+esc(n.source||'—')+'</div></div></div>').join('')+
     '</div><div class="sub" style="font-size:11px;margin-top:6px">上市主体：'+
       esc(ent.listed_name||'未匹配')+(ent.listed_code?'（'+esc(ent.listed_code)+'）':'')+
       ' · 关系：'+esc(ent.relation||'—')+' · 实控人：'+esc(ent.controller||'—')+
       ' · 识别置信度：'+esc(ent.confidence||'—')+'</div></div>';
  // 为什么持股
  h+='<div class="look-sec"><h4>为什么会买它 · 证据链</h4>'+
     (d.reasons||[]).map(r=>'<div class="look-reason"><div class="rt">'+
       '<span class="tag '+(r.tag==='已披露'?'buy':'neutral')+'">'+esc(r.tag||'推断')+'</span> '+esc(r.title||'')+
       '</div><div class="rd">'+esc(r.detail||'')+'</div><div class="rs">依据：'+esc(r.source||'—')+'</div></div>').join('')+'</div>';
  // 标的吸引力
  if(a.latest_year){
    h+='<div class="look-sec"><h4>标的吸引力 · 分红是核心线索</h4><div class="look-kv">'+
      '<div><div class="k">最近完整年度</div><div class="v">'+esc(a.latest_year)+' 年</div></div>'+
      '<div><div class="k">每股分红</div><div class="v">'+(a.latest_dps!=null?fmt(a.latest_dps,2)+' 元':'—')+'</div></div>'+
      '<div><div class="k">对应股息率</div><div class="v" style="color:var(--up)">'+(a.latest_yield!=null?fmt(a.latest_yield,2)+'%':'—')+'</div></div>'+
      '<div><div class="k">派息率</div><div class="v">'+(a.latest_payout!=null?fmt(a.latest_payout,1)+'%':'—')+'</div></div>'+
      '<div><div class="k">当年派息次数</div><div class="v">'+(a.latest_count||1)+' 次</div></div>'+
      '<div><div class="k">分红连续性</div><div class="v" style="font-size:11px">'+esc(a.stability||'—')+'</div></div>'+
      (a.interim_year?'<div><div class="k">更新的中期分红</div><div class="v">'+esc(a.interim_year)+' 年 '+(a.interim_dps!=null?fmt(a.interim_dps,2)+' 元':'')+'</div></div>':'')+
      '</div></div>';
  }
  // 主体自身画像
  const of=d.owner_facts||{};
  if(Object.keys(of).length){
    h+='<div class="look-sec"><h4>持股主体自身画像 · 钱从哪来</h4><div class="look-kv">'+
      Object.keys(of).map(k=>'<div><div class="k">'+esc(k)+'</div><div class="v">'+
        (typeof of[k]==='number'?(Math.abs(of[k])>=1e8?fmt(of[k]/1e8,2)+' 亿':fmt(of[k],2)):esc(of[k]))+'</div></div>').join('')+
      '</div></div>';
  }
  // 公开证据
  if((d.evidence||[]).length){
    h+='<div class="look-sec"><h4>公开报道原文 · 可点击核实</h4>'+
      d.evidence.map(x=>'<a class="look-ev" href="'+esc(x.url||'#')+'" target="_blank" rel="noopener noreferrer">'+
        '<div class="et">'+esc(x.title||'')+'</div>'+
        '<div class="em">'+esc(x.source||'')+' · '+esc(x.date||'')+'</div>'+
        '<div class="eq">'+esc(x.quote||'')+'</div></a>').join('')+'</div>';
  }
  // 历史轨迹
  if((d.history||[]).length){
    h+='<div class="look-sec"><h4>该股东在本股的历史轨迹</h4><div style="overflow-x:auto"><table><thead><tr>'+
      ['报告期','名次','持股数','占比','变动'].map(x=>'<th>'+x+'</th>').join('')+
      '</tr></thead><tbody>'+d.history.map(x=>'<tr><td class="code">'+esc(x.date||'')+
      '</td><td>'+esc(x.rank||'—')+'</td><td>'+fmtShares(x.shares)+'</td><td>'+fmt(x.ratio,2)+
      '%</td><td>'+esc(x.change||'—')+'</td></tr>').join('')+'</tbody></table></div></div>';
  }
  // 该股东还投资了哪些股票（异步加载，不阻塞主体穿透结果）
  h+='<div class="look-sec" id="lookPortfolioSec"><h4>该股东还投资了哪些股票 · 全市场穿透</h4>'+
     '<div class="sub" style="font-size:12px;padding:8px 0">正在扫描全市场十大股东缓存…</div></div>';
  // 免责
  h+='<div class="look-note">'+esc(v.confidence||'')+'。'+esc((d.caveats||[]).join('；'))+
     '<br/>'+esc(d.disclaimer||'')+'</div>';
  lookBody.innerHTML=h;
  loadLookPortfolio(lookHolderName);
}

// ===== 股东全市场持仓穿透：这个股东还投资了哪些股票 =====
function loadLookPortfolio(name){
  const box=document.getElementById('lookPortfolioSec');
  if(!box) return;
  api('/api/holder/portfolio?holder='+encodeURIComponent(name||lookHolderName))
    .then(p=>renderLookPortfolio(p,box))
    .catch(e=>{ if(box&&document.body.contains(box))
      box.innerHTML='<h4>该股东还投资了哪些股票 · 全市场穿透</h4><div class="sub">加载失败：'+esc(e.message||e)+'</div>'; });
}
function renderLookPortfolio(p,box){
  if(!box||!document.body.contains(box)) return;
  if(!p||!p.ok){
    box.innerHTML='<h4>该股东还投资了哪些股票 · 全市场穿透</h4><div class="sub">'+
      esc((p&&p.error)||'未在全市场十大股东中找到该主体')+'</div>';
    return;
  }
  const s=p.summary||{}, rows=p.rows||[], ent=p.entity||{};
  let h='<h4>该股东还投资了哪些股票 · 全市场穿透 <span class="tag info">'+(s.stock_count||0)+' 家</span></h4>';
  h+='<div class="look-kv">'+
     '<div><div class="k">直接持股主体</div><div class="v">'+(s.direct_count||0)+' 家</div></div>'+
     '<div><div class="k">同集团关联主体</div><div class="v">'+(s.affiliated_count||0)+' 家</div></div>'+
     '<div><div class="k">最重持仓</div><div class="v" style="color:var(--up)">'+
       esc(s.top_stock||'—')+(s.top_ratio!=null?' '+fmt(s.top_ratio,2)+'%':'')+'</div></div>'+
     '<div><div class="k">主体自身持股合计</div><div class="v">'+
       (s.direct_shares!=null&&s.direct_shares>0?fmtShares(s.direct_shares):'—')+'</div></div>'+
     '<div><div class="k">最新报告期</div><div class="v">'+esc(s.latest_report||'—')+'</div></div>'+
     '<div><div class="k">关联识别置信度</div><div class="v" style="font-size:11px">'+esc(s.group_confidence||'—')+'</div></div>'+
     '</div>';
  if(ent.token){
    h+='<div class="sub" style="font-size:11px;margin-top:6px">核心字号「'+esc(ent.token)+'」'+
       (ent.listed_name?' · 对应上市公司 '+esc(ent.listed_name)+(ent.listed_code?'（'+esc(ent.listed_code)+'）':''):'')+
       (ent.controller?' · 实控人 '+esc(ent.controller):'')+
       (ent.direct_keys&&ent.direct_keys.length?' · 精确主体：'+esc(ent.direct_keys.join('、')):'')+'</div>';
  }
  h+='<div style="overflow-x:auto;margin-top:8px"><table><thead><tr>'+
     ['代码','股票','持股主体','关系','名次','占总股本','持股数','变动','报告期'].map(x=>'<th>'+x+'</th>').join('')+
     '</tr></thead><tbody>'+rows.map(r=>'<tr>'+
       '<td><span class="port-stock" data-code="'+esc(r.code)+'" title="点击查看该股">'+esc(r.code)+'</span></td>'+
       '<td>'+esc(r.stock||'—')+'</td>'+
       '<td class="sub" style="font-size:11px">'+esc(r.holder_name||'—')+'</td>'+
       '<td><span class="tag '+(r.layer==='direct'?'buy':'neutral')+'">'+
         esc(r.layer==='direct'?'直接':'同集团')+'</span></td>'+
       '<td>'+esc(r.rank||'—')+'</td>'+
       '<td class="'+(r.layer==='direct'?'up':'')+'">'+(r.ratio!=null?fmt(r.ratio,2)+'%':'—')+'</td>'+
       '<td>'+fmtShares(r.shares)+'</td>'+
       '<td class="sub" style="font-size:11px">'+changeCell({change:(r.change&&r.change!=='不变'&&/^-?\d+$/.test(String(r.change)))?Number(r.change):null,change_raw:r.change})+'</td>'+
       '<td class="code">'+esc(r.end_date||r.report_date||'—')+'</td>'+
       '</tr>').join('')+'</tbody></table></div>';
  if(p.truncated) h+='<div class="sub" style="font-size:11px;margin-top:5px">共 '+esc(s.record_count||rows.length)+
     ' 条持仓记录，仅显示占比最高的 '+rows.length+' 条</div>';
  h+='<div class="sub" style="font-size:11px;margin-top:6px">'+esc(p.note||'')+'</div>';
  box.innerHTML=h;
  box.querySelectorAll('.port-stock').forEach(el=>{
    el.addEventListener('click',()=>{
      closeLookthrough();
      document.getElementById('stockSearch').value=el.getAttribute('data-code');
      document.querySelector('.tab[data-page="stock"]').click();
      loadStock();
    });
  });
}

// ===== 股东深度分析 =====
document.getElementById('holderDeep').addEventListener('click',function(){
  if(!currentStock){ alert('请先选择一只股票'); return; }
  const code=currentStock.code;
  if(deepBusy) return;
  if(deepCache[code]){ renderDeep(deepCache[code]); return; }
  deepBusy=true;
  const btn=this; btn.disabled=true; btn.textContent='分析中…';
  const prog=document.getElementById('deepProgress'), bar=document.getElementById('deepBar');
  const status=document.getElementById('deepStatus'), pct=document.getElementById('deepPct');
  prog.style.display='block'; bar.style.width='0%'; status.textContent='提交任务…'; pct.textContent='';
  document.getElementById('deepContent').style.display='none';
  api('/api/holders/deep',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({code})}).
  then(d=>{
    const jid=d.job_id;
    deepTimer=setInterval(()=>{
      api('/api/job/'+jid).then(j=>{
        if(j.total>0){ const p=Math.round(j.progress/j.total*100); bar.style.width=p+'%'; pct.textContent=p+'%'; }
        status.textContent=j.message||'采集中…';
        if(j.status==='done'){
          clearInterval(deepTimer); deepTimer=null; deepBusy=false;
          btn.disabled=false; btn.textContent='深度分析';
          prog.style.display='none';
          deepCache[code]=j.result; renderDeep(j.result);
        }else if(j.status==='error'){
          clearInterval(deepTimer); deepTimer=null; deepBusy=false;
          btn.disabled=false; btn.textContent='深度分析';
          status.textContent='失败：'+j.error;
        }
      }).catch(e=>{
        clearInterval(deepTimer); deepTimer=null; deepBusy=false;
        btn.disabled=false; btn.textContent='深度分析'; status.textContent='轮询失败：'+e.message;
      });
    },1000);
  }).catch(e=>{
    deepBusy=false; btn.disabled=false; btn.textContent='深度分析';
    status.textContent='提交失败：'+e.message;
  });
});
function insightToneClass(t){ return t==='warn'?'var(--down)':(t==='good'?'var(--up)':'var(--muted)'); }
function renderDeep(d){
  if(d.is_fund){
    document.getElementById('deepContent').style.display='block';
    document.getElementById('deepMeta').textContent='';
    document.getElementById('deepControl').innerHTML='';
    document.getElementById('deepCategory').innerHTML='<div class="empty" style="padding:20px">'+d.message+'</div>';
    document.getElementById('deepTrendChart').innerHTML='';
    document.getElementById('deepInsights').innerHTML='';
    document.getElementById('deepFlow').innerHTML='';
    document.getElementById('deepChanges').innerHTML='';
    document.getElementById('deepCompliance').innerHTML='';
    return;
  }
  document.getElementById('deepContent').style.display='block';
  const fetched=d.fetched_at?new Date(d.fetched_at*1000).toLocaleString('zh-CN'):'—';
  document.getElementById('deepMeta').innerHTML=(d.name||d.code)+' · 报告期 '+d.report_date+' · 分析'+d.periods+'期 · 采集于'+fetched+(d.stale?' · <span style="color:var(--warn)">离线缓存</span>':'');
  // 控制权卡
  const c=d.control||{};
  const ctrl=[
    {label:'第一大股东',value:(c.top1_name||'—').slice(0,10),sub:c.nature||'—'},
    {label:'第一大股东持股',value:(c.top1_ratio!==undefined?fmt(c.top1_ratio,2)+'%':'—'),sub:'前二差距 '+(c.gap_1_2!=null?fmt(c.gap_1_2,2):'—')+' 个百分点'},
    {label:'控制权格局',value:c.control_level||'—',sub:c.balance||''},
    {label:'CR5 / CR10',value:(d.summary&&d.summary.cr5!==null?d.summary.cr5+'% / '+d.summary.cr10+'%':'—'),sub:'本期合计持股'}
  ];
  document.getElementById('deepControl').innerHTML=ctrl.map(m=>
    '<div class="card" style="padding:12px"><div class="sub">'+m.label+'</div>'+
    '<div style="font-size:16px;font-weight:700;margin:4px 0">'+m.value+'</div>'+
    '<div class="sub" style="font-size:11px">'+m.sub+'</div></div>').join('');
  // 分类分布（横向条）
  const cats=d.category_dist||{};
  const catColor={'国资/产业集团':'#4f46e5','外资/陆股通':'#dc2626','公募基金':'#059669','私募/创投':'#d97706','社保基金':'#0891b2','保险资金':'#7c3aed','自然人':'#64748b'};
  const catKeys=Object.keys(cats);
  const catTotal=catKeys.reduce((a,k)=>a+(Number(cats[k])||0),0);
  document.getElementById('deepCategory').innerHTML=catKeys.length?
    '<div class="sub" style="font-size:11px;margin-bottom:6px">条形按占十大股东合计比例归一化 · 数值为占总股本比例</div>'+
    catKeys.map(k=>
    '<div style="margin-bottom:8px"><div class="row" style="justify-content:space-between;font-size:12px"><span>'+k+'</span><b>'+fmt(cats[k],2)+'%</b></div>'+
    '<div style="height:7px;background:var(--border);border-radius:4px;margin-top:3px;overflow:hidden">'+
    '<div style="height:100%;width:'+(catTotal>0?Math.min(100,Number(cats[k])/catTotal*100):0)+'%;background:'+(catColor[k]||'#94a3b8')+'"></div></div></div>').join(''):'<div class="sub">无数据</div>';
  // 集中度趋势图
  const series=(d.trend_top&&d.trend_top.series)||[];
  const dom=document.getElementById('deepTrendChart');
  if(typeof echarts!=='undefined'&&series.length){
    if(deepChart) deepChart.dispose();
    deepChart=echarts.init(dom);
    const dates=series.map(s=>s.date.slice(0,6)).reverse();
    deepChart.setOption({
      tooltip:{trigger:'axis'},
      legend:{data:['CR5','CR10'],textStyle:{color:'#64748b'},top:0},
      grid:{left:40,right:12,top:30,bottom:24},
      xAxis:{type:'category',data:dates,axisLabel:{color:'#64748b',fontSize:10},axisLine:{lineStyle:{color:'#e5e8ec'}}},
      yAxis:{type:'value',axisLabel:{color:'#64748b',fontSize:10,formatter:'{value}%'},splitLine:{lineStyle:{color:'#f1f5f9'}}},
      series:[
        {name:'CR5',type:'line',smooth:true,data:series.map(s=>s.cr5).reverse(),lineStyle:{color:'#4f46e5',width:2},itemStyle:{color:'#4f46e5'},symbolSize:5},
        {name:'CR10',type:'line',smooth:true,data:series.map(s=>s.cr10).reverse(),lineStyle:{color:'#dc2626',width:2},itemStyle:{color:'#dc2626'},symbolSize:5}
      ]
    });
  }else{ dom.innerHTML='<div class="sub">无趋势数据</div>'; }
  // 深度洞察
  const ins=d.insights||[];
  document.getElementById('deepInsights').innerHTML=ins.map(x=>
    '<div style="display:flex;gap:10px;padding:8px 0;border-bottom:1px solid var(--border);font-size:12px">'+
    '<span style="color:'+insightToneClass(x.tone)+';font-weight:700;min-width:12px">'+(x.tone==='warn'?'!':(x.tone==='good'?'+':'·'))+'</span>'+
    '<span>'+x.text+'</span></div>').join('');
  // 重点股东变动画像（产业资本新进/跨界持股等，规则推断）
  const notable=d.notable_events||[];
  document.getElementById('deepNotable').innerHTML=notable.length?notable.map(e=>
    '<div style="padding:8px 0;border-bottom:1px solid var(--border)">'+
    '<div class="row" style="gap:7px;flex-wrap:wrap;align-items:center"><b style="font-size:12px">'+esc(e.name)+'</b>'+
    (e.tags||[]).map(t=>'<span class="tag '+(e.is_new?'buy':'info')+'">'+esc(t)+'</span>').join('')+
    '<span class="sub" style="font-size:11px">'+fmt(e.ratio,2)+'% · '+esc(e.change||'—')+'</span></div>'+
    '<div class="sub" style="font-size:11px;margin-top:4px;line-height:1.6">'+esc(e.reading||'')+'</div></div>').join(''):
    '<div class="sub">本期无重点关注事件</div>';
  // 新进/退出
  const tt=d.trend_top||{};
  document.getElementById('deepFlow').innerHTML=
    '<div class="sub" style="color:var(--up);margin-bottom:4px">新进（'+(tt.entrants||[]).length+'）</div>'+
    ((tt.entrants||[]).length?tt.entrants.map(x=>'<div style="font-size:12px;padding:3px 0">+ '+x+'</div>').join(''):'<div class="sub">无</div>')+
    '<div class="sub" style="color:var(--down);margin:10px 0 4px">退出（'+(tt.exits||[]).length+'）</div>'+
    ((tt.exits||[]).length?tt.exits.map(x=>'<div style="font-size:12px;padding:3px 0">- '+x+'</div>').join(''):'<div class="sub">无</div>');
  // 连续增减持
  const add=tt.continuous_add||[], cut=tt.continuous_cut||[];
  document.getElementById('deepChanges').innerHTML=
    '<div class="sub" style="color:var(--up);margin-bottom:4px">连续增持</div>'+
    (add.length?add.map(x=>'<div style="font-size:12px;padding:3px 0" title="'+x.name+'">+ '+x.name.slice(0,22)+' <span class="sub">('+x.up+'期)</span></div>').join(''):'<div class="sub">无</div>')+
    '<div class="sub" style="color:var(--down);margin:10px 0 4px">连续减持</div>'+
    (cut.length?cut.map(x=>'<div style="font-size:12px;padding:3px 0" title="'+x.name+'">- '+x.name.slice(0,22)+' <span class="sub">('+x.down+'期)</span></div>').join(''):'<div class="sub">无</div>');
  // 合规说明
  const cp=d.compliance||{};
  document.getElementById('deepCompliance').innerHTML='<div class="sub" style="font-size:11px;line-height:1.8">'+
    '<b>合规采集说明：</b>数据来自东方财富F10投资者无需登录即可查看的公开披露页面（底层为上市公司法定定期报告），'+
    '仅用于个人学习研究，不分发不商用。采集限速 '+(cp.min_interval||1.2)+'s/请求、失败指数退避重试 '+(cp.max_retries||2)+' 次、缓存优先减少请求、本地审计留痕。'+
    '<br/><span style="color:var(--warn)">'+(d.disclaimer||'')+'</span></div>';
}
window.addEventListener('resize',()=>{ if(deepChart) deepChart.resize(); if(relChart) relChart.resize(); if(synthRadar) synthRadar.resize(); });

// ===== 关联分析 =====
document.getElementById('relLoad').addEventListener('click',async function(){
  if(!currentStock){ alert('请先选择一只股票'); return; }
  const code=currentStock.code, btn=this;
  btn.disabled=true; btn.textContent='分析中…';
  document.getElementById('relEmpty').style.display='none';
  try{
    const d=relCache[code]||await api('/api/relation/'+code);
    relCache[code]=d; renderRelation(d);
  }catch(e){
    document.getElementById('relEmpty').style.display='block';
    document.getElementById('relEmpty').innerHTML='<div class="empty">失败：'+e.message+'</div>';
  }finally{ btn.disabled=false; btn.textContent='重新分析'; }
});
function renderRelation(d){
  document.getElementById('relContent').style.display='block';
  if(d.error){
    document.getElementById('relBench').innerHTML='<div class="empty">'+d.error+'</div>';
    return;
  }
  const b=d.benchmark||{};
  const cards=[
    {l:'相对基准',v:b.index_name||'—',s:b.corr!==undefined?'相关系数 '+fmt(b.corr,3):''},
    {l:'Beta',v:b.beta!==undefined?fmt(b.beta,3):'—',s:'>1 波动放大'},
    {l:'区间超额收益',v:b.excess_return!==undefined?((b.excess_return>0?'+':'')+fmt(b.excess_return,2)+'%'):'—',s:'vs 基准'},
    {l:'最大回撤/波动',v:(b.max_drawdown!==undefined?fmt(b.max_drawdown,2)+'%':'—'),s:'年化波动 '+(b.ann_vol!=null?fmt(b.ann_vol,2):'—')+'%'},
  ];
  document.getElementById('relBench').innerHTML=cards.map(c=>
    '<div class="dim-card"><div class="sub">'+c.l+'</div><div class="score">'+c.v+'</div><div class="sub" style="font-size:10px">'+c.s+'</div></div>').join('');
  // 联动标的
  const rels=d.correlations||[];
  document.getElementById('relList').innerHTML=rels.length?rels.map(x=>
    '<div class="rel-row"><span class="nm">'+x.code+' '+(x.name||'')+'</span>'+
    '<span class="tag neutral">'+(x.level||'')+'</span>'+
    '<div class="rel-bar"><i style="width:'+Math.min(100,Math.abs(x.corr)*100)+'%;background:'+(x.corr>=0?'var(--up)':'var(--down)')+'"></i></div>'+
    '<b style="min-width:44px;text-align:right;color:'+(x.corr>=0?'var(--up)':'var(--down)')+'">'+(x.corr>0?'+':'')+fmt(x.corr,3)+'</b></div>').join(''):'<div class="sub">无足够数据计算联动</div>';
  // 相对强弱曲线
  const rc=d.relative_curve||[];
  const dom=document.getElementById('relChart');
  if(typeof echarts!=='undefined'&&rc.length>1){
    if(relChart) relChart.dispose();
    relChart=echarts.init(dom);
    relChart.setOption({
      tooltip:{trigger:'axis'},legend:{data:['本股','沪深300'],textStyle:{color:'#64748b'},top:0},
      grid:{left:42,right:12,top:30,bottom:24},
      xAxis:{type:'category',data:rc.map(x=>x.i),axisLabel:{show:false},axisLine:{lineStyle:{color:'#e5e8ec'}}},
      yAxis:{type:'value',axisLabel:{color:'#64748b',fontSize:10,formatter:'{value}'},splitLine:{lineStyle:{color:'#f1f5f9'}}},
      series:[
        {name:'本股',type:'line',smooth:true,data:rc.map(x=>x.stock),lineStyle:{color:'#dc2626',width:2},itemStyle:{color:'#dc2626'},symbol:'none'},
        {name:'沪深300',type:'line',smooth:true,data:rc.map(x=>x.bench),lineStyle:{color:'#64748b',width:1},itemStyle:{color:'#64748b'},symbol:'none'}
      ]
    });
  }else{ dom.innerHTML='<div class="sub">无曲线数据</div>'; }
  // 产业链关联
  const cl=d.chain_links||[];
  document.getElementById('relChain').innerHTML=cl.length?
    '<div class="sub" style="font-weight:600;margin-bottom:6px">产业链关联标的（来自知识库）</div>'+
    cl.map(x=>'<span class="signal-pill info" data-code="'+x.code+'" style="cursor:pointer">'+x.name+' · '+x.stage+'</span>').join(''):'';
  document.getElementById('relDisclaimer').innerHTML='<div class="sub" style="font-size:11px;color:var(--warn)">'+(d.disclaimer||'')+'</div>';
}

// ===== 政策分析 =====
document.getElementById('policyLoad').addEventListener('click',async function(){
  if(!currentStock){ alert('请先选择一只股票'); return; }
  const code=currentStock.code, btn=this;
  btn.disabled=true; btn.textContent='分析中…';
  document.getElementById('policyEmpty').style.display='none';
  try{
    const d=policyCache[code]||await api('/api/policy/'+code);
    policyCache[code]=d; renderPolicy(d);
  }catch(e){
    document.getElementById('policyEmpty').style.display='block';
    document.getElementById('policyEmpty').innerHTML='<div class="empty">失败：'+e.message+'</div>';
  }finally{ btn.disabled=false; btn.textContent='重新分析'; }
});
function renderPolicy(d){
  document.getElementById('policyContent').style.display='block';
  const ps=d.policy_sentiment||{};
  const sum=[
    {l:'政策情绪',v:ps.label||'—',s:'相关 '+(ps.related_count||0)+' 条'},
    {l:'情绪分',v:ps.score!==undefined?ps.score:'—',s:'0-100，50中性'},
    {l:'相关主题',v:(d.stock_themes||[]).length,s:(d.stock_themes||[]).map(x=>x.theme).slice(0,2).join('/')||'无'}
  ];
  document.getElementById('policySummary').innerHTML=sum.map(c=>
    '<div class="dim-card"><div class="sub">'+c.l+'</div><div class="score">'+c.v+'</div><div class="sub" style="font-size:10px">'+c.s+'</div></div>').join('');
  const news=d.related_news||[];
  document.getElementById('policyNews').innerHTML=news.length?news.map(x=>{
    const col=x.sentiment==='偏多'?'var(--up)':(x.sentiment==='偏空'?'var(--down)':'var(--muted)');
    return '<div class="policy-item">'+
      (x.url?'<a href="'+esc(x.url)+'" target="_blank" rel="noopener" class="t" style="color:var(--text);text-decoration:none">'+esc(x.title)+'</a>':'<div class="t">'+esc(x.title)+'</div>')+
      '<div class="m"><span style="color:'+col+';font-weight:600">'+x.sentiment+' '+x.score+'</span> · '+x.time+' · '+(x.matched_themes||[]).join('/')+
      (x.increment&&x.increment.length?' · <span style="color:var(--accent)">增量:'+x.increment.join(',')+'</span>':'')+'</div></div>';
  }).join(''):'<div class="sub">未匹配到与该股主题相关的政策快讯</div>';
  const heat=d.theme_heat||{};
  document.getElementById('policyHeat').innerHTML=Object.keys(heat).length?Object.keys(heat).map(k=>{
    const h=heat[k];
    return '<div class="rel-row"><span class="nm">'+k+(h.relevance==='高'?' <span class="src-tag real">相关</span>':'')+'</span>'+
      '<span class="sub">'+h.count+'条</span>'+
      '<div class="rel-bar"><i style="width:'+Math.min(100,h.count)+'%;background:'+(h.avg_score>=60?'var(--up)':(h.avg_score<=40?'var(--down)':'#94a3b8'))+'"></i></div>'+
      '<b style="min-width:34px;text-align:right">'+h.avg_score+'</b></div>';
  }).join(''):'<div class="sub">无主题热度数据</div>';
  document.getElementById('policyDisclaimer').innerHTML='<div class="sub" style="font-size:11px;color:var(--warn)">'+(d.disclaimer||'')+'</div>';
}

// ===== 多维综合决策辅助 =====
document.getElementById('synthLoad').addEventListener('click',function(){
  if(!currentStock){ alert('请先选择一只股票'); return; }
  const code=currentStock.code;
  if(synthBusy) return;
  if(synthCache[code]){ renderSynthesis(synthCache[code]); return; }
  synthBusy=true;
  const btn=this; btn.disabled=true; btn.textContent='汇总中…';
  const prog=document.getElementById('synthProgress'), bar=document.getElementById('synthBar');
  const status=document.getElementById('synthStatus'), pct=document.getElementById('synthPct');
  prog.style.display='block'; bar.style.width='0%';
  document.getElementById('synthContent').style.display='none';
  api('/api/synthesis',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code})})
  .then(d=>{
    const jid=d.job_id;
    synthTimer=setInterval(()=>{
      api('/api/job/'+jid).then(j=>{
        if(j.total>0){ const p=Math.round(j.progress/j.total*100); bar.style.width=p+'%'; pct.textContent=p+'%'; }
        status.textContent=j.message||'汇总中…';
        if(j.status==='done'){
          clearInterval(synthTimer); synthTimer=null; synthBusy=false;
          btn.disabled=false; btn.textContent='重新生成'; prog.style.display='none';
          synthCache[code]=j.result; renderSynthesis(j.result);
        }else if(j.status==='error'){
          clearInterval(synthTimer); synthTimer=null; synthBusy=false;
          btn.disabled=false; btn.textContent='生成综合信号'; status.textContent='失败：'+j.error;
        }
      }).catch(e=>{ clearInterval(synthTimer); synthTimer=null; synthBusy=false; btn.disabled=false; btn.textContent='生成综合信号'; status.textContent='轮询失败：'+e.message; });
    },1200);
  }).catch(e=>{ synthBusy=false; btn.disabled=false; btn.textContent='生成综合信号'; status.textContent='提交失败：'+e.message; });
});
function scoreColor(s){ return s>=65?'var(--up)':(s<=35?'var(--down)':'var(--muted)'); }
function renderSynthesis(d){
  document.getElementById('synthContent').style.display='block';
  const ov=d.overall;
  const col=scoreColor(ov);
  document.getElementById('synthOverall').innerHTML='<span style="color:'+col+'">'+(ov!==null?ov:'—')+'</span>'+
    ' <span style="font-size:14px;color:var(--muted)">'+d.overall_label+'</span>';
  // 六维卡
  const dims=d.dimensions||{};
  document.getElementById('synthDims').innerHTML=Object.keys(dims).map(k=>{
    const v=dims[k], s=v.score;
    return '<div class="dim-card"><div class="row" style="justify-content:space-between"><span class="sub">'+v.label+'</span>'+
      '<b style="color:'+(s!==null?scoreColor(s):'var(--hint)')+'">'+(s!==null?s:'—')+'</b></div>'+
      '<div class="dim-bar"><i style="width:'+(s!==null?s:0)+'%;background:'+(s!==null?scoreColor(s):'#cbd5e1')+'"></i></div>'+
      '<div class="sub" style="font-size:10px;margin-top:5px">'+JSON.stringify(v.detail||{}).slice(0,50)+'</div></div>';
  }).join('');
  // 雷达图
  const dom=document.getElementById('synthRadar');
  const keys=Object.keys(dims);
  if(typeof echarts!=='undefined'&&keys.length){
    if(synthRadar) synthRadar.dispose();
    synthRadar=echarts.init(dom);
    synthRadar.setOption({
      radar:{indicator:keys.map(k=>({name:dims[k].label,max:100})),radius:'62%',center:['50%','52%'],
        axisName:{color:'#64748b',fontSize:10},splitLine:{lineStyle:{color:'#e5e8ec'}},splitArea:{areaStyle:{color:['#fafbfc','#fff']}}},
      series:[{type:'radar',data:[{value:keys.map(k=>dims[k].score||0),name:'评分',
        lineStyle:{color:'#4f46e5',width:2},itemStyle:{color:'#4f46e5'},areaStyle:{color:'rgba(79,70,229,0.15)'}}]}]
    });
  }
  // 共振/矛盾
  const notes=d.notes||[];
  document.getElementById('synthSignals').innerHTML=notes.map(n=>{
    const cls=n.tone==='good'?'good':(n.tone==='warn'?'bad':'info');
    return '<div style="padding:6px 0"><span class="signal-pill '+cls+'">'+(n.tone==='good'?'共振':(n.tone==='warn'?'分歧':'中性'))+'</span> '+n.text+'</div>';
  }).join('');
  // 风险
  const risks=d.risks||[];
  document.getElementById('synthRisks').innerHTML=risks.length?risks.map(r=>'<div class="risk-item">⚠ '+r+'</div>').join(''):'<div class="sub">基于当前数据未触发明确风险规则</div>';
  document.getElementById('synthDisclaimer').innerHTML='<div class="sub" style="font-size:11px;line-height:1.7;color:var(--warn)">'+(d.disclaimer||'')+'</div>';
}

/* ---------- 持仓透视：当前个股 × 本地持仓 结合视角 ---------- */
let holdingViewSeq = 0;  // 快速切换股票时丢弃过期响应
async function renderHoldingView(d){
  const seq=++holdingViewSeq;
  const code=String(d.code);
  if(portfolioReady) await portfolioReady;  // 等持仓从服务端加密文件加载完成
  const holds=holdings;
  const h=holds.find(x=>String(x.code).padStart(6,'0')===code);
  const emptyEl=document.getElementById('holdingEmpty');
  const contentEl=document.getElementById('holdingContent');
  const chip=document.getElementById('holdingChip');
  if(!h){
    chip.textContent='未持仓';
    chip.className='chip';
    emptyEl.style.display='block'; contentEl.style.display='none';
    return;
  }
  chip.textContent='已持仓 '+h.shares+' 股';
  chip.className='chip up';
  let port=null;
  try{
    port=await api('/api/portfolio',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({holdings:holds,days:120})});
  }catch(e){}
  if(seq!==holdingViewSeq) return;  // 已切到其他股票，丢弃
  const det=(port&&port.details)?port.details.find(x=>x.code===code):null;
  const lastBar=d.bars.length?d.bars[d.bars.length-1]:null;
  const last=det?det.last:(lastBar?lastBar.close:null);
  const mv=det?det.market_value:(last!=null?last*h.shares:null);
  const pnl=det?det.pnl:(last!=null&&h.cost!=null?(last-h.cost)*h.shares:null);
  const pnlPct=det?det.pnl_pct:(last!=null&&h.cost>0?(last/h.cost-1)*100:null);
  const totalMV=(port&&port.details)?port.details.reduce((s,x)=>s+(x.market_value||0),0):null;
  const weight=(totalMV&&mv!=null&&totalMV>0)?mv/totalMV*100:null;
  const totalPnl=(port&&port.summary)?port.summary.total_pnl:null;
  const fmtMoney=v=>v==null?'—':(Math.abs(v)>=1e8?fmt(v/1e8,2)+'亿':fmt(v,2));
  const metrics=[
    ['持仓成本',fmtPrice(h.cost,code),''],
    ['最新现价',fmtPrice(last,code),''],
    ['持股数量',h.shares+' 股',''],
    ['持仓市值',fmtMoney(mv),''],
    ['浮动盈亏',pnl==null?'—':(pnl>=0?'+':'')+fmtMoney(pnl),pnl>=0?'up':(pnl<0?'down':'')],
    ['盈亏比例',pnlPct==null?'—':pct(pnlPct),pnlPct>=0?'up':(pnlPct<0?'down':'')],
    ['占组合市值',weight==null?'—':fmt(weight,1)+'%',''],
    ['组合总盈亏',totalPnl==null?'—':(totalPnl>=0?'+':'')+fmtMoney(totalPnl),totalPnl>=0?'up':(totalPnl<0?'down':'')],
  ];
  document.getElementById('holdingMetrics').innerHTML=metrics.map(m=>
    `<div class="metric"><div class="k">${m[0]}</div><div class="v ${m[2]}">${m[1]}</div></div>`).join('');
  // 结合日线统计的客观描述（描述性陈述，非买卖建议）
  const s=d.stats||{};
  const notes=[];
  if(pnlPct!=null){
    let t=`现价较你的成本 ${pct(pnlPct)}`;
    if(s.price_percentile!=null) t+=`，当前价处于近 250 日 ${fmt(s.price_percentile,1)}% 分位`;
    notes.push({tone:pnlPct>=0?'up':'down',text:t+'。'});
  }
  if(s.chg20!=null&&weight!=null){
    notes.push({tone:s.chg20>=0?'up':'down',
      text:`近 20 日该股 ${pct(s.chg20)}，按当前权重 ${fmt(weight,1)}% 估算，对组合净值影响约 ${pct(s.chg20*weight/100)}。`});
  }
  if(s.volume_ratio!=null&&s.volume_ratio>1.5){
    notes.push({tone:'',text:`今日量比 ${fmt(s.volume_ratio,2)}（较 5 日均量明显放大），请结合量价方向自行判断。`});
  }
  if(s.volatility_annual!=null&&weight!=null){
    notes.push({tone:'',text:`近 60 日年化波动 ${fmt(s.volatility_annual,1)}%，折算单日波动约 ${fmt(s.volatility_annual/Math.sqrt(244),1)}%，对组合单日影响约 ${fmt(s.volatility_annual/Math.sqrt(244)*weight/100,2)}%。`});
  }
  document.getElementById('holdingNotes').innerHTML=notes.length?
    notes.map(n=>`<div class="sub" style="margin-bottom:6px"><span class="tag ${n.tone==='up'?'buy':(n.tone==='down'?'sell':'info')}">${n.tone==='up'?'▲':(n.tone==='down'?'▼':'●')}</span> ${n.text}</div>`).join('')
    :'<div class="sub">暂无可用结合描述</div>';
  emptyEl.style.display='none'; contentEl.style.display='block';
}

/* ================= 全国地图分布 ================= */
let mapData=null, mapInited=false, mapLoading=false, mapGeoOK=false;
let mapDim='province', mapMetric='count', mapFilter=null, mapSearchKw='', mapPage=1;
let mapChart=null, mapBarChart=null;

const MAP_DIMS=[
  ['province','按省份'],['industry','按行业'],['csrc','按证监会行业'],['board','按上市板块'],
  ['mv','按市值规模'],['pe','按市盈率'],['pb','按市净率'],['price','按价格区间'],
  ['chg','按当日涨跌'],['tr','按换手率'],['amt','按成交额'],['amp','按振幅'],
  ['lb','按量比'],['year','按上市年份'],['sh','按股本规模']];

const MAP_DIM_ORDER={
  mv:['3000亿以上','1000~3000亿','500~1000亿','100~500亿','100亿以下'],
  pe:['0~20','20~40','40~100','100以上','亏损/无PE'],
  pb:['破净(<1)','1~3','3~10','10以上'],
  price:['5元以下','5~10元','10~30元','30~100元','100元以上'],
  chg:['涨停/大涨(≥9.9%)','涨3~9.9%','涨0~3%','平','跌0~3%','跌3~9.9%','跌停/大跌'],
  tr:['<1%','1~3%','3~10%','>10%'],
  amt:['<1亿','1~5亿','5~20亿','20~100亿','>100亿'],
  amp:['<2%','2~5%','5~10%','>10%'],
  lb:['明显缩量(<0.8)','平量(0.8~1.2)','温和放量(1.2~2)','明显放量(>2)'],
  year:['1999年前','2000~2009','2010~2015','2016~2020','2021以来'],
  sh:['<5亿股','5~20亿股','20~100亿股','>100亿股']};

function mapDimKey(s, dim){
  switch(dim){
    case 'province': return s.p||'未知';
    case 'industry': return s.ind||'未知';
    case 'csrc': return s.csrc||'未知';
    case 'board': return s.b;
    case 'mv': { const v=s.mv; if(v==null)return'未知'; if(v>=3000)return'3000亿以上'; if(v>=1000)return'1000~3000亿'; if(v>=500)return'500~1000亿'; if(v>=100)return'100~500亿'; return'100亿以下'; }
    case 'pe': { const v=s.pe; if(v==null||v<=0)return'亏损/无PE'; if(v<20)return'0~20'; if(v<40)return'20~40'; if(v<100)return'40~100'; return'100以上'; }
    case 'pb': { const v=s.pb; if(v==null)return'未知'; if(v<1)return'破净(<1)'; if(v<3)return'1~3'; if(v<10)return'3~10'; return'10以上'; }
    case 'price': { const v=s.close; if(v==null)return'未知'; if(v<5)return'5元以下'; if(v<10)return'5~10元'; if(v<30)return'10~30元'; if(v<100)return'30~100元'; return'100元以上'; }
    case 'chg': { const v=s.chg; if(v==null)return'未知'; if(v>=9.9)return'涨停/大涨(≥9.9%)'; if(v>=3)return'涨3~9.9%'; if(v>0)return'涨0~3%'; if(v===0)return'平'; if(v>-3)return'跌0~3%'; if(v>-9.9)return'跌3~9.9%'; return'跌停/大跌'; }
    case 'tr': { const v=s.tr; if(v==null)return'未知'; if(v<1)return'<1%'; if(v<3)return'1~3%'; if(v<10)return'3~10%'; return'>10%'; }
    case 'amt': { const v=s.amt; if(v==null)return'未知'; if(v<1)return'<1亿'; if(v<5)return'1~5亿'; if(v<20)return'5~20亿'; if(v<100)return'20~100亿'; return'>100亿'; }
    case 'amp': { const v=s.amp; if(v==null)return'未知'; if(v<2)return'<2%'; if(v<5)return'2~5%'; if(v<10)return'5~10%'; return'>10%'; }
    case 'lb': { const v=s.lb; if(v==null)return'未知'; if(v<0.8)return'明显缩量(<0.8)'; if(v<1.2)return'平量(0.8~1.2)'; if(v<2)return'温和放量(1.2~2)'; return'明显放量(>2)'; }
    case 'year': { const v=s.year; if(!v)return'未知'; if(v<2000)return'1999年前'; if(v<2010)return'2000~2009'; if(v<2016)return'2010~2015'; if(v<2021)return'2016~2020'; return'2021以来'; }
    case 'sh': { const v=s.sh; if(v==null)return'未知'; if(v<5)return'<5亿股'; if(v<20)return'5~20亿股'; if(v<100)return'20~100亿股'; return'>100亿股'; }
  }
  return '未知';
}

function mapAggregate(dim){
  const m={};
  for(const s of (mapData.stocks||[])){
    const k=mapDimKey(s,dim);
    if(!m[k]) m[k]={count:0,chgSum:0};
    m[k].count++;
    if(s.chg!=null) m[k].chgSum+=s.chg;
  }
  let items=Object.entries(m).map(([k,v])=>({k,count:v.count,avg:v.count?v.chgSum/v.count:0}));
  const order=MAP_DIM_ORDER[dim];
  if(order) items.sort((a,b)=>order.indexOf(a.k)-order.indexOf(b.k));
  else items.sort((a,b)=>b.count-a.count);
  return items;
}

async function initMapPage(){
  if(mapInited){ return; }
  mapInited=true;
  const dims=document.getElementById('mapDims');
  dims.innerHTML=MAP_DIMS.map(([k,name])=>
    `<span class="chip${k===mapDim?' on':''}" data-dim="${k}">${name}</span>`).join('');
  dims.querySelectorAll('.chip').forEach(c=>{
    c.addEventListener('click',()=>{
      mapDim=c.dataset.dim;
      dims.querySelectorAll('.chip').forEach(x=>x.classList.toggle('on',x===c));
      if(mapDim!=='province'&&mapFilter&&mapFilter.dim!=='province') setMapFilter(null);
      renderMapView();
    });
  });
  document.getElementById('mapMetricCount').addEventListener('click',function(){
    mapMetric='count'; this.classList.add('on');
    document.getElementById('mapMetricChg').classList.remove('on'); renderMapChart();
  });
  document.getElementById('mapMetricChg').addEventListener('click',function(){
    mapMetric='chg'; this.classList.add('on');
    document.getElementById('mapMetricCount').classList.remove('on'); renderMapChart();
  });
  document.getElementById('mapRefresh').addEventListener('click',async function(){
    this.disabled=true; this.textContent='刷新中…';
    try{ await loadMapData(true); }catch(e){ alert('刷新失败：'+e.message); }
    this.disabled=false; this.textContent='刷新行情';
  });
  document.getElementById('mapSearch').addEventListener('input',function(){
    mapSearchKw=this.value.trim().toLowerCase(); mapPage=1; renderMapTable();
  });
  mapChart=echarts.init(document.getElementById('mapChart'));
  mapBarChart=echarts.init(document.getElementById('mapBar'));
  window.addEventListener('resize',()=>{ mapChart.resize(); mapBarChart.resize(); });
  await loadMapData(false);
}

async function loadMapData(refresh){
  if(mapLoading) return;
  mapLoading=true;
  const t0=Date.now();
  const hint=document.getElementById('mapHint');
  hint.textContent=refresh?'正在联网刷新行情…':'正在加载地图数据…';
  try{
    const d=await api('/api/map/stocks'+(refresh?'?refresh=1':''));
    mapData=d;
    if(!mapGeoOK){
      const r=await fetch(API+'/api/map/geo');
      const geo=await r.json();
      echarts.registerMap('china', geo);
      mapGeoOK=true;
    }
    hint.textContent=`本地聚合 · 本次耗时 ${((Date.now()-t0)/1000).toFixed(1)}s`;
    document.getElementById('mapUpdated').textContent='属性更新 '+new Date((d.attr_ts||0)*1000).toLocaleDateString('zh-CN');
    renderMapView();
  }catch(e){
    hint.textContent='加载失败：'+e.message;
  }finally{ mapLoading=false; }
}

function renderMapView(){
  renderMapChart();
  renderMapBar();
  renderMapTable();
}

function renderMapChart(){
  const stocks=mapData.stocks||[];
  const byProv={};
  for(const s of stocks){
    const k=s.p||'未知';
    if(!byProv[k]) byProv[k]={count:0,chgSum:0};
    byProv[k].count++;
    if(s.chg!=null) byProv[k].chgSum+=s.chg;
  }
  const data=Object.entries(byProv).filter(([k])=>k!=='未知')
    .map(([k,v])=>({name:k,value:mapMetric==='count'?v.count:+(v.chgSum/v.count).toFixed(2)}));
  const isCount=mapMetric==='count';
  const opt={
    tooltip:{trigger:'item',backgroundColor:'#fff',borderColor:'#e5e8ec',textStyle:{color:'#1f2733',fontSize:12},
      formatter:p=>{
        const v=p.value;
        if(v==null) return p.name+'<br/>无数据';
        const st=byProv[p.name]||{count:0,chgSum:0};
        const avg=st.count?(st.chgSum/st.count):0;
        return isCount? `${p.name}<br/>企业数量：<b>${v}</b> 家 · 平均涨跌 ${avg>0?'+':''}${avg.toFixed(2)}%`
                      : `${p.name}<br/>平均涨跌幅：<b style="color:${v>0?'#d23b3b':(v<0?'#15803d':'#6b7480')}">${v>0?'+':''}${v}%</b>（${st.count} 家）`;
      }},
    visualMap: isCount? {
        min:0, max:Math.max(...data.map(x=>x.value),10), left:10, bottom:20,
        text:['企业多','少'], calculable:false, inRange:{color:['#e8effc','#a9c6f0','#5b8dd9','#2563eb','#1d4ed8']}
      } : {
        min:-3, max:3, left:10, bottom:20, calculable:true,
        text:['平均上涨','平均下跌'], inRange:{color:['#15803d','#7cb342','#f1f5f9','#e05d5d','#d23b3b']}
      },
    series:[{
      type:'map', map:'china', roam:true,
      emphasis:{label:{fontSize:11},itemStyle:{areaColor:'#fbbf24'}},
      select:{disabled:true},
      data:data,
      label:{show:false},
      itemStyle:{borderColor:'#fff',borderWidth:.6},
    }],
  };
  mapChart.setOption(opt,true);
  mapChart.off('click');
  mapChart.on('click',p=>{
    if(p.name) setMapFilter({dim:'province',value:p.name});
  });
}

function renderMapBar(){
  const items=mapAggregate(mapDim);
  const top=items.slice(0,60);
  const dimName=(MAP_DIMS.find(d=>d[0]===mapDim)||['',''])[1];
  mapBarChart.setOption({
    title:{text:`${dimName} · 分类分布（点击柱条筛选）`,left:6,top:2,textStyle:{fontSize:12,color:'#6b7480',fontWeight:500}},
    tooltip:{trigger:'item',backgroundColor:'#fff',borderColor:'#e5e8ec',textStyle:{color:'#1f2733',fontSize:12},
      formatter:p=>`${p.name}<br/>${p.value.count} 家 · 平均涨跌 ${p.value.avg>0?'+':''}${p.value.avg.toFixed(2)}%`},
    grid:{left:6,right:16,top:28,bottom:34,containLabel:true},
    xAxis:{type:'value',splitLine:{lineStyle:{color:'#eef1f5'}},axisLabel:{fontSize:10,color:'#9aa5b4'}},
    yAxis:{type:'category',inverse:true,data:top.map(x=>x.k),axisLabel:{fontSize:11,color:'#4a5568'},axisTick:{show:false},axisLine:{lineStyle:{color:'#e5e8ec'}}},
    dataZoom:[{type:'slider',yAxisIndex:0,right:2,width:14,start:0,end:Math.min(100,Math.ceil(14/Math.max(top.length,1)*100)),borderColor:'#e5e8ec'}],
    series:[{type:'bar',data:top.map(x=>({value:x.count,avg:x.avg,name:x.k})),
      itemStyle:{color:'#2563eb',borderRadius:[0,3,3,0]},barMaxWidth:14,
      label:{show:true,position:'right',fontSize:10,color:'#6b7480'}}],
  },true);
  mapBarChart.off('click');
  mapBarChart.on('click',p=>{ if(p.name) setMapFilter({dim:mapDim,value:p.name}); });
}

function setMapFilter(f){
  mapFilter=f; mapPage=1;
  const chip=document.getElementById('mapFilterChip');
  if(f){
    const dimName=(MAP_DIMS.find(d=>d[0]===f.dim)||['',''])[1];
    chip.textContent=`✕ ${dimName}: ${f.value}`;
    chip.style.display='inline-flex';
  }else{ chip.style.display='none'; }
  renderMapTable();
}

function mapFilteredStocks(){
  let list=mapData?mapData.stocks||[]:[];
  if(mapFilter) list=list.filter(s=>mapDimKey(s,mapFilter.dim)===mapFilter.value);
  if(mapSearchKw) list=list.filter(s=>s.c.includes(mapSearchKw)||s.n.toLowerCase().includes(mapSearchKw));
  return list;
}

function renderMapTable(){
  const el=document.getElementById('mapTable');
  const all=mapFilteredStocks();
  const per=100, pages=Math.max(1,Math.ceil(all.length/per));
  if(mapPage>pages) mapPage=pages;
  const rows=all.slice((mapPage-1)*per,mapPage*per);
  document.getElementById('mapTableCount').textContent=`共 ${all.length} 只 · 第 ${mapPage}/${pages} 页`;
  el.innerHTML='<table><thead><tr><th>代码</th><th>名称</th><th>省份</th><th>行业</th><th>板块</th><th>现价</th><th>涨跌幅</th><th>成交额</th><th>换手</th><th>PE(TTM)</th><th>总市值</th></tr></thead><tbody>'+
    (rows.length?rows.map(s=>`<tr style="cursor:pointer" data-code="${s.c}" title="点击分析该股">
      <td class="code">${s.c}</td><td>${esc(s.n)}</td><td>${esc(s.p)}</td><td>${esc(s.ind)||'—'}</td><td>${esc(s.b)}</td>
      <td>${fmtPrice(s.close,s.c)}</td>
      <td class="${(s.chg||0)>0?'up':((s.chg||0)<0?'down':'')}">${pct(s.chg)}</td>
      <td>${s.amt!=null?fmt(s.amt,2)+'亿':'—'}</td>
      <td>${s.tr!=null?fmt(s.tr,2)+'%':'—'}</td>
      <td>${s.pett!=null?fmt(s.pett,1):'—'}</td>
      <td>${s.mv!=null?fmt(s.mv,0)+'亿':'—'}</td>
    </tr>`).join(''):'<tr><td colspan="11" style="text-align:center;color:var(--hint);padding:20px">无匹配个股</td></tr>')+
    '</tbody></table>';
  el.querySelectorAll('tr[data-code]').forEach(tr=>{
    tr.addEventListener('click',()=>{
      document.getElementById('stockSearch').value=tr.dataset.code;
      document.querySelector('.tab[data-page="stock"]').click();
      loadStock();
    });
  });
  const pager=document.getElementById('mapPager');
  let html='';
  if(pages>1){
    html+=`<button class="btn-ghost" ${mapPage<=1?'disabled':''} id="mapPrev">上一页</button>`;
    html+=`<span class="sub">${mapPage} / ${pages}</span>`;
    html+=`<button class="btn-ghost" ${mapPage>=pages?'disabled':''} id="mapNext">下一页</button>`;
  }
  pager.innerHTML=html;
  const prev=document.getElementById('mapPrev'), next=document.getElementById('mapNext');
  if(prev) prev.addEventListener('click',()=>{ if(mapPage>1){mapPage--;renderMapTable();} });
  if(next) next.addEventListener('click',()=>{ if(mapPage<pages){mapPage++;renderMapTable();} });
}
document.getElementById('mapFilterChip').addEventListener('click',()=>setMapFilter(null));

// ============ 量化选股推荐 ============
let picksPolling=null;
function renderPicks(p){
  const body=document.getElementById('picksBody');
  if(!p.ok){ body.innerHTML='<div class="empty" style="padding:16px">选股失败：'+(p.error||'未知原因')+'</div>'; return; }
  const w=p.weights||{}, lab=p.factor_labels||{};
  const wChips=Object.keys(w).map(k=>`<span class="chip" style="font-size:11px">${lab[k]||k} ×${Math.round(w[k]*100)}%</span>`).join(' ');
  const gradeCls=g=>g==='A'?'buy':(g==='B'?'buy':(g==='D'?'sell':'info'));
  const rows=p.picks.map((r,i)=>`<tr style="cursor:pointer" data-code="${r.code}" title="点击分析该股">
      <td class="code">${i+1}</td>
      <td class="code">${r.code}</td><td>${esc(r.name)}</td>
      <td><span class="tag ${gradeCls(r.grade)}">${r.grade}${r.grade_label}</span></td>
      <td><b>${fmt(r.score,1)}</b></td>
      <td>${esc(r.top_factor)||'—'}</td>
      <td class="${(r.mom20||0)>0?'up':((r.mom20||0)<0?'down':'')}">${r.mom20!=null?pct(r.mom20):'—'}</td>
      <td>${r.trend!=null?pct(r.trend):'—'}</td>
      <td>${r.vol20!=null?fmt(r.vol20,2)+'%':'—'}</td>
      <td>${r.pe!=null?fmt(r.pe,1):'—'}</td>
      <td>${r.pb!=null?fmt(r.pb,2):'—'}</td>
      <td>${r.roe!=null?fmt(r.roe,1)+'%':'—'}</td>
      <td>${r.turnover!=null?fmt(r.turnover,2)+'%':'—'}</td>
      <td>${r.amt20_yi!=null?fmt(r.amt20_yi,1)+'亿':'—'}</td>
      <td><b>${r.pos_suggest}</b></td>
      <td class="down">${r.stop_loss!=null?fmt(r.stop_loss,2):'—'}</td>
    </tr>`).join('');
  const hist=window._picksHistory||[];
  const histHtml=hist.length?`<div style="margin-top:16px">
    <div class="sub" style="margin-bottom:6px">历期跟踪（生成日收盘 → 最近交易日实际涨跌，供策略有效性自检）</div>
    <div style="overflow-x:auto"><table><thead><tr><th>生成日</th><th>期数收益均值</th><th>Top5 明细</th></tr></thead><tbody>${
      hist.map(h=>`<tr>
        <td class="code">${h.date}</td>
        <td class="${(h.avg_ret||0)>0?'up':((h.avg_ret||0)<0?'down':'')}"><b>${h.avg_ret!=null?pct(h.avg_ret):'—'}</b></td>
        <td style="font-size:12px">${(h.picks||[]).slice(0,5).map(x=>`${esc(x.name)}<span class="${(x.ret||0)>0?'up':((x.ret||0)<0?'down':'')}"> ${x.ret!=null?pct(x.ret):'…'}</span>`).join(' · ')}</td>
      </tr>`).join('')}</tbody></table></div></div>`:'';
  body.innerHTML=`
    <div class="row" style="gap:6px;flex-wrap:wrap;margin-bottom:10px">
      <span class="sub">快照 ${p.as_of} · 交易数据截至 ${p.trade_date} · 全市场 ${p.universe} 只 → 有效样本 ${p.valid} 只</span>
    </div>
    <div class="row" style="gap:6px;flex-wrap:wrap;margin-bottom:10px">${wChips}</div>
    <div style="overflow-x:auto"><table><thead><tr>
      <th>#</th><th>代码</th><th>名称</th><th>评级</th><th>综合分</th><th>主贡献因子</th>
      <th>20日动量</th><th>MA20趋势</th><th>20日波动</th><th>PE</th><th>PB</th><th>ROE</th><th>换手</th><th>日均额</th><th>建议仓位</th><th>止损位</th>
    </tr></thead><tbody>${rows}</tbody></table></div>
    <div class="sub" style="margin-top:10px">约束：剔除 ST/退市/北交所/次新(&lt;120日)/当日涨停/日均成交额&lt;5000万；因子为横截面排名百分位加权合成；建议仓位按综合分映射且单票≤10%；止损位=收盘×(1−2.5×20日波动)。因子权重为经验设定，未做单因子回测优化；本清单为统计参考非投资建议。</div>
    ${histHtml}`;
  body.querySelectorAll('tr[data-code]').forEach(tr=>{
    tr.addEventListener('click',()=>{
      document.getElementById('stockSearch').value=tr.dataset.code;
      document.querySelector('.tab[data-page="stock"]').click();
      loadStock();
    });
  });
}
function loadPicksHistory(){
  api('/api/picks/history').then(d=>{ window._picksHistory=d.history||[]; }).catch(()=>{});
}
async function runPicks(){
  const btn=document.getElementById('picksRun'), body=document.getElementById('picksBody');
  if(btn.disabled) return;
  btn.disabled=true; const old=btn.textContent; btn.textContent='扫描中…';
  const topN=document.getElementById('picksN').value;
  body.innerHTML='<div class="sub" style="padding:14px 0">正在拉取全市场快照并计算因子（约1-2分钟）…</div>';
  try{
    const j=await api('/api/picks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({top_n:Number(topN)})});
    picksPolling=setInterval(async()=>{
      try{
        const s=await api('/api/job/'+j.job_id);
        if(s.status==='running'&&s.total>0){
          body.innerHTML=`<div class="sub" style="padding:14px 0">计算因子中 ${s.progress}/${s.total} …</div>`;
        }else if(s.status==='done'){
          clearInterval(picksPolling); picksPolling=null;
          btn.disabled=false; btn.textContent=old;
          renderPicks((s.result&&s.result.picks)||{ok:false,error:'空结果'});
          loadPicksHistory();
        }else if(s.status==='error'){
          clearInterval(picksPolling); picksPolling=null;
          btn.disabled=false; btn.textContent=old;
          body.innerHTML='<div class="empty" style="padding:16px">选股失败：'+(s.error||'未知错误')+'</div>';
        }
      }catch(e){ /* 轮询失败忽略，下轮重试 */ }
    },3000);
  }catch(e){
    btn.disabled=false; btn.textContent=old;
    body.innerHTML='<div class="empty" style="padding:16px">任务启动失败：'+e.message+'</div>';
  }
}
document.getElementById('picksRun').addEventListener('click',runPicks);
loadPicksHistory();

// ===== 投资人版图：全市场股权穿透 =====
let ctlInited=false, ctlChart=null, ctlTimer=null;
function initControllerPage(){
  if(ctlInited) return;
  ctlInited=true;
  loadControllerStatus();
  api('/api/controllers/list?limit=120').then(d=>{
    renderControllerList(d.items||[]);
    const first=(d.items||[]).find(x=>x.type!=='未披露')||(d.items||[])[0];
    if(first) selectController(first.name);
  }).catch(e=>document.getElementById('ctlList').innerHTML='<div class="sub">榜单加载失败：'+esc(e.message)+'</div>');
}
function loadControllerStatus(){
  api('/api/controllers/status').then(d=>{
    const age=d.graph_age_seconds||0;
    const timeTxt=age<60?'刚刚更新':(age<3600?Math.floor(age/60)+' 分钟前':(age<86400?Math.floor(age/3600)+' 小时前':Math.floor(age/86400)+' 天前'));
    const cn=d.cninfo||{};
    document.getElementById('ctlStatus').textContent='实控人 '+d.cached+' 只 · 公告证据 '+(cn.with_annual||0)+' 家 · '+timeTxt;
    const ms=[
      {k:'已采集上市公司',v:d.stock_count||0,s:'本地缓存覆盖'},
      {k:'已披露实控人',v:d.with_controller||0,s:'含自然人/国资/企业'},
      {k:'实控人主体数',v:d.controller_count||0,s:'按名称聚合'},
      {k:'公告证据',v:cn.with_annual||0,s:'巨潮年报已抓取'+(cn.with_shareholders?(' · 股东表 '+cn.with_shareholders):'')}
    ];
    document.getElementById('ctlMetrics').innerHTML=ms.map(m=>'<div class="card" style="margin:0;padding:12px">'+
      '<div class="sub" style="font-size:11px;margin:0">'+m.k+'</div><div style="font-size:20px;font-weight:700;margin-top:3px">'+m.v+'</div>'+
      '<div class="sub" style="font-size:10px;margin:0">'+m.s+'</div></div>').join('');
  }).catch(()=>{});
}
function renderControllerList(items){
  if(!items.length){ document.getElementById('ctlList').innerHTML='<div class="sub">暂无数据，请先采集</div>'; return; }
  document.getElementById('ctlList').innerHTML=items.map(x=>
    '<div class="rel-row" data-ctl="'+esc(x.name)+'" style="cursor:pointer">'+
    '<div class="nm"><div style="font-weight:600;font-size:12px">'+esc(x.name)+'</div>'+
    '<div class="sub" style="font-size:10px">'+esc((x.sample||[]).join('、'))+'</div></div>'+
    '<span class="tag '+(x.type==='国资'?'info':(x.type==='自然人'?'buy':'neutral'))+'">'+x.count+' 家</span></div>').join('');
}
document.getElementById('ctlList').addEventListener('click',function(e){
  const el=e.target.closest('[data-ctl]');
  if(el) selectController(el.getAttribute('data-ctl'));
});
let ctlSearchTimer=null;
document.getElementById('ctlSearch').addEventListener('input',function(){
  const q=this.value.trim();
  clearTimeout(ctlSearchTimer);
  if(!q){ api('/api/controllers/list?limit=120').then(d=>renderControllerList(d.items||[])); return; }
  ctlSearchTimer=setTimeout(()=>{
    api('/api/controllers/search?q='+encodeURIComponent(q)).then(d=>{
      // 「未披露/无实际控制人」是711家公司的兜底分组，点它毫无分析价值，隐藏
      const its=(d.items||[]).filter(x=>!(x.kind==='controller'&&x.type==='未披露'));
      document.getElementById('ctlList').innerHTML=its.length?its.map(x=>
        // 搜到公司必须传代码：传实控人名会让无实控人公司落到711家兜底组
        '<div class="rel-row" data-ctl="'+esc(x.kind==='company'?x.code:x.name)+'" style="cursor:pointer">'+
        '<div class="nm"><div style="font-weight:600;font-size:12px">'+esc(x.name)+'</div>'+
        '<div class="sub" style="font-size:10px">'+(x.kind==='company'?('上市公司 '+esc(x.code)+' · 实控人 '+esc(x.controller)):(x.kind==='entity'?('控股平台 · 实控人 '+esc(x.controller)):esc(x.type||'')))+'</div></div>'+
        '<span class="tag '+(x.kind==='company'?'info':(x.kind==='entity'?'buy':'neutral'))+'">'+(x.kind==='company'?esc(x.code):(x.count+' 家'))+'</span></div>').join(''):'<div class="sub">无匹配结果</div>';
    }).catch(()=>{});
  },260);
});
function selectController(name){
  window._ctlCurrent=name;
  document.getElementById('ctlTitle').textContent=name;
  document.getElementById('ctlSub').textContent='正在整理穿透链条…';
  api('/api/controllers/detail?name='+encodeURIComponent(name)).then(d=>{
    if(d.name) document.getElementById('ctlTitle').textContent=d.name;
    document.getElementById('ctlSub').textContent=d.type+' · 直接控制 '+d.company_count+' 家 · 跨公司关联 '+d.related_count+' 家';
    renderControllerGraph(d);
    renderControllerDetail(d);
  }).catch(e=>{ document.getElementById('ctlSub').textContent=e.message; });
}
function renderControllerGraph(d){
  const cats=['实控人','控股平台','直接控制','关联公司'];
  const colors=['#2563eb','#8b5cf6','#d64545','#94a3b8'];
  const ids=new Set((d.nodes||[]).map(n=>n.id));
  const allNodes=d.nodes||[];
  const main=allNodes.filter(n=>n.category<=2);
  const extra=allNodes.filter(n=>n.category===3).slice(0,60);
  const nodes=(main.concat(extra)).slice(0,180).map(n=>({
    id:n.id,name:n.name,category:n.category,
    symbolSize:n.symbolSize||18,
    value:n.ratio||null,
    label:{show:n.category!==3},
    extra:{type:n.type||'',role:n.role||'',code:n.code||''}
  }));
  const links=(d.links||[]).filter(l=>ids.has(l.source)&&ids.has(l.target)).slice(0,500);
  const el=document.getElementById('ctlChart');
  if(ctlChart){ ctlChart.dispose(); }
  ctlChart=echarts.init(el);
  ctlChart.setOption({
    tooltip:{trigger:'item',formatter:p=>{
      if(p.dataType==='edge') return esc(p.data.source)+' → '+esc(p.data.target)+'<br/>'+esc(p.data.value||'');
      const x=p.data||{}; return '<b>'+esc(x.name)+'</b><br/>'+(x.extra&&x.extra.role?esc(x.extra.role)+'<br/>':'')+(x.value!=null?'持股 '+fmt(x.value,2)+'%':'');
    }},
    legend:[{data:cats,top:0,textStyle:{fontSize:11}}],
    animationDuration:600,
    series:[{
      type:'graph',layout:'force',roam:true,draggable:true,
      categories:cats.map((c,i)=>({name:c,itemStyle:{color:colors[i]}})),
      data:nodes,links:links,
      label:{show:true,position:'right',fontSize:10,formatter:p=>p.name&&p.name.length>13?p.name.slice(0,13)+'…':p.name},
      force:{repulsion:170,edgeLength:[55,150],gravity:.08},
      lineStyle:{opacity:.4,curveness:.12,color:'source'},
      emphasis:{focus:'adjacency',lineStyle:{width:2,opacity:.9}}
    }]
  });
}
function renderControllerDetail(d){
  let h='';
  const tbl=(title,head,rows)=>'<div class="look-sec"><h4>'+title+'</h4><div style="overflow-x:auto"><table><thead><tr>'+
    head.map(x=>'<th>'+x+'</th>').join('')+'</tr></thead><tbody>'+rows+'</tbody></table></div></div>';
  h+=tbl('直接控制的上市公司（'+d.companies.length+'）',['代码','公司','控股平台/第一大股东','平台持股','实控人持股','报告期','公告证据'],
    d.companies.map(c=>{
      const ci=c.cninfo||{};
      const ev=ci.available?
        '<a href="'+esc(ci.source_url||'#')+'" target="_blank" rel="noopener" title="'+
          esc(((ci.controller_texts||[])[0]||'')+' · '+(ci.annual&&ci.annual.date||''))+'">年报 ↗</a>'+
          (ci.change_count?' <span class="sub">+'+ci.change_count+'变动</span>':'')
        :'<span class="sub">—</span>';
      return '<tr><td class="code">'+esc(c.code)+'</td><td>'+esc(c.name||'—')+'</td><td>'+esc(c.owner||'—')+
        '</td><td>'+(c.owner_ratio!=null?fmt(c.owner_ratio,2)+'%':'—')+'</td><td>'+(c.controller_ratio!=null?fmt(c.controller_ratio,2)+'%':'—')+
        '</td><td class="sub">'+esc(c.report_date||'—')+'</td><td>'+ev+'</td></tr>';
    }).join(''));
  if((d.entities||[]).length){
    h+=tbl('控制链上的主体 / 平台',['主体名称','角色','直接控制公司数','全市场出现公司数'],
      d.entities.map(e=>'<tr><td>'+esc(e.name)+'</td><td class="sub">'+esc(e.role||'—')+'</td><td>'+e.own_codes.length+'</td><td>'+e.market_count+'</td></tr>').join(''));
  }
  if((d.related||[]).length){
    h+=tbl('该投资人关联的其它上市公司（'+d.related_count+'）',['代码','公司','关联主体','持股比例'],
      d.related.slice(0,50).map(r=>'<tr><td class="code">'+esc(r.code)+'</td><td>'+esc(r.name||'—')+
        '</td><td class="sub">'+esc((r.via||[]).join('、'))+'</td><td>'+(r.ratio!=null?fmt(r.ratio,2)+'%':'—')+'</td></tr>').join('')+
      (d.related_count>50?'<tr><td colspan="4" class="sub">仅显示前 50 条，共 '+d.related_count+' 条</td></tr>':''));
  }
  document.getElementById('ctlDetail').innerHTML=h;
}
function startControllerScan(rebuild){
  const btn=document.getElementById(rebuild?'ctlRebuild':'ctlScan');
  const old=btn.textContent; btn.disabled=true; btn.textContent='采集中…';
  const box=document.getElementById('ctlProgress'), bar=document.getElementById('ctlProgBar'), txt=document.getElementById('ctlProgText');
  box.style.display='block'; bar.style.width='0%'; txt.textContent='提交任务…';
  api('/api/controllers/scan',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(rebuild?{}:{workers:6})}).then(d=>{
    clearInterval(ctlTimer);
    ctlTimer=setInterval(()=>{
      api('/api/job/'+d.job_id).then(j=>{
        if(j.total>0){ bar.style.width=Math.round(j.progress/j.total*100)+'%'; }
        txt.textContent=j.message||'采集中…';
        if(j.status==='done'){
          clearInterval(ctlTimer); btn.disabled=false; btn.textContent=old;
          txt.textContent='完成 · '+((j.result&&j.result.done)||0)+' 只更新，失败 '+((j.result&&j.result.failed)||0);
          loadControllerStatus();
          api('/api/controllers/list?limit=120').then(x=>renderControllerList(x.items||[]));
        }else if(j.status==='error'){
          clearInterval(ctlTimer); btn.disabled=false; btn.textContent=old; txt.textContent='失败：'+j.error;
        }
      }).catch(e=>{ clearInterval(ctlTimer); btn.disabled=false; btn.textContent=old; txt.textContent=e.message; });
    },1000);
  }).catch(e=>{ btn.disabled=false; btn.textContent=old; txt.textContent='启动失败：'+e.message; });
}
function startCninfoScan(){
  const btn=document.getElementById('ctlCnScan');
  const old=btn.textContent; btn.disabled=true; btn.textContent='补采中…';
  const box=document.getElementById('ctlProgress'), bar=document.getElementById('ctlProgBar'), txt=document.getElementById('ctlProgText');
  box.style.display='block'; bar.style.width='0%'; txt.textContent='提交任务…';
  api('/api/cninfo/scan',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({workers:4})}).then(d=>{
    clearInterval(ctlTimer);
    ctlTimer=setInterval(()=>{
      api('/api/job/'+d.job_id).then(j=>{
        if(j.total>0){ bar.style.width=Math.round(j.progress/j.total*100)+'%'; }
        txt.textContent=j.message||'补采中…';
        if(j.status==='done'){
          clearInterval(ctlTimer); btn.disabled=false; btn.textContent=old;
          const r=j.result||{};
          txt.textContent='完成 · '+(r.with_annual||0)+' 家年报，'+(r.done||0)+' 成功，'+(r.failed||0)+' 失败';
          loadControllerStatus();
          if(document.getElementById('ctlTitle')&&window._ctlCurrent)
            selectController(window._ctlCurrent);
        }else if(j.status==='error'){
          clearInterval(ctlTimer); btn.disabled=false; btn.textContent=old; txt.textContent='失败：'+j.error;
        }
      }).catch(e=>{ clearInterval(ctlTimer); btn.disabled=false; btn.textContent=old; txt.textContent=e.message; });
    },1000);
  }).catch(e=>{ btn.disabled=false; btn.textContent=old; txt.textContent='启动失败：'+e.message; });
}
document.getElementById('ctlScan').addEventListener('click',()=>startControllerScan(false));
document.getElementById('ctlRebuild').addEventListener('click',()=>startControllerScan(true));
document.getElementById('ctlCnScan').addEventListener('click',startCninfoScan);

// ===== 股权穿透图（独立全屏页面） =====
let eqInited=false, eqChart=null, eqData=null, eqTimer=null;
const EQ_CATS=['实控人','控股平台','直接控制','关联公司'];
const EQ_COLORS=['#2563eb','#8b5cf6','#d64545','#94a3b8'];
function initEquityPage(){
  if(eqInited) return;
  eqInited=true;
  api('/api/controllers/list?limit=40').then(d=>{
    const items=(d.items||[]).filter(x=>x.type!=='未披露').slice(0,20);
    document.getElementById('eqHotList').innerHTML=items.length?items.map(x=>
      '<div class="eq-hot" data-name="'+esc(x.name)+'" title="控制 '+x.count+' 家公司" style="padding:7px 9px;margin-bottom:5px;border:1px solid var(--border);border-radius:8px;cursor:pointer;font-size:12px">'+
      '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:'+(x.type==='国资'?'#d64545':'#2563eb')+';margin-right:7px"></span>'+
      esc(x.name)+' <span class="sub" style="float:right">'+x.count+' 家</span></div>').join('')
      :'<div class="sub">暂无数据，请先在投资人版图采集</div>';
    document.querySelectorAll('.eq-hot').forEach(el=>el.addEventListener('click',()=>{
      eqSearchByName(el.dataset.name);
    }));
  }).catch(e=>document.getElementById('eqHotList').innerHTML='<div class="sub">加载失败：'+esc(e.message)+'</div>');
}
function eqSearchByName(name){
  document.getElementById('eqSearch').value=name;
  document.getElementById('eqStatus').textContent='正在整理穿透链条…';
  api('/api/controllers/detail?name='+encodeURIComponent(name)).then(d=>{
    eqRender(d,name);
  }).catch(e=>{ document.getElementById('eqStatus').textContent='失败：'+e.message; });
}
function eqRender(d,query){
  eqData=d;
  document.getElementById('eqEmpty').style.display='none';
  document.getElementById('eqTitle').textContent=d.name;
  document.getElementById('eqSub').textContent=d.type+' · 直接控制 '+d.company_count+' 家 · 跨公司关联 '+d.related_count+' 家';
  document.getElementById('eqNodeInfo').style.display='none';
  document.getElementById('eqStatus').textContent='共 '+(d.nodes||[]).length+' 个节点 · '+(d.links||[]).length+' 条关系';
  const depth=parseInt(document.getElementById('eqDepth').value||'60');
  eqDraw(d,depth);
}
function eqDraw(d,relatedLimit){
  const ids=new Set((d.nodes||[]).map(n=>n.id));
  const all=d.nodes||[];
  const main=all.filter(n=>n.category<=2);
  const extra=all.filter(n=>n.category===3).slice(0,relatedLimit);
  const nodes=main.concat(extra).map(n=>({
    id:n.id,name:n.name,category:n.category,
    symbolSize:Math.max(12,Math.min(58,(n.symbolSize||18))),
    value:n.ratio||null,
    label:{show:n.category!==3},
    extra:{type:n.type||'',role:n.role||'',code:n.code||'',ratio:n.ratio,
           market_count:n.market_count||0}
  }));
  const shown=new Set(nodes.map(n=>n.id));
  const links=(d.links||[]).filter(l=>shown.has(l.source)&&shown.has(l.target)).slice(0,900);
  if(eqChart){ eqChart.dispose(); }
  eqChart=echarts.init(document.getElementById('eqChart'));
  eqChart.setOption({
    tooltip:{trigger:'item',formatter:p=>{
      if(p.dataType==='edge') return esc(p.data.source)+' → '+esc(p.data.target)+'<br/>'+esc(p.data.value||'');
      const x=p.data||{}; const e=x.extra||{};
      let h='<b>'+esc(x.name)+'</b>';
      if(e.type) h+='<br/>类型：'+esc(e.type);
      if(e.role) h+='<br/>角色：'+esc(e.role);
      if(e.code) h+='<br/>代码：'+esc(e.code);
      if(e.ratio!=null) h+='<br/>持股 '+fmt(e.ratio,2)+'%';
      if(e.market_count) h+='<br/>全市场出现 '+e.market_count+' 家公司';
      return h;
    }},
    legend:[{data:EQ_CATS,top:0,textStyle:{fontSize:11}}],
    animationDuration:500,
    series:[{
      type:'graph',layout:'force',roam:true,draggable:true,
      categories:EQ_CATS.map((c,i)=>({name:c,itemStyle:{color:EQ_COLORS[i]}})),
      data:nodes,links:links,
      label:{show:true,position:'right',fontSize:11,formatter:p=>p.name&&p.name.length>14?p.name.slice(0,14)+'…':p.name},
      force:{repulsion:210,edgeLength:[60,160],gravity:.08,layoutAnimation:true},
      lineStyle:{opacity:.42,curveness:.12,color:'source'},
      emphasis:{focus:'adjacency',lineStyle:{width:2,opacity:.95}}
    }]
  });
  eqChart.off('click');
  eqChart.on('click',p=>{
    if(p.dataType!=='node') return;
    eqShowNode(p.data);
  });
}
function eqShowNode(node){
  const e=node.extra||{};
  let h='<div style="font-size:14px;font-weight:600;margin-bottom:8px">'+esc(node.name)+'</div>';
  h+='<div class="sub" style="margin:2px 0">类别：'+esc(EQ_CATS[node.category]||'—')+'</div>';
  if(e.type) h+='<div class="sub" style="margin:2px 0">类型：'+esc(e.type)+'</div>';
  if(e.role) h+='<div class="sub" style="margin:2px 0">角色：'+esc(e.role)+'</div>';
  if(e.ratio!=null) h+='<div class="sub" style="margin:2px 0">持股：'+fmt(e.ratio,2)+'%</div>';
  if(e.market_count) h+='<div class="sub" style="margin:2px 0">全市场出现：'+e.market_count+' 家公司</div>';
  if(e.code){
    h+='<div style="margin-top:10px;display:flex;gap:6px;flex-wrap:wrap">'+
       '<button class="btn btn-ghost" id="eqGotoStock">查看该股分析</button>'+
       (eqData&&eqData.name?'<button class="btn btn-ghost" id="eqBack">返回 '+esc(eqData.name)+'</button>':'')+'</div>';
  }
  document.getElementById('eqNodeBody').innerHTML=h;
  document.getElementById('eqNodeInfo').style.display='block';
  if(e.code){
    document.getElementById('eqGotoStock').addEventListener('click',()=>{
      const code=String(e.code).padStart(6,'0');
      document.getElementById('stockSearch').value=code;
      document.querySelector('.tab[data-page="stock"]').click();
      loadStock();
    });
    if(eqData&&eqData.name){
      document.getElementById('eqBack').addEventListener('click',()=>eqSearchByName(eqData.name));
    }
  }
}
document.getElementById('eqSearchBtn').addEventListener('click',()=>{
  const q=document.getElementById('eqSearch').value.trim();
  if(!q){ document.getElementById('eqStatus').textContent='请输入关键词'; return; }
  eqSearchByName(q);
});
document.getElementById('eqSearch').addEventListener('keydown',e=>{
  if(e.key==='Enter') document.getElementById('eqSearchBtn').click();
});
document.getElementById('eqDepth').addEventListener('change',()=>{
  if(eqData) eqDraw(eqData,parseInt(document.getElementById('eqDepth').value||'60'));
});
document.getElementById('eqReset').addEventListener('click',()=>{
  if(!eqData) return;
  eqDraw(eqData,parseInt(document.getElementById('eqDepth').value||'60'));
});
window.addEventListener('resize',()=>{ if(eqChart&&document.getElementById('page-equity').classList.contains('active')) eqChart.resize(); });

/* ===== 个股页模块折叠 + 锚点导航 ===== */
const STOCK_MOD_DEFAULT_OPEN=new Set(['decision','overview','kline']); // 默认展开的核心模块
const STOCK_MOD_META=[
  {id:'decision',label:'投资决策'},{id:'overview',label:'概览指标'},{id:'kline',label:'K线主图'},
  {id:'stats',label:'行情统计'},{id:'holding',label:'持仓透视'},
  {id:'signal',label:'近期信号'},{id:'volume',label:'量能分析'},
  {id:'models',label:'多模型'},{id:'predict',label:'概率评级'},
  {id:'fin',label:'财报分析'},{id:'sent',label:'个股舆情'},
  {id:'chain',label:'产业链'},{id:'holder',label:'前十大股东'},
  {id:'synth',label:'多维决策'},{id:'rel',label:'关联分析'},
  {id:'policy',label:'政策分析'}
];
let stockModsBuilt=false, stockModCharts={};
function stockModState(){
  let s={}; try{ s=JSON.parse(localStorage.getItem('stock_mod_state')||'{}'); }catch(e){}
  return s;
}
function saveStockModState(){
  const s={};
  document.querySelectorAll('#stockContent .stock-mod').forEach(m=>{ s[m.dataset.mod]=!m.classList.contains('collapsed'); });
  try{ localStorage.setItem('stock_mod_state',JSON.stringify(s)); }catch(e){}
}
function buildStockModules(){
  if(stockModsBuilt) return;
  const sc=document.getElementById('stockContent');
  const cards=Array.from(sc.children).filter(el=>el.classList.contains('card'));
  if(cards.length<10) return; // 结构未就绪
  stockModsBuilt=true;
  const saved=stockModState();
  // 顶部锚点导航
  const nav=document.createElement('div'); nav.id='stockModNav';
  nav.innerHTML='<div class="mod-nav-scroll" id="stockModNavScroll"></div>'+
    '<div class="mod-nav-act"><span class="mod-nav-chip" data-all="open" title="展开全部模块">全展开</span>'+
    '<span class="mod-nav-chip" data-all="close" title="收起全部模块">全收起</span></div>';
  sc.insertBefore(nav, sc.firstChild);
  const scroll=document.getElementById('stockModNavScroll');
  cards.forEach((card,i)=>{
    const meta=STOCK_MOD_META[i]||{id:'mod'+i,label:'模块'+(i+1)};
    card.classList.add('stock-mod'); card.dataset.mod=meta.id;
    // 在卡片顶部插入可点击的折叠条（复用卡片第一个 .row 或 h2 作为标题）
    const first=card.firstElementChild;
    const titleEl=first&&(first.classList.contains('row')||first.tagName==='H2')?first:null;
    const bar=document.createElement('div');
    bar.className='mod-fold-bar';
    bar.style.cssText='display:flex;align-items:center;gap:8px;cursor:pointer;user-select:none;padding:'+(titleEl?'0 0 10px':'0 0 12px')+';font-size:13px;font-weight:600;color:var(--text)';
    bar.innerHTML='<span class="mod-head-arrow">▼</span><span>'+esc(meta.label)+'</span>'+
      '<span class="sub" style="margin:0 0 0 auto;font-weight:400;font-size:11px">点击'+(saved[meta.id]!==undefined?(saved[meta.id]?'收起':'展开'):(STOCK_MOD_DEFAULT_OPEN.has(meta.id)?'收起':'展开'))+'</span>';
    card.insertBefore(bar, card.firstChild);
    // 折叠内容 = bar 之后的所有子元素
    const body=document.createElement('div'); body.className='mod-fold-body';
    while(bar.nextSibling) body.appendChild(bar.nextSibling);
    card.appendChild(body);
    const open=saved[meta.id]!==undefined?saved[meta.id]:STOCK_MOD_DEFAULT_OPEN.has(meta.id);
    applyStockFold(card,bar,open);
    // 折叠条点击
    bar.addEventListener('click',(e)=>{
      if(e.target.closest('button,.chip,input,select')) return;
      toggleStockMod(card);
    });
    // 导航 chip
    const chip=document.createElement('span');
    chip.className='mod-nav-chip'+(open?' on':''); chip.dataset.goto=meta.id;
    chip.textContent=meta.label; scroll.appendChild(chip);
    chip.addEventListener('click',()=>{
      if(card.classList.contains('collapsed')) toggleStockMod(card,true);
      const y=card.getBoundingClientRect().top+window.scrollY-70;
      window.scrollTo({top:y,behavior:'smooth'});
      syncStockModNav();
    });
  });
  // 全展开/全收起
  nav.querySelectorAll('[data-all]').forEach(b=>b.addEventListener('click',()=>{
    const open=b.dataset.all==='open';
    document.querySelectorAll('#stockContent .stock-mod').forEach(m=>applyStockFold(m,m.querySelector('.mod-fold-bar'),open));
    syncStockModNav(); saveStockModState();
  }));
  syncStockModNav();
}
function applyStockFold(card,bar,open){
  const body=card.querySelector('.mod-fold-body');
  card.classList.toggle('collapsed',!open);
  if(body) body.style.display=open?'':'none';
  const hint=bar&&bar.querySelector('.sub'); if(hint) hint.textContent='点击'+(open?'收起':'展开');
  if(open){
    // 展开后 resize 该模块内所有图表（延迟等 display 生效）
    setTimeout(()=>{
      body&&body.querySelectorAll('div[_echarts_instance_]').forEach(el=>{
        const ins=echarts.getInstanceByDom(el); if(ins) try{ins.resize();}catch(e){}
      });
    },80);
  }
}
function toggleStockMod(card,forceOpen){
  const bar=card.querySelector('.mod-fold-bar');
  const open=forceOpen===true?true:card.classList.contains('collapsed');
  applyStockFold(card,bar,open);
  syncStockModNav(); saveStockModState();
}
function syncStockModNav(){
  document.querySelectorAll('#stockModNav .mod-nav-chip[data-goto]').forEach(c=>{
    const m=document.querySelector('#stockContent .stock-mod[data-mod="'+c.dataset.goto+'"]');
    if(m) c.classList.toggle('on',!m.classList.contains('collapsed'));
  });
}
// 在 renderStock 成功渲染后由 renderStock 调用 buildStockModules()

/* ===== 投资决策引擎面板 ===== */
let decisionChart=null, decisionSeq=0;
const DEC_TONE={'buy':'var(--up)','sell':'var(--down)','neutral':'var(--muted)'};
async function loadDecision(code){
  const seq=++decisionSeq;
  const body=document.getElementById('decisionBody');
  if(!body) return;
  body.innerHTML='<div class="sub" style="padding:14px 0">正在汇总全部可量化信号，生成投资决策…</div>';
  try{
    const d=await api('/api/decision/'+encodeURIComponent(code));
    if(seq!==decisionSeq) return;
    renderDecision(d);
    try{ loadDecisionHistory(code); }catch(e){}
  }catch(e){
    if(seq!==decisionSeq) return;
    body.innerHTML='<div class="empty" style="padding:18px">投资决策生成失败：'+esc(e.message)+'</div>';
  }
}
function renderDecision(d){
  const body=document.getElementById('decisionBody');
  if(!d.ok){ body.innerHTML='<div class="empty" style="padding:18px">'+esc(d.error||d.message||'不可用')+'</div>'; return; }
  const a=d.action||{};
  const tone=DEC_TONE[a.tone]||'var(--muted)';
  const sc=d.composite;
  // 每日快照状态条
  const snapTag=d.from_snapshot
    ? '<span style="color:var(--up)">✓ 当日快照（'+esc(d.snapshot_date||d.as_of||'—')+'）</span>'
    : '<span style="color:var(--accent)">● 新生成快照（'+esc(d.snapshot_date||d.as_of||'—')+'）</span>';
  let h='<div class="row" style="justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:12px;padding:8px 12px;background:var(--surface-2);border-radius:8px">';
  h+='<div class="sub" style="font-size:11px">'+snapTag+' · 当日固定，日线更新到新交易日后自动重算</div>';
  h+='<button class="btn" id="decisionRefresh" style="font-size:11px;padding:4px 10px">↻ 重算今日</button>';
  h+='</div>';
  // 信号可信度因子（第9因子：动态调节技术类权重与置信度）
  const rel=d.reliability;
  if(rel&&rel.score!=null){
    const rc=rel.score>=70?'#1d4ed8':(rel.score>=55?'#2563eb':(rel.score>=40?'#d97706':'#dc2626'));
    const adj=(rel.factor!=null&&rel.factor<0.99)?(' · 技术类权重×'+fmt(rel.factor,2)):'';
    h+='<div style="margin-bottom:12px;padding:8px 12px;border:1px solid '+rc+'33;background:'+rc+'0d;border-radius:8px">';
    h+='<div class="row" style="justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px">';
    h+='<div style="font-size:12px;color:'+rc+';font-weight:700">🛡 信号可信度 '+fmt(rel.score,0)+'/100（'+esc(rel.label||'')+'）</div>';
    h+='<div class="sub" style="font-size:10px">影响：'+esc((rel.applies_to||[]).join('/'))+' 三项权重'+esc(adj)+'</div>';
    h+='</div>';
    h+='<div class="sub" style="font-size:11px;margin-top:3px">'+esc(rel.detail||'')+
       (rel.score<55?' · <span style="color:'+rc+'">该股技术类信号历史不可靠，已自动降权</span>':'')+'</div>';
    h+='</div>';
  }
  // 大盘环境（总闸门：弱市压缩多空偏离与置信度）
  const env=d.market_env;
  if(env&&env.score!=null){
    const ec=env.score<35?'#dc2626':(env.score<45?'#d97706':(env.score>=65?'#1d4ed8':'#2563eb'));
    h+='<div style="margin-bottom:12px;padding:8px 12px;border:1px solid '+ec+'33;background:'+ec+'0d;border-radius:8px">';
    h+='<div class="row" style="justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px">';
    h+='<div style="font-size:12px;color:'+ec+';font-weight:700">🌐 大盘环境 '+fmt(env.score,0)+'/100（'+esc(env.label||'')+'）</div>';
    h+='<div class="sub" style="font-size:10px">弱市自动压缩综合分与置信度'+
       (env.score<45?' · <span style="color:'+ec+'">系统性风险偏高</span>':'')+'</div></div>';
    h+='<div class="sub" style="font-size:11px;margin-top:3px">'+esc(env.detail||'')+'</div>';
    const idxs=env.indices||[];
    if(idxs.length){
      h+='<div class="row" style="gap:6px;flex-wrap:wrap;margin-top:6px">';
      idxs.forEach(x=>{
        const col=x.above_ma20?'var(--up)':'var(--down)';
        h+='<span class="chip" style="font-size:10px;color:'+col+'">'+esc(x.name)+' '+(x.above_ma20?'MA20上':'MA20下')+' '+_pctTag(x.ret20)+'</span>';
      });
      h+='</div>';
    }
    h+='</div>';
  }
  // 顶部：动作徽章 + 综合分 + 置信度 + 动量
  h+='<div class="row" style="justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">';
  h+='<div><div class="sub" style="margin-bottom:4px">投资决策 · 全维度量化融合</div>'+
     '<div style="font-size:26px;font-weight:800;color:'+tone+'">'+esc(a.label||'—')+
     ' <span style="font-size:13px;font-weight:500;color:var(--muted)">'+(a.strength?('信号强度 '+a.strength+'/5'):'')+'</span></div>'+
     '<div class="sub" style="margin:4px 0 0">'+esc(a.hint||'')+'</div></div>';
  h+='<div style="display:flex;gap:18px;align-items:center">';
  if(sc!=null){
    const c=tone;
    h+='<div style="text-align:center"><div style="font-size:30px;font-weight:800;color:'+c+'">'+fmt(sc,1)+'</div>'+
       '<div class="sub" style="font-size:11px">综合分 /100</div></div>';
  }
  h+='<div style="text-align:center"><div style="font-size:22px;font-weight:700">'+d.confidence+'</div>'+
     '<div class="sub" style="font-size:11px">置信度</div></div>';
  // 建议仓位（风险预算折算，弱市收缩）
  const pos=d.position;
  if(pos&&pos.suggested_pct!=null){
    const pc=pos.suggested_pct>0?'var(--up)':'var(--muted)';
    h+='<div style="text-align:center"><div style="font-size:22px;font-weight:700;color:'+pc+'">'+
       (pos.suggested_pct>0?fmt(pos.suggested_pct,1)+'%':'0')+'</div>'+
       '<div class="sub" style="font-size:11px">建议仓位'+(pos.suggested_pct>0?'':'·暂不建仓')+'</div></div>';
  }
  h+='<div style="text-align:center;padding:6px 12px;border-radius:8px;background:var(--surface-2)">'+
     '<div style="font-size:14px;font-weight:700;color:'+(d.momentum==='enhancing'?'var(--up)':(d.momentum==='weakening'?'var(--down)':'var(--muted)'))+'">'+
     esc(d.momentum_label||'')+'</div><div class="sub" style="font-size:11px">信号动量</div></div>';
  h+='</div></div>';
  h+='<div class="sub" style="font-size:11px;margin:8px 0 0">'+esc(d.confidence_detail||'')+' · 数据截止 '+(d.as_of||'—')+' · 耗时 '+(d.elapsed_ms||0)+'ms</div>';
  if(pos){
    h+='<div class="sub" style="font-size:11px;margin-top:4px">📐 '+esc(pos.basis||'')+
       ' · 止损参考 -'+fmt(pos.stop_loss_pct,1)+'%'+
       (pos.suggested_pct>0?'':' · 若转为买入仓位上限 '+fmt(pos.cap_pct,1)+'%')+'</div>';
  }
  // 主体：雷达图 + 维度条
  h+='<div class="grid g2" style="margin-top:16px">';
  h+='<div><div class="sub" style="font-weight:600;margin-bottom:6px">多维信号雷达（'+((d.dimensions||[]).length||12)+'维，50=中性）</div><div id="decisionRadar" style="height:320px"></div></div>';
  h+='<div><div class="sub" style="font-weight:600;margin-bottom:6px">各维度得分与权重</div><div id="decisionDims"></div></div>';
  h+='</div>';
  // 买卖信号清单
  const buy=d.buy_signals||[], sell=d.sell_signals||[];
  h+='<div class="grid g2" style="margin-top:16px">';
  h+='<div><div class="sub" style="color:var(--up);font-weight:600;margin-bottom:6px">偏多信号（'+buy.length+'）</div>'+
     (buy.length?buy.map(x=>'<div style="font-size:12px;padding:5px 0;border-bottom:1px solid var(--surface-2)">• '+esc(x.text)+' <span class="sub" style="font-size:10px">['+esc(x.source)+']</span></div>').join(''):'<div class="sub">无</div>')+'</div>';
  h+='<div><div class="sub" style="color:var(--down);font-weight:600;margin-bottom:6px">偏空信号（'+sell.length+'）</div>'+
     (sell.length?sell.map(x=>'<div style="font-size:12px;padding:5px 0;border-bottom:1px solid var(--surface-2)">• '+esc(x.text)+' <span class="sub" style="font-size:10px">['+esc(x.source)+']</span></div>').join(''):'<div class="sub">无</div>')+'</div>';
  h+='</div>';
  // 增强/减弱归因
  const en=d.enhance||[], we=d.weaken||[];
  if(en.length||we.length){
    h+='<div style="margin-top:14px;padding:12px;background:var(--surface-2);border-radius:8px">';
    if(en.length) h+='<div style="font-size:12px;margin-bottom:4px"><span style="color:var(--up);font-weight:600">信号增强</span>：'+esc(en.join('、'))+'（这些维度近期在改善）</div>';
    if(we.length) h+='<div style="font-size:12px"><span style="color:var(--down);font-weight:600">信号减弱</span>：'+esc(we.join('、'))+'（这些维度近期在恶化）</div>';
    h+='</div>';
  }
  // 风险
  if((d.risks||[]).length){
    h+='<div style="margin-top:12px;padding:12px;border:1px solid #f0d9a0;background:#fffaf0;border-radius:8px">';
    h+='<div class="sub" style="color:#a86a00;font-weight:600;margin-bottom:4px">风险提示</div>';
    h+=d.risks.map(r=>'<div style="font-size:12px;color:#8a5a00">⚠ '+esc(r)+'</div>').join('');
    h+='</div>';
  }
  h+='<div class="sub" style="font-size:10px;margin-top:12px;line-height:1.6">'+esc(d.disclaimer||'')+'</div>';
  body.innerHTML=h;
  // 维度条
  const dims=d.dimensions||[];
  document.getElementById('decisionDims').innerHTML=dims.map(x=>{
    const v=x.score, col=v==null?'var(--hint)':(v>=62?'var(--up)':(v<=38?'var(--down)':'var(--muted)'));
    const pct=v==null?0:v;
    const trend=x.trend==='improving'?'↑':(x.trend==='weakening'?'↓':'');
    return '<div style="margin-bottom:8px">'+
      '<div class="row" style="justify-content:space-between;font-size:11px;margin-bottom:2px">'+
      '<span>'+esc(x.label)+' <span class="sub" style="font-size:10px">w='+(x.weight*100).toFixed(0)+'%</span> '+trend+'</span>'+
      '<span style="color:'+col+';font-weight:600">'+(v==null?'—':fmt(v,0))+'</span></div>'+
      '<div style="height:5px;background:var(--border);border-radius:3px;overflow:hidden">'+
      '<div style="height:100%;width:'+pct+'%;background:'+col+';transition:width .4s"></div></div>'+
      (x.detail?'<div class="sub" style="font-size:10px;margin-top:2px">'+esc(x.detail)+'</div>':'')+
      '</div>';
  }).join('');
  // 雷达图
  try{
    if(decisionChart){ decisionChart.dispose(); }
    decisionChart=echarts.init(document.getElementById('decisionRadar'));
    const usable=dims.filter(x=>x.score!=null);
    decisionChart.setOption({
      tooltip:{},
      radar:{indicator:dims.map(x=>({name:x.label,max:100})),radius:'65%',
        axisName:{fontSize:11,color:'#6b7480'},
        splitLine:{lineStyle:{color:'#e5e8ec'}},splitArea:{areaStyle:{color:['#fafbfc','#fff']}},
        axisLine:{lineStyle:{color:'#e5e8ec'}}},
      series:[{type:'radar',data:[{
        value:dims.map(x=>x.score==null?50:x.score),name:'维度得分',
        areaStyle:{color:'rgba(37,99,235,.18)'},lineStyle:{color:'#2563eb',width:2},
        itemStyle:{color:'#2563eb'},symbolSize:4
      }]}]
    });
  }catch(e){}
  // 重算今日按钮
  const rb=document.getElementById('decisionRefresh');
  if(rb) rb.onclick=()=>{
    const code=(document.getElementById('stockSearch')&&document.getElementById('stockSearch').value||'').trim().match(/\d{6}/);
    if(!code) return;
    rb.disabled=true; rb.textContent='重算中…';
    api('/api/decision/'+code[0]+'?refresh=1').then(nd=>{
      // api() 已解包为决策对象本身（含 ok 字段）
      if(nd){ renderDecision(nd); if(decisionHistLoaded) loadDecisionAccuracy(code[0],true); }
    }).catch(()=>{}).finally(()=>{ rb.disabled=false; rb.textContent='↻ 重算今日'; });
  };
  // 历史账本折叠交互（绑定一次）
  bindDecisionHistoryToggle();
}
window.addEventListener('resize',()=>{ if(decisionChart) try{decisionChart.resize();}catch(e){} });

/* ===== 每日快照账本与准确性验证 ===== */
let decisionHistLoaded=false, decisionHistCode='', decisionHistSeq=0;
function bindDecisionHistoryToggle(){
  const t=document.getElementById('decisionHistoryToggle');
  if(!t||t._bound) return;
  t._bound=true;
  t.onclick=()=>{
    const body=document.getElementById('decisionHistoryBody');
    const arrow=document.getElementById('decisionHistoryArrow');
    if(!body) return;
    const open=body.style.display!=='none';
    body.style.display=open?'none':'block';
    arrow.textContent=open?'▾':'▴';
    if(!open && decisionHistCode) loadDecisionAccuracy(decisionHistCode);
  };
}
function loadDecisionHistory(code){
  decisionHistCode=code; decisionHistLoaded=false;
  const body=document.getElementById('decisionHistoryBody');
  const arrow=document.getElementById('decisionHistoryArrow');
  if(body){ body.style.display='none'; if(arrow) arrow.textContent='▾'; }
  bindDecisionHistoryToggle();
}
function loadDecisionAccuracy(code,force){
  const seq=++decisionHistSeq;
  const body=document.getElementById('decisionHistoryBody');
  if(!body) return;
  body.innerHTML='<div class="sub" style="padding:14px 0">正在用真实走势验证历史快照与信号…</div>';
  api('/api/decision/accuracy/'+encodeURIComponent(code)+(force?'?force=1':'')).then(data=>{
    if(seq!==decisionHistSeq) return;
    // api() 成功时已解包为 data，失败走 catch（保留 data.ok 兼容后端内层结构）
    if(data&&data.ok===false){ body.innerHTML='<div class="empty">验证失败：'+esc(data.error||data.message||'数据不可用')+'</div>'; return; }
    renderDecisionAccuracy(data||{});
    decisionHistLoaded=true;
  }).catch(e=>{
    if(seq!==decisionHistSeq) return;
    body.innerHTML='<div class="empty">验证失败：'+esc((e&&e.message)||'接口无响应')+'</div>';
  });
}
function _pctTag(v){
  if(v==null) return '<span class="sub">—</span>';
  const col=v>0?'var(--up)':(v<0?'var(--down)':'var(--muted)');
  return '<span style="color:'+col+';font-weight:600">'+(v>0?'+':'')+fmt(v,2)+'%</span>';
}
function renderDecisionAccuracy(D){
  const body=document.getElementById('decisionHistoryBody');
  const ss=D.snapshot_stats||{}, bt=D.backtest||{}, bs=bt.stats||{};
  let h='';
  // ---- 第一部分：历史信号回放（样本多，快速检验择时逻辑）----
  h+='<div class="sub" style="font-weight:600;margin-bottom:8px">A. 历史信号回放 · 择时逻辑检验</div>';
  h+='<div class="sub" style="font-size:11px;margin-bottom:10px;line-height:1.6">在本地日线上每隔几个交易日重放一次「技术面+量能」信号，用其后的真实涨跌检验方向是否判对。'+
     '口径：买入后涨 / 卖出后跌 = 命中；主看 T+5。'+(bt.cached?'（缓存结果）':'')+'</div>';
  if(bt.error||!bs.total){
    h+='<div class="empty" style="padding:12px">'+esc(bt.error||'暂无回放样本')+'</div>';
  }else{
    const acc=bs.accuracy;
    h+='<div class="grid g4" style="margin-bottom:12px">';
    h+='<div style="text-align:center;padding:10px;background:var(--surface-2);border-radius:8px">'+
       '<div style="font-size:24px;font-weight:800;color:'+(acc!=null&&acc>=55?'var(--up)':(acc!=null&&acc<45?'var(--down)':'var(--muted)'))+'">'+
       (acc!=null?acc+'%':'—')+'</div><div class="sub" style="font-size:10px">方向命中率（T+5）</div></div>';
    h+='<div style="text-align:center;padding:10px;background:var(--surface-2);border-radius:8px">'+
       '<div style="font-size:24px;font-weight:800">'+bs.directional_n+'</div><div class="sub" style="font-size:10px">可验证信号数</div></div>';
    const gb=bs.groups||{}, buy=gb.buy||{}, sell=gb.sell||{};
    h+='<div style="text-align:center;padding:10px;background:var(--surface-2);border-radius:8px">'+
       '<div style="font-size:24px;font-weight:800;color:var(--up)">'+(buy.hit_rate!=null?buy.hit_rate+'%':'—')+'</div>'+
       '<div class="sub" style="font-size:10px">买入命中 ('+(buy.n||0)+'次)</div></div>';
    h+='<div style="text-align:center;padding:10px;background:var(--surface-2);border-radius:8px">'+
       '<div style="font-size:24px;font-weight:800;color:var(--down)">'+(sell.hit_rate!=null?sell.hit_rate+'%':'—')+'</div>'+
       '<div class="sub" style="font-size:10px">卖出命中 ('+(sell.n||0)+'次)</div></div>';
    h+='</div>';
    // 评分区分度
    const bks=bs.score_buckets||[];
    if(bks.length){
      h+='<div class="sub" style="font-size:11px;margin-bottom:6px">评分区分度（信号分档 → 其后 T+5 平均收益）</div>';
      h+='<div class="grid g3" style="margin-bottom:14px">';
      bks.forEach(b=>{
        h+='<div style="padding:10px;border:1px solid var(--border);border-radius:8px">'+
           '<div class="sub" style="font-size:11px">'+esc(b.label)+' · '+b.n+'个</div>'+
           '<div style="font-size:18px;font-weight:700;margin-top:4px">'+_pctTag(b.avg_ret)+'</div>'+
           '<div class="sub" style="font-size:10px;margin-top:2px">上涨占比 '+(b.win_rate!=null?b.win_rate+'%':'—')+'</div></div>';
      });
      h+='</div>';
    }
  }
  // ---- 第二部分：每日快照账本 ----
  h+='<div class="row" style="justify-content:space-between;align-items:center;margin:6px 0 8px">'+
     '<div class="sub" style="font-weight:600">B. 每日快照账本 · 实际战绩</div>'+
     '<div class="sub" style="font-size:10px">共 '+(ss.total||0)+' 份快照，随时间自动累积并用真实走势回填</div></div>';
  const snaps=D.snapshots||[];
  if(!snaps.length){
    h+='<div class="empty" style="padding:12px">暂无快照。查看个股决策时会自动固化当日快照。</div>';
  }else{
    h+='<div style="overflow-x:auto"><table style="width:100%;font-size:11px;border-collapse:collapse">';
    h+='<thead><tr style="color:var(--muted);text-align:left;border-bottom:1px solid var(--border)">'+
       '<th style="padding:6px 4px">交易日</th><th>动作</th><th>综合分</th><th>置信</th>'+
       '<th>T+1</th><th>T+3</th><th>T+5</th><th>T+10</th></tr></thead><tbody>';
    snaps.forEach(s=>{
      const a=s.action||{}, tone=a.tone||'neutral', col=DEC_TONE[tone]||'var(--muted)';
      const fw=s.forward||{};
      h+='<tr style="border-bottom:1px solid var(--surface-2)">'+
         '<td style="padding:6px 4px;white-space:nowrap">'+esc(s.as_of||'—')+'</td>'+
         '<td style="color:'+col+';font-weight:600;white-space:nowrap">'+esc(a.label||'—')+'</td>'+
         '<td>'+(s.composite==null?'—':fmt(s.composite,1))+'</td>'+
         '<td>'+(s.confidence==null?'—':s.confidence)+'</td>'+
         '<td>'+_pctTag(fw.d1)+'</td><td>'+_pctTag(fw.d3)+'</td>'+
         '<td>'+_pctTag(fw.d5)+'</td><td>'+_pctTag(fw.d10)+'</td></tr>';
    });
    h+='</tbody></table></div>';
    h+='<div class="sub" style="font-size:10px;margin-top:8px;line-height:1.6">未来收益在交易日尚未到来时显示「—」，随日线更新自动回填。'+
       '本回测为机械化历史验证，不构成投资建议；历史命中不代表未来表现。</div>';
  }
  body.innerHTML=h;
}
