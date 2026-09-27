// Twitch Auto-Recorder Unmute — background service worker (v6.31)
// On {type:'tar-unmute', username}: find open twitch.tv/<username> tabs
// (www./m., popout, player.twitch.tv/?channel=<username>), clear Chrome's
// tab-level mute, and ask the twitch content script to unmute the player.

const RESERVED = new Set(['directory', 'videos', 'settings', 'subscriptions', 'inventory', 'wallet', 'search', 'downloads', 'jobs', 'turbo', 'p', 'friends', 'messages', 'drops', 'moderator']);

function channelFromUrl(raw){
  let u;
  try { u = new URL(raw); } catch (e) { return null; }
  const host = u.hostname.toLowerCase();
  if (host === 'player.twitch.tv') {
    const ch = u.searchParams.get('channel');
    return ch ? ch.toLowerCase() : null;
  }
  if (!(host === 'twitch.tv' || host === 'www.twitch.tv' || host === 'm.twitch.tv')) return null;
  const parts = u.pathname.split('/').filter(Boolean).map(function(p){ return p.toLowerCase(); });
  if (!parts.length) return null;
  // twitch.tv/popout/<user>/player (and /chat, ignored for audio but harmless)
  if (parts[0] === 'popout' && parts[1]) return parts[1];
  if (parts[0] === 'moderator' && parts[1]) return parts[1];
  if (RESERVED.has(parts[0])) return null;
  // Only the channel page itself (live player), not /videos, /clips, /about …
  if (parts.length > 1 && parts[1] !== 'home') return null;
  return parts[0];
}

async function findTabs(username){
  const tabs = await chrome.tabs.query({ url: ['*://twitch.tv/*', '*://*.twitch.tv/*'] });
  return tabs.filter(function(t){ return t.url && channelFromUrl(t.url) === username; });
}

function sendToTab(tabId, msg){
  return new Promise(function(resolve){
    try {
      chrome.tabs.sendMessage(tabId, msg, function(res){
        const err = chrome.runtime.lastError;
        if (err) resolve({ __noReceiver: true, error: err.message });
        else resolve(res || { ok: false, error: 'empty response' });
      });
    } catch (e) { resolve({ __noReceiver: true, error: String(e) }); }
  });
}

async function unmuteTab(tab){
  const out = { tabId: tab.id, url: tab.url, tabWasMuted: false, tabUnmuted: false, player: null };
  try {
    if (tab.mutedInfo && tab.mutedInfo.muted) {
      out.tabWasMuted = true;
      await chrome.tabs.update(tab.id, { muted: false });
      out.tabUnmuted = true;
    } else {
      // Harmless even if already unmuted.
      await chrome.tabs.update(tab.id, { muted: false });
    }
  } catch (e) { out.tabError = String(e && e.message || e); }

  let res = await sendToTab(tab.id, { type: 'tar-unmute-player' });
  if (res && res.__noReceiver) {
    // Tab was open before the extension was installed/reloaded → inject now.
    try {
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ['twitch-content.js'] });
      res = await sendToTab(tab.id, { type: 'tar-unmute-player' });
    } catch (e) {
      res = { ok: false, error: 'inject failed: ' + String(e && e.message || e) };
    }
  }
  if (res && res.__noReceiver) res = { ok: false, error: res.error };
  out.player = res;
  out.ok = !!(res && res.ok);
  return out;
}

async function handleUnmute(username){
  const tabs = await findTabs(username);
  if (!tabs.length) {
    return { ok: false, tabsFound: 0, unmuted: 0, changed: false, error: 'no open twitch.tv/' + username + ' tab' };
  }
  const results = await Promise.all(tabs.map(unmuteTab));
  const unmuted = results.filter(function(r){ return r.ok; }).length;
  const changed = results.some(function(r){ return r.tabUnmuted || (r.player && (r.player.wasMuted || r.player.clickedButton) && r.ok); });
  const autoplayBlocked = results.some(function(r){ return r.player && r.player.autoplayBlocked; });
  return { ok: unmuted > 0, tabsFound: tabs.length, unmuted, changed, autoplayBlocked, results };
}

chrome.runtime.onMessage.addListener(function(msg, sender, sendResponse){
  if (!msg || msg.type !== 'tar-unmute') return;
  const username = String(msg.username || '').trim().toLowerCase();
  if (!/^[a-z0-9_]{1,25}$/.test(username)) { sendResponse({ ok: false, error: 'bad username', tabsFound: 0, unmuted: 0 }); return; }
  handleUnmute(username).then(sendResponse, function(e){
    sendResponse({ ok: false, error: String(e && e.message || e), tabsFound: 0, unmuted: 0 });
  });
  return true;
});
