const TARGETS = ['gpt', 'claude'];
const LABELS = { gpt: 'GPT', claude: 'Claude' };

async function request(message) {
  try { return await chrome.runtime.sendMessage(message); }
  catch (error) { return { ok: false, error: String(error) }; }
}

async function refresh(overrides = {}) {
  const result = await request({ type: 'LOUNGE_BRIDGE_BINDING_STATUS' });
  if (result?.bridgeUrl) document.querySelector('#bridge-url').value = result.bridgeUrl;
  for (const target of TARGETS) {
    const el = document.querySelector(`#status-${target}`);
    if (overrides[target]) { el.textContent = overrides[target]; continue; }
    const entry = result?.status?.[target];
    el.textContent = entry?.bound
      ? `已绑定标签页 ${entry.tabId}（${entry.origin}）`
      : '尚未绑定；该目标的唤醒会保持 pending 并重试。';
  }
}

for (const target of TARGETS) {
  document.querySelector(`#bind-${target}`).addEventListener('click', async () => {
    const result = await request({ type: 'LOUNGE_BRIDGE_BIND_CURRENT_TAB', target });
    await refresh({
      [target]: result?.ok
        ? `当前标签页已绑定为 ${LABELS[target]}。`
        : `绑定失败：${result?.error === 'unsupported_target_tab' ? '当前标签页不是 ' + LABELS[target] + ' 支持的域名' : (result?.error ?? '未知错误')}`,
    });
  });
  document.querySelector(`#unbind-${target}`).addEventListener('click', async () => {
    const result = await request({ type: 'LOUNGE_BRIDGE_UNBIND_TARGET', target });
    await refresh({ [target]: result?.ok ? '已解除绑定。' : `解除失败：${result?.error ?? '未知错误'}` });
  });
}

refresh();



document.querySelector('#save-url').addEventListener('click', async () => {
  const result = await request({ type: 'LOUNGE_BRIDGE_SET_URL', url: document.querySelector('#bridge-url').value });
  if (!result?.ok) alert(result?.error ?? 'Unable to save Bridge URL');
});
