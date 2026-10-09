const en = document.getElementById('en'), min = document.getElementById('min'), st = document.getElementById('st');
chrome.storage.local.get({enabled: true, minSizeMB: 2}, c => { en.checked = c.enabled; min.value = c.minSizeMB; });
en.onchange = () => chrome.storage.local.set({enabled: en.checked});
min.onchange = () => chrome.storage.local.set({minSizeMB: +min.value || 0});
fetch('http://127.0.0.1:18790/api/ext/ping?from=ext').then(r => r.json())
  .then(d => st.textContent = '✅ 已连上 KOI v' + d.version)
  .catch(() => st.textContent = '⚠️ KOI 没有在运行，下载会交还给浏览器');
