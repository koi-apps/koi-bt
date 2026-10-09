const KOI = 'http://127.0.0.1:18790';
const DEFAULTS = {enabled: true, minSizeMB: 2};

async function cfg() { return Object.assign({}, DEFAULTS, await chrome.storage.local.get(Object.keys(DEFAULTS))); }

async function alive() {
  try { const r = await fetch(KOI + '/api/ext/ping?from=ext'); return (await r.json()).app === 'koi'; } catch (e) { return false; }
}

async function send(url, extra = {}) {
  let cookie = '';
  try {
    if (/^https?:/.test(url)) cookie = (await chrome.cookies.getAll({url})).map(c => c.name + '=' + c.value).join('; ');
  } catch (e) {}
  const r = await fetch(KOI + '/api/ext/add', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(Object.assign({url, cookie, ua: navigator.userAgent}, extra)),
  });
  return (await r.json()).ok;
}

chrome.downloads.onCreated.addListener(async item => {
  const c = await cfg();
  if (!c.enabled) return;
  const url = item.finalUrl || item.url;
  if (!/^(https?|ftp):/i.test(url)) return;                 // blob: / data: 只有浏览器自己能下
  if (item.fileSize > 0 && item.fileSize < c.minSizeMB * 1048576) return;  // 小文件让浏览器自己下
  if (!(await alive())) return;                             // KOI 没开就不拦
  try {
    await chrome.downloads.cancel(item.id);
    await chrome.downloads.erase({id: item.id});
  } catch (e) {}
  const ok = await send(url, {referer: item.referrer || '', filename: item.filename || ''});
  if (!ok) chrome.downloads.download({url});                // 交接失败就还给浏览器
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({id: 'koi-link', title: '用 KOI BT下载', contexts: ['link']});
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === 'koi-link') await send(info.linkUrl, {referer: tab ? tab.url : ''});
});

// 定时报个平安，KOI 设置页能显示「扩展已连接」
chrome.alarms.create('koi-ping', {periodInMinutes: 5});
chrome.alarms.onAlarm.addListener(a => { if (a.name === 'koi-ping') alive(); });
chrome.runtime.onStartup.addListener(() => alive());
chrome.runtime.onInstalled.addListener(() => alive());
