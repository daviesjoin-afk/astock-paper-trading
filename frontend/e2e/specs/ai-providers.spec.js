import { test, expect, openSettings, clickAndApprove, approveModal, waitForApi } from '../fixtures.js';

test.use({ operatorUnlocked: true });

async function openAi(page){
  await openSettings(page);
  await page.getByTestId('settings-section-evolution').click();
  await expect(page.getByTestId('ai-provider-library')).toBeVisible();
}

async function createProvider(page,name,model){
  await page.getByTestId('ai-provider-editor').selectOption('');
  await page.getByTestId('ai-provider-name').fill(name);
  await page.getByTestId('ai-provider-url').fill('https://custom.example.com/v1');
  await page.getByTestId('ai-provider-key').fill('fake-e2e-provider-key');
  await page.getByTestId('ai-provider-models').fill(model);
  const response=await clickAndApprove(page,page.getByTestId('ai-provider-save'),/\/ai-review\/providers(?:\?|$)/);
  expect(response.status()).toBe(200);
  const result=await response.json();
  await expect(page.getByTestId('ai-provider-editor')).toHaveValue(result.provider.id);
  await expect(page.getByTestId('ai-provider-key')).toHaveValue('');
  return result.provider.id;
}

async function choose(page,slot,providerId,model){
  await page.getByTestId(`ai-slot-${slot}-provider`).selectOption(providerId);
  await page.getByTestId(`ai-slot-${slot}-model`).selectOption(model);
  await page.getByTestId(`ai-slot-${slot}-enabled`).check();
  await page.getByTestId(`ai-slot-${slot}-save`).click();
  const saved=waitForApi(page,new RegExp(`/ai-review/slots/${slot}$`),'PUT');
  await approveModal(page);
  const response=await saved;
  expect(response.status()).toBe(200);
  await expect(page.getByTestId(`ai-slot-${slot}-status`)).toHaveText('已就绪');
}

