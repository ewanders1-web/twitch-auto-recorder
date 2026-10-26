// ==UserScript==
// @name         Twitch Raid Reset
// @namespace    https://twitch.tv/
// @version      1.2
// @description  Redirects back to the original streamer when a raid occurs, and (new) can return home after a set ends — works for multiple streamers
// @match        https://www.twitch.tv/*
// @run-at       document-idle
// @grant        none
// ==/UserScript==

(function () {
    'use strict';

    // ================================================================
    // OPTIONS — edit these to taste
    // ================================================================
    const CONFIG = {
        // NEW in v1.2: when a watched streamer's set ends (their channel
        // stays offline for a few minutes after having been live), send
        // this tab "home" so it's parked and ready for the next set.
        RETURN_HOME_AFTER_SET: true,

        // Where "home" is:
        //   'channel'   — the streamer's own channel page (default)
        //   'frontpage' — the Twitch front page, https://www.twitch.tv/
        HOME_MODE: 'channel',

        // The streamers this applies to — add or remove names freely
        // (lowercase, exactly as in their twitch.tv/<name> URL).
        // This is the multi-streamer support: one list, every tab checks
        // its own streamer against it. Leave the list empty ([]) to
        // apply to ANY streamer a tab is parked on.
        STREAMERS: [
            'whistleface',
            'sweetdeesauce',
            'skratchbastid',
            'thegafflive',
            'djicyice',
            'lilypita',
            'dj_sweettooth'
        ],

        // How long the channel must stay offline before the set counts
        // as over. Guards against brief drops/reconnects mid-set.
        SET_END_OFFLINE_MS: 180000, // 3 minutes

        // How often to check whether the set has ended.
        SET_END_POLL_MS: 30000 // 30 seconds
    };
    // ================================================================

    const STORAGE_KEY = 'twitch_original_streamer'; // kept from v1.0 so existing tabs carry over
    const REDIRECT_KEY = 'twitch_raid_reset_ts';
    const SEEN_LIVE_KEY = 'twitch_raid_reset_seen_live';
    const OFFLINE_SINCE_KEY = 'twitch_raid_reset_offline_since';
    const RETURNED_KEY = 'twitch_raid_reset_returned_home';
    const REDIRECT_COOLDOWN_MS = 8000; // bounce at most once per 8s (prevents redirect loops)

    // First path segment of non-channel Twitch pages — never treat these as a streamer.
    const ignoredPaths = new Set([
        'directory', 'videos', 'downloads', 'wallet', 'settings',
        'subscriptions', 'search', 'drops', 'store', 'turbo', 'prime',
        'bits', 'jobs', 'legal', 'about', 'blog', 'charity',
        'moderator', 'popout', 'embed'
    ]);

    function getCurrentChannel() {
        const segments = window.location.pathname.split('/').filter(Boolean);
        if (segments.length === 0) return null;
        const first = segments[0].toLowerCase();
        return ignoredPaths.has(first) ? null : first;
    }

    function justRedirected() {
        const ts = Number(sessionStorage.getItem(REDIRECT_KEY) || 0);
        return Date.now() - ts < REDIRECT_COOLDOWN_MS;
    }

    function checkAndReset(reason) {
        const current = getCurrentChannel();
        let original = sessionStorage.getItem(STORAGE_KEY);

        // FIX 1: adopt the first channel visited in this tab even if you got
        // there via SPA navigation. v1.0 only set this on full page loads, so
        // opening twitch.tv and clicking through to a streamer left nothing
        // stored and raids were never caught.
        if (!original && current) {
            sessionStorage.setItem(STORAGE_KEY, current);
            original = current;
        }

        // FIX 2: check on full page loads too. v1.0 only watched for in-page
        // URL changes after load, so a raid that landed as a fresh page load
        // never triggered the redirect.
        if (original && current && current !== original && !justRedirected()) {
            console.log(`[Twitch Raid Reset] (${reason}) moved from ${original} to ${current} — redirecting back...`);
            sessionStorage.setItem(REDIRECT_KEY, String(Date.now()));
            window.location.replace(`https://www.twitch.tv/${original}`);
        }
    }

    // ---------------------------------------------------------------
    // NEW in v1.2: return home after the set ends
    // ---------------------------------------------------------------

    function isChannelLive() {
        // Live badge next to the channel name/avatar while broadcasting.
        return Boolean(
            document.querySelector('[data-a-target="tw-live-indicator"]') ||
            document.querySelector('.live-indicator')
        );
    }

    function isChannelOffline() {
        // Twitch marks the player area when a channel isn't live.
        if (document.querySelector('[data-a-target="player-offline-container"]')) return true;
        const player = document.querySelector('[data-a-target="player-container"]');
        if (player && /is offline/i.test(player.innerText || '')) return true;
        return false;
    }

    function streamerEnabled(channel) {
        if (!CONFIG.STREAMERS || CONFIG.STREAMERS.length === 0) return true;
        return CONFIG.STREAMERS.includes(channel);
    }

    function returnHome(channel, reason) {
        sessionStorage.setItem(RETURNED_KEY, '1');
        sessionStorage.setItem(REDIRECT_KEY, String(Date.now()));
        const url = CONFIG.HOME_MODE === 'frontpage'
            ? 'https://www.twitch.tv/'
            : `https://www.twitch.tv/${channel}`;
        console.log(`[Twitch Raid Reset] (${reason}) set over for ${channel} — returning home: ${url}`);
        window.location.replace(url);
    }

    function checkSetEnd() {
        if (!CONFIG.RETURN_HOME_AFTER_SET) return;

        const channel = getCurrentChannel();
        if (!channel || !streamerEnabled(channel)) return;

        // Only act in a tab parked on its own streamer. If a raid has
        // carried this tab elsewhere, the raid reset above handles it.
        const home = sessionStorage.getItem(STORAGE_KEY);
        if (home && home !== channel) return;

        if (isChannelLive()) {
            // Set is on (or a new set started): arm the watcher and clear
            // any state left over from the previous set.
            if (sessionStorage.getItem(SEEN_LIVE_KEY) !== '1' ||
                sessionStorage.getItem(RETURNED_KEY) === '1') {
                console.log(`[Twitch Raid Reset] ${channel} is live — watching for the set to end.`);
            }
            sessionStorage.setItem(SEEN_LIVE_KEY, '1');
            sessionStorage.removeItem(OFFLINE_SINCE_KEY);
            sessionStorage.removeItem(RETURNED_KEY);
            return;
        }

        // Not live. Only counts as "set over" if this tab actually saw
        // the streamer live — never fire on a channel that was just
        // sitting offline.
        if (sessionStorage.getItem(SEEN_LIVE_KEY) !== '1') return;
        if (sessionStorage.getItem(RETURNED_KEY) === '1') return;
        if (!isChannelOffline()) return;

        const since = Number(sessionStorage.getItem(OFFLINE_SINCE_KEY) || 0);
        if (!since) {
            sessionStorage.setItem(OFFLINE_SINCE_KEY, String(Date.now()));
            return;
        }
        if (Date.now() - since >= CONFIG.SET_END_OFFLINE_MS && !justRedirected()) {
            returnHome(channel, 'set-end');
        }
    }

    // ---------------------------------------------------------------

    // Catch raids that land as a full page load.
    checkAndReset('load');

    // Catch SPA navigation by hooking the History API (which Twitch's router
    // uses) plus back/forward. Far more reliable than watching the DOM for
    // URL changes, which the router can slip past.
    let lastUrl = window.location.href;
    function onUrlChange() {
        if (window.location.href === lastUrl) return;
        lastUrl = window.location.href;
        checkAndReset('spa');
    }

    const origPushState = history.pushState.bind(history);
    history.pushState = function (...args) {
        origPushState(...args);
        onUrlChange();
    };

    const origReplaceState = history.replaceState.bind(history);
    history.replaceState = function (...args) {
        origReplaceState(...args);
        onUrlChange();
    };

    window.addEventListener('popstate', onUrlChange);

    // Fallback: DOM watcher in case a navigation ever slips past the History
    // API. onUrlChange() no-ops when the URL hasn't changed, so this is cheap.
    new MutationObserver(onUrlChange).observe(document.documentElement, {
        childList: true,
        subtree: true
    });

    // Poll for the end of the set (see CONFIG at the top).
    if (CONFIG.RETURN_HOME_AFTER_SET) {
        checkSetEnd();
        setInterval(checkSetEnd, CONFIG.SET_END_POLL_MS);
    }
})();
