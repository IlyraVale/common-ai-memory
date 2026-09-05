// Assigns a small, per-target, monotonically increasing local delivery
// sequence (1, 2, 3, ...) for use by delivery-key.js's dedupe check.
//
// The Lounge Bridge's own message sequence is a single global counter
// shared by the configured human, GPT, and Claude across the whole room, so by the time a
// target's tab is ever bound it can already be at an arbitrary value (70,
// 1000, ...). Feeding that raw number into delivery-key.js's "a brand new
// sequence's first attempt must be delivery_attempt 1" style reasoning has
// nothing to do with how large the number is — see delivery-key.js's own
// comment for the actual bug — but a local counter is still the right fix
// the caller asked for: it keeps this extension's own replay bookkeeping
// small and independent of Lounge's room-wide numbering, and makes "is this
// a message we have never locally seen before" a trivial `>` comparison
// again instead of something that has to reason about backoff/attempt
// counts at all.
//
// This module only decides local sequence numbers. It never touches the
// original Lounge sequence, which must still flow through untouched
// end to end: it is what /v1/browser-result reports delivery outcomes
// against, what the wake prompt text embeds, and what lounge_wake_ack(...)
// acknowledges on the Lounge Bridge side.

export function defaultLocalSequenceState() {
  return { nextLocalSequence: 1, currentLoungeSequence: null, currentLocalSequence: null };
}

function normalized(state) {
  if (
    state && typeof state === 'object' &&
    Number.isSafeInteger(state.nextLocalSequence) && state.nextLocalSequence >= 1 &&
    (state.currentLoungeSequence === null || Number.isSafeInteger(state.currentLoungeSequence)) &&
    (state.currentLocalSequence === null || Number.isSafeInteger(state.currentLocalSequence))
  ) {
    return state;
  }
  return defaultLocalSequenceState();
}

// Pure: given the persisted per-target state and the Lounge sequence from
// the current wake event, returns the local sequence to use plus the state
// to persist. Retrying the same Lounge sequence (a second delivery attempt
// for a message that has not been acked yet) reuses the same local
// sequence; any other Lounge sequence — including one lower than the last,
// which happens when an unacked delivery's ack_timeout_seconds lapses and
// an older pending message is re-offered ahead of it — is treated as a new
// local sequence, since neither case can collide with a previously assigned
// local sequence.
export function resolveLocalSequence(persistedState, loungeSequence) {
  if (!Number.isSafeInteger(loungeSequence)) {
    throw new Error('loungeSequence must be a safe integer');
  }
  const state = normalized(persistedState);
  if (state.currentLoungeSequence === loungeSequence && Number.isSafeInteger(state.currentLocalSequence)) {
    return { localSequence: state.currentLocalSequence, state };
  }
  const localSequence = state.nextLocalSequence;
  const nextState = {
    nextLocalSequence: localSequence + 1,
    currentLoungeSequence: loungeSequence,
    currentLocalSequence: localSequence,
  };
  return { localSequence, state: nextState };
}

