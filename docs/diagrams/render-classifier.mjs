import fs from 'node:fs/promises';
import { D2 } from '@d2lang/d2';

const d2 = new D2();
try {
  const source = await fs.readFile(new URL('classifier.d2', import.meta.url), 'utf8');
  // Compile once: both themes have exactly the same graph and layout.
  const compiled = await d2.compile(source, { layout: 'elk' });
  for (const [name, themeID] of [['light', 0], ['dark', 200]]) {
    const svg = await d2.render(compiled.diagram, {
      ...compiled.renderOptions, themeID, pad: 30, salt: `classifier-${name}`,
    });
    await fs.writeFile(new URL(`classifier-${name}.svg`, import.meta.url), svg);
    console.log(`Rendered classifier-${name}.svg`);
  }
} finally {
  await d2.dispose();
}
