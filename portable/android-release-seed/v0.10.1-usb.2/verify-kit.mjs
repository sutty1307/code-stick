import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const manifest = JSON.parse(fs.readFileSync(path.join(root, 'FILES-SHA256.json'), 'utf8'));
let failures = 0;
for (const [relative, expected] of Object.entries(manifest)) {
  const full = path.resolve(root, relative);
  if (!full.startsWith(root + path.sep)) throw new Error('Invalid manifest path');
  try {
    const hash = createHash('sha256');
    for await (const data of fs.createReadStream(full)) hash.update(data);
    if (hash.digest('hex') !== expected) throw new Error('checksum mismatch');
  } catch (error) {
    console.error(`${relative}: ${error.message}`);
    failures++;
  }
}
console.log(`Checked ${Object.keys(manifest).length} files; ${failures} failures.`);
process.exitCode = failures ? 1 : 0;
