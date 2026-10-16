/**
 * `lib/app-nav.ts` 的导航规则单元测试（纯函数，用 node 跑）。
 *
 * 覆盖：
 *   * `isNavActive` 精确匹配 + matchPrefix 回落 + **不双高亮**
 *   * basePath 剥离
 *   * `activeGroupOf` 分组反查
 *   * `allNavItems` 扁平化完整性
 *
 * 运行：`node --experimental-strip-types lib/app-nav.test.mjs`
 */
import {test} from 'node:test';
import assert from 'node:assert/strict';

import {NAV_GROUPS, allNavItems, isNavActive, activeGroupOf} from './app-nav.ts';

const byHref = (href) => allNavItems().find((i) => i.href === href);

test('allNavItems 扁平化：数量 = 各组之和，且 href 唯一', () => {
  const flat = allNavItems();
  const sum = NAV_GROUPS.reduce((n, g) => n + g.items.length, 0);
  assert.equal(flat.length, sum);
  const hrefs = flat.map((i) => i.href);
  assert.equal(new Set(hrefs).size, hrefs.length, 'href 必须唯一（否则 key 冲突）');
});

test('精确匹配：路径 === href', () => {
  assert.equal(isNavActive(byHref('/accounts'), '/accounts'), true);
  assert.equal(isNavActive(byHref('/accounts'), '/accounts/'), true, '尾斜杠应容忍');
  assert.equal(isNavActive(byHref('/accounts'), '/keys'), false);
});

test('basePath 会被剥掉', () => {
  assert.equal(isNavActive(byHref('/accounts'), '/panel/accounts/', '/panel'), true);
  assert.equal(isNavActive(byHref('/accounts'), '/panel/accounts', '/panel'), true);
});

test('不得双高亮：/models/catalog 只亮「模型中心」不亮「模型」', () => {
  const models = byHref('/models');
  const catalog = byHref('/models/catalog');
  assert.equal(isNavActive(catalog, '/models/catalog'), true);
  assert.equal(
    isNavActive(models, '/models/catalog'), false,
    '「模型」不该借 matchPrefix 抢「模型中心」的高亮（实测踩过的 bug）',
  );
  // 但 /models 自身仍应精确高亮
  assert.equal(isNavActive(models, '/models'), true);
});

test('不得双高亮：/settings/sync 只亮「线上同步」不亮「设置」', () => {
  const settings = byHref('/settings');
  const sync = byHref('/settings/sync');
  assert.equal(isNavActive(sync, '/settings/sync'), true);
  assert.equal(isNavActive(settings, '/settings/sync'), false,
               '「设置」是靠 matchPrefix 兜底的，不该与具体子页同时亮');
  assert.equal(isNavActive(settings, '/settings'), true);
});

test('matchPrefix 兜底：未登记的更深路径落到父条目', () => {
  const settings = byHref('/settings');
  // /settings/not-registered 没有对应条目 → 回落到「设置」
  assert.equal(isNavActive(settings, '/settings/not-registered'), true);
});

test('activeGroupOf 反查分组', () => {
  assert.equal(activeGroupOf('/dashboard'), 'overview');
  assert.equal(activeGroupOf('/accounts'), 'ops');
  assert.equal(activeGroupOf('/stats'), 'data');
  assert.equal(activeGroupOf('/schedules'), 'automation');
  assert.equal(activeGroupOf('/audit'), 'governance');
  assert.equal(activeGroupOf('/backup'), 'system');
  assert.equal(activeGroupOf('/no-such-page'), null);
});

test('每个条目都有 iconKey 与 labelKey（渲染契约）', () => {
  for (const item of allNavItems()) {
    assert.ok(item.iconKey, `${item.href} 缺 iconKey`);
    assert.ok(item.labelKey, `${item.href} 缺 labelKey`);
    assert.ok(item.href.startsWith('/'), `${item.href} 应以 / 开头`);
  }
});
