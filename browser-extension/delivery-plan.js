// Pure planning step used by service-worker.js's pollTarget, split into its
// own module (no chrome.* dependency) so it can be unit tested directly —
// service-worker.js itself registers chrome.alarms/runtime/tabs listeners
// and starts polling as soon as it is imported, which is not something a
// plain Node test should trigger.

import { deliveryDecision } from './delivery-key.js';
import { resolveLocalSequence } from './local-sequence.js';

// Resolves the local delivery sequence for this event (see
// local-sequence.js) and decides, via delivery-key.js, whether this is a
// real new attempt worth acting on. `loungeSequence` is always exactly
// `event.sequence` unchanged — the local sequence exists only to drive this
// decision and the persisted dedupe tracker; every call site that talks to
// the Lounge Bridge (POST /v1/browser-result, and separately the
// lounge_wake_ack MCP tool the model calls) must keep using loungeSequence,
// never localSequence.
export function planDelivery(target, event, current) {
  const { localSequence, state: localSequenceState } =
    resolveLocalSequence(current.localSequence[target], event.sequence);
  const decision = deliveryDecision(
    { sequence: localSequence, delivery_attempt: event.delivery_attempt },
    { lastDeliverySequence: current.delivery[target]?.sequence ?? 0, lastDeliveryAttempt: current.delivery[target]?.attempt ?? 0 },
  );
  return {
    allow: decision.allow,
    reason: decision.reason ?? null,
    loungeSequence: event.sequence,
    localSequence,
    nextLocalSequenceState: localSequenceState,
    nextDelivery: { ...current.delivery, [target]: { sequence: localSequence, attempt: event.delivery_attempt } },
  };
}


