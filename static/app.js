const $ = id => document.getElementById(id);
let data = [], sum = {}, filter = 'all', sel = new Set(), lastEvent = 0, firstPoll = true;
let previews = [], pickedFiles = [], detId = null, detTab = 'info', detTimer = null;
const VIDEO = /\.(mp4|mkv|avi|mov|wmv|flv|webm|m4v|ts|rmvb|rm|mpg|mpeg|m2ts)$/i;

/* ---------- 工具 ---------- */
function fmtB(b){if(b==null||b<0)return'-';const u=['B','KB','MB','GB','TB'];let i=0;while(b>=1024&&i<4){b/=1024;i++}return(i?b.toFixed(b<10?2:1):b)+' '+u[i]}
const fmtR = b => fmtB(b)+'/s';
const LANG=(window.I18N&&I18N.lang)||'zh-CN', EN=LANG==='en';
function fmtT(s){if(s==null)return'∞';s=Math.round(s);
  const U=EN?['s','m','h','d']:LANG==='zh-TW'?['秒','分','小時','天']:['秒','分','小时','天'], sp=EN?' ':'';
  if(s<60)return s+U[0];if(s<3600)return Math.floor(s/60)+U[1]+sp+(s%60)+U[0];if(s<86400)return Math.floor(s/3600)+U[2]+sp+Math.floor(s%3600/60)+U[1];return Math.floor(s/86400)+U[3]+sp+Math.floor(s%86400/3600)+U[2]}
function fmtD(t){if(!t||t<=0)return'-';const d=new Date(t*1000);return d.toLocaleString(LANG,{hour12:false})}
const esc = s => String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(t){const d=document.createElement('div');d.textContent=t;$('toast').appendChild(d);setTimeout(()=>d.remove(),3500)}
async function api(p,b){const r=await fetch(p,b===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});if(r.status===401){location.href='/login';return{}}return r.json()}
function closeM(id){$(id).classList.remove('on');if(id==='m-det'){detId=null;clearInterval(detTimer)}}
function openM(id){$(id).classList.add('on')}
document.querySelectorAll('.mask').forEach(m=>m.addEventListener('mousedown',e=>{if(e.target===m)closeM(m.id)}));
document.addEventListener('keydown',e=>{
  if(e.key==='Escape')document.querySelectorAll('.mask.on').forEach(m=>closeM(m.id));
  if(e.key==='Delete'&&sel.size&&!document.querySelector('.mask.on')&&!e.target.closest('input,textarea'))askRemove();
  if((e.ctrlKey||e.metaKey)&&e.key==='a'&&!e.target.closest('input,textarea')&&!document.querySelector('.mask.on')){e.preventDefault();data.filter(pass).forEach(t=>sel.add(t.id));render()}
});

