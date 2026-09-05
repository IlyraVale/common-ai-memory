// Polls the AI Lounge Bridge's browser-adapter endpoints
// (GET /v1/browser-wake/<target>, POST /v1/browser-result) and relays
// delivery to whichever tab is bound to that target's content script. This
// preserves the production service-worker delivery loop, generalized
// to two independently bound targets (gpt, claude) instead of one, and
// talking to the Lounge Bridge's own HTTP surface. It never reports success unless the content script actually
// confirmed the message was sent; every other outcome (target missing,
// unbound, content script unavailable, busy, send failure) is reported as
// ok:false so the Lounge Bridge keeps the delivery pending and retries.

import { targetDecision, validStoredTarget, TARGET_ORIGINS } from './target-binding.js';
import { defaultLocalSequenceState } from './local-sequence.js';
import { planDelivery } from './delivery-plan.js';

const TARGETS = ['gpt', 'claude'];

function defaultSettings() {
  return {
    targetTabs: { gpt: null, claude: null },
    // Dedupe tracker for delivery-key.js, keyed by LOCAL sequence (see
    // local-sequence.js), not the raw Lounge message sequence.
    delivery: { gpt: { sequence: 0, attempt: 0 }, claude: { sequence: 0, attempt: 0 } },
    // Lounge-sequence -> local-sequence assignment, persisted per target.
    localSequence: { gpt: defaultLocalSequenceState(), claude: defaultLocalSequenceState() },
    bridgeUrl: 'http://localhost:8879',
  };
}
async function settings() {
  const stored = await chrome.storage.local.get(defaultSettings());
  return {
    targetTabs: { ...defaultSettings().targetTabs, ...(stored.targetTabs ?? {}) },
    delivery: { ...defaultSettings().delivery, ...(stored.delivery ?? {}) },
    localSequence: { ...defaultSettings().localSequence, ...(stored.localSequence ?? {}) },
    bridgeUrl: stored.bridgeUrl ?? defaultSettings().bridgeUrl,
  };
}

