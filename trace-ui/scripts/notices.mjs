import {readFileSync, readdirSync, writeFileSync, existsSync} from 'node:fs'
import path from 'node:path'

// Keep upstream notices with the self-contained production bundle, including OFL fonts.
const packages = ['react', 'react-dom', 'scheduler', 'radix-ui', 'lucide-react',
  'cn', 'clsx', 'tailwind-merge', 'class-variance-authority', 'tw-animate-css',
  '@fontsource-variable/geist', '@fontsource-variable/geist-mono',
  ...readdirSync('node_modules/@radix-ui').map(name => `@radix-ui/${name}`)]
const notices = packages.map(name => {
  const directory = path.join('node_modules', name)
  if (!existsSync(directory)) return ''
  const licenses = readdirSync(directory).filter(file => /^(license|copying|ofl)(\.|$)/i.test(file))
  return licenses.map(file => `${name}\n${readFileSync(path.join(directory, file), 'utf8')}`).join('\n\n')
}).filter(Boolean).join('\n\n--------------------\n\n')
writeFileSync('../src/decision_flywheel/vendor/trace-ui/THIRD_PARTY_NOTICES.txt', notices)
