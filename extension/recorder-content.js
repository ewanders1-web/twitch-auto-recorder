// Twitch Auto-Recorder Unmute — recorder page bridge (v6.31)
// Relays window.postMessage({source:'tar', type:'tar-unmute', username, reqId})
// from the recorder page to the background worker, and posts results back as
// {source:'tar-ext', type:'tar-unmute-result', ...}. Announces presence with
// {source:'tar-ext', type:'tar-ext-hello', version}.
(function(){
  if (window.__tarRecorderBridge) return;
  window.__tarRecorderBridge = true;
  const VERSION = chrome.runtime.getManifest().version;
  const TARGET = window.location.origin === 'null' || window.location.protocol === 'file:' ? '*' : window.location.origin;

  function post(msg){
    msg.source = 'tar-ext';
    try { window.postMessage(msg, TARGET); } catch (e) { window.postMessage(msg, '*'); }
  }
  function hello(){ post({ type: 'tar-ext-hello', version: VERSION }); }

  window.addEventListener('message', function(ev){
    if (ev.source !== window) return;
    const d = ev.data;
    if (!d || typeof d !== 'object' || d.source !== 'tar') return;
    if (d.type === 'tar-ext-ping') { hello(); return; }
    if (d.type !== 'tar-unmute') return;
    const username = String(d.username || '').trim().toLowerCase();
    const reqId = d.reqId || null;
    if (!/^[a-z0-9_]{1,25}$/.test(username)) {
      post({ type: 'tar-unmute-result', reqId, username, ok: false, error: 'bad username', tabsFound: 0, unmuted: 0 });
      return;
    }
    try {
      chrome.runtime.sendMessage({ type: 'tar-unmute', username, reason: d.reason || '' }, function(res){
        const err = chrome.runtime.lastError;
        if (err || !res) {
          post({ type: 'tar-unmute-result', reqId, username, ok: false, error: (err && err.message) || 'no response', tabsFound: 0, unmuted: 0 });
          return;
        }
        res.type = 'tar-unmute-result';
        res.reqId = reqId;
        res.username = username;
        post(res);
      });
    } catch (e) {
      // Extension reloaded/updated → this content script context is orphaned.
      post({ type: 'tar-unmute-result', reqId, username, ok: false, error: 'extension context invalidated — reload the recorder page', tabsFound: 0, unmuted: 0 });
    }
  });

  hello();
})();
