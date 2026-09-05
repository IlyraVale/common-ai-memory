import assert from 'node:assert/strict';
import test from 'node:test';

import { defaultLocalSequenceState, resolveLocalSequence } from '../local-sequence.js';

test('a target seeing its first-ever Lounge sequence gets local sequence 1, no matter how large that Lounge sequence is', () => {
  const { localSequence, state } = resolveLocalSequence(defaultLocalSequenceState(), 70);
  assert.equal(localSequence, 1);
  assert.equal(state.currentLoungeSequence, 70);
  assert.equal(state.currentLocalSequence, 1);
  assert.equal(state.nextLocalSequence, 2);
});

test('undefined/missing persisted state is treated the same as a fresh target', () => {
  const { localSequence } = resolveLocalSequence(undefined, 1000);
  assert.equal(localSequence, 1);
});

test('retrying the same Lounge sequence reuses the same local sequence instead of incrementing', () => {
  const first = resolveLocalSequence(defaultLocalSequenceState(), 70);
  const retry = resolveLocalSequence(first.state, 70);
  assert.equal(retry.localSequence, first.localSequence);
  assert.equal(retry.state.nextLocalSequence, first.state.nextLocalSequence);
});

test('a different Lounge sequence always gets the next local sequence, including one lower than before (a stale item re-offered ahead of a newer one that is still awaiting ack)', () => {
  const seenGpt71 = resolveLocalSequence(defaultLocalSequenceState(), 71);
  const seenGpt72 = resolveLocalSequence(seenGpt71.state, 72);
  assert.equal(seenGpt72.localSequence, 2);
  const seenGpt70Again = resolveLocalSequence(seenGpt72.state, 70);
  assert.equal(seenGpt70Again.localSequence, 3);
  assert.equal(seenGpt70Again.state.nextLocalSequence, 4);
});

test('state persisted across a simulated service-worker restart round-trips through resolveLocalSequence unchanged', () => {
  const first = resolveLocalSequence(defaultLocalSequenceState(), 70);
  // Simulate chrome.storage.local persisting `first.state` and the service
  // worker being torn down and recreated before the retry arrives.
  const persisted = JSON.parse(JSON.stringify(first.state));
  const afterRestart = resolveLocalSequence(persisted, 70);
  assert.equal(afterRestart.localSequence, first.localSequence);
  assert.deepEqual(afterRestart.state, first.state);
});

test('resolveLocalSequence never returns or leaks the Lounge sequence as the local sequence', () => {
  const { localSequence } = resolveLocalSequence(defaultLocalSequenceState(), 70);
  assert.notEqual(localSequence, 70);
});


