// Render every D2 diagram in this directory to one self-theming SVG.
//
//   node render.mjs           write <name>.svg for each <name>.d2
//   node render.mjs --check   fail if any committed SVG is out of date
//
// Files that start with "_" are shared imports, not diagrams.

import { readdir, readFile, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { D2 } from '@d2lang/d2';

const here = dirname(fileURLToPath(import.meta.url));
const check = process.argv.includes('--check');

const names = (await readdir(here)).filter((name) => name.endsWith('.d2')).sort();
const fs = Object.fromEntries(
  await Promise.all(names.map(async (name) => [name, await readFile(join(here, name), 'utf8')])),
);
const css = await readFile(join(here, '_cyclotron.css'), 'utf8');

const d2 = new D2();
const stale = [];
try {
  for (const name of names.filter((name) => !name.startsWith('_'))) {
    const { diagram, renderOptions } = await d2.compile({ fs, inputPath: name });
    const svg = withRoleStyles(await d2.render(diagram, renderOptions), css);
    const output = join(here, name.replace(/\.d2$/, '.svg'));
    if (check) {
      const committed = await readFile(output, 'utf8').catch(() => null);
      if (committed !== svg) stale.push(output);
    } else {
      await writeFile(output, svg);
      console.log(`rendered ${name}`);
    }
  }
} finally {
  await d2.dispose();
}

if (stale.length) {
  console.error(`Out-of-date diagrams; run \`make diagrams\`:\n  ${stale.join('\n  ')}`);
  process.exit(1);
}

// D2 tags each shape and connection group with its classes. Append the role
// stylesheet last inside the diagram so it overrides the theme colours.
function withRoleStyles(svg, styles) {
  const end = svg.lastIndexOf('</svg></svg>');
  if (end < 0) throw new Error('Unexpected D2 SVG structure');
  return `${svg.slice(0, end)}<style>${styles.replace(/\s+/g, ' ').trim()}</style>${svg.slice(end)}`;
}
