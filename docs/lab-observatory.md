# The central Cyclotron lab: observe, rate and complete items once, for every site

Status: **design for review** (2026-10-28). Nothing here is built.

## Why

The combined Anthus Cyclotron now makes one decision per item for all six publications, about 4 s each
(2,035 decisions on 2026-10-28). Today you can see those decisions only per site, in each newsroom. Three
things should move to the central lab:

1. **Observe:** every decision, every retraining cycle, and current alignment, in one place.
2. **Rate once for every site:** Accept or Reject each publication's output on one item, in one screen.
   Rating an item once for six sites gives about six times the labels for the same effort.
3. **Complete the item once for every site:** when an item lacks its full content (a landing page instead of
   the article, a PDF nobody extracted), a person fixes it once. Every site then gets the corrected item and a
   fresh decision on it. A fix made in one newsroom also flows back to the Cyclotron and on to the other sites.

## What exists today

- **Items and revisions.** A standing item is keyed by its id, and new values become an immutable new
  revision (r1, r2, …) in the catalog. Values are stored once by content (`valuesRef`).
- **Re-deciding.** `Cyclotron.decide` already supersedes an item's decision when its content fingerprint
  changes. Nothing sends a changed revision today, so it never happens in practice.
- **Labels.** These are recorded per output and per item revision (`record_machine_feedback`). Retractions
  exist.
- **UI.** The lab UI (`trace-ui`) has the pieces: multi-label card, calibration bins, reliability curve,
  versions, cycle timeline, held-out report, status. They read a run's `currentItem` and event stream, which
  is the labeling-session model. They don't yet list a standing run's decisions.
- **Papyrus.** It decides via `POST /machine/decisions` and sends feedback via
  `/machine/publications/{pub}/operations`. Each site keeps its own Reference per URL; pending References
  await human Accept/Reject.

## The central item

An **item** is one piece of source content, shared by every publication. It has:

| Part | Meaning |
|---|---|
| Identity | a stable item id. Papyrus already shares a candidate across monitors by canonical URL; the lab keys on that same identity, so one URL is one item for all sites (to confirm with Papyrus: the id it sends today) |
| Revisions | immutable values: title, url, summary, text, source, attachments, each stored by content |
| Provenance per revision | who or what produced it: the monitor, extraction (PDF, GROBID), a person in the lab, or a publication's editor, and when |
| Completeness | an assessment of each revision (below), recorded with it |
| Decisions | one per revision: every output's label, confidence, probabilities, review routing, request composition, model, and cost |
| Labels | per output and revision, from the lab and from every newsroom, each with who and where |

### Completeness

Each revision gets a completeness assessment computed from its values. It is recorded as data on the
revision, so it can be audited and reproduced. Initial rules (deterministic; no model call):

- `text` missing or empty;
- `text` much shorter than a story. The threshold is a named, reviewed setting shown in the UI, never a cap
  on what's accepted;
- a landing-page signature: text close to the title plus boilerplate;
- a PDF link in the source with no extracted text;
- the request fitter had to trim the target (`composition.target_trimmed`). That is not incomplete, but it is
  worth showing.

Incomplete items are flagged and listed. They're still decided, so nothing stalls; the flag tells people
where help is needed.

### Completing an item

A new revision can come from three places:

