// Generic browser-delivery primitives (visibility checks, composer lookup,
// text entry, submit-and-confirm state machine) reused from
// lounge-cruise-agent's edge-extension/content-script.js. Everything
// Instance-specific wake/autonomy instructions have been dropped: the Lounge
// Bridge already sends the full fixed prompt as `event.message`, so this
// script only needs to submit that text and confirm it was actually sent.
//
// Selectors below cover both target sites this extension supports:
// ChatGPT (chatgpt.com / chat.openai.com) selectors are carried over as-is
// from the proven lounge-cruise-agent implementation. Claude.ai selectors
// are best-effort additions and have not been exercised against a live
// Claude.ai session; if they turn out to be wrong, submission fails closed
// (composer_missing / message_submitted_timeout / assistant_turn_start_timeout)
// and the Lounge Bridge simply retries instead of a false success.

function isAriaHidden(element) {
  if (!element) return true;
  return element.closest?.('[aria-hidden="true"]') !== null ||
    element.getAttribute?.('aria-hidden') === 'true';
}

function visible(element) {
  if (!element || isAriaHidden(element)) return false;
  const style = getComputedStyle(element);
  if (style.display === 'none' || style.visibility === 'hidden') return false;
  const rect = element.getBoundingClientRect();
  if (rect.width > 0 && rect.height > 0) return true;
  return element.offsetParent !== null || element.getClientRects().length > 0;
}

function composer() {
  const candidates = [
    document.querySelector('#prompt-textarea'),
    document.querySelector('div.ProseMirror[contenteditable="true"]'),
    ...document.querySelectorAll('textarea, [contenteditable="true"]'),
  ].filter(Boolean).filter((candidate) => !isAriaHidden(candidate));
  return candidates.find(visible) ?? candidates.find((candidate) => !candidate.disabled);
}

function waitForDom(predicate, timeoutMs) {
  const evaluate = () => {
    try {
      return predicate();
    } catch {
      return null;
    }
  };
  const immediate = evaluate();
  if (immediate) return Promise.resolve(immediate);

  return new Promise((resolve) => {
    let settled = false;
    let timeout = null;
    let observer = null;

    const finish = (value) => {
      if (settled) return;
      settled = true;
      observer?.disconnect();
      if (timeout !== null) clearTimeout(timeout);
      resolve(value);
    };

    observer = new MutationObserver(() => {
      if (settled) return;
      const result = evaluate();
      if (result) finish(result);
    });
    observer.observe(document.documentElement, {
      subtree: true,
      childList: true,
      attributes: true,
      characterData: true,
    });
    timeout = setTimeout(() => {
      if (settled) return;
      // Last-chance check: a predicate-satisfying DOM change that landed
      // without ever notifying the observer (or landed in the same tick the
      // observer callback was already scheduled to fire) would otherwise
      // resolve null here even though the condition is actually true right
      // now. This is still purely a failure-timeout fallback, not a poll —
      // it runs exactly once, only at the timeout boundary.
      try {
        const finalResult = evaluate();
        if (finalResult) {
          finish(finalResult);
          return;
        }
      } catch {
        // transient DOM state; timeout still resolves null
      }
      finish(null);
    }, timeoutMs);

    const afterObserve = evaluate();
    if (afterObserve) finish(afterObserve);
  });
}

async function waitForComposer(timeoutMs = 10_000) {
  return waitForDom(() => composer(), timeoutMs);
}

function enterNativeValueText(target, text) {
  const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(target), 'value')?.set;
  setter?.call(target, text);
  target.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }));
  target.dispatchEvent(new Event('change', { bubbles: true }));
}

// contenteditable editors (Claude's ProseMirror included) keep their own
// internal editor state alongside the DOM. Writing target.textContent
// directly desyncs that state without ever telling the editor a real edit
// happened, which is why Send stayed disabled even though the text was
// visibly there. Route the write through the same transaction path a real
// keystroke/paste would use instead: select the existing content and let
// document.execCommand('insertText', ...) perform the insertion, since
// browsers still implement it as a genuine editing command (with real
// beforeinput/input events) for contenteditable even though it is
// deprecated for other uses.
function enterContentEditableText(target, text) {
  target.focus();

  const selection = window.getSelection();
  const range = document.createRange();
  range.selectNodeContents(target);
  selection.removeAllRanges();
  selection.addRange(range);

  let inserted = false;
  try {
    inserted = document.execCommand('insertText', false, text);
  } catch {
    inserted = false;
  }
  // Some editors report success but still end up out of sync with what was
  // actually requested; trust the resulting DOM text over the return value.
  if (inserted && editableText(target) !== text.trim()) inserted = false;

  if (!inserted) {
    const beforeInput = new InputEvent('beforeinput', {
      bubbles: true,
      cancelable: true,
      inputType: 'insertText',
      data: text,
    });
    const notClaimedByEditor = target.dispatchEvent(beforeInput);
    if (notClaimedByEditor) {
      // The editor did not preventDefault on beforeinput, meaning it is not
      // going to perform the insertion itself — writing the DOM here is a
      // last resort, not a double-write on top of editor-owned state.
      target.textContent = text;
      target.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }));
    }
    // If beforeinput WAS prevented, the editor claims to be handling this
    // insertion through its own internal transaction. Forcing a second,
    // conflicting textContent write here would fight that transaction
    // instead of completing it, so this intentionally does nothing further
    // and leaves whatever the editor already did in place.
  }

  target.dispatchEvent(new Event('change', { bubbles: true }));
}

