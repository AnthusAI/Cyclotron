/**
 * cyclotron-status/v1: what one classifier of a cyclotron is doing now.
 * Mirrors src/decision_flywheel/schemas/cyclotron-status.v1.schema.json.
 * A Python spec checks that every schema field is named here.
 */
export type ReviewRateState = 'full' | 'onboarding' | 'tapering' | 'steady' | 'raised' | 'manual'

export type CyclotronStatus = {
  schema: 'cyclotron-status/v1'
  /** version counts rubric, example and question changes; refits counts ML model refits since then. */
  cyclotron: {id: string; classifier: string; version: number; refits: number; fingerprint: string}
  asOf: string
  alignment: {
    window: number
    labels: number
    accuracy: number | null
    precision: number | null
    recall: number | null
    /** Label precision and recall refer to; null means macro average. */
    positiveLabel: string | null
    measuredOn: string
  }
  calibration: {
    /** Mean confidence of the reviewed decisions. */
    saysSure: number | null
    /** Share of those decisions the reviewers agreed with. */
    isRight: number | null
    gapPoints: number | null
    curve: {bin: number; count: number; confidence: number; accuracy: number}[]
  }
  reviewRate: {
    state: ReviewRateState
    rate: number
    reason: string
    auditFloor: number | null
    confidenceThreshold: number | null
    override: {rate: number; expiresAt: string | null; setBy: string; setAt: string} | null
    /** Decisions until the program next checks its conditions. */
    nextCheckAfterDecisions: number | null
    /** The next step's conditions, in words a reviewer can read. */
    nextStep: string | null
    expectedReviewsPerWeek: number | null
  }
  lastChange: {
    kind: 'promoted' | 'refit' | 'dropped' | 'definition'
    fromVersion: number | null
    toVersion: number
    at: string | null
    summary: string
    /** Labels the ML model was refit on, for a refit. */
    labels: number | null
  } | null
  pending: {decisionsAwaitingReview: number; staleSince: string | null}
}

/** Narrow unknown JSON (for example a stored snapshot) to a v1 status. */
export function isCyclotronStatus(value: unknown): value is CyclotronStatus {
  const status = value as Partial<CyclotronStatus> | null
  return Boolean(status && status.schema === 'cyclotron-status/v1' && status.cyclotron && status.alignment
    && status.calibration && status.reviewRate && status.pending && 'lastChange' in status)
}