/* ---------- 列表 ---------- */
const isDone = t => t.health.level==='done';
const isBad = t => t.health.level==='red'||t.health.level==='yellow';
function pass(t){
  const q=$('q').value.trim().toLowerCase();
  if(q&&!t.name.toLowerCase().includes(q))return false;
  const c=$('catFilter').value; if(c&&t.category!==c)return false;
  return {all:true,dl:!isDone(t),done:isDone(t),bad:isBad(t),http:t.type==='http'}[filter];
}
function render(){
  $('c-all').textContent=data.length||'';
  $('c-dl').textContent=data.filter(t=>!isDone(t)).length||'';
  $('c-done').textContent=data.filter(isDone).length||'';
  $('c-bad').textContent=data.filter(isBad).length||'';
  $('c-http').textContent=data.filter(t=>t.type==='http').length||'';
  for(const id of [...sel]) if(!data.find(t=>t.id===id)) sel.delete(id);
  ['b-resume','b-pause','b-del','b-up','b-down'].forEach(b=>$(b).disabled=!sel.size);
  { // 全选框：只管当前分类 / 搜索下看得到的任务
    const vis=data.filter(pass), n=vis.filter(t=>sel.has(t.id)).length, cb=$('selAll');
    cb.checked=vis.length>0&&n===vis.length; cb.indeterminate=n>0&&n<vis.length;
    $('selN').textContent=sel.size?`已选 ${sel.size}`:'';
  }
  const rows=data.filter(pass).sort(cmpRows);
  renderHead();
  if(!rows.length){
    $('list').innerHTML=data.length?'<div class="empty">这个分类下没有任务</div>':
      `<div class="empty"><img src="/static/logo.png" width="96" alt=""><p style="font-size:16px;color:var(--text)">还没有下载任务</p>
       <p>点「添加下载」，或者直接把种子文件拖进来<br>复制一个磁力链也会自动弹出</p></div>`;
    return;
  }
  $('list').innerHTML=rows.map(rowHtml).join('');
}
function rowHtml(t){
  const pct=(t.progress*100).toFixed(1), done=isDone(t);
  const cls=done?'done':t.error?'err':(t.paused||t.queued)?'paused':'';
  let meta;
  if(t.need_select) meta=`<span>先挑要哪些文件，再开始下</span>`;
  else if(done) meta=`<span>大小 <b>${fmtB(t.size)}</b></span>${t.type==='bt'?`<span>已上传 <b>${fmtB(t.uploaded)}</b></span><span>分享率 <b>${t.ratio}</b></span>`:''}<span>${t.state}</span>`;
  else meta=`<span><b>${pct}%</b> · ${t.size?fmtB(t.done)+' / '+fmtB(t.size):'大小未知'}</span>
    ${t.type==='bt'?`<span title="已连上 / 全网">做种 <b>${t.seeds}</b>/${t.seeds_total}</span><span>下载者 <b>${t.peers}</b>/${t.peers_total}</span>
    ${t.availability!=null?`<span title="线上能凑出几份完整文件，低于 1 就可能下不完">可用性 <b>${t.availability.toFixed(2)}</b></span>`:''}`:`<span>${t.threads||0} 线程</span>`}<span>${t.state}</span>`;
  let segs='';
  if(t.type==='http'&&!done&&t.segments&&t.segments.length>1&&t.size){
    segs='<div class="segs">'+t.segments.slice().sort((a,b)=>a[0]-b[0]).map(s=>{const len=s[1]-s[0]+1;const p=len>0?Math.min(100,(s[2]-s[0])/len*100):100;return`<i style="flex:${len}"><b style="width:${p}%"></b></i>`}).join('')+'</div>';
  }
  const acts=[];
  if(t.paused||t.queued) acts.push(`<button onclick="a1('resume','${t.id}')" title="开始">▶</button>`);
  else if(!done||t.type==='bt') acts.push(`<button onclick="a1('pause','${t.id}')" title="暂停">⏸</button>`);
  if(t.need_select) acts.push(`<button class="primary" onclick="openSelect('${t.id}')">📂 选文件</button>`);
  if(t.type==='bt'&&t.has_metadata) acts.push(`<button onclick="playBest('${t.id}')" title="边下边播">▶️ 播放</button>`);
  if(t.type==='bt'&&!done&&!t.need_select) acts.push(`<button onclick="openDet('${t.id}','diag')" title="为什么慢 / 为什么不动">🩺 诊断</button>`);
  if(t.type==='bt') acts.push(`<button onclick="openDet('${t.id}','info')">详情</button>`);
  if(t.type==='http'&&t.error) acts.push(`<button onclick="a1('resume','${t.id}')">重试</button>`);
  acts.push(`<button onclick="a1('open','${t.id}')" title="打开所在文件夹">📂</button>`);
  return `<div class="row ${sel.has(t.id)?'sel':''} ${t.need_select?'need':''}" data-id="${t.id}" ondblclick="openDet('${t.id}','info')">
    <input type="checkbox" ${sel.has(t.id)?'checked':''} onclick="tog('${t.id}',event)">
    <div style="min-width:0">
      <div class="name" title="${esc(t.name)}">${t.type==='http'?'<span class="badge http">直链</span>':''}${t.category?`<span class="badge cat">${esc(t.category)}</span>`:''}${t.sequential?'<span class="badge" title="顺序下载中">顺序</span>':''}<span class="t">${esc(t.name)}</span></div>
      <div class="meta">${meta}</div>
      <div class="prog ${cls}"><i style="width:${pct}%"></i></div>${segs}
      <div class="pills"><div class="health h-${t.health.level}"><span class="dot"></span><span>${esc(t.health.text)}</span></div>${seedPill(t)}</div>
      ${t.error&&t.type==='bt'?`<div class="err">出错：${esc(t.error)}</div>`:''}
    </div>
    <div class="eta">${etaHtml(t,done)}</div>
    <div class="spd">${spdHtml(t,done)}</div>
    <div class="acts">${acts.join('')}</div></div>`;
}
// 下完后还在做种：显示「正在做种」以及还要做多久
function seedPill(t){
  const sd=t.seed; if(!sd)return'';
  // 时间、数字单独放一个元素，方便多语言翻译
  let tail='';
  if(sd.why==='own') tail='<span>自己分享的，一直做种</span>';
  else if(sd.left!=null) tail=`<span>还剩</span> <b>${fmtT(sd.left)}</b>`+(sd.ratio_target?` <span>或分享率到</span> <b>${sd.ratio_target}</b>`:'');
  else if(sd.ratio_target) tail=`<span>分享率到</span> <b>${sd.ratio_target}</b> <span>停止</span>`;
  else tail='<span>一直做种</span>';
  return `<div class="health h-seed"><span class="dot"></span><span>正在做种</span> · ${tail}</div>`;
}
// 剩余时间：放在速度左边（迅雷 / qBittorrent 都是把剩余时间和速度挨着放）
function etaHtml(t,done){
  if(done) return `<div class="v ok">✓</div><div class="l">已完成</div>`;
  if(t.need_select) return `<div class="v z">—</div><div class="l">等你选文件</div>`;
  if(t.error) return `<div class="v z">—</div><div class="l">出错</div>`;
  if(t.paused) return `<div class="v z">—</div><div class="l">已暂停</div>`;
  if(t.queued) return `<div class="v z">—</div><div class="l">排队中</div>`;
  const v=t.eta==null?'∞':fmtT(t.eta);
  return `<div class="v ${t.eta==null?'z':''}">${v}</div><div class="l">剩余</div>`;
}
// 行右侧醒目的速度：没在动就显示灰色的 0，避免整列忽有忽无
function spdHtml(t,done){
  const idle=t.paused||t.queued;
  const d=done?'':`<div class="d ${t.down>0?'':'z'}"><span class="ar">⬇</span>${idle?'—':fmtR(t.down)}</div>`;
  const u=t.type==='bt'?`<div class="u ${t.up>0?'':'z'}"><span class="ar">⬆</span>${idle?'—':fmtR(t.up)}</div>`:'';
  return d+u;
}
/* 排序：点表头切换，再点一次反过来；记在本机 */
let sortK='added', sortD=-1;
try{ const sv=JSON.parse(localStorage.getItem('koi_sort')||'null'); if(sv){sortK=sv.k;sortD=sv.d} }catch(e){}
const SORT_VAL={
  name:t=>t.name.toLowerCase(), progress:t=>t.progress, size:t=>t.size||0, added:t=>t.added||0,
  eta:t=>isDone(t)?Infinity:(t.paused||t.queued||t.eta==null)?Infinity-1:t.eta,   // 快下完的排前面，没在动的排后面
  down:t=>(t.down||0)*1e6+(t.up||0),                                             // 先按下载速度，再按上传速度
  // 完成时间：已完成的用实际完成时间，没下完的用「现在 + 剩余时间」预计完成时间，没在动的排最后
  done:t=>isDone(t)?(t.completed||t.added||0):(t.paused||t.queued||t.eta==null)?Infinity:Date.now()/1000+t.eta,
};
function cmpRows(a,b){ const f=SORT_VAL[sortK]||SORT_VAL.added, x=f(a), y=f(b); return (x<y?-1:x>y?1:0)*sortD || (b.added-a.added) }
function setSort(k){ if(sortK===k) sortD=-sortD; else { sortK=k; sortD=(k==='name'||k==='eta'||k==='done')?1:-1 } try{localStorage.setItem('koi_sort',JSON.stringify({k:sortK,d:sortD}))}catch(e){} render() }
function renderHead(){ document.querySelectorAll('#listhead .lh').forEach(b=>{ const on=b.dataset.k===sortK; b.classList.toggle('on',on); b.dataset.arrow=on?(sortD>0?' ▲':' ▼'):'' }) }
document.querySelectorAll('#listhead .lh').forEach(b=>b.onclick=()=>setSort(b.dataset.k));
function toggleAll(on){ const vis=data.filter(pass); if(on) vis.forEach(t=>sel.add(t.id)); else vis.forEach(t=>sel.delete(t.id)); render() }
function tog(id,e){e.stopPropagation();sel.has(id)?sel.delete(id):sel.add(id);render()}
document.addEventListener('click',e=>{const r=e.target.closest('.row');if(r&&!e.target.closest('button,input')){const id=r.dataset.id;if(e.shiftKey&&sel.size){const ids=data.filter(pass).map(t=>t.id);const a=ids.indexOf([...sel].pop()),b=ids.indexOf(id);ids.slice(Math.min(a,b),Math.max(a,b)+1).forEach(x=>sel.add(x))}else if(!e.ctrlKey&&!e.metaKey){const only=sel.size===1&&sel.has(id);sel.clear();if(!only)sel.add(id)}else{sel.has(id)?sel.delete(id):sel.add(id)}render()}});
async function act(a){await api('/api/action',{action:a,ids:[...sel]});poll1()}
async function a1(a,id){await api('/api/action',{action:a,ids:[id]});poll1()}
async function allAct(a){await api('/api/action',{action:a,ids:data.map(t=>t.id)});poll1()}
document.querySelectorAll('#tabs .tab').forEach(b=>b.onclick=()=>{document.querySelectorAll('#tabs .tab').forEach(x=>x.classList.remove('on'));b.classList.add('on');filter=b.dataset.f;render()});
$('q').oninput=render; $('catFilter').onchange=render;

/* ---------- 轮询 / 事件 ---------- */
async function poll1(){
  const r=await api('/api/state?since='+lastEvent); if(!r.list)return;
  data=r.list; sum=r.summary; render(); renderSum();
  for(const e of r.events){ lastEvent=Math.max(lastEvent,e.id); if(!firstPoll) onEvent(e) }
  lastEvent=Math.max(lastEvent,sum.event_id||0); firstPoll=false;
}
async function poll(){try{await poll1()}catch(e){} setTimeout(poll,1000)}
function onEvent(e){
  if(e.kind==='clipboard'){ if(!$('m-add').classList.contains('on')) openAdd(e.body); else { const v=$('a-links').value; if(!v.includes(e.body)) $('a-links').value=(v?v+'\n':'')+e.body } return }
  if(e.kind==='select'){ if(!document.querySelector('.mask.on')) openSelect(e.body); else toast('有任务等你选文件'); return }
  if(e.kind==='done'){ toast('✅ 下载完成：'+e.body); return }
  if(e.kind==='all_done'){ toast('🎉 全部下载完成'); return }
  if(e.kind==='update'){ updDismissed=false; renderUpdate(); return }
  if(e.kind==='error'){ toast('⚠️ '+e.body); return }
  toast(e.title+(e.body?'：'+e.body:''));
}
function renderSum(){
  $('s-down').textContent=fmtR(sum.down); $('s-up').textContent=fmtR(sum.up);
  const n=sum.net||{};
  $('s-port').innerHTML=n.cgnat?`<span class="bad" title="运营商给你的不是独立公网 IP，别人连不进你">⚠ 运营商内网（CGNAT），别人连不进你</span>`
    :n.incoming?'<span class="ok" title="端口是通的">✔ 端口畅通</span>'
    :`<span title="端口映射：${esc(n.upnp)}">端口：${n.upnp&&n.upnp.startsWith('失败')?'<span class="warn">映射失败，观察中</span>':'观察中'}</span>`;
  $('s-dht').textContent=`DHT ${sum.dht_nodes??'-'} 节点 · tracker ${sum.trackers}`;
  $('s-ban').innerHTML=sum.banned?`🛡 已屏蔽吸血 <b>${sum.banned}</b>`:'🛡 吸血屏蔽开启';
  if(!sum.settings.anti_leech)$('s-ban').textContent='';
  $('s-ver').textContent='v'+sum.version;
  renderUpdate(); showJustUpdated();
  $('afterAll').value=sum.settings.after_all_done;
  $('b-alt').classList.toggle('on',!!sum.alt_active);
  $('b-alt').textContent=sum.alt_active?'🐢 乌龟模式中':'🐢 乌龟模式';
  const cats=Object.keys(sum.categories||{}); const cf=$('catFilter'); const cur=cf.value;
  const html='<option value="">全部分类</option>'+cats.map(c=>`<option ${c===cur?'selected':''}>${esc(c)}</option>`).join('');
  if(cf.dataset.h!==html){cf.innerHTML=html;cf.dataset.h=html}
  cf.style.display=cats.length?'':'none';
  spark(sum.history||[]);
}
function spark(h){
  const c=$('spark'); if(!c.offsetWidth)return; const dpr=devicePixelRatio||1;
  c.width=c.offsetWidth*dpr; c.height=c.offsetHeight*dpr; const g=c.getContext('2d'); g.scale(dpr,dpr);
  const W=c.offsetWidth,H=c.offsetHeight; const max=Math.max(1,...h.map(x=>x[1]),...h.map(x=>x[2]));
  const line=(k,col)=>{g.beginPath();h.forEach((p,i)=>{const x=i/(Math.max(1,h.length-1))*W,y=H-2-(p[k]/max)*(H-4);i?g.lineTo(x,y):g.moveTo(x,y)});g.strokeStyle=col;g.lineWidth=1.5;g.stroke()};
  line(2,'#22c55e88'); line(1,getComputedStyle(document.documentElement).getPropertyValue('--accent'));
}
async function toggleAlt(){await saveOne('alt_enabled',!sum.settings.alt_enabled);toast(sum.settings.alt_enabled?'已关闭乌龟模式':'已开启乌龟模式')}
async function saveOne(k,v){await api('/api/settings',{[k]:v});poll1()}


