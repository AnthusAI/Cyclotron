import { afterEach, expect, test, vi } from 'vitest'
import { requestId } from './requestId'

afterEach(() => vi.unstubAllGlobals())

test('a plain HTTP browser without randomUUID can create unique submission IDs', () => {
  let sequence = 0
  vi.stubGlobal('crypto', {
    getRandomValues: (bytes: Uint8Array) => {
      bytes.fill(++sequence)
      return bytes
    },
  })
  const first = requestId()
  expect(first).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  expect(requestId()).not.toBe(first)
})

test('a secure browser uses its native UUID generator', () => {
  vi.stubGlobal('crypto', { randomUUID: () => 'native-id' })
  expect(requestId()).toBe('native-id')
})
