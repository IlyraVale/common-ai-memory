// Generic tab-binding primitives extracted from the production extension.
// that the supported-origin set is passed in per call instead of being a
// single module-level constant, since this extension binds two independent
// targets (gpt, claude) to two different sites instead of one.

export const TARGET_ORIGINS = Object.freeze({
  gpt: Object.freeze(['https://chatgpt.com', 'https://chat.openai.com']),
  claude: Object.freeze(['https://claude.ai']),
});

export function originForSupportedUrl(rawUrl, allowedOrigins) {
  try {
    const url = new URL(rawUrl);
    return allowedOrigins.includes(url.origin) ? url.origin : null;
  } catch { return null; }
}

export function validStoredTarget(target, allowedOrigins) {
  return target !== null && typeof target === 'object' &&
    Number.isSafeInteger(target.tabId) && target.tabId >= 0 &&
    allowedOrigins.includes(target.origin) &&
    typeof target.boundAt === 'string' && Number.isFinite(Date.parse(target.boundAt));
}

export function targetDecision(target, tab, allowedOrigins) {
  if (!validStoredTarget(target, allowedOrigins)) return { ok: false, result: 'target_tab_not_bound' };
  if (!tab || tab.id !== target.tabId) return { ok: false, result: 'target_tab_unavailable' };
  const currentOrigin = originForSupportedUrl(tab.url, allowedOrigins);
  if (currentOrigin !== target.origin) return { ok: false, result: 'target_tab_origin_mismatch' };
  return { ok: true };
}