/* ---------- 文件选择器（添加预览 / 选文件弹窗 / 详情页共用）：勾选、排序、筛选 ---------- */
const EXT = p => { const m=/\.([a-z0-9]{1,6})$/i.exec(p); return m?m[1].toLowerCase():'' };
const KIND = p => { const e=EXT(p);
  if(VIDEO.test(p))return'视频'; if(/^(srt|ass|ssa|sub|idx|vtt|sup)$/.test(e))return'字幕';
  if(/^(mp3|flac|wav|aac|m4a|ape|ogg|opus|dts|ac3)$/.test(e))return'音频'; if(/^(jpg|jpeg|png|gif|webp|bmp)$/.test(e))return'图片';
  if(/^(zip|rar|7z|tar|gz|xz|iso|img)$/.test(e))return'压缩/镜像'; if(/^(txt|nfo|pdf|doc|docx|md|url|html?)$/.test(e))return'文档';
  if(/^(exe|msi|apk|dmg)$/.test(e))return'程序'; return e?e.toUpperCase():'其他' };
const pickers = {};
class FilePicker {
  /* 文件树：按文件夹折叠、文件夹整体勾选（三态）、点表头排序。勾选只改数据，重画时保持滚动位置。 */
  constructor(key, el, files, opt={}) {
    this.key=key; this.opt=opt; this.files=files;
    this.checked={}; this.prio={}; this.closed=new Set();
    files.forEach(f=>{ this.checked[f.index]=opt.allChecked?true:(f.priority===undefined||f.priority>0); this.prio[f.index]=f.priority>0?f.priority:4 });
    this.sort={k:'name',d:1};
    this.buildTree();
    // 文件很多时默认只展开第一层
    if(files.length>60) this.allFolders(this.root).forEach(n=>{ if(n.depth>=1) this.closed.add(n.path) });
    pickers[key]=this; if(el) this.attach(el);
  }
  buildTree(){
    const parts=this.files.map(f=>f.path.split('/'));
    // 所有文件都在同一个顶层文件夹里时（种子名），把它去掉，少一层
    const strip=parts.length>1&&parts.every(p=>p.length>1&&p[0]===parts[0][0])?1:0;
    const root={name:'',path:'',dirs:{},files:[],depth:-1};
    this.files.forEach((f,i)=>{
      const p=parts[i].slice(strip); let n=root;
      for(let j=0;j<p.length-1;j++){ const path=p.slice(0,j+1).join('/'); n=n.dirs[p[j]]||(n.dirs[p[j]]={name:p[j],path,dirs:{},files:[],depth:j}); }
      n.files.push(Object.assign({},f,{name:p[p.length-1]}));
    });
    this.root=root;
  }
  allFolders(n,out=[]){ Object.values(n.dirs).forEach(d=>{out.push(d);this.allFolders(d,out)}); return out }
  filesIn(n,out=[]){ out.push(...n.files); Object.values(n.dirs).forEach(d=>this.filesIn(d,out)); return out }
  stat(n){ const fs=this.filesIn(n); let c=0,sz=0,tot=0; fs.forEach(f=>{tot+=f.size; if(this.checked[f.index]){c++;sz+=f.size}}); return {c,n:fs.length,sz,tot} }
  attach(el){ this.el=el; this.render() }
  setSort(k){ this.sort = this.sort.k===k?{k,d:-this.sort.d}:{k,d:k==='size'?-1:1}; this.render() }
  apply(fn){ this.files.forEach(f=>this.checked[f.index]=fn(f)); this.render(); this.opt.onChange&&this.opt.onChange() }
  toggle(i,v){ this.checked[i]=v; this.render(); this.opt.onChange&&this.opt.onChange() }
  toggleDir(path,v){ const n=this.allFolders(this.root).find(d=>d.path===path); this.filesIn(n).forEach(f=>this.checked[f.index]=v); this.render(); this.opt.onChange&&this.opt.onChange() }
  fold(path){ this.closed.has(path)?this.closed.delete(path):this.closed.add(path); this.render() }
  total(){ let n=0,s=0; this.files.forEach(f=>{if(this.checked[f.index]){n++;s+=f.size}}); return [n,s] }
  priorities(n){ const pr=new Array(n).fill(0); this.files.forEach(f=>pr[f.index]=this.checked[f.index]?(this.opt.prio?this.prio[f.index]:4):0); return pr }
  rows(n,depth,out){
    const {k,d}=this.sort, key=x=>k==='size'?(x.size??this.stat(x).tot):k==='kind'?(x.files?'':KIND(x.name)+EXT(x.name)):(x.name||'').toLowerCase();
    const cmp=(a,b)=>{const x=key(a),y=key(b);return (x<y?-1:x>y?1:0)*d};
    Object.values(n.dirs).sort(cmp).forEach(dir=>{ out.push({dir,depth}); if(!this.closed.has(dir.path)) this.rows(dir,depth+1,out) });
    n.files.slice().sort(cmp).forEach(f=>out.push({f,depth}));
    return out;
  }
  render(){
    if(!this.el)return;
    const K=this.key, arrow=k=>this.sort.k===k?(this.sort.d>0?' ▲':' ▼'):'';
    const old=this.el.querySelector('.ftree'), scroll=old?old.scrollTop:0;
    const [n,s]=this.total(), pad=dp=>`padding-left:${6+dp*18}px`;
    const rows=this.rows(this.root,0,[]).map(r=>{
      if(r.dir){ const st=this.stat(r.dir), open=!this.closed.has(r.dir.path);
        return `<tr class="dir"><td><input type="checkbox" ${st.c===st.n?'checked':''} ${st.c&&st.c<st.n?'data-ind="1"':''} onchange="pickers['${K}'].toggleDir(${JSON.stringify(r.dir.path).replace(/"/g,'&quot;')},this.checked)"></td>
          <td style="${pad(r.depth)};cursor:pointer" onclick="pickers['${K}'].fold(${JSON.stringify(r.dir.path).replace(/"/g,'&quot;')})"><span class="tw">${open?'▾':'▸'}</span> 📁 <span class="notr">${esc(r.dir.name)}</span> <span class="sub small">${st.c}/${st.n}</span></td>
          <td></td><td style="white-space:nowrap">${fmtB(st.tot)}</td>${this.opt.progress?'<td></td>':''}${this.opt.prio?'<td></td>':''}${this.opt.play?'<td></td>':''}</tr>`; }
      const f=r.f;
      return `<tr><td><input type="checkbox" ${this.checked[f.index]?'checked':''} onchange="pickers['${K}'].toggle(${f.index},this.checked)"></td>
        <td style="${pad(r.depth)}" class="fname"><span class="notr" title="${esc(f.path)}">${esc(f.name)}</span></td><td class="small sub" style="white-space:nowrap">${KIND(f.name)}</td><td style="white-space:nowrap">${fmtB(f.size)}</td>
        ${this.opt.progress?`<td>${f.size?(f.done/f.size*100).toFixed(1):100}%</td>`:''}
        ${this.opt.prio?`<td><select style="width:auto;padding:2px" onchange="pickers['${K}'].prio[${f.index}]=+this.value"><option value="1" ${this.prio[f.index]<4?'selected':''}>低</option><option value="4" ${this.prio[f.index]==4?'selected':''}>普通</option><option value="7" ${this.prio[f.index]==7?'selected':''}>最高</option></select></td>`:''}
        ${this.opt.play?`<td>${VIDEO.test(f.name)?`<button style="padding:2px 8px" onclick="play('${this.opt.play}',${f.index})">▶️</button>`:''}</td>`:''}</tr>`;
    }).join('');
    this.el.innerHTML=`
     <div class="fp-bar">
       <span class="chips"><button onclick="pickers['${K}'].apply(()=>true)">全选</button><button onclick="pickers['${K}'].apply(()=>false)">全不选</button><button onclick="pickers['${K}'].apply(f=>VIDEO.test(f.path))">只要视频</button><button onclick="pickers['${K}'].apply(f=>VIDEO.test(f.path)||KIND(f.path)==='字幕')">视频+字幕</button><button onclick="pickers['${K}'].apply(f=>f.size>100*2**20)">只要 &gt;100MB</button></span>
       <span class="grow"></span><span class="sub small">已选 <b>${n}</b>/${this.files.length} 个，共 <b>${fmtB(s)}</b></span>
     </div>
     <div class="ftree" style="max-height:${this.opt.height||320}px"><table class="t ftbl">
      <tr><th style="width:28px"><input type="checkbox" ${n===this.files.length?'checked':''} ${n&&n<this.files.length?'data-ind="1"':''} title="全选 / 全不选" onchange="pickers['${K}'].apply(()=>this.checked)"></th>
        <th style="cursor:pointer" onclick="pickers['${K}'].setSort('name')">名称<span>${arrow('name')}</span></th>
        <th style="cursor:pointer;width:70px" onclick="pickers['${K}'].setSort('kind')">类型<span>${arrow('kind')}</span></th>
        <th style="cursor:pointer;width:90px" onclick="pickers['${K}'].setSort('size')">大小<span>${arrow('size')}</span></th>
        ${this.opt.progress?'<th style="width:60px">进度</th>':''}${this.opt.prio?'<th style="width:70px">优先级</th>':''}${this.opt.play?'<th style="width:44px"></th>':''}</tr>
      ${rows}</table></div>`;
    this.el.querySelectorAll('input[data-ind]').forEach(c=>c.indeterminate=true);
    this.el.querySelector('.ftree').scrollTop=scroll;
  }
}

