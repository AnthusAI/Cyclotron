import {describe, expect, it} from 'vitest'
import example from '../../src/decision_flywheel/schemas/cyclotron-status.v1.example.json'
import {isCyclotronStatus, type CyclotronStatus} from './cyclotronStatus'

describe('cyclotron-status/v1', () => {
  it('accepts the published example as a typed status', () => {
    const status: CyclotronStatus = example as CyclotronStatus
    expect(isCyclotronStatus(status)).toBe(true)
    expect(status.reviewRate.reason).toMatch(/target/)
  })

  it('refuses other JSON', () => {
    expect(isCyclotronStatus({schema: 'cyclotron-status/v2'})).toBe(false)
    expect(isCyclotronStatus(null)).toBe(false)
  })
})
