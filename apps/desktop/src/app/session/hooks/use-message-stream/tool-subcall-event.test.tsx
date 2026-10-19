import type { GatewayEvent } from '@hermes/shared'
import { act, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $draftingToolSessions } from '@/store/tool-drafting'

import { type MessageStreamHarness, renderMessageStream } from './test-harness'

const SID = 'session-1'

// Module-scoped so a test can seed session state (e.g. interrupted) before the
// handler reads it — `sessionInterrupted` resolves against this map.
const sessionStates = new Map<string, ClientSessionState>()
let stream: MessageStreamHarness

function mountStream() {
  stream = renderMessageStream(SID, { states: sessionStates })
}

function emit(type: GatewayEvent['type'], payload: GatewayEvent['payload'] = {}, sessionId = SID) {
  act(() => stream.handleEvent({ payload, session_id: sessionId, type }))
}

function draftedTool(sessionId = SID) {
  return $draftingToolSessions.get()[sessionId]?.name
}

describe('sandbox sub-call progress (tool.subcall)', () => {
  beforeEach(() => {
    sessionStates.clear()
    $draftingToolSessions.set({})
  })

  afterEach(() => {
    cleanup()
    sessionStates.clear()
    $draftingToolSessions.set({})
    vi.restoreAllMocks()
  })

  // A tool call made INSIDE execute_code is progress chrome, not a transcript
  // row: it names the activity on the same transient status line a drafting
  // tool uses, and nothing else.
  it('names the sandbox sub-call on the transient status line', () => {
    mountStream()

    emit('tool.subcall', {
      duration_s: 0.4,
      error: null,
      name: 'web_search',
      parent: 'execute_code',
      preview: 'query=x'
    })

    expect(draftedTool()).toBe('web_search')
  })

  // A stopped turn can still emit frames before the backend notices; naming a
  // call we will never run leaves the label up until something else retires it.
  it('ignores a sub-call after the user hit stop', () => {
    sessionStates.set(SID, { ...createClientSessionState(), interrupted: true })
    mountStream()

    emit('tool.subcall', { name: 'web_search' })

    expect(draftedTool()).toBeUndefined()
  })

  it('ignores a nameless sub-call frame', () => {
    mountStream()

    emit('tool.subcall', {})

    // setSessionDraftingTool('') deletes the entry, same as a nameless
    // tool.generating frame: nothing to name, nothing left up.
    expect(draftedTool()).toBeUndefined()
  })
})