/* ---------- 在线更新 ---------- */
let updDismissed=false, updAsked=null, updShown=false;
function updLater(){closeM('m-updready');toast('好的，退出 KOI 时会自动安装')}
async function updNow(){closeM('m-updready');await api('/api/update/apply',{});poll1()}
async function updAck(){closeM('m-updated');await api('/api/update/ack',{})}
function showJustUpdated(){
  const j=sum.just_updated; if(!j||updShown)return; updShown=true;
  $('ud-ver').textContent='v'+j.to; $('ud-notes').textContent=j.notes||''; $('ud-notes').style.display=j.notes?'':'none'; openM('m-updated');
}
function renderUpdate(){
  const u=sum.update||{}, lc=u.last_check||{}, st=u.state||{}, el=$('upd');
  if($('cur-ver'))$('cur-ver').textContent='v'+(u.current||sum.version);
  const busy=['downloading','unpacking','ready','applying'].includes(st.stage);
  if(!(lc.ok&&lc.update)&&!busy&&st.stage!=='error'){el.style.display='none';return}
  if(updDismissed&&!busy){el.style.display='none';return}
  el.style.display='flex';
  if(st.stage==='downloading'||st.stage==='unpacking'){
    const p=st.total?st.done/st.total*100:0;
    el.innerHTML=`⬇️ 正在下载新版本 ${esc(lc.version||'')}… <span class="bar"><i style="width:${p.toFixed(0)}%"></i></span> ${fmtB(st.done)} / ${fmtB(st.total)}${st.stage==='unpacking'?' · 解压中':''}`;
  } else if(st.stage==='ready'){
    el.innerHTML=`✅ <b>新版本 ${esc(lc.version||'')} 已下载好</b><span class="sub small">退出 KOI 时会自动安装</span><span class="grow"></span><button class="primary" onclick="updNow()">立即重启并更新</button>`;
    if(updAsked!==lc.version){updAsked=lc.version; $('ur-ver').textContent='v'+lc.version; $('ur-notes').textContent=lc.notes||''; $('ur-notes').style.display=lc.notes?'':'none'; openM('m-updready')}
  } else if(st.stage==='applying'){
    el.innerHTML='🔄 正在重启并安装新版本，大约 10 秒后自动打开…';
  } else if(st.stage==='error'){
    el.innerHTML=`⚠️ 更新失败：${esc(st.msg)} <button onclick="doUpdate()">重试</button><button class="ghost" onclick="updDismissed=true;renderUpdate()">✕</button>`;
  } else {
    el.innerHTML=`🎉 <b>发现新版本 ${esc(lc.version)}</b><span class="sub small">（当前 v${esc(lc.current)}，${fmtB(lc.size)}）</span><span class="notes">${esc(lc.notes||'')}</span>
      ${u.frozen?'<button class="primary" onclick="doUpdate()">⚡ 一键更新</button>':u.docker?`<span class="sub small">${esc(u.hint||'')}</span>`:'<span class="sub small">源码运行版不能自动更新</span>'}<button class="ghost" onclick="updDismissed=true;renderUpdate()" title="稍后再说">✕</button>`;
  }
}
async function checkUpdate(manual){
  if($('upd-msg'))$('upd-msg').textContent='检查中…';
  const r=await api('/api/update/check',{}); await poll1();
  const msg=!r.ok?(r.msg||'检查失败'):r.update?`发现新版本 ${r.version}`:`已经是最新版（v${r.current}）`;
  if($('upd-msg'))$('upd-msg').textContent=msg;
  if(manual){toast(msg); if(r.ok&&r.update){updDismissed=false;renderUpdate()}}
}
async function doUpdate(){updDismissed=false; await api('/api/update/start',{}); poll1()}

/* ---------- 添加 ---------- */
function fillCats(selId){const s=$(selId);const cur=s.value;s.innerHTML='<option value="">无</option>'+Object.keys(sum.categories||{}).map(c=>`<option ${c===cur?'selected':''}>${esc(c)}</option>`).join('')}
function openAdd(links=''){
  $('a-links').value=links; previews=[]; pickedFiles=[]; curPv=0; Object.keys(pickers).filter(k=>k.startsWith('pv')).forEach(k=>delete pickers[k]); renderPreview(); setTimeout(refreshDisk,50);
  $('a-path').value=sum.settings?.save_path||''; fillCats('a-cat');
  $('a-select').checked=!!sum.settings?.select_files_on_add; $('a-paused').checked=false;
  openM('m-add'); setTimeout(()=>$('a-links').focus(),50);
}
$('a-cat').onchange=()=>{const c=(sum.categories||{})[$('a-cat').value];if(c&&c.save_path)$('a-path').value=c.save_path};
$('a-drop').onclick=()=>$('a-file').click();
$('a-file').onchange=e=>pickTorrents([...e.target.files]);
['dragover','dragleave','drop'].forEach(ev=>$('a-drop').addEventListener(ev,e=>{e.preventDefault();e.stopPropagation();$('a-drop').classList.toggle('over',ev==='dragover');if(ev==='drop')pickTorrents([...e.dataTransfer.files])}));
async function pickTorrents(files){
  files=files.filter(f=>f.name.toLowerCase().endsWith('.torrent')); if(!files.length)return;
  pickedFiles=pickedFiles.concat(files);
  const fd=new FormData(); files.forEach(f=>fd.append('files',f,f.name));
  const r=await(await fetch('/api/preview',{method:'POST',body:fd})).json();
  previews=previews.concat(r); renderPreview();
}
let curPv=0, diskFree=null;
function pvInclude(i,v){ previews[i].include=v; renderPreview() }
function pvRemove(i){ previews.splice(i,1); pickedFiles.splice(i,1); Object.keys(pickers).filter(k=>k.startsWith('pv')).forEach(k=>delete pickers[k]);
  previews.forEach((p,j)=>{ if(p.picker){ p.picker.key='pv'+j; pickers['pv'+j]=p.picker } }); if(curPv>=previews.length)curPv=Math.max(0,previews.length-1); renderPreview() }