test('保存任意提供商，AI1、AI2 下拉选择，刷新后保留且不回显密钥',async({page})=>{
  await openAi(page);
  const first=await createProvider(page,'自有服务 <A>','alpha-model\ncommon-model');
  const second=await createProvider(page,'自有服务 B','beta-model');
  await page.getByTestId('ai-slot-ai2-provider').selectOption(second);
  await page.getByTestId('ai-slot-ai2-model').selectOption('beta-model');
  await choose(page,'ai1',first,'alpha-model');
  await expect(page.getByTestId('ai-slot-ai2-provider')).toHaveValue(second);
  await expect(page.getByTestId('ai-slot-ai2-model')).toHaveValue('beta-model');
  await choose(page,'ai2',second,'beta-model');
  await expect(page.getByTestId('ai-review-single-slot').locator('option[value="ai1"]')).toHaveText('AI1 · 自有服务 <A>');
  await expect(page.getByTestId('ai-review-single-slot').locator('option[value="ai2"]')).toHaveText('AI2 · 自有服务 B');
  await page.reload(); await openAi(page);
  await expect(page.getByTestId('ai-slot-ai1-provider')).toHaveValue(first);
  await expect(page.getByTestId('ai-slot-ai1-model')).toHaveValue('alpha-model');
  await expect(page.getByTestId('ai-slot-ai2-provider')).toHaveValue(second);
  await expect(page.getByTestId('ai-slot-ai2-model')).toHaveValue('beta-model');
  await expect(page.getByTestId('ai-slot-ai1-provider').locator('option:checked')).toHaveText('自有服务 <A>');
  expect(await page.getByTestId('ai-slot-ai1-model').evaluate(node=>node.tagName)).toBe('SELECT');
  await expect(page.getByTestId('ai-slot-ai1').locator('input[type=password],input[type=url]')).toHaveCount(0);
  const view=await (await page.request.get('/api/settings/ai-review')).json();
  expect(JSON.stringify(view)).not.toContain('fake-e2e-provider-key');
  await choose(page,'ai1',second,'beta-model');
  await expect(page.getByTestId('ai-slot-ai2-provider')).toHaveValue(second);
  await expect(page.getByTestId('ai-slot-ai2-model')).toHaveValue('beta-model');
  // 两个 AI 共享的提供商修改后，运行配置同步采用新地址。
  await page.getByTestId('ai-provider-editor').selectOption(second);
  await page.getByTestId('ai-provider-url').fill('https://changed.example.com/v1');
  await page.getByTestId('ai-provider-save').click();
  const saved=waitForApi(page,/\/ai-review\/providers\//,'PUT');
  await approveModal(page);
  const response=await saved;
  expect(response.status()).toBe(200);
  const changed=(await response.json()).review;
  expect(changed.slots.ai1.base_url).toBe('https://changed.example.com/v1');
  expect(changed.slots.ai2.base_url).toBe('https://changed.example.com/v1');
});

test('获取模型失败保留手填模型，下拉联动不覆盖另一路选择',async({page})=>{
  await openAi(page);
  const provider=await createProvider(page,'离线模型服务','manual-model\nother-model');
  await page.getByTestId('ai-slot-ai1-provider').selectOption(provider);
  await page.getByTestId('ai-slot-ai1-model').selectOption('manual-model');
  await page.getByTestId('ai-slot-ai2-provider').selectOption(provider);
  await page.getByTestId('ai-slot-ai2-model').selectOption('other-model');
  await page.route('**/providers/*/models/refresh?confirmed=true',route=>route.fulfill({json:{ok:false,error:'network_error',models:['manual-model','other-model']}}));
  await page.getByTestId('ai-provider-refresh').click();
  await expect(page.locator('#aiProviderFeedback')).toContainText('可在上方手动填写模型');
  await expect(page.getByTestId('ai-slot-ai1-model')).toHaveValue('manual-model');
  await expect(page.getByTestId('ai-slot-ai2-model')).toHaveValue('other-model');
  await page.unroute('**/providers/*/models/refresh?confirmed=true');
  await page.route('**/providers/*/models/refresh?confirmed=true',route=>route.fulfill({json:{ok:true,models:['manual-model','other-model','remote-model'],count:3}}));
  await page.getByTestId('ai-provider-refresh').click();
  await expect(page.getByTestId('ai-slot-ai1-model').locator('option[value="remote-model"]')).toHaveCount(1);
  await expect(page.getByTestId('ai-slot-ai1-model')).toHaveValue('manual-model');
  await expect(page.getByTestId('ai-slot-ai2-model')).toHaveValue('other-model');
  // 小屏标签与控件必须在同一列，不发生横向溢出。
  await page.setViewportSize({width:375,height:812});
  const panel=page.getByTestId('ai-slot-ai1');
  const select=await page.getByTestId('ai-slot-ai1-provider').boundingBox();
  const box=await panel.boundingBox();
  expect(select.x+select.width).toBeLessThanOrEqual(box.x+box.width);
  await panel.screenshot({path:'e2e/.artifacts/ai-provider-mobile.png'});
  const library=await page.getByTestId('ai-provider-library').boundingBox();
  const url=await page.getByTestId('ai-provider-url').boundingBox();
  expect(url.x+url.width).toBeLessThanOrEqual(library.x+library.width);
  await page.locator('#themeToggle').click();
  const labelColor=await page.locator('label[for="aiProviderUrl"] b').evaluate(node=>getComputedStyle(node).color);
  const inputColor=await page.getByTestId('ai-provider-url').evaluate(node=>getComputedStyle(node).color);
  expect(labelColor).toBe(inputColor);
  await page.getByTestId('ai-provider-library').screenshot({path:'e2e/.artifacts/ai-provider-mobile-dark.png'});
  await page.locator('#themeToggle').click();
  await page.setViewportSize({width:1440,height:1080});
  await page.getByTestId('settings-result').screenshot({path:'e2e/.artifacts/ai-provider-desktop.png'});
});

test('清除共享提供商密钥会停用两路调用，刷新不会恢复旧密钥',async({page})=>{
  await openAi(page);
  const provider=await createProvider(page,'待清除密钥的服务','clear-model');
  await choose(page,'ai1',provider,'clear-model');
  await choose(page,'ai2',provider,'clear-model');
  await page.getByTestId('ai-provider-editor').selectOption(provider);
  await page.locator('#aiProviderClear').click();
  const saved=waitForApi(page,/\/ai-review\/providers\//,'PUT');
  await approveModal(page);
  expect((await saved).status()).toBe(200);
  await expect(page.getByTestId('ai-slot-ai1-status')).toHaveText('未就绪');
  await expect(page.getByTestId('ai-slot-ai2-status')).toHaveText('未就绪');
  await page.reload(); await openAi(page);
  await expect(page.getByTestId('ai-slot-ai1-status')).toHaveText('未就绪');
  await expect(page.getByTestId('ai-slot-ai2-status')).toHaveText('未就绪');
  await page.getByTestId('ai-provider-editor').selectOption(provider);
  await expect(page.locator('#aiProviderKeyStatus')).toHaveText('未配置密钥');
});
