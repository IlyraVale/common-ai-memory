import assert from 'node:assert/strict';
import test from 'node:test';

import { planDelivery } from '../delivery-plan.js';
import { defaultLocalSequenceState } from '../local-sequence.js';

function freshSettings() {
  return {
    delivery: { gpt: { sequence: 0, attempt: 0 }, claude: { sequence: 0, attempt: 0 } },
    localSequence: { gpt: defaultLocalSequenceState(), claude: defaultLocalSequenceState() },
  };
}

test('a fresh target whose first Lounge message is already seq=70 (accumulated room history) is allowed, using local sequence 1', () => {
  const current = freshSettings();
  const event = { sequence: 70, target: 'gpt', source: 'alice', delivery_attempt: 1, message: 'wake' };
  const plan = planDelivery('gpt', event, current);
  assert.equal(plan.allow, true);
  assert.equal(plan.localSequence, 1);
  assert.equal(plan.loungeSequence, 70, 'the original Lounge sequence must be preserved unchanged for reporting/ack, not replaced by the local one');
});

test('the same fresh target, seq=70, already reported delivery_attempt=4 by the bridge (real failures accumulated before this extension ever ran) is still allowed', () => {
  const current = freshSettings();
  const event = { sequence: 70, target: 'gpt', source: 'alice', delivery_attempt: 4, message: 'wake' };
  const plan = planDelivery('gpt', event, current);
  assert.equal(plan.allow, true);
  assert.equal(plan.localSequence, 1);
});

test('after a real attempt is recorded (success or failure), a same-sequence retry with a higher attempt is allowed and reuses the same local sequence', () => {
  let current = freshSettings();
  const first = planDelivery('gpt', { sequence: 70, target: 'gpt', delivery_attempt: 1 }, current);
  assert.equal(first.allow, true);
  // Simulate persisting the outcome of a FAILED delivery attempt, exactly as
  // service-worker.js now does for every outcome, not just success.
  current = { ...current, delivery: first.nextDelivery, localSequence: { ...current.localSequence, gpt: first.nextLocalSequenceState } };

  const retry = planDelivery('gpt', { sequence: 70, target: 'gpt', delivery_attempt: 2 }, current);
  assert.equal(retry.allow, true);
  assert.equal(retry.localSequence, 1, 'retrying the same still-unacked Lounge message must reuse local sequence 1, not mint a new one');
  assert.equal(retry.loungeSequence, 70);
});

test('service-worker restart: persisted state from before the restart round-trips through planDelivery unchanged', () => {
  let current = freshSettings();
  const before = planDelivery('gpt', { sequence: 70, target: 'gpt', delivery_attempt: 1 }, current);
  current = { ...current, delivery: before.nextDelivery, localSequence: { ...current.localSequence, gpt: before.nextLocalSequenceState } };

  // Simulate the service worker being torn down and recreated: state.json
  // and chrome.storage.local persist, everything in module scope does not.
  const restarted = JSON.parse(JSON.stringify(current));

  const afterRestart = planDelivery('gpt', { sequence: 70, target: 'gpt', delivery_attempt: 2 }, restarted);
  assert.equal(afterRestart.allow, true);
  assert.equal(afterRestart.localSequence, 1);

  const nextLoungeMessage = planDelivery('gpt', { sequence: 71, target: 'gpt', delivery_attempt: 1 }, {
    ...restarted,
    delivery: afterRestart.nextDelivery,
    localSequence: { ...restarted.localSequence, gpt: afterRestart.nextLocalSequenceState },
  });
  assert.equal(nextLoungeMessage.allow, true);
  assert.equal(nextLoungeMessage.localSequence, 2, 'a genuinely new Lounge message after a restart must still get the next local sequence, not collide with 1');
  assert.equal(nextLoungeMessage.loungeSequence, 71);
});

test('claude and gpt track completely independent local sequences', () => {
  const current = freshSettings();
  const gptPlan = planDelivery('gpt', { sequence: 70, target: 'gpt', delivery_attempt: 1 }, current);
  const claudePlan = planDelivery('claude', { sequence: 70, target: 'claude', delivery_attempt: 1 }, current);
  assert.equal(gptPlan.localSequence, 1);
  assert.equal(claudePlan.localSequence, 1);
  assert.notEqual(gptPlan.nextDelivery.gpt, claudePlan.nextDelivery.claude);
});