async function controllerGet(path) {
  const { bridgeUrl } = await settings();
  const response = await fetch(`${bridgeUrl}${path}`, { headers: { 'content-type': 'application/json' } });
  if (!response.ok) throw new Error(`controller_http_${response.status}`);
  return response.json();
}
async function controllerPost(path, body) {
  const { bridgeUrl } = await settings();
  const response = await fetch(`${bridgeUrl}${path}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok && response.status !== 409) throw new Error(`controller_http_${response.status}`);
  try { return await response.json(); } catch { return {}; }
}
async function report(target, sequence, deliveryAttempt, ok, detail) {
  try {
    await controllerPost('/v1/browser-result', {
      target, sequence, delivery_attempt: deliveryAttempt, ok, detail: detail ?? null,
    });
  } catch { /* The Lounge Bridge's 20s lease will re-offer this event on its own. */ }
}

async function boundTab(target, current) {
  const stored = current.targetTabs[target];
  const origins = TARGET_ORIGINS[target];
  if (!validStoredTarget(stored, origins)) return { tab: null, result: 'target_tab_not_bound' };
  let tab;
  try { tab = await chrome.tabs.get(stored.tabId); }
  catch {
    await chrome.storage.local.set({ targetTabs: { ...current.targetTabs, [target]: null } });
    return { tab: null, result: 'target_tab_unavailable' };
  }
  const decision = targetDecision(stored, tab, origins);
  if (!decision.ok) return { tab: null, result: decision.result };
  return { tab, result: 'ok' };
}

async function recoverContentScript(tab) {
  if (!chrome.scripting || typeof chrome.scripting.executeScript !== 'function') return false;
  try {
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ['content-script.js'] });
    const ping = await chrome.tabs.sendMessage(tab.id, { type: 'LOUNGE_BRIDGE_PING' });
    return ping?.ok === true && ping?.status === 'ready';
  } catch { return false; }
}

async function pingWithRecovery(tab) {
  try {
    const result = await chrome.tabs.sendMessage(tab.id, { type: 'LOUNGE_BRIDGE_PING' });
    if (result?.ok === true && result?.status === 'ready') return result;
  } catch { /* fall through to recovery */ }
  const recovered = await recoverContentScript(tab);
  if (!recovered) return null;
  try { return await chrome.tabs.sendMessage(tab.id, { type: 'LOUNGE_BRIDGE_PING' }); }
  catch { return null; }
}

async function pollTarget(target) {
  let payload;
  try { payload = await controllerGet(`/v1/browser-wake/${target}`); }
  catch { return; }
  const event = payload?.event;
  if (!event) return;

  const current = await settings();
  const plan = planDelivery(target, event, current);
  if (!plan.allow) {
    // Not a real delivery outcome (most likely a re-entrant poll racing
    // itself) — do not report it, since that would count as a wasted real
    // attempt on the Lounge Bridge side for something this extension never
    // actually tried.
    console.warn(`[lounge-bridge] local dedupe rejected ${target} lounge_seq=${event.sequence} local_seq=${plan.localSequence} attempt=${event.delivery_attempt}: ${plan.reason}`);
    return;
  }
  // Record that this attempt number is now in flight for every outcome
  // below, not only success — see delivery-key.js's comment for why
  // updating this only on success was the original bug.
  await chrome.storage.local.set({
    delivery: plan.nextDelivery,
    localSequence: { ...current.localSequence, [target]: plan.nextLocalSequenceState },
  });

  const { tab, result } = await boundTab(target, current);
  if (!tab) {
    await report(target, event.sequence, event.delivery_attempt, false, result);
    return;
  }

  try {
    const ping = await pingWithRecovery(tab);
    if (ping?.ok !== true || ping?.status !== 'ready') {
      await report(target, event.sequence, event.delivery_attempt, false, 'target_content_script_unavailable');
      return;
    }
    const busy = await chrome.tabs.sendMessage(tab.id, { type: 'LOUNGE_BRIDGE_BUSY_CHECK' });
    if (busy?.ok !== true) {
      await report(target, event.sequence, event.delivery_attempt, false, 'target_content_script_unavailable');
      return;
    }
    if (busy.busy) {
      await report(target, event.sequence, event.delivery_attempt, false, 'assistant_busy');
      return;
    }
    const refreshedTab = await chrome.tabs.get(tab.id);
    const refreshed = await settings();
    const finalDecision = targetDecision(refreshed.targetTabs[target], refreshedTab, TARGET_ORIGINS[target]);
    if (!finalDecision.ok) {
      await report(target, event.sequence, event.delivery_attempt, false, finalDecision.result);
      return;
    }
    const response = await chrome.tabs.sendMessage(tab.id, { type: 'LOUNGE_BRIDGE_WAKE', event });
    if (response?.ok) {
      await report(target, event.sequence, event.delivery_attempt, true, response.assistantTurnKind ?? 'delivered');
    } else {
      await report(target, event.sequence, event.delivery_attempt, false, response?.error ?? 'send_failed');
    }
  } catch (error) {
    await report(target, event.sequence, event.delivery_attempt, false, `extension_error:${String(error).slice(0, 200)}`);
  }
}

async function poll() {
  for (const target of TARGETS) {
    // eslint-disable-next-line no-await-in-loop
    await pollTarget(target);
  }
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === 'LOUNGE_BRIDGE_SET_URL') {
    try {
      const url = new URL(message.url);
      if (url.protocol !== 'http:' || url.hostname !== 'localhost') throw new Error('bridge_url_must_be_localhost');
      chrome.storage.local.set({ bridgeUrl: url.origin }).then(() => sendResponse({ ok: true, bridgeUrl: url.origin }));
    } catch (error) { sendResponse({ ok: false, error: String(error) }); }
    return true;
  }
  if (message?.type === 'LOUNGE_BRIDGE_BINDING_STATUS') {
    settings().then(async (current) => {
      const status = {};
      for (const target of TARGETS) {
        const stored = current.targetTabs[target];
        const origins = TARGET_ORIGINS[target];
        if (!validStoredTarget(stored, origins)) { status[target] = { bound: false }; continue; }
        try {
          const tab = await chrome.tabs.get(stored.tabId);
          if (!targetDecision(stored, tab, origins).ok) throw new Error('stored_target_unavailable');
          status[target] = { bound: true, tabId: stored.tabId, origin: stored.origin };
        } catch {
          await chrome.storage.local.set({ targetTabs: { ...current.targetTabs, [target]: null } });
          status[target] = { bound: false };
        }
      }
      sendResponse({ ok: true, status, bridgeUrl: current.bridgeUrl });
    });
    return true;
  }
  if (message?.type === 'LOUNGE_BRIDGE_BIND_CURRENT_TAB') {
    const target = message.target;
    if (!TARGETS.includes(target)) { sendResponse({ ok: false, error: 'unknown_target' }); return false; }
    chrome.tabs.query({ active: true, currentWindow: true }).then(async ([tab]) => {
      const origins = TARGET_ORIGINS[target];
      let origin = null;
      try { origin = origins.includes(new URL(tab?.url ?? '').origin) ? new URL(tab.url).origin : null; }
      catch { origin = null; }
      if (!tab?.id || !origin) { sendResponse({ ok: false, error: 'unsupported_target_tab' }); return; }
      const current = await settings();
      const boundTarget = { tabId: tab.id, origin, boundAt: new Date().toISOString() };
      await chrome.storage.local.set({
        targetTabs: { ...current.targetTabs, [target]: boundTarget },
        delivery: { ...current.delivery, [target]: { sequence: 0, attempt: 0 } },
        localSequence: { ...current.localSequence, [target]: defaultLocalSequenceState() },
      });
      sendResponse({ ok: true });
    }).catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (message?.type === 'LOUNGE_BRIDGE_UNBIND_TARGET') {
    const target = message.target;
    if (!TARGETS.includes(target)) { sendResponse({ ok: false, error: 'unknown_target' }); return false; }
    settings().then(async (current) => {
      await chrome.storage.local.set({ targetTabs: { ...current.targetTabs, [target]: null } });
      sendResponse({ ok: true });
    }).catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  return false;
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const current = await settings();
  let changed = false;
  const next = { ...current.targetTabs };
  for (const target of TARGETS) {
    if (next[target]?.tabId === tabId) { next[target] = null; changed = true; }
  }
  if (changed) await chrome.storage.local.set({ targetTabs: next });
});

chrome.runtime.onInstalled.addListener(() => chrome.alarms.create('lounge-bridge-poll', { periodInMinutes: 0.5 }));
chrome.runtime.onStartup.addListener(() => chrome.alarms.create('lounge-bridge-poll', { periodInMinutes: 0.5 }));
let pollInFlight = null;
function schedulePoll() {
  if (pollInFlight) return pollInFlight;
  pollInFlight = poll().finally(() => { pollInFlight = null; });
  return pollInFlight;
}
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === 'lounge-bridge-poll') schedulePoll().catch(() => {});
});
schedulePoll().catch(() => {});
