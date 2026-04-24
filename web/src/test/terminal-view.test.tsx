import { render } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { TerminalView } from '../components/TerminalView'

const mocks = vi.hoisted(() => ({
  terminalConstructor: vi.fn(),
  fit: vi.fn(),
}))

vi.mock('@xterm/xterm', () => ({
  Terminal: vi.fn().mockImplementation((options) => {
    mocks.terminalConstructor(options)
    return {
      rows: 24,
      cols: 80,
      loadAddon: vi.fn(),
      open: vi.fn(),
      onSelectionChange: vi.fn(),
      attachCustomKeyEventHandler: vi.fn(),
      onData: vi.fn(),
      focus: vi.fn(),
      write: vi.fn(),
      dispose: vi.fn(),
    }
  }),
}))

vi.mock('@xterm/addon-fit', () => ({
  FitAddon: vi.fn().mockImplementation(() => ({
    fit: mocks.fit,
  })),
}))

class MockWebSocket {
  static OPEN = 1
  readyState = MockWebSocket.OPEN
  binaryType = ''
  onopen: (() => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onclose: (() => void) | null = null

  constructor(_url: string) {
    queueMicrotask(() => this.onopen?.())
  }

  send = vi.fn()
  close = vi.fn()
}

class MockResizeObserver {
  observe = vi.fn()
  disconnect = vi.fn()
}

describe('TerminalView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.stubGlobal('WebSocket', MockWebSocket)
    vi.stubGlobal('ResizeObserver', MockResizeObserver)
  })

  it('configures Option as Meta so Zellij Alt shortcuts reach the PTY on macOS', () => {
    render(
      <TerminalView
        terminalId="term-1"
        provider="claude_code"
        agentProfile="code_supervisor"
        onClose={() => {}}
      />
    )

    expect(mocks.terminalConstructor).toHaveBeenCalledWith(
      expect.objectContaining({
        macOptionIsMeta: true,
      })
    )
  })
})
