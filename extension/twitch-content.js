// Twitch Auto-Recorder Unmute — twitch.tv player unmuter (v6.31)
// Receives {type:'tar-unmute-player'} from the background worker, then over ~10s:
//   • clicks Twitch's mute button when it shows the muted state
//   • sets <video>.muted=false and raises volume from 0 → 0.5
// Replies {ok, hadVideo, wasMuted, unmuted, clickedButton, autoplayBlocked, attempts}.
(function(){
  if (window.__tarTwitchUnmute) return;
  window.__tarTwitchUnmute = true;

  const BTN_SEL = [
    'button[data-a-target="player-mute-unmute-button"]',
    '[data-a-target="player-mute-unmute-button"]',
    '.player-controls button[aria-label*="mute" i]',
    'button[aria-label^="Unmute" i]'
  ];

  function findVideo(){
    const vids = Array.from(document.querySelectorAll('video'));
    if (!vids.length) return null;
    // Prefer the main player (largest, playing).
    vids.sort(function(a, b){
      const sa = (a.clientWidth * a.clientHeight) + (a.paused ? 0 : 1e7);
      const sb = (b.clientWidth * b.clientHeight) + (b.paused ? 0 : 1e7);
      return sb - sa;
    });
    return vids[0];
  }
  function findButton(){
    for (const sel of BTN_SEL) {
      const el = document.querySelector(sel);
      if (el) return el;
    }
    return null;
  }
  function buttonShowsMuted(btn, video){
    const label = ((btn.getAttribute('aria-label') || '') + ' ' + (btn.getAttribute('title') || '')).toLowerCase();
    if (/\bunmute\b/.test(label)) return true;       // "Unmute (m)" → currently muted
    if (/\bmute\b/.test(label)) return false;        // "Mute (m)"   → currently audible
    // Unknown/localized label: fall back to the video state.
    return !!(video && (video.muted || video.volume === 0));
  }

  async function attemptOnce(state){
    const video = findVideo();
    const btn = findButton();
    if (video) state.hadVideo = true;
    if (video && (video.muted || video.volume === 0)) state.wasMuted = true;
    if (btn && buttonShowsMuted(btn, video)) {
      state.wasMuted = true;
      try { btn.click(); state.clickedButton = true; } catch (e) {}
      await new Promise(function(r){ setTimeout(r, 250); });
    }
    if (!video) return false;
    if (video.muted) { try { video.muted = false; } catch (e) {} }
    if (video.volume === 0) { try { video.volume = 0.5; } catch (e) {} }
    // Chrome may pause a video unmuted without a user gesture (autoplay policy).
    if (video.paused && !video.ended && video.readyState >= 2) {
      try { await video.play(); }
      catch (e) {
        state.autoplayBlocked = true;
        // Keep the stream playing (muted) rather than leaving it paused.
        try { video.muted = true; await video.play(); } catch (e2) {}
        return false;
      }
    }
    const b2 = findButton();
    const btnMuted = b2 ? buttonShowsMuted(b2, video) : false;
    return !video.muted && video.volume > 0 && !btnMuted;
  }

  async function unmuteWithRetries(){
    const state = { ok: false, hadVideo: false, wasMuted: false, unmuted: false, clickedButton: false, autoplayBlocked: false, attempts: 0, url: location.href };
    const deadline = Date.now() + 10000;
    while (Date.now() < deadline) {
      state.attempts++;
      let done = false;
      try { done = await attemptOnce(state); } catch (e) { state.error = String(e && e.message || e); }
      if (done) { state.ok = true; state.unmuted = true; break; }
      if (state.autoplayBlocked) break;
      await new Promise(function(r){ setTimeout(r, 1000); });
    }
    return state;
  }

  chrome.runtime.onMessage.addListener(function(msg, sender, sendResponse){
    if (!msg || msg.type !== 'tar-unmute-player') return;
    unmuteWithRetries().then(sendResponse, function(e){ sendResponse({ ok: false, error: String(e) }); });
    return true; // async response
  });
})();
