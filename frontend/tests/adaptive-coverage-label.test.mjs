import test from 'node:test';
import assert from 'node:assert/strict';
import { adaptiveCoverageLabel } from '../src/features/adaptive.js';

/* 闭环准入台的成交覆盖口径：分母只能来自后端 owner 的当前周期参与集合。
   拿不到分母时只报分子，绝不写死策略数量，也不出现 "/ 0"。 */

test('没有参与集合分母时只报分子，不声称比例', () => {
  assert.equal(adaptiveCoverageLabel(2, undefined), '2 个账户');
  assert.equal(adaptiveCoverageLabel(2, null), '2 个账户');
  assert.equal(adaptiveCoverageLabel(2, 0), '2 个账户');
  assert.equal(adaptiveCoverageLabel(2, 'not-a-number'), '2 个账户');
});

test('有分母时报当前参与账户，不用退役时代的固定数字', () => {
  assert.equal(adaptiveCoverageLabel(3, 3), '3 / 3 当前参与账户');
  assert.equal(adaptiveCoverageLabel(1, '3'), '1 / 3 当前参与账户');
  assert.doesNotMatch(adaptiveCoverageLabel(1, 3), /\/\s*5\b/);
});

test('缺失或非法分子退成 0，不打印 NaN', () => {
  assert.equal(adaptiveCoverageLabel(undefined, 3), '0 / 3 当前参与账户');
  assert.equal(adaptiveCoverageLabel(null, undefined), '0 个账户');
  assert.equal(adaptiveCoverageLabel('x', 3), '0 / 3 当前参与账户');
});
