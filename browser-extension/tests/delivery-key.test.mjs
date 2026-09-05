import assert from 'node:assert/strict';
import test from 'node:test';

import { deliveryDecision } from '../delivery-key.js';

test('a brand-new local sequence is allowed even when its first reported delivery_attempt is already greater than 1 (prior real failures before this local sequence was ever tracked)', () => {
  // This is the regression this fix targets: local sequence 1 (assigned to
  // Lounge seq 70, the first message ever seen for this target), but the
  // Lounge Bridge already retried it a few times server-side before the
  // extension successfully bound a tab, so delivery_attempt is 4, not 1.
  const decision = deliveryDecision(
    { sequence: 1, delivery_attempt: 4 },
    { lastDeliverySequence: 0, lastDeliveryAttempt: 0 },
  );
  assert.equal(decision.allow, true);
});

test('a retry of the same local sequence is allowed when the attempt number increases', () => {
  const decision = deliveryDecision(
    { sequence: 1, delivery_attempt: 2 },
    { lastDeliverySequence: 1, lastDeliveryAttempt: 1 },
  );
  assert.equal(decision.allow, true);
});

test('the same (sequence, attempt) pair reported twice is rejected as a duplicate', () => {
  const decision = deliveryDecision(
    { sequence: 1, delivery_attempt: 1 },
    { lastDeliverySequence: 1, lastDeliveryAttempt: 1 },
  );
  assert.equal(decision.allow, false);
  assert.equal(decision.reason, 'duplicate_or_lower_attempt');
});

test('a lower attempt for the same sequence than what was already tracked is rejected', () => {
  const decision = deliveryDecision(
    { sequence: 1, delivery_attempt: 1 },
    { lastDeliverySequence: 1, lastDeliveryAttempt: 2 },
  );
  assert.equal(decision.allow, false);
  assert.equal(decision.reason, 'duplicate_or_lower_attempt');
});

test('an older sequence than what was already tracked is rejected', () => {
  const decision = deliveryDecision(
    { sequence: 1, delivery_attempt: 1 },
    { lastDeliverySequence: 2, lastDeliveryAttempt: 1 },
  );
  assert.equal(decision.allow, false);
  assert.equal(decision.reason, 'older_sequence');
});

test('a non-positive or non-integer delivery_attempt is always rejected', () => {
  assert.equal(deliveryDecision({ sequence: 1, delivery_attempt: 0 }, { lastDeliverySequence: 0, lastDeliveryAttempt: 0 }).allow, false);
  assert.equal(deliveryDecision({ sequence: 1, delivery_attempt: -1 }, { lastDeliverySequence: 0, lastDeliveryAttempt: 0 }).allow, false);
  assert.equal(deliveryDecision({ sequence: 1, delivery_attempt: 1.5 }, { lastDeliverySequence: 0, lastDeliveryAttempt: 0 }).allow, false);
});

test('delivery_attempt is allowed to grow past 2, unlike lounge-cruise-agent which caps delivery at 2 attempts', () => {
  const decision = deliveryDecision(
    { sequence: 1, delivery_attempt: 9 },
    { lastDeliverySequence: 1, lastDeliveryAttempt: 8 },
  );
  assert.equal(decision.allow, true);
});


