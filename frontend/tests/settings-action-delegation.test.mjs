import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

const read = (name) => fs.readFileSync(new URL(name, import.meta.url), 'utf8');
const settings = read('../src/features/settings.js');
const providers = read('../src/features/ai-provider-settings.js');
const capability = read('../src/features/ai-capability.js');
const bridge = read('../src/bridge.js');
const boot = read('../src/boot.js');
const index = read('../index.html');
const settingsPage = index.slice(index.indexOf('<div id="p-settings"'), index.indexOf('<div id="p-paper"'));

test('设置中心由本页承接动作，禁止内联处理器和全局桥回流', () => {
  assert.doesNotMatch(settings + providers + capability + settingsPage,
    /\bon(?:click|change|input|submit)\s*=["']/);
  assert.doesNotMatch(bridge,
    /window\.(?:unlockOperatorTab|clearOperatorTab|clearWxpusherToken|resetSettingsSection|saveAiProvider|saveAiReviewMode|saveAiSlot|saveSettingsSection|setSettingsSection|testAiSlot|testWxpusher|editAiProvider|changeAiSlotProvider|refreshAiProviderModels|issueAiCapability|revokeAiCapability|refreshAiCapabilities|clearAiProviderKey)\s*=/);
  assert.match(boot, /installSettingsActions\(document\)/);
});

test('所有已渲染的设置动作都有明确分发，检查不能为空', () => {
  const rendered = new Set(Array.from(
    (settings + providers + capability + settingsPage).matchAll(/data-settings-action="([\w-]+)"/g),
    (m) => m[1]));
  const dispatch = settings.slice(
    settings.indexOf('var actions={'),
    settings.indexOf('function dispatch('));
  const handled = new Set(Array.from(
    dispatch.matchAll(/^\s*'([\w-]+)':/gm), (m) => m[1]));
  assert.ok(rendered.size >= 15, '没有采集到完整设置动作，守卫空转');
  assert.deepEqual([...rendered].sort(), [...handled].sort());
});

test('设置页的二级导航也走页面动作，不回到内联处理器', () => {
  const tabs = Array.from(settingsPage.matchAll(/data-settings-action="section-tab" data-settings-section="([a-z]+)"/g),
    (m) => m[1]);
  assert.deepEqual(
    tabs.sort(),
    ['evolution', 'execution', 'notification', 'operator', 'risk', 'simulation', 'strategy'],
    '设置二级导航的分组不完整');
});
