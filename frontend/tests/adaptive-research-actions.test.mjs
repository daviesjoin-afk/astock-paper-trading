import test from 'node:test';
import assert from 'node:assert/strict';
import { adaptiveShanghaiDate } from '../src/features/adaptive.js';

test('research business date follows Shanghai when browser local date differs', () => {
  const instant = new Date('2026-09-27T00:30:00.000Z');
  const browserLocal = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/Los_Angeles', year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(instant);
  assert.equal(browserLocal, '2026-09-26');
  assert.equal(adaptiveShanghaiDate(instant), '2026-09-27');
});