function enterText(target, text) {
  target.focus();
  if (target instanceof HTMLTextAreaElement || target instanceof HTMLInputElement) {
    enterNativeValueText(target, text);
    return;
  }
  enterContentEditableText(target, text);
}

const CONFIRMATION_TIMEOUTS_MS = Object.freeze({
  composerCleared: 5_000,
  messageSubmitted: 8_000,
  assistantTurnStarted: 15_000,
});
const SEND_BUTTON_SELECTOR =
  '[data-testid="send-button"], button[aria-label*="Send" i], button[aria-label*="发送"], button[type="submit"]';
const STOP_BUTTON_SELECTOR =
  '[data-testid="stop-button"], button[data-testid*="stop" i], button[aria-label*="Stop" i], button[aria-label*="停止生成"], button[aria-label*="停止响应"]';
const USER_MESSAGE_SELECTOR = '[data-message-author-role="user"], [data-testid="user-message"]';
const ASSISTANT_MESSAGE_SELECTOR =
  '[data-message-author-role="assistant"], [data-testid="assistant-message"], [data-testid*="assistant-message"], [data-message-author="assistant"], [data-role="assistant"]';
const CLAUDE_TURN_SELECTOR = '[data-testid="conversation-turn"], [data-testid^="conversation-turn-"]';
const CLAUDE_STREAMING_SELECTOR = '[data-is-streaming="true"], [data-testid*="streaming" i]';

function editableText(target) {
  return (target?.value ?? target?.textContent ?? '').trim();
}

// Diagnostics only, for the send_button_missing failure path. Deliberately
// reports lengths/booleans about the composer and candidate buttons, never
// the actual chat text.
function describeSendCandidate(el) {
  return {
    tag: el.tagName ?? null,
    ariaLabel: el.getAttribute?.('aria-label') ?? null,
    dataTestId: el.getAttribute?.('data-testid') ?? null,
    type: el.getAttribute?.('type') ?? null,
    disabled: Boolean(el.disabled),
    visible: visible(el),
  };
}
function composerDebugInfo(target) {
  const text = editableText(target);
  return {
    targetTag: target?.tagName ?? null,
    targetClass: typeof target?.className === 'string' ? target.className : null,
    contenteditable: target?.getAttribute?.('contenteditable') ?? null,
    editableTextLength: text.length,
    editableTextEmpty: text.length === 0,
    sendCandidates: [...document.querySelectorAll(SEND_BUTTON_SELECTOR)].map(describeSendCandidate),
  };
}

async function waitUntil(predicate, timeoutMs) {
  return waitForDom(predicate, timeoutMs);
}

function findSendButton() {
  return [...document.querySelectorAll(SEND_BUTTON_SELECTOR)]
    .filter((candidate) => !isAriaHidden(candidate))
    .find(
      (candidate) =>
        !candidate.disabled &&
        (
          visible(candidate) ||
          candidate.matches('[data-testid="send-button"], button[type="submit"]')
        ),
    ) ?? null;
}

function messageNodes(selector) {
  return [...document.querySelectorAll(selector)].filter(visible);
}

function findSubmittedUserMessage(marker) {
  return messageNodes(USER_MESSAGE_SELECTOR).find((node) => (node.textContent ?? '').includes(marker)) ?? null;
}

function follows(candidate, reference) {
  return Boolean(reference.compareDocumentPosition(candidate) & Node.DOCUMENT_POSITION_FOLLOWING);
}

function findGeneratingControl() {
  const control = [...document.querySelectorAll(STOP_BUTTON_SELECTOR)].find(visible);
  if (control) return control;
  return [...document.querySelectorAll(CLAUDE_STREAMING_SELECTOR)].find(visible) ?? null;
}

function isUserTurn(turn) {
  return turn.matches?.(USER_MESSAGE_SELECTOR) || Boolean(turn.querySelector?.(USER_MESSAGE_SELECTOR));
}

function findClaudeAssistantTurnAfter(userMessage) {
  const userTurn = userMessage.closest?.(CLAUDE_TURN_SELECTOR) ?? userMessage;
  return [...document.querySelectorAll(CLAUDE_TURN_SELECTOR)]
    .filter(visible)
    .find((turn) => follows(turn, userTurn) && !isUserTurn(turn)) ?? null;
}

