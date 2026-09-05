// Adapted from lounge-cruise-agent's edge-extension/delivery-key.js.
//
// `event.sequence` here must be the per-target LOCAL delivery sequence from
// local-sequence.js, not the raw Lounge message sequence — see
// local-sequence.js for why. `settings.lastDeliverySequence` /
// `lastDeliveryAttempt` must be updated after every real delivery attempt
// this extension makes for a target, success or failure, not only on
// success; the caller (service-worker.js) is responsible for that.
//
// The original "a new sequence's first attempt must be delivery_attempt 1"
// rule from lounge-cruise-agent was dropped. It assumed the tracked
// (sequence, attempt) always advances in lockstep with the real attempt
// count, which only holds if it is updated on every attempt. When it is
// only updated on success (as lounge-cruise-agent's own service-worker.js
// also does), a single failed delivery followed by a retry reports
// delivery_attempt=2 for a sequence the tracker has never recorded
// (lastDeliverySequence still 0) and gets permanently rejected as
// "new_sequence_must_start_at_one" — which was the actual bug this history
// note exists to explain. A local sequence is, by construction, only ever
// assigned once per distinct Lounge message for a target, so seeing a new
// local sequence at all is already sufficient proof this is not a replay;
// requiring its first reported attempt number to equal 1 added nothing
// besides that failure mode.

export function deliveryDecision(event, settings) {
  const attempt = event.delivery_attempt;
  if (!Number.isSafeInteger(attempt) || attempt < 1) return { allow: false, reason: 'invalid_attempt' };
  const lastSequence = settings.lastDeliverySequence || 0;
  const lastAttempt = settings.lastDeliverySequence ? settings.lastDeliveryAttempt : 0;
  if (event.sequence < lastSequence) return { allow: false, reason: 'older_sequence' };
  if (event.sequence === lastSequence && attempt <= lastAttempt) {
    return { allow: false, reason: 'duplicate_or_lower_attempt' };
  }
  return { allow: true };
}


