# Recorded trace explorer

React and TypeScript shell for the offline Decision Flywheel trace artifact. Uses
the shadcn CLI's default Radix Nova components, Tailwind v4 semantic tokens, Geist
fonts, and the existing native vis-timeline controller.

```sh
npm ci
npm run build
npm test
```

The build commits self-contained JavaScript, CSS, embedded fonts and upstream
license notices under `src/decision_flywheel/vendor/trace-ui`. Python users do not
need Node or network access to export or view a recording. Rebuild these assets
after changing the UI sources. The Python exporter embeds them, the recording,
and vis-timeline into one HTML file with a no-network content security policy.

The shell mounts synchronously once. Its stable element IDs form the adapter
contract with the trace controller. Local checkbox components bridge Radix state
to the controller's native change events; the root does not re-render over
imperatively populated event details. Requests, responses, labels and
configuration snapshots remain original recording data, not UI-generated data.

New shadcn primitives should be installed with `npx shadcn@latest add <component>`.
Keep generated components under `src/components/ui` and use semantic color tokens.
The production IIFE explicitly replaces `process.env.NODE_ENV` at build time;
there is no Node environment in the exported page. The startup spec executes an
actual Python-exported artifact in jsdom and needs the repository's `.venv`.

The shadcn CLI is a development dependency only. Its current upstream toolchain
has an unpatched `braces` stack-exhaustion advisory (GHSA-vfj7-8cjw-p6xm); do not
feed untrusted glob patterns to the CLI. It is not bundled into the viewer.
