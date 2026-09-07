import { useEffect, useState } from 'react'
import { consumeTokenFromFragment, getToken, clearToken, login } from './api'
import Chat from './components/Chat'
import Approvals from './components/Approvals'
import Alerts from './components/Alerts'
import Inventory from './components/Inventory'
import Reports from './components/Reports'

type Tab = 'chat' | 'approvals' | 'alerts' | 'inventory' | 'reports'

const TABS: { id: Tab; label: string }[] = [
  { id: 'chat', label: 'Chat' },
  { id: 'approvals', label: 'Aprovações' },
  { id: 'alerts', label: 'Alertas' },
  { id: 'inventory', label: 'Inventário' },
  { id: 'reports', label: 'Relatórios' },
]

export default function App() {
  const [authed, setAuthed] = useState(false)
  const [tab, setTab] = useState<Tab>('chat')
  // Bumped whenever an approval decision lands, so the pending-count badge
  // and the Approvals list refresh together.
  const [refreshKey, setRefreshKey] = useState(0)

  useEffect(() => {
    consumeTokenFromFragment()
    setAuthed(getToken() !== null)
  }, [])

  if (!authed) {
    return (
      <div className="login-screen">
        <div className="login-card">
          <h1>SAI</h1>
          <p className="subtitle">Copiloto de Infraestrutura</p>
          <button className="btn btn-primary" onClick={login}>
            Entrar com Microsoft
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          SAI <span className="brand-sub">Copiloto de Infraestrutura</span>
        </div>
        <nav className="tabs">
          {TABS.map((t) => (
            <button
              key={t.id}
              className={`tab ${tab === t.id ? 'tab-active' : ''}`}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </nav>
        <button
          className="btn btn-ghost"
          onClick={() => {
            clearToken()
            setAuthed(false)
          }}
        >
          Sair
        </button>
      </header>

      <main className="content">
        {tab === 'chat' && <Chat onActionProposed={() => setRefreshKey((k) => k + 1)} />}
        {tab === 'approvals' && (
          <Approvals refreshKey={refreshKey} onDecision={() => setRefreshKey((k) => k + 1)} />
        )}
        {tab === 'alerts' && <Alerts />}
        {tab === 'inventory' && <Inventory />}
        {tab === 'reports' && <Reports />}
      </main>
    </div>
  )
}
