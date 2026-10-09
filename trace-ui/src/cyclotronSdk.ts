/**
 * JSON shapes the Python application SDK (decision_flywheel.Cyclotron)
 * returns: Decision.to_json(), Review.to_json() and subscribe() events.
 * A Python spec checks that every key the SDK writes is named here.
 */
import type {ReviewRateState} from './cyclotronStatus'

export type CyclotronDecision = {
  decisionId: string
  itemId: string
  createdAt: string
  classifiers: Record<string, {
    label: string
    confidence: number | null
    probabilities: Record<string, number> | null
    /** Language and structure version; ML model refits do not change it. */
    version: number
    fingerprint: string
  }>
  review: {
    selected: boolean
    /** program: low confidence or sampled at the rate; audit: the random audit share; null: not sent to review. */
    reason: 'program' | 'audit' | null
    propensity: number
    detail: string
  }
}

export type CyclotronReview = {
  reviewId: string
  decisionId: string
  itemId: string
  classifier: string
  kind: 'label' | 'correction' | 'undo' | 'no-label'
  label: string | null
  explanation: string | null
  reasonCode: string | null
  reviewer: string | null
  selectedBy: 'program' | 'audit' | 'reviewer'
  createdAt: string
}

type EventBase = {cyclotronId: string; at: string; cursor: number}

export type CyclotronEvent = EventBase & (
  | {kind: 'decision'; decision: CyclotronDecision}
  | {kind: 'review'; review: CyclotronReview}
  | {kind: 'promoted' | 'refit' | 'dropped'; classifier: string; version: number; refits: number;
     summary: string; labels: number | null; fingerprint: string | null}
  | {kind: 'review-rate-changed'; state: ReviewRateState; rate: number; reason: string;
     evidence?: {labels: number; accuracy: number | null; gap_points: number | null; enough_evidence: boolean; met: boolean};
     window?: number}
)

/** One page from subscribe(after, limit). */
export type CyclotronEventPage = {events: CyclotronEvent[]; cursor: number}