function pvShow(i){ curPv=i; renderPreview() }
function renderPreview(){
  const box=$('a-preview');
  $('m-add').querySelector('.dlg').classList.toggle('wide-add',previews.length>0);
  $('a-links').rows=previews.length?2:5;
  if(!previews.length){box.innerHTML='';updateAddSummary();return}
  previews.forEach((p,i)=>{ if(p.include===undefined)p.include=!p.error;
    if(!p.error&&!p.picker) p.picker=new FilePicker('pv'+i,null,p.files,{allChecked:true,height:230,onChange:()=>renderTaskRows()}) });
  box.innerHTML=`<div class="tasks-wrap"><table class="t tasks" id="a-tasks"></table></div><div id="a-files"></div>`;
  renderTaskRows();
  const p=previews[curPv];
  if(p&&!p.error){ $('a-files').innerHTML=`<div class="sub small" style="margin:10px 0 2px">📂 <b class="notr">${esc(p.name)}</b> 的文件</div><div id="a-picker"></div>`; p.picker.attach($('a-picker')) }
  else $('a-files').innerHTML='';
  updateAddSummary();
}
function renderTaskRows(){
  const t=$('a-tasks'); if(!t)return;
  t.innerHTML=`<tr><th style="width:28px"></th><th>种子（${previews.length} 个）</th><th style="width:110px">已选文件</th><th style="width:150px">大小</th><th style="width:34px"></th></tr>`+
    previews.map((p,i)=>{ if(p.error) return `<tr><td></td><td class="bad" colspan="3">${esc(p.filename)}：${esc(p.error)}</td><td><button class="ghost" onclick="pvRemove(${i})" title="移除">✕</button></td></tr>`;
      const [n,s]=p.picker.total();
      return `<tr class="${i===curPv?'cur':''} ${p.include?'':'off'}" onclick="if(!event.target.closest('input,button'))pvShow(${i})" style="cursor:pointer">
        <td><input type="checkbox" ${p.include?'checked':''} onchange="pvInclude(${i},this.checked)" title="是否添加这个种子"></td>
        <td class="tname"><span class="notr" title="${esc(p.name)}">${esc(p.name)}</span></td>
        <td>${n}/${p.files.length}</td><td style="white-space:nowrap">${fmtB(s)}${s!==p.size?` <span class="sub small">/ ${fmtB(p.size)}</span>`:''}</td>
        <td><button class="ghost" onclick="pvRemove(${i})" title="移除">✕</button></td></tr>` }).join('');
  updateAddSummary();
}
async function updateAddSummary(){
  const el=$('a-sum'); if(!el)return;
  const on=previews.filter(p=>!p.error&&p.include); let files=0,size=0;
  on.forEach(p=>{const [n,s]=p.picker.total();files+=n;size+=s});
  const links=$('a-links').value.split('\n').filter(x=>x.trim()).length;
  const parts=[]; if(on.length)parts.push(`${on.length} 个种子 · ${files} 个文件 · ${fmtB(size)}`); if(links)parts.push(`${links} 个链接`);
  el.innerHTML=parts.join(' ＋ ')+(diskFree!=null?` <span class="${on.length&&size>diskFree?'bad':'sub'}">（磁盘剩余 ${fmtB(diskFree)}）</span>`:'');
}
async function refreshDisk(){ try{ const r=await api('/api/disk?path='+encodeURIComponent($('a-path').value)); diskFree=r.free; }catch(e){ diskFree=null } updateAddSummary() }
async function doAdd(){
  const fd=new FormData();
  fd.append('save_path',$('a-path').value); fd.append('category',$('a-cat').value);
  fd.append('links',$('a-links').value); fd.append('select_files',$('a-select').checked?'1':'0'); fd.append('paused',$('a-paused').checked?'1':'0');
  const pr={};
  previews.forEach(p=>{if(p.error||!p.include)return;pr[p.filename]=p.picker.priorities(Math.max(...p.files.map(f=>f.index))+1)});
  fd.append('priorities',JSON.stringify(pr));
  pickedFiles.forEach((f,i)=>{ if(previews[i]&&previews[i].include&&!previews[i].error) fd.append('files',f,f.name) });
  const r=await(await fetch('/api/add',{method:'POST',body:fd})).json();
  closeM('m-add');
  if(r.errors&&r.errors.length)alert('有些没加进去：\n'+r.errors.join('\n'));
  if(r.added&&r.added.length)toast(`已添加 ${r.added.length} 个任务`);
  poll1();
}
let dragN=0;
window.addEventListener('dragenter',e=>{if(document.querySelector('.mask.on'))return;e.preventDefault();dragN++;$('dragAll').classList.add('on')});
window.addEventListener('dragleave',()=>{if(--dragN<=0){dragN=0;$('dragAll').classList.remove('on')}});
window.addEventListener('dragover',e=>e.preventDefault());
window.addEventListener('drop',e=>{e.preventDefault();dragN=0;$('dragAll').classList.remove('on');if(document.querySelector('.mask.on'))return;
  const fs=[...e.dataTransfer.files].filter(f=>f.name.toLowerCase().endsWith('.torrent'));const txt=e.dataTransfer.getData('text');
  if(fs.length){openAdd();pickTorrents(fs)}else if(txt)openAdd(txt.trim());
});
document.addEventListener('paste',e=>{if(e.target.closest('input,textarea'))return;const t=(e.clipboardData.getData('text')||'').trim();if(/^(magnet:|thunder:|qqdl:|flashget:|https?:)/i.test(t))openAdd(t)});

/* ---------- 删除 ---------- */
let delIds=[];
function askRemove(){delIds=[...sel];$('d-text').textContent=`确定删除选中的 ${delIds.length} 个任务？`;$('d-files').checked=false;openM('m-del')}
async function doRemove(){await api('/api/action',{action:'remove',ids:delIds,delete_files:$('d-files').checked});sel.clear();closeM('m-del');toast('已删除');poll1()}

/* ---------- 选文件 ---------- */
let selFor=null;
async function openSelect(id){
  selFor=id; const fs=await api('/api/files/'+id); const t=data.find(x=>x.id===id);
  $('sel-name').textContent=t?t.name:'';
  new FilePicker('sel',$('sel-picker'),fs,{allChecked:!!t?.need_select,height:360});
  openM('m-select');
}
async function confirmSelect(){
  const fp=pickers.sel, n=Math.max(...fp.files.map(f=>f.index))+1, pr=fp.priorities(n);
  if(!pr.some(x=>x)){alert('至少选一个文件');return}
  await api('/api/files/'+selFor,{priorities:pr}); await api('/api/action',{action:'resume',ids:[selFor]});
  closeM('m-select'); toast('开始下载选中的文件'); poll1();
}

/* ---------- 播放 ---------- */
async function playBest(id){
  const fs=await api('/api/files/'+id); const vids=fs.filter(f=>VIDEO.test(f.path)).sort((a,b)=>b.size-a.size);
  if(!vids.length){toast('这个任务里没有视频文件');return}
  if(vids.length===1)return play(id,vids[0].index);
  openDet(id,'files');
}
async function play(id,idx){
  if(sum.player){const r=await api(`/api/play_external/${id}/${idx}`,{}); if(r.ok){toast(`已用 ${r.player} 打开，边下边播`);return}}
  window.open(`/play/${id}/${idx}`,'_blank');
}

