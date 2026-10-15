# Shared priming source review candidate

Local branch: `codex/priming-shared-release`. No remote branch, PR, tag, npm or
PyPI publication was created. Approval target is precisely the public source
branch **AnthusAI/Cyclotron:codex/priming-shared-release**, a **draft PR into main**,
and, only after review/authorized merge and a reviewed package version bump to
**0.2.2**, proposed **ui-v0.2.2**. The current0.2.1 manifest supports the Git-SHA
review candidate; it is not ready to receive that tag. The tag is not
required to install an immutable reviewed Git commit and is not created here.
No cloud activation is part of this source review.

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
priming explanations remain separate from required operational-review explanations.

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
