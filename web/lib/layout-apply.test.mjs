/**
 * `lib/layout-apply.ts` 的 `applyLayout` 单元测试（纯函数，node 跑）。
 *
 * 覆盖：隐藏、排序、分组改名、新增页面自动可见、以及**配置页不可隐藏**的保护。
 *
 * 运行：`node --experimental-strip-types lib/layout-apply.test.mjs`
 *
 * 为什么被测逻辑不在 `layout-context.tsx` 里：那个文件含 JSX，Node 的
 * type stripping **不处理 .tsx**（实测报 ERR_UNKNOWN_FILE_EXTENSION），
 * 纯逻辑放那儿就没法单测。所以拆成 `layout-apply.ts`（参见该文件顶部说明）。
 */
import {test} from 'node:test';
import assert from 'node:assert/strict';

import {applyLayout, MUST_KEEP_VISIBLE} from './layout-apply.ts';
import {NAV_GROUPS, allNavItems} from './app-nav.ts';

const flat = () => allNavItems().map((i) => i.href);

test('MUST_KEEP_VISIBLE 包含布局配置页', () => {
  assert.ok(MUST_KEEP_VISIBLE.includes('/settings/layout'));
});

test('默认配置：全部可见且顺序不变', () => {
  const g = applyLayout(NAV_GROUPS, {hidden: [], order: [], groupLabels: {}});
  assert.deepEqual(g.flatMap((x) => x.items.map((i) => i.href)), flat());
});

test('隐藏：被隐藏的条目不出现', () => {
  const g = applyLayout(NAV_GROUPS, {hidden: ['/audit', '/logs'], order: [], groupLabels: {}});
  const hrefs = g.flatMap((x) => x.items.map((i) => i.href));
  assert.ok(!hrefs.includes('/audit'));
  assert.ok(!hrefs.includes('/logs'));
  assert.ok(hrefs.includes('/accounts'), '其他条目不受影响');
});

test('配置页不可隐藏（防用户把自己锁在门外）', () => {
  const g = applyLayout(NAV_GROUPS, {hidden: ['/settings/layout'], order: [], groupLabels: {}});
  const hrefs = g.flatMap((x) => x.items.map((i) => i.href));
  assert.ok(hrefs.includes('/settings/layout'),
            '即使写进 hidden 也必须可见 —— 否则用户再也进不去配置页恢复');
});

test('排序：order 里的在前，未列出的保持默认顺序在后', () => {
  const g = applyLayout(NAV_GROUPS, {hidden: [], order: ['/accounts', '/dashboard'], groupLabels: {}});
  const hrefs = g.flatMap((x) => x.items.map((i) => i.href));
  // 组内顺序变了，但「两组各有自己的次序」——
  // /accounts 属 ops 组、/dashboard 属 overview 组，所以看组内相对次序
  const ops = g.find((x) => x.key === 'ops');
  assert.equal(ops.items[0].href, '/accounts', '/accounts 应在 ops 组内排第一');
  const ov = g.find((x) => x.key === 'overview');
  assert.equal(ov.items[0].href, '/dashboard');
});

test('分组改名：labelOverride 注入', () => {
  const g = applyLayout(NAV_GROUPS, {hidden: [], order: [], groupLabels: {ops: '运维组'}});
  const ops = g.find((x) => x.key === 'ops');
  assert.equal(ops.labelOverride, '运维组');
  const data = g.find((x) => x.key === 'data');
  assert.ok(!data.labelOverride, '未改名的组不带 labelOverride');
});

test('空组被剔除（全隐藏的组不渲染空标题）', () => {
  const dataHrefs = NAV_GROUPS.find((x) => x.key === 'data').items.map((i) => i.href);
  const g = applyLayout(NAV_GROUPS, {hidden: dataHrefs, order: [], groupLabels: {}});
  assert.ok(!g.some((x) => x.key === 'data'), '数据组全隐藏后不应出现空组');
});

test('未知 href 被忽略（不报错、不影响渲染）', () => {
  const g = applyLayout(NAV_GROUPS, {hidden: ['/nope'], order: ['/nope', '/accounts'], groupLabels: {}});
  const hrefs = g.flatMap((x) => x.items.map((i) => i.href));
  assert.equal(hrefs.filter((h) => h === '/nope').length, 0);
  assert.ok(hrefs.includes('/accounts'));
  assert.equal(hrefs.length, flat().length, '未知项不应减少真实条目');
});

test('新增页面自动可见（差量存法的关键好处）', () => {
  // 模拟：用户存过布局（隐藏 audit），之后项目新增了页面
  const cfg = {hidden: ['/audit'], order: ['/accounts'], groupLabels: {}};
  const g1 = applyLayout(NAV_GROUPS, cfg);
  const hrefs1 = g1.flatMap((x) => x.items.map((i) => i.href));
  // 所有「未被显式隐藏」的条目都在 → 将来加页面时它默认就在菜单里
  for (const h of flat()) {
    if (h === '/audit') continue;
    assert.ok(hrefs1.includes(h), `${h} 应可见（差量布局不该让新页面消失）`);
  }
});
