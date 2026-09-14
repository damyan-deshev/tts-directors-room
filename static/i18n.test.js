'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const i18n = require('./i18n.js');

assert.equal(i18n.DEFAULT_LOCALE, 'en');
assert.deepEqual(i18n.SUPPORTED_LOCALES, ['en', 'bg']);
assert.deepEqual(Object.keys(i18n.MESSAGES.en).sort(), Object.keys(i18n.MESSAGES.bg).sort());
assert.equal(i18n.interpolate('Hello {name}', {name: 'Higgs'}), 'Hello Higgs');

const root = path.resolve(__dirname, '..');
const sources = [
  fs.readFileSync(path.join(root, 'index.html'), 'utf8'),
  fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8'),
];
const used = new Set();
for (const source of sources) {
  for (const match of source.matchAll(/(?:data-i18n(?:-title|-aria|-placeholder)?=["']|\bt\(["'])([a-z0-9_.-]+)/gi)) {
    used.add(match[1]);
  }
}
const missing = [...used].filter(key => !Object.prototype.hasOwnProperty.call(i18n.MESSAGES.en, key));
assert.deepEqual(missing, [], `Missing translation keys: ${missing.join(', ')}`);
console.log(`i18n OK: ${Object.keys(i18n.MESSAGES.en).length} paired messages, ${used.size} referenced keys.`);
