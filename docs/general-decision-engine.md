# Cyclotron is a general decision engine: bindings, not publications

Status: **design for review** (2026-10-29). Nothing here is built.

## Why

Cyclotron decides about items and learns from people's reviews. News publications (the Papyrus sites) are its
first heavy user. The next ones are different: filtering email, and classifying inbound leads for a
lead-generation business. Ryan, 2026-10-29: "Cyclotron and the lab UI need to be generalized."

Over the last weeks the word "publication" spread from the Papyrus integration into Cyclotron's own concepts:
the hosted contract, the run configuration, the routes, the stored job kind, the lab API and the lab UI. Most of
that code is already general. It runs a standing run, bound to named consumers that each own some outputs, and
takes labels back from people at the caller. Only the names are about publications. This design renames the
general parts, keeps the old names working until Papyrus moves, and leaves the parts that really are Papyrus's
inside a Papyrus adapter.

## The test for every name

A name passes if it reads correctly for all three of these:

| Use | Items | A binding (one consumer) | Its outputs | Who reviews |
|---|---|---|---|---|
| News | a story, a paper, a filing | `anth-us`, `distinctive` | `anth-us-relevance` | an editor in that site's newsroom |
| Email | one message | `support-inbox` | `needs-human`, `spam` | a support agent in the help desk |
| Leads | one inbound lead | `acme-sales` | `qualified`, `segment` | a sales rep in the CRM |

"Publication", "newsroom", "site" and "editor" fail the email and lead rows. "Binding", "standing run",
"caller", "output" and "reviewer" pass all three.

## Vocabulary

These rows go into `docs/glossary.md`, which stays the source of truth.

| Concept | Use | Retire (as a generic word) |
|---|---|---|
| A long-lived run that decides each item a caller sends, and learns from the reviews that come back | standing run | publication run |
| One named consumer of some of a standing run's outputs, at a pinned revision (Anth.us, a support inbox, a sales team) | binding | publication |
| The application that sends items and reviews on a binding's behalf (Papyrus, a mail gateway, a CRM) | caller | site, host app |
| Where a label came from | review source: `lab` or `caller` | newsroom |
| The person at the caller who reviewed | reviewer (already the glossary word) | editor (outside the editorial example) |

Why "binding": the code already uses it for exactly this. `/machine/decisions` takes `bindings: {id: revision}`,
feedback carries `bindingRevision`, and `PublicationBinding` is a binding plus a publication-only name. Why not
"target": the glossary already retires "target" as a generic word (it collided with the item being judged), so
`PublicationTargets` becomes "standing runs", not "targets". Why not "client" or "consumer": neither appears in the
code for this concept, and "client" already means the HTTP/embedded SDK (`HTTPPublicationClient`).

A binding also needs a **display name**. Today it has only an id (`anth-us`), and the lab shows the id. A display
name lives in the standing-run configuration beside the id, so the lab can say "Decides for Anth.us, Shipped"
without knowing what a publication is.

## What changes

### General code with publication names (rename, keep an alias)

| Today | Becomes | Compatibility |
|---|---|---|
| contract `cyclotron-publication/v1` (`publication_lab.py`) | `cyclotron-decision/v1` | v1 accepted as an alias, same operations |
| `PublicationBinding`, `PublicationTarget`, `PublicationLab`, `PublicationActor`, `*PublicationClient`, `create_publication_*` | `Binding`, `StandingRunConfig`, `DecisionLab`, `CallerActor`, `*DecisionClient`, `create_decision_*` | the old Python names stay as deprecated aliases for one release |
| env `CYCLOTRON_PUBLICATION_TARGETS`, stack parameter `PublicationTargets`; JSON key `publicationId` | `CYCLOTRON_STANDING_RUNS`, `StandingRuns`; `bindingId` (plus optional `name`) | the loader reads either, and refuses both at once; the stack moves in one change set |
| `/machine/publications/{publication}/operations`, `…/commands/{job_id}`; `/api/publications/{publication}/…` | `/machine/bindings/{binding}/operations`, `…/commands/{job_id}`; `/api/bindings/{binding}/…` | old paths routed to the same handlers until Papyrus has moved; IAM grants both |
| `/api/publication-runs/bootstrap`, config key `publication_bootstrap`, record kind `publication-bootstrap` | `/api/standing-runs/bootstrap`, `standing_bootstrap`, `standing-bootstrap` | old route kept; stored runs keep their frozen config (never rewritten: the workspace binding is its hash) and are read under either key |
| stored job kind `publication`; record kind `publication-record`; event field `publications` | `decision`; `decision-record`; `bindings` | dual read forever for stored rows; new rows use the new kind; no rewrite of history |
| lab API `standing-runs` field `publications`, per-output `publication` | `bindings` (with `name`), per-output `binding` | the lab UI is the only reader; both ship together |
| error copy "publication binding changed", "output is not part of this publication binding", "publication contract" | "binding changed", "output is not part of this binding", "decision contract" | copy only |

