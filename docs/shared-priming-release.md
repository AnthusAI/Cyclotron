# Shared priming release and pin

## Published pin

The current pin is one commit,
**002e6f2b933aa528980338230bb4a4003afc2938** on `main` (engine v0.30.0, web
package 0.2.3). Both halves come from that commit, and a host pins the commit,
never a tag or a branch.

| Half | Release | Pin |
|---|---|---|
| Python engine | [v0.30.0](https://github.com/AnthusAI/Cyclotron/releases/tag/v0.30.0) | `decision-flywheel[web,priming] @ git+https://github.com/AnthusAI/Cyclotron.git@002e6f2b933aa528980338230bb4a4003afc2938` |
| UI package | [ui-v0.2.3](https://github.com/AnthusAI/Cyclotron/releases/tag/ui-v0.2.3) | `https://github.com/AnthusAI/Cyclotron/releases/download/ui-v0.2.3/cyclotron-0.2.3.tgz` with lock integrity `sha512-5WTojm9teGKHIs+p7YdBFP/EgTJrcP0PdPcU67VNjEYRLgeuQ2gq9UvL40Lv5bty7gf9DtnlLMWyjdbdMRr+RA==` (SHA-256 `60ec1d3dcb40626008e36f96d147427a9c173a4fdfeebf93cd61864405723a76`) |

A host that also reads repository files outside the package, such as
`docs/diagrams`, pins the source archive of the same commit instead:
`https://github.com/AnthusAI/Cyclotron/archive/002e6f2b933aa528980338230bb4a4003afc2938.tar.gz`.
Cyclotron-web does this.

Verified 2026-10-20: `ui-v0.2.3` points at the pinned commit, and
`scripts/dev/release_ui.sh` ran the web tests and build there, checked the
vendored bundle, and packed the uploaded tarball (39 files). The fresh-consumer
checks below were run for the earlier 0.2.2 pin and not repeated for 0.2.3.

Earlier pin (2026-10-17): commit a565dafb1f6953e1eb4d2efb1069396303afe0c6,
engine v0.21.0, `ui-v0.2.2`.

Verified 2026-10-17:

- `v0.21.0` peels to the pinned commit and `ui-v0.2.2` points at it. The
  [Test and release run](https://github.com/AnthusAI/Cyclotron/actions/runs/39011967316)
  on the main promotion `733ef29` passed test, diagrams and release.
- The uploaded `cyclotron-0.2.2.tgz` unpacks to exactly the 36 files that
  `npm pack` produces at the pinned commit, so the asset is the tagged source.
- A fresh virtualenv installing `@v0.21.0` resolves to the pinned commit and
  reports version 0.21.0.
- A fresh Next 15.5 / React 19 app with `transpilePackages: ["cyclotron"]`,
  installed from the release asset, imports every concrete export in
  `package.json` (including `components/priming-panel` and `priming-client`)
  and prerenders.
- Cyclotron-web pinned to the commit archive passes its tests, typecheck and
  static build.

Neither GitHub release is marked immutable, so the tags themselves can still be
moved by a repository admin. The commit hash and the lock integrity above do not
depend on the tags. Hosted activation still needs real account authorization
and storage acceptance; this pin does not claim either.

## Review candidate (historical)

The rest of this document is the source review record that preceded the
release. It describes the local branch `codex/priming-shared-release` as it
stood before promotion, including the 0.2.1 manifest and the `FULL_SHA`
placeholder that the published pin above now fills.

## Composition

Public integration base is c09f70b33b2b96a4e4cc2acca360af47d26620a8, Python0.20.1.
The supported storage source is 37fe4f9dab562f2e606d72c94a2a0a82d00400cd.
Their fork e14323cdc9c894a2b6a9fce7f8e63e89fb5a49ca differs from public main
only in the two version tokens, independently verified. The source delta is
path-filtered onto public main, preserving version0.20.1 and new core exports.
Source-branch ancestry is not merged. Deployment files, activation evidence,
Kanbus state, generated console vendor assets and console frontend are excluded.
The standalone console frontend is not advertised as refreshed by this candidate.

Exact source hashes and paths are in shared-priming-release-manifest.json.
Storage/priming/S3/guard code remains the supported storage owner's source;
old native priming/engine files are never overlaid. Our three publication modules,
five tests, execution harness and deployment-contract documentation come from
0123708 (unchanged publication implementation from9911). The shared PrimingPanel,
client, LabelingReview and ReviewControl also exactly match0123708. Optional
priming and operational-review explanations are optional; their input purposes
remain separate. A later narrow publication-adapter correction accepts omitted,
null and blank operational explanations while retaining required configured labels
and the native SDK's existing whitespace normalization. No SDK mode or replay
normalization changes are included.

The storage owner confirmed additive dev extra includes priming, and web includes
its priming self-extra for the advertised file/XLSX APIs. Accounts and
accounts-cloud extras are retained. Base Python installation keeps optional
provider/web dependencies optional. S3 authority is settled; permissions/session
composition and activation remain host responsibilities.

## Consumer install and lock contract

Once this exact source commit is authorized and reachable remotely, replace
FULL_SHA below with its complete immutable hash (never a moving branch):

```sh
npm install --ignore-scripts --save-exact 'cyclotron@git+https://github.com/AnthusAI/Cyclotron.git#FULL_SHA'
python -m pip install 'decision-flywheel[web,priming] @ git+https://github.com/AnthusAI/Cyclotron.git@FULL_SHA'
```

For an accounts/S3 cloud host use the same source SHA with `[accounts-cloud]`.
The browser imports `cyclotron/components/priming-panel`, `cyclotron/priming-client`,
`cyclotron/components/review-control`, `cyclotron/components/cyclotron-status`,
and the existing styles. React/ReactDOM>=19 are peers. Root package dependencies
include Radix UI, lucide-react, cn, class-variance-authority and bundled font
packages. TypeScript/TSX sources are exported for the consumer's bundler.
Python imports `decision_flywheel.publication_lab`, `.publication_records`,
`.publication_steering`, plus the existing priming and SDK APIs. One source SHA
must supply both Python and UI; no undocumented local overlay may be required.

Root package remains `private:true`, manifest version0.2.1, with no prepare,
install or other lifecycle script. `npm pack --offline --ignore-scripts` succeeds.
Do not publish to npm's unrelated cyclotron package. Commit both the Papyrus
package.json Git-SHA dependency and package-lock.json resolved source/hash changes;
review that no local file:/tmp archive or moving branch remains. Run npm ci with
--ignore-scripts and the actual integrated UI/HTTP checks in a clean consumer.
The exact local pack hash is review evidence, not a claim that npm's Git lock
format necessarily stores a tarball integrity field. If a vetted tarball is used
instead, retain its integrity and immutable source provenance explicitly.

Python wheel is0.20.1 for local review only. Do not upload it as a new0.20.1
registry release. The existing semantic-release main workflow chooses a version
only after separately authorized integration; upload_to_pypi=false. No PyPI lane
is introduced. A Git-SHA source dependency is the concrete proposed initial pin.

## Proof and limitations

208 focused publication/priming/S3/execution tests,83 actual Papyrus publication
harness tests,21 shared UI tests, and TypeScript tsc-b pass. Tests are offline;
providers/S3 use fakes and publication transport uses ASGI. Reused existing local
node dependencies; no heavy installation or cloud action. A local no-isolation
wheel build succeeds and contains publication/priming/S3 modules and0.20.1 metadata.
The36-file UI pack contains all concrete exports and exact reviewed UI sources.
Pack SHA256:46228c0dfbc6ac52b882fe3136d5c90c03e456d5df92bb8792fd7dcfe97d5193.
These focused checks do not claim the entire historical suite or hosted activation.

Optional operational explanation correction:55 focused publication tests and97
cross-owner publication/Papyrus checks pass (overlapping sets). Both embedded and
HTTP reviews accept omitted/null/empty/whitespace explanations, preserve native
normalization on reopen/retry, reject non-text/oversize values and missing or
unconfigured labels. Native engine, replay, guard and binding policies are unchanged.