1. **The lab:** a person pastes the full text, uploads a PDF, or gives a better URL. A PDF is stored once by
   content; text extraction runs where it runs today (Papyrus's extraction, GROBID), and the extracted text
   becomes the new revision's `text`. Provenance records the person and the method.
2. **A publication:** a newsroom editor who finds the PDF sends the improved values back with a new machine
   operation, `POST /machine/items/{itemId}/revisions` (SigV4, like the decision route). The body carries the
   values, provenance (site, editor, method), and the revision it improves on. The same values as the latest
   revision return it without a write; a stale base revision is refused, so the editor sees the newer content.
3. **The monitor:** re-collection, as today.

**What a new revision does**, as one queued job:
- The Cyclotron re-decides the item for every output (one Jev call), superseding the old decision.
- It then rings the decision-ready doorbell, now carrying the item and its revision.
- Every publication then fetches the new revision and the new decision and updates its Reference, so all six
  sites get the corrected content, not just the one where the fix happened.

**Labels across revisions.** A label applies to the revision it was given on. When content changes, earlier
labels are kept and shown, but are marked as given on revision N. Whether they still count toward training
and alignment is a policy decision (open question 2).

## The lab screens

Everything is on the existing lab app at `lab.cyclotron.anth.us`, under the combined Cyclotron's standing run,
using the lab's existing components where they fit. Icons are Lucide.

1. **Decisions.**
   - **List:** a live, paginated list of decisions, newest first. Each row shows title, source, completeness
     flag, the six outputs' labels and confidences, review routing, and label status per output.
   - **Filters:** publication, label, confidence range, needs review, labeled or unlabeled, incomplete,
     source, and time.
   - **Health:** throughput, queue length, decision time and failures, at the top.
2. **Item detail.**
   - **Content:** the current revision's content, with revision history and provenance.
   - **Decisions:** every output's decision with probabilities, the exact request and response (from the trace
     events), the request composition (examples shown, trimming), and the model and cost.
   - **Labels:** all labels from the lab and every newsroom, with who and when.
3. **Central rating.**
   - **Card:** on the item detail and in a flip-through queue (Prev/Next and swipe, as in the newsroom UI), a
     Cyclotron row and a You row per output, with **Accept** and **Reject** buttons, not dropdowns, and a
     comment box below.
   - **Writing labels:** one submit records a label per rated output, through the same label path as
     newsroom feedback, with the lab as the source.
   - **Queue order:** items needing review first; most uncertain first within those.
4. **Complete content.** On an incomplete item: paste text, upload a PDF, or give a URL. This creates a new
   revision as above, with progress until the re-decision lands.
5. **Alignment.**
   - **Per output, over time:** rolling agreement between the Cyclotron and human labels (accuracy,
     precision, recall), calibration (the existing reliability curve and bins), and the include rate.
   - **Splits:** by version and by label source (lab or newsroom).
   - **Held-out:** the held-out report where one exists.
6. **Retraining.**
   - **Timeline:** priming events, refits, promoted and rejected versions, with what changed (rubric,
     examples), the labels used, and the agreement before and after. This uses the existing versions and
     cycle-timeline components, fed from the standing run's events.
   - **Coverage:** how many labels each output has gained since its last retraining.

## Contracts with Papyrus

| Direction | Change |
|---|---|
| Papyrus → Cyclotron | new `POST /machine/items/{itemId}/revisions` (content update with provenance). Feedback is unchanged, but it carries the item revision it was given on (it already does) |
| Cyclotron → Papyrus | the decision-ready doorbell carries the item id and revision. Papyrus treats a new revision of a known item as an update to every site's Reference: new content and new decision, prior labels kept and marked |
| Both | one item identity across sites (confirm the id Papyrus sends) |

## Build order (each a PR; each deploy is Ryan's call)

1. **Decisions list and item detail** (read-only) on the standing run, with health. These are the
   observability you asked for first.
2. **Alignment and Retraining** views fed from the standing run's events and labels.
3. **Central rating** (Accept/Reject per output) through the shared label path.
4. **Content revisions:** completeness assessment, the lab completion UI, the machine revision route, and
   re-decide and notify. Plus the Papyrus side: send revisions, apply new revisions to every site.

Steps 1–2 need nothing from Papyrus. Step 3 needs only the label conflict rule (open question 1). Step 4 needs
the Papyrus contract.

## Open questions for Ryan

1. **Conflicting labels:** when a lab label and a newsroom label disagree on the same item and output, which
   counts? Proposal: keep both, count the latest per person, and show the disagreement.
2. **Labels after a content change:** do labels on an older revision still count once content changes?
   Proposal: they keep counting until someone labels the new revision, and they are marked as such.
3. **Who may complete content and rate in the lab:** any lab member, or a role?
4. **Completeness threshold:** what "much shorter than a story" means to start. It's a reviewed setting, shown
   in the UI.