/* ---------- 详情 ---------- */
document.querySelectorAll('#det-tabs .dtab').forEach(b=>b.onclick=()=>{detTab=b.dataset.t;document.querySelectorAll('#det-tabs .dtab').forEach(x=>x.classList.toggle('on',x===b));loadDet(true)});
function openDet(id,tab){
  const t=data.find(x=>x.id===id); if(!t)return; if(t.type==='http'){openM('m-det');}
  detId=id; detTab=tab||'info'; $('det-title').textContent=t.name;
  document.querySelectorAll('#det-tabs .dtab').forEach(x=>{x.classList.toggle('on',x.dataset.t===detTab);x.style.display=t.type==='http'&&!['info','speed'].includes(x.dataset.t)?'none':''});
  openM('m-det'); loadDet(true); clearInterval(detTimer); detTimer=setInterval(()=>loadDet(false),2000);
}
async function loadDet(force){
  if(!detId)return; const id=detId, tab=detTab, body=$('det-body');
  if(tab==='files'){ if(!force)return; return renderFiles(id) }
  if(tab==='diag'){ if(!force&&!body.dataset.auto)return; const d=await api('/api/diagnose/'+id); return renderDiag(id,d) }
  const d=await api('/api/details/'+id); if(detId!==id||detTab!==tab)return;
  if(tab==='info')renderInfo(id,d);
  if(tab==='peers')renderPeers(d);
  if(tab==='trackers')renderTrackers(d);
  if(tab==='speed')renderChart(d.history||[]);
}
function renderInfo(id,d){
  const i=d.info, t=data.find(x=>x.id===id)||{};
  if(d.http){$('det-body').innerHTML=`<div class="kv"><div>地址</div><div class="mono">${esc(i.url)}</div><div>保存到</div><div class="mono">${esc(i.save_path)}</div><div>大小</div><div>${fmtB(i.size)}</div><div>多线程</div><div>${i.ranges?'支持（动态分段）':'服务器不支持，只能单线程'}</div></div>`;return}
  const pieces=(d.pieces||[]).map(v=>`<i style="background:${v>=1?'var(--accent)':v>0?'color-mix(in srgb,var(--accent) '+Math.round(v*100)+'%,var(--bar))':'transparent'}"></i>`).join('');
  $('det-body').innerHTML=`
   <div class="sub small">分块图（蓝色 = 已下完的部分）</div><div class="pieces">${pieces}</div>
   <div class="kv">
    <div>状态</div><div><span>${esc(t.state)}</span> · ${(t.progress*100).toFixed(2)}%</div>
    <div>保存到</div><div class="mono">${esc(i.save_path)} <a href="#" onclick="a1('open','${id}');return false">打开</a></div>
    <div>大小</div><div>${fmtB(i.size)}（${i.total_pieces} 块 × ${fmtB(i.piece_size)}）</div>
    <div>已下载 / 已上传</div><div>${fmtB(i.downloaded)} / ${fmtB(i.uploaded)} · 分享率 ${t.ratio}</div>
    <div>浪费</div><div>${fmtB(i.wasted)}（校验失败或重复收到的数据）</div>
    <div>连接数</div><div>${i.connections}</div>
    <div>添加时间</div><div>${fmtD(i.added)}</div>
    <div>完成时间</div><div>${fmtD(i.completed)}</div>
    <div>特征码</div><div class="mono">${i.info_hash}</div>
    <div>磁力链</div><div class="mono">${esc(i.magnet).slice(0,120)}… <a href="#" onclick="navigator.clipboard.writeText(${JSON.stringify(i.magnet).replace(/"/g,'&quot;')});toast('已复制');return false">复制</a></div>
    ${i.comment?`<div>备注</div><div>${esc(i.comment)}</div>`:''}
    ${i.private?`<div>类型</div><div>私有种子</div>`:''}
   </div>
   <div class="two" style="margin-top:14px">
     <div><label class="l">这个任务单独限速 下载 KB/s（0 = 不限）</label><input id="pl-d" type="number" value="${i.down_limit}"></div>
     <div><label class="l">上传 KB/s</label><input id="pl-u" type="number" value="${i.up_limit}"></div>
   </div>
   <div class="two"><div><label class="l">分类</label><select id="pl-cat"></select></div><div><label class="l">移动到</label><input id="pl-move" class="pathpick" data-kind="folder" placeholder="新文件夹路径"></div></div>
   <div class="btns" style="justify-content:flex-start">
     <button onclick="saveTorrentOpts('${id}')">保存</button>
     <button onclick="a1('sequential','${id}');toast('已切换顺序下载')">${t.sequential?'关闭':'开启'}顺序下载</button>
     <button onclick="a1('recheck','${id}');toast('开始重新校验')">重新校验</button>
     <button onclick="a1('reannounce','${id}');toast('已重新找人')">重新找人</button>
     <button onclick="a1('force_start','${id}');toast('已强制开始')">强制开始（不排队）</button>
     ${i.total_pieces?`<button onclick="location.href='/api/export/${id}'">导出种子文件</button>`:''}
   </div>`;
  fillCats('pl-cat'); $('pl-cat').value=t.category||'';
}
async function saveTorrentOpts(id){
  await api('/api/action',{action:'limit',ids:[id],down_kb:+$('pl-d').value||0,up_kb:+$('pl-u').value||0});
  await api('/api/action',{action:'category',ids:[id],category:$('pl-cat').value});
  if($('pl-move').value.trim())await api('/api/action',{action:'move',ids:[id],path:$('pl-move').value.trim()});
  toast('已保存'); poll1();
}
async function renderFiles(id){
  const fs=await api('/api/files/'+id);
  if(!fs.length){$('det-body').innerHTML='<p class="sub">还在获取种子信息，拿到后才能看到文件列表</p>';return}
  $('det-body').innerHTML=`<div class="sub small">取消勾选的文件不会下载（已下的部分保留）。点表头可以排序，视频可以点 ▶️ 边下边播。</div><div id="df-picker"></div>
   <div class="btns" style="justify-content:flex-start"><button class="primary" onclick="saveFiles('${id}')">保存文件选择</button></div>`;
  new FilePicker('df',$('df-picker'),fs,{progress:true,prio:true,play:id,height:9999});
}
async function saveFiles(id){
  const fp=pickers.df, pr=fp.priorities(Math.max(...fp.files.map(f=>f.index))+1);
  await api('/api/files/'+id,{priorities:pr}); toast('已保存'); poll1();
}
function renderDiag(id,d){
  const ic={ok:'✅',warn:'⚠️',bad:'❌'}; const fixName={reannounce:'重新找人',force_start:'强制开始',resume:'继续',clear_error:'清除错误重试',unlimit:'取消限速',unlimit_one:'取消这个任务的限速'};
  const bad=d.filter(x=>x.level!=='ok');
  $('det-body').innerHTML=`<div style="display:flex;gap:8px;align-items:center;margin-bottom:10px">
     <b>${bad.length?`发现 ${bad.length} 个问题`:'一切正常，慢的话通常是做种的人少或者对方上传慢'}</b><span class="grow"></span>
     ${bad.some(x=>x.fix)?`<button class="primary" onclick="doFix('${id}','all')">🔧 一键修复</button>`:''}<button onclick="loadDet(true)">重新检查</button></div>`+
   d.map(x=>`<div class="diag"><div class="ic">${ic[x.level]}</div><div class="grow"><div class="tt">${esc(x.title)}</div><div class="sub small">${esc(x.detail)}</div></div>${x.fix&&x.level!=='ok'?`<button onclick="doFix('${id}','${x.fix}')">${fixName[x.fix]||'修复'}</button>`:''}</div>`).join('');
}
async function doFix(id,what){await api('/api/fix/'+id,{what});toast('已执行，几十秒后再看效果');setTimeout(()=>{if(detId===id&&detTab==='diag')loadDet(true)},3000);poll1()}
function renderPeers(d){
  $('det-body').innerHTML=`<div class="sub small" style="margin-bottom:6px">共 ${d.peers.length} 个连接</div><table class="t"><tr><th>地址</th><th>客户端</th><th>对方进度</th><th>⬇ 从他那</th><th>⬆ 给他</th><th>累计 ⬇/⬆</th><th>来源</th></tr>${d.peers.map(p=>`<tr>
   <td class="mono">${esc(p.ip)}${p.seed?' <span class="pill">做种</span>':''}${p.encrypted?' 🔒':''}</td><td>${esc(p.client)}</td><td>${(p.progress*100).toFixed(1)}%</td>
   <td>${fmtR(p.down)}</td><td>${fmtR(p.up)}</td><td>${fmtB(p.downloaded)} / ${fmtB(p.uploaded)}</td><td>${p.source}</td></tr>`).join('')}</table>`;
}
function renderTrackers(d){
  const col={'正常':'ok','失败':'bad','正在联系':'warn','未联系':'sub'};
  $('det-body').innerHTML=`<table class="t"><tr><th>地址</th><th>状态</th><th>做种/下载</th><th>信息</th></tr>${d.trackers.map(t=>`<tr>
   <td class="mono">${esc(t.url)}</td><td class="${col[t.status]}">${t.status}</td><td>${t.seeds>=0?t.seeds:'-'} / ${t.peers>=0?t.peers:'-'}</td><td class="small">${esc(t.message)}</td></tr>`).join('')}</table>`;
}
function renderChart(h){
  $('det-body').innerHTML='<canvas class="chart" id="chart"></canvas><div class="small sub" style="margin-top:6px"><span style="color:var(--accent)">━ 下载</span>　<span style="color:#22c55e">━ 上传</span>　最近 10 分钟（每 2 秒一个点）</div>';
  const c=$('chart'),dpr=devicePixelRatio||1; c.width=c.offsetWidth*dpr; c.height=c.offsetHeight*dpr; const g=c.getContext('2d'); g.scale(dpr,dpr);
  const W=c.offsetWidth,H=c.offsetHeight,pad=50; const max=Math.max(1024,...h.map(x=>x[1]),...h.map(x=>x[2]));
  g.strokeStyle=getComputedStyle(document.documentElement).getPropertyValue('--line');g.fillStyle=getComputedStyle(document.documentElement).getPropertyValue('--sub');g.font='11px sans-serif';
  for(let i=0;i<=4;i++){const y=10+(H-30)*i/4;g.beginPath();g.moveTo(pad,y);g.lineTo(W,y);g.stroke();g.fillText(fmtR(max*(1-i/4)),0,y+4)}
  const line=(k,col,fill)=>{if(!h.length)return;g.beginPath();h.forEach((p,i)=>{const x=pad+i/Math.max(1,h.length-1)*(W-pad),y=10+(H-30)*(1-p[k]/max);i?g.lineTo(x,y):g.moveTo(x,y)});g.strokeStyle=col;g.lineWidth=2;g.stroke();if(fill){g.lineTo(W,H-20);g.lineTo(pad,H-20);g.closePath();g.fillStyle=fill;g.fill()}};
  line(1,'#2f7bff','rgba(47,123,255,.12)'); line(2,'#22c55e');
}

