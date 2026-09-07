import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import Approvals from '../components/Approvals'
import { api, type ActionOut } from '../api'

/**
 * The safety-relevant behaviour of the approval UI: a high/critical action
 * cannot be approved by a single stray click — the operator must type an
 * explicit confirmation first.
 */

function action(overrides: Partial<ActionOut> = {}): ActionOut {
  return {
    id: 'act-1',
    tool_name: 'sql_restore_database',
    risk_level: 'critical',
    parameters: { database: 'prod', backup_file: '\\\\backup\\prod.bak' },
    proposed_description: 'Restaurar banco prod',
    status: 'proposed',
    requires_approval: true,
    ...overrides,
  }
}

describe('Approvals', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('keeps Approve disabled for a critical action until CONFIRMO is typed', async () => {
    vi.spyOn(api, 'listActions').mockResolvedValue([action()])
    const approve = vi.spyOn(api, 'approveAction').mockResolvedValue(action({ status: 'succeeded' }))

    render(<Approvals refreshKey={0} onDecision={() => {}} />)

    const button = await screen.findByRole('button', { name: /aprovar e executar/i })
    expect(button).toBeDisabled()

    await userEvent.type(screen.getByPlaceholderText('CONFIRMO'), 'CONFIRMO')
    expect(button).toBeEnabled()

    await userEvent.click(button)
    await waitFor(() => expect(approve).toHaveBeenCalledWith('act-1'))
  })

  it('does not gate a low-risk action behind the typed confirmation', async () => {
    vi.spyOn(api, 'listActions').mockResolvedValue([
      action({ risk_level: 'low', tool_name: 'zabbix_acknowledge_problem' }),
    ])

    render(<Approvals refreshKey={0} onDecision={() => {}} />)

    const button = await screen.findByRole('button', { name: /aprovar e executar/i })
    expect(button).toBeEnabled()
    expect(screen.queryByPlaceholderText('CONFIRMO')).not.toBeInTheDocument()
  })

  it('shows the exact parameters that will be executed', async () => {
    vi.spyOn(api, 'listActions').mockResolvedValue([action()])
    const { container } = render(<Approvals refreshKey={0} onDecision={() => {}} />)

    await screen.findByText(/Restaurar banco prod/)
    expect(screen.getByText(/parâmetros exatos/i)).toBeInTheDocument()

    // The operator must be able to see the real payload, not a summary of it.
    const params = container.querySelector('.params pre')
    expect(params).not.toBeNull()
    expect(params!.textContent).toContain('"database": "prod"')
    expect(params!.textContent).toContain('prod.bak')
  })

  it('allows rejecting without any confirmation gate', async () => {
    vi.spyOn(api, 'listActions').mockResolvedValue([action()])
    const reject = vi.spyOn(api, 'rejectAction').mockResolvedValue(action({ status: 'rejected' }))

    render(<Approvals refreshKey={0} onDecision={() => {}} />)

    const button = await screen.findByRole('button', { name: /rejeitar/i })
    expect(button).toBeEnabled()
    await userEvent.click(button)

    await waitFor(() => expect(reject).toHaveBeenCalled())
  })

  it('reports an error from the backend instead of implying success', async () => {
    vi.spyOn(api, 'listActions').mockResolvedValue([action()])
    vi.spyOn(api, 'approveAction').mockRejectedValue(new Error('role operator cannot approve'))

    render(<Approvals refreshKey={0} onDecision={() => {}} />)

    await userEvent.type(await screen.findByPlaceholderText('CONFIRMO'), 'CONFIRMO')
    await userEvent.click(screen.getByRole('button', { name: /aprovar e executar/i }))

    expect(await screen.findByText(/role operator cannot approve/)).toBeInTheDocument()
  })
})