function findAssistantTurnAfter(userMessage) {
  const assistantMessage = messageNodes(ASSISTANT_MESSAGE_SELECTOR).find((node) => follows(node, userMessage));
  if (assistantMessage) return { kind: 'assistant_message', node: assistantMessage };

  const claudeTurn = findClaudeAssistantTurnAfter(userMessage);
  if (claudeTurn) return { kind: 'claude_assistant_turn', node: claudeTurn };

  const generatingControl = findGeneratingControl();
  if (generatingControl) {
    return { kind: 'generating', node: generatingControl };
  }
  return null;
}

function assistantIsGenerating() {
  return Boolean(findGeneratingControl());
}

function describeDomCandidate(el) {
  return {
    tag: el.tagName ?? null,
    dataTestId: el.getAttribute?.('data-testid') ?? null,
    role: el.getAttribute?.('role') ?? null,
    ariaLabel: el.getAttribute?.('aria-label') ?? null,
    className: typeof el.className === 'string' ? el.className : null,
  };
}

function assistantTurnDebugInfo(userMessage) {
  const assistantCandidates = [
    ...document.querySelectorAll(ASSISTANT_MESSAGE_SELECTOR),
    ...document.querySelectorAll(CLAUDE_TURN_SELECTOR),
    ...document.querySelectorAll(CLAUDE_STREAMING_SELECTOR),
  ];
  return {
    hostname: location.hostname,
    assistantCandidates: [...new Set(assistantCandidates)].map(describeDomCandidate),
    stopCandidates: [...document.querySelectorAll(STOP_BUTTON_SELECTOR)].map((candidate) => ({
      ...describeDomCandidate(candidate),
      disabled: Boolean(candidate.disabled),
      visible: visible(candidate),
    })),
    userMessageFound: Boolean(userMessage?.isConnected),
  };
}

function confirmedExistingSubmission(marker) {
  const userMessage = findSubmittedUserMessage(marker);
  if (!userMessage) return null;
  const assistantTurn = findAssistantTurnAfter(userMessage);
  if (!assistantTurn) return null;
  return { ok: true, stages: ['message_submitted', 'assistant_turn_started'],
    assistantTurnKind: assistantTurn.kind, alreadySubmitted: true };
}

async function submitMessage(event, text, marker) {
  const target = await waitForComposer();
  const stages = [];
  const existingSubmission = confirmedExistingSubmission(marker);
  if (existingSubmission) return existingSubmission;
  if (event.delivery_attempt > 1 && assistantIsGenerating()) {
    return { ok: false, error: 'assistant_busy', stages };
  }
  if (!target) return { ok: false, error: 'composer_missing', stages };
  if (editableText(target)) return { ok: false, error: 'composer_not_empty', stages };
  enterText(target, text);

  const button = await waitForDom(() => findSendButton(), 5_000);
  if (!button) {
    const debug = composerDebugInfo(target);
    if (editableText(target) === text.trim()) enterText(target, '');
    return { ok: false, error: 'send_button_missing', stages, debug };
  }
  button.click();

  const cleared = await waitUntil(() => {
    const currentComposer = composer();
    return currentComposer && editableText(currentComposer) === '' ? currentComposer : null;
  }, CONFIRMATION_TIMEOUTS_MS.composerCleared);
  if (!cleared) {
    const submittedDespiteUnclearedComposer = confirmedExistingSubmission(marker);
    if (submittedDespiteUnclearedComposer) return submittedDespiteUnclearedComposer;
    const currentComposer = composer();
    if (currentComposer && editableText(currentComposer).includes(marker)) enterText(currentComposer, '');
    return { ok: false, error: 'composer_clear_timeout', stages };
  }
  stages.push('composer_cleared');

  const userMessage = await waitUntil(
    () => findSubmittedUserMessage(marker),
    CONFIRMATION_TIMEOUTS_MS.messageSubmitted,
  );
  if (!userMessage) return { ok: false, error: 'message_submitted_timeout', stages };
  stages.push('message_submitted');

  const assistantTurn = await waitUntil(
    () => findAssistantTurnAfter(userMessage),
    CONFIRMATION_TIMEOUTS_MS.assistantTurnStarted,
  );
  if (!assistantTurn) {
    return {
      ok: false,
      error: 'assistant_turn_start_timeout',
      stages,
      debug: assistantTurnDebugInfo(userMessage),
    };
  }
  stages.push('assistant_turn_started');

  return { ok: true, stages, assistantTurnKind: assistantTurn.kind };
}

function submitWake(event) {
  const marker = `seq=${event.sequence}, target=${event.target}`;
  return submitMessage(event, event.message, marker);
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === 'LOUNGE_BRIDGE_PING') {
    sendResponse({ ok: true, status: 'ready' });
    return false;
  }
  if (message?.type === 'LOUNGE_BRIDGE_BUSY_CHECK') {
    sendResponse({ ok: true, busy: assistantIsGenerating() });
    return false;
  }
  if (message?.type !== 'LOUNGE_BRIDGE_WAKE') return false;
  submitWake(message.event).then(sendResponse).catch((error) => sendResponse({ ok: false, error: String(error) }));
  return true;
});

