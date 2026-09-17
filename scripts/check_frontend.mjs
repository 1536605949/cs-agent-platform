#!/usr/bin/env node
/**
 * Static consistency check for the build-free frontend.
 *
 * The pages have no bundler and no test runner, so a typo in an element id
 * would only surface as a runtime crash in the browser. This script cross
 * checks every `$('id')` / `getElementById('id')` lookup in the JS against the
 * `id="..."` attributes present in the matching HTML file.
 *
 * Usage: node scripts/check_frontend.mjs
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const staticDir = join(root, 'app', 'static');

const PAIRS = [
  { html: 'index.html', js: 'app.js', label: '聊天页' },
  { html: 'admin.html', js: 'admin.js', label: '管理后台' },
];

// Ids that are created dynamically at runtime rather than present in the HTML.
const DYNAMIC_IDS = new Set(['rating-box']);

let failures = 0;

for (const { html, js, label } of PAIRS) {
  const htmlSource = readFileSync(join(staticDir, html), 'utf8');
  const jsSource = readFileSync(join(staticDir, js), 'utf8');

  const htmlIds = new Set([...htmlSource.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]));

  const lookups = new Set();
  for (const pattern of [/\$\('([^']+)'\)/g, /getElementById\('([^']+)'\)/g]) {
    for (const match of jsSource.matchAll(pattern)) lookups.add(match[1]);
  }

  const missing = [...lookups].filter((id) => !htmlIds.has(id) && !DYNAMIC_IDS.has(id));
  const unused = [...htmlIds].filter((id) => !lookups.has(id));

  console.log(`\n== ${label}（${html} + ${js}）==`);
  console.log(`   HTML 中 id 数量: ${htmlIds.size}    JS 中查找的 id 数量: ${lookups.size}`);

  if (missing.length) {
    failures += missing.length;
    console.log(`   [FAIL] JS 查找了 HTML 中不存在的 id: ${missing.join(', ')}`);
  } else {
    console.log('   [PASS] JS 查找的所有 id 均存在于 HTML');
  }

  if (unused.length) {
    console.log(`   [INFO] HTML 中未被 JS 直接引用的 id（可能是纯样式用途）: ${unused.join(', ')}`);
  }

  // Every static asset the page loads must exist on disk.
  const assets = [...htmlSource.matchAll(/(?:href|src)="(\/static\/[^"]+)"/g)].map((m) => m[1]);
  const missingAssets = [];
  for (const asset of assets) {
    const file = join(staticDir, asset.replace('/static/', ''));
    try {
      readFileSync(file);
    } catch {
      missingAssets.push(asset);
    }
  }
  if (missingAssets.length) {
    failures += missingAssets.length;
    console.log(`   [FAIL] 引用了不存在的静态资源: ${missingAssets.join(', ')}`);
  } else {
    console.log(`   [PASS] ${assets.length} 个静态资源引用全部存在`);
  }

  // Guard against the classic "element is null" crash: every addEventListener
  // target must have been verified to exist above.
  const eventTargets = [...jsSource.matchAll(/\$\('([^']+)'\)\.addEventListener/g)].map((m) => m[1]);
  const badTargets = eventTargets.filter((id) => !htmlIds.has(id) && !DYNAMIC_IDS.has(id));
  if (badTargets.length) {
    failures += badTargets.length;
    console.log(`   [FAIL] 对不存在的元素绑定事件: ${badTargets.join(', ')}`);
  } else {
    console.log(`   [PASS] ${eventTargets.length} 处事件绑定目标全部存在`);
  }
}

console.log(`\n${'='.repeat(52)}`);
console.log(failures === 0 ? '结果：全部通过' : `结果：${failures} 项失败`);
console.log('='.repeat(52));
process.exit(failures === 0 ? 0 : 1);
