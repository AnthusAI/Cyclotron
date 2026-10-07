// Export the checked HTML through Archify's canonical SVG export.
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const archifyRoot = process.env.ARCHIFY_HOME;
if (!archifyRoot) throw new Error('Set ARCHIFY_HOME to the installed Archify skill directory.');
const { ChromeVisualBrowser, findChrome } = await import(
  pathToFileURL(path.join(archifyRoot, 'bin/visual-check.mjs')).href
);
const browser = new ChromeVisualBrowser(findChrome());
try {
  for (const name of ['classifier', 'prediction', 'improvement', 'evaluation', 'cyclotron-harness', 'cyclotron-scorecards']) {
    const artifactPath = path.resolve('docs/diagrams', `${name}.html`);
    await browser.inspect({ artifactPath, width: 1440, height: 900, theme: 'light' });
    const sessionId = await browser.sessionPromise;
    const result = await browser.cdp.send('Runtime.evaluate', {
      expression: `(async () => {
        let exported;
        const original = URL.createObjectURL;
        URL.createObjectURL = blob => { exported = blob; return original.call(URL, blob); };
        try {
          await Archify.exportMenu.run('svg');
          if (!exported) throw new Error('No SVG was exported.');
          return await exported.text();
        } finally { URL.createObjectURL = original; }
      })()`,
      awaitPromise: true,
      returnByValue: true,
    }, sessionId);
    if (result.exceptionDetails || typeof result.result?.value !== 'string') {
      throw new Error(`Canonical export failed for ${name}.`);
    }
    fs.writeFileSync(path.resolve('docs/diagrams', `${name}.svg`), result.result.value);
    console.log(`Exported ${name}.svg`);
  }
} finally {
  await browser.close();
}
