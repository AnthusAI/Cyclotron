# Changelog

All notable changes to this project are documented in this file.

This changelog is maintained by semantic-release from conventional commits.

## Web package 0.2.1 (2026-10-09)

- Status strip reads "Tapering · 25% reviewed"; the card takes a classifier display name.
- Review control works with no reason list; field ids derive from item and decision ids (fixes a hydration mismatch in server-rendered host pages).
- `seed_rubrics` on `Cyclotron.open` lets an application seed the first rubric without changing the definition.

## Web package 0.2.0 (2026-10-09)

The npm package `cyclotron` (the web components and TypeScript types) is
versioned separately from the Python package and released as a tarball on
GitHub (`ui-v0.2.0`, built with `make pack-ui`).

- New exports: `cyclotron/components/review-control` (ReviewControl),
  `cyclotron/components/cyclotron-status` (CyclotronStatusView),
  `cyclotron/cyclotron-status` (the cyclotron-status/v1 type),
  `cyclotron/sdk-types` (Decision, Review and subscribe event shapes from the
  Python SDK) and `cyclotron/styles/components.css`.
- `styles/components.css` holds component rules only, with no global reset
  and no `:root` theme, so an application can import it safely.
  `styles/shared.css` (the console and marketing theme) imports it.
- The tarball now includes `CalibrationBins.tsx` and
  `CalibrationProvenance.tsx`, which `ModelComparison` and
  `ReliabilityCurve` import; a spec checks that every packed import resolves
  inside the tarball and that none uses the `@/` alias.
- Removed the `cyclotron/lib/utils` export, whose file did not exist.