### Papyrus's own parts (move behind an adapter)

- **Review source.** Feedback today must carry `editor: {site, subject}`, and the lab turns a
  `publication-machine-feedback` event into `{kind: "newsroom", publication, site, editor, asserted}`
  (`hosted_machine_publications.py`, `lab_decisions.label_source`). The general shape is
  `{kind: "caller", binding, caller, reviewer, asserted, principal}`. `caller` is the calling application's
  name for where the review happened (for Papyrus, the site), `reviewer` is the reviewer's subject. The old
  `editor` field is accepted and mapped onto it, so Papyrus needn't change on day one.
- **Caller credentials.** `VerifiedSiteCredential` and `prospectus_site_credentials.py` verify a credential the
  calling app forwards. The mechanism is general; it becomes a caller credential, with Prospectus as one
  verifier.
- **The curation route** `POST /api/lab/publication` is Papyrus's. It moves under the Papyrus adapter and keeps
  its path.

The machine routes stay SigV4 with the same invoker role. A new caller, such as a mail gateway, gets its own
standing run (or bindings on a shared one) in the same configuration, and none of its code says "publication".

### Lab UI

- **Copy:** "Publication output" becomes "Output". "· publication X" becomes "· for {binding name}". "An editor at
  a newsroom (asserted by the site)" becomes "{reviewer} at {caller} (asserted by the caller)". The
  sample question "Is this relevant to the publication?" becomes neutral.
- **Retired words:** `glossary_test.py` gains `publication`, `newsroom` and `site` as retired words for trace-ui
  copy, with the allowlist for Papyrus-specific strings that must keep them.
- **Display names:** the Decisions view, the Cyclotrons list and the run picker show binding display names.

## Build order (each a PR, merged green; each deploy as usual)

1. **Lab copy and the lab API** (no external contract change). The UI copy, the `standing-runs` and decisions
   fields renamed (`bindings`, `binding`), the review source shape in the lab API, and glossary rows plus the
   retired-word check. Ships on its own.
2. **Neutral names beside the old ones.** The new contract id, routes, bootstrap route, configuration key and
   stack parameter, with every old name still accepted. New stored rows use the `decision` kinds; reads accept
   both. Python aliases for the renamed classes. Docs (`hosted-publications.md`,
   `publication-deployment-contract.md`) become `standing-runs.md` and `decision-contract.md`, with a
   Papyrus section.
3. **Papyrus moves** to the new routes, contract id, `bindingId` and the `caller`/`reviewer` feedback shape. This
   is a Papyrus PR; Cyclotron changes nothing.
4. **Old names retire.** Once nothing has called an old route for a release, the aliases for routes, contract id,
   configuration key and Python names are removed. Stored job and record kinds keep their dual read: history is
   never rewritten.

Step 1 needs nothing from Papyrus. Step 2 is additive. Step 3 is the only cross-repo step, and step 4 waits for
it.

## What does not change

- Cyclotron's engine (classifiers, decisions, reviews, cycles) already says nothing about publications.
- Stored runs, items, labels and records keep their bytes. A run's frozen configuration is never rewritten.
- One label still belongs to one binding's output (Ryan, 2026-10-29). In the new words: a review sent on one
  binding trains and measures only that binding's outputs.

## Open questions for Ryan

1. **"Binding" in the UI.** The code says binding. In the lab, should people see "binding", or just the
   display name ("Decides for Anth.us")? Proposal: show display names, and call the concept "binding" only on
   configuration screens.
2. **Where display names live.** In the standing-run configuration (one place, deployed with the stack), or in the
   account catalog (editable in the lab)? Proposal: the catalog, so a new use case needs no stack change for a
   label.
3. **A first non-news use case to prove it.** Email filtering or lead scoring, end to end against the new names,
   before step 4? Proposal: yes, a small lead-scoring standing run in its own account.