/* ---------- 设置 ---------- */
const DAYS=['一','二','三','四','五','六','日'];
document.querySelectorAll('#set-tabs .dtab').forEach(b=>b.onclick=()=>{document.querySelectorAll('#set-tabs .dtab').forEach(x=>x.classList.toggle('on',x===b));document.querySelectorAll('#m-set .sec').forEach(s=>s.classList.toggle('on',s.dataset.s===b.dataset.s))});
function openSettings(){
  const s=sum.settings||{};
  for(const k in s){const el=$('st-'+k);if(!el)continue;if(el.type==='checkbox')el.checked=!!s[k];else el.value=s[k]}
  $('days').innerHTML=DAYS.map((d,i)=>`<label class="chk" style="margin:0"><input type="checkbox" class="day" value="${i}" ${(s.schedule_days||[]).includes(i)?'checked':''}> 周${d}</label>`).join('');
  loadRemote();
  const m=s.seed_time_limit_min, pv=!s.seed_after_done?'off':m===0?'forever':['10','30','60','180','1440'].includes(String(m))?String(m):'custom';
  $('seed-preset').value=pv; $('seed-min').value=m||60; seedPreset();
  $('trkn').textContent=`当前 ${sum.trackers} 个`; renderBans(); renderCats(); openM('m-set');
}
function renderBans(){
  $('bann').textContent=sum.banned||0;
  $('ban-table').innerHTML='<tr><th>IP</th><th>原因 / 客户端</th><th>时间</th></tr>'+(sum.banned_list||[]).slice().reverse().map(b=>`<tr><td class="mono">${esc(b.ip)}</td><td>${esc(b.client)}</td><td>${fmtD(b.time)}</td></tr>`).join('');
}
function renderCats(){
  const c=sum.categories||{};
  $('cat-list').innerHTML=Object.keys(c).length?Object.entries(c).map(([k,v])=>`<div class="list-item"><b>${esc(k)}</b><span class="mono grow">${esc(v.save_path||'（默认位置）')}</span><button class="danger" onclick="delCat('${esc(k)}')">删除</button></div>`).join(''):'<p class="sub small">还没有分类</p>';
}
async function addCat(){const n=$('cat-name').value.trim();if(!n)return;await api('/api/categories',{name:n,save_path:$('cat-path').value.trim()});$('cat-name').value=$('cat-path').value='';await poll1();renderCats()}
async function delCat(n){await api('/api/categories',{name:n,delete:true});await poll1();renderCats()}
async function saveSettings(){
  const s=sum.settings, out={};
  for(const k in s){const el=$('st-'+k);if(!el)continue;
    if(el.type==='checkbox')out[k]=el.checked;else if(el.type==='number')out[k]=+el.value||0;else out[k]=el.value}
  out.schedule_days=[...document.querySelectorAll('.day:checked')].map(c=>+c.value);
  const sp=$('seed-preset').value;
  out.seed_after_done = sp!=='off';
  out.seed_time_limit_min = sp==='off'?(s.seed_time_limit_min||10):sp==='forever'?0:sp==='custom'?Math.max(1,+$('seed-min').value||10):+sp;
  if(out.remote_enabled&&!out.remote_password){alert('开远程访问必须设密码');return}
  const langChanged=out.language!==s.language;
  await api('/api/settings',out); if(langChanged){location.reload();return}
  toast('设置已保存'); poll1();
  if(document.querySelector('#set-tabs .dtab.on')?.dataset.s==='remote'){loadRemote()}else{closeM('m-set')}
}
async function loadRemote(){
  const r=await api('/api/remote/info'); const box=$('remote-box'); if(!box)return;
  const kinds={lan:'家里 / 公司局域网',tailscale:'Tailscale（在外面也能连）',public:'公网 IP（需要路由器端口转发）'};
  if(!r.enabled){box.innerHTML='<div class="sub small">勾选上面的开关、设好密码并保存后，这里会显示手机要打开的地址和二维码。</div>'}
  else if(!r.has_password){box.innerHTML='<div class="bad small">还没设密码，远程访问不会生效。</div>'}
  else box.innerHTML=r.addrs.length?`<div class="sub small" style="margin-bottom:6px">✅ 已开启，立即生效。手机连同一个网络，扫码或打开下面的地址：</div>
    <div style="display:flex;gap:14px;flex-wrap:wrap">${r.addrs.map(a=>`<div style="text-align:center;border:1px solid var(--line);border-radius:10px;padding:8px">
      <img src="/api/remote/qr?text=${encodeURIComponent(a.url)}" width="130" height="130" alt="" style="background:#fff;border-radius:6px">
      <div class="mono notr" style="margin-top:4px">${esc(a.url)}</div><div class="sub small">${kinds[a.kind]}</div>
      <button style="padding:2px 8px;margin-top:4px" onclick="navigator.clipboard.writeText('${a.url}');toast('已复制')">复制地址</button></div>`).join('')}</div>`
    :'<div class="sub small">没找到本机的网络地址</div>';
  const e=r.ext; $('ext-status').innerHTML=e&&Date.now()/1000-e.time<900?`<span class="ok">✅ 浏览器扩展已连接</span>（${fmtD(e.time)}）`:'<span class="sub">还没检测到浏览器扩展</span>';
}
function seedPreset(){$('seed-custom').style.display=$('seed-preset').value==='custom'?'':'none'}
async function refreshTrk(){$('trkn').textContent='更新中…';const r=await api('/api/trackers/refresh',{});$('trkn').textContent=r.count?`已更新：${r.count} 个`:'更新失败，连不上列表地址'}
async function associate(){const r=await api('/api/associate',{});toast(r.message||'完成')}

/* ---------- RSS ---------- */
let rssTab='feeds', rssData=null, editRule=null;
document.querySelectorAll('#rss-tabs .dtab').forEach(b=>b.onclick=()=>{rssTab=b.dataset.r;document.querySelectorAll('#rss-tabs .dtab').forEach(x=>x.classList.toggle('on',x===b));renderRss()});
async function openRss(){rssData=await api('/api/rss');openM('m-rss');renderRss()}
async function rssOp(b){if(b.op==='refresh'||b.op==='add_feed')toast('正在抓取…');rssData=await api('/api/rss',b);renderRss();if(b.op==='refresh')toast('已刷新')}
function renderRss(){
  const d=rssData, body=$('rss-body'); if(!d)return;
  if(rssTab==='feeds'){
    body.innerHTML=`<div class="two"><input id="rf-url" placeholder="RSS 地址，例如 https://mikanani.me/RSS/Bangumi?bangumiId=..."><div style="display:flex;gap:6px"><input id="rf-int" type="number" value="15" title="多少分钟刷新一次" style="width:90px"><span class="sub small" style="align-self:center">分钟</span><button class="primary" onclick="rssOp({op:'add_feed',url:$('rf-url').value,interval:+$('rf-int').value})">＋ 订阅</button></div></div>
     <div class="hint">第一次订阅时，已有的旧条目不会自动下载，只下以后新出的。想要旧的，在下面列表里手动点「下载」。</div>`+
     d.feeds.map(f=>`<div style="margin-top:14px"><div style="display:flex;gap:8px;align-items:center"><b>${esc(f.name)}</b><span class="sub small mono grow">${esc(f.url)}</span>${f.error?`<span class="bad small">${esc(f.error)}</span>`:''}<button class="danger" onclick="if(confirm('取消订阅？'))rssOp({op:'remove_feed',id:'${f.id}'})">取消订阅</button></div>
       <div class="ftree" style="max-height:260px"><table class="t">${(d.items[f.id]||[]).map(it=>`<tr><td class="p">${esc(it.title)}</td><td class="small sub" style="white-space:nowrap">${it.size?fmtB(it.size):''}</td><td style="width:60px">${it.link?`<button style="padding:2px 8px" onclick='rssOp({op:"download",link:${JSON.stringify(it.link).replace(/'/g,"&#39;")}});toast("已添加")'>下载</button>`:''}</td></tr>`).join('')||'<tr><td class="sub">还没有条目</td></tr>'}</table></div></div>`).join('');
  } else if(rssTab==='rules'){
    const r=editRule||{name:'',include:'',exclude:'',feeds:[],save_path:'',category:'',enabled:true};
    body.innerHTML=`<div class="hint">规则：新条目标题<b>包含</b>里的所有关键词（空格分隔）、且<b>不包含</b>排除词，就自动下载。也可以写正则，用 /.../ 包起来。<br>例子：包含「葬送的芙莉莲 1080p 简」，排除「合集 繁」。</div>
     <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:10px">
      <div>${d.rules.map(x=>`<div class="list-item"><input type="checkbox" ${x.enabled?'checked':''} onchange='rssOp({op:"set_rule",rule:Object.assign(${JSON.stringify(x).replace(/'/g,"&#39;")},{enabled:this.checked})})'><b class="grow">${esc(x.name||x.include)}</b><button onclick='editRule=${JSON.stringify(x).replace(/'/g,"&#39;")};renderRss()'>编辑</button><button class="danger" onclick="rssOp({op:'remove_rule',id:'${x.id}'})">删</button></div>`).join('')||'<p class="sub">还没有规则</p>'}
       <button style="margin-top:8px" onclick="editRule=null;renderRss()">＋ 新规则</button></div>
      <div><label class="l">规则名</label><input id="rr-name" value="${esc(r.name)}">
       <label class="l">包含</label><input id="rr-inc" value="${esc(r.include)}">
       <label class="l">排除</label><input id="rr-exc" value="${esc(r.exclude)}">
       <label class="l">只对这些订阅生效（都不勾 = 全部）</label><div>${d.feeds.map(f=>`<label class="chk" style="margin:2px 0"><input type="checkbox" class="rr-feed" value="${f.id}" ${(r.feeds||[]).includes(f.id)?'checked':''}> ${esc(f.name)}</label>`).join('')}</div>
       <div class="two"><div><label class="l">保存到（空 = 默认）</label><input id="rr-path" class="pathpick" data-kind="folder" value="${esc(r.save_path)}"></div><div><label class="l">分类</label><select id="rr-cat"></select></div></div>
       <div class="btns" style="justify-content:flex-start"><button onclick="testRule()">🧪 用现有条目试试</button><button class="primary" onclick="saveRule()">保存规则</button></div>
       <div id="rr-test"></div></div></div>`;
    fillCats('rr-cat'); $('rr-cat').value=r.category||'';
  } else {
    body.innerHTML='<table class="t"><tr><th>时间</th><th>标题</th><th>规则</th></tr>'+(d.log||[]).map(l=>`<tr><td style="white-space:nowrap">${fmtD(l.time)}</td><td class="p">${esc(l.title)}</td><td>${esc(l.rule)}</td></tr>`).join('')+'</table>'+(d.log&&d.log.length?'':'<p class="sub">还没有自动下载过</p>');
  }
}
function ruleFromForm(){return Object.assign({},editRule||{}, {name:$('rr-name').value,include:$('rr-inc').value,exclude:$('rr-exc').value,feeds:[...document.querySelectorAll('.rr-feed:checked')].map(c=>c.value),save_path:$('rr-path').value,category:$('rr-cat').value,enabled:editRule?editRule.enabled:true})}
async function testRule(){const r=await api('/api/rss',{op:'test_rule',rule:ruleFromForm()});$('rr-test').innerHTML=`<div class="hint">匹配到 ${r.length} 条：<br>${r.map(esc).join('<br>')||'（没有）'}</div>`}
async function saveRule(){const r=ruleFromForm();if(!r.include&&!confirm('「包含」是空的，会下载订阅里所有新条目，确定？'))return;await rssOp({op:'set_rule',rule:r});editRule=null;renderRss();toast('规则已保存')}

/* ---------- 做种子 ---------- */
function openCreate(){$('cr-out').style.display='none';openM('m-create')}
async function doCreate(){
  $('cr-btn').disabled=true;$('cr-btn').textContent='正在计算校验值…';
  const r=await api('/api/create',{path:$('cr-path').value.trim(),trackers:$('cr-trk').value,private:$('cr-private').checked,comment:$('cr-comment').value,seed:$('cr-seed').checked});
  $('cr-btn').disabled=false;$('cr-btn').textContent='生成种子';
  $('cr-out').style.display='block';
  $('cr-out').innerHTML=r.ok?`✅ 已生成：<span class="mono">${esc(r.file)}</span> <a href="#" onclick="api('/api/open_path',{path:${JSON.stringify(r.file.replace(/[\\/][^\\/]*$/,'')).replace(/"/g,'&quot;')}});return false">打开文件夹</a>`:`<span class="bad">失败：${esc(r.error)}</span>`;
  poll1();
}

poll();

/* ---------- 选择文件夹：点路径输入框就弹出系统窗口，小白不用手打路径 ---------- */
const isLocalUI = ['127.0.0.1','localhost'].includes(location.hostname);
async function pickPath(inp){
  if(!isLocalUI||inp.dataset.picking)return;
  inp.dataset.picking='1';
  try{
    let kind=inp.dataset.kind;
    if(kind==='any') kind=confirm('要分享的是一个文件夹吗？\n点「确定」选文件夹，点「取消」选单个文件。')?'folder':'file';
    const r=await api('/api/pick',{kind,start:inp.value});
    if(r.ok){ inp.value=r.path; inp.dispatchEvent(new Event('change')); inp.dispatchEvent(new Event('input')) }
  } finally { setTimeout(()=>delete inp.dataset.picking,300) }
}
document.addEventListener('click',e=>{ const inp=e.target.closest('input.pathpick'); if(inp) pickPath(inp) });
// 每个路径框右边加一个「浏览」按钮（手机远程访问时只能手动输入，不加）
function decoratePaths(root=document){
  if(!isLocalUI)return;
  root.querySelectorAll('input.pathpick:not([data-deco])').forEach(inp=>{
    inp.dataset.deco='1'; inp.readOnly=false; inp.style.cursor='pointer'; inp.title='点击选择文件夹';
    const w=document.createElement('div'); w.className='pathrow'; inp.parentNode.insertBefore(w,inp); w.appendChild(inp);
    const b=document.createElement('button'); b.type='button'; b.textContent='📂 浏览'; b.onclick=()=>pickPath(inp); w.appendChild(b);
  });
}
new MutationObserver(()=>decoratePaths()).observe(document.body,{childList:true,subtree:true}); decoratePaths();
