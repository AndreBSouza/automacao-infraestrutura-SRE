import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, type InventoryItemOut } from '../api'

const SOURCES = ['', 'azure', 'azure_devops', 'sql_server', 'grafana', 'zabbix', 'linux', 'nginx', 'f5']

export default function Inventory() {
  const [items, setItems] = useState<InventoryItemOut[]>([])
  const [source, setSource] = useState('')
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(true)
  const [syncing, setSyncing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const load = useCallback(() => {
    setLoading(true)
    api
      .listInventory(source || undefined)
      .then(setItems)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [source])

  useEffect(load, [load])

  async function sync() {
    setSyncing(true)
    setError(null)
    setNotice(null)
    try {
      await api.syncInventory()
      setNotice('Sincronização iniciada em segundo plano. Atualize em instantes.')
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSyncing(false)
    }
  }

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return items
    return items.filter(
      (i) =>
        i.name.toLowerCase().includes(q) ||
        i.resource_type.toLowerCase().includes(q) ||
        (i.owner_hint ?? '').toLowerCase().includes(q),
    )
  }, [items, query])

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>Inventário do ambiente</h2>
        <div className="toolbar">
          <select value={source} onChange={(e) => setSource(e.target.value)}>
            {SOURCES.map((s) => (
              <option key={s} value={s}>
                {s === '' ? 'Todas as fontes' : s}
              </option>
            ))}
          </select>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filtrar por nome, tipo, responsável…"
          />
          <button className="btn btn-ghost" onClick={sync} disabled={syncing}>
            {syncing ? 'Sincronizando…' : 'Sincronizar agora'}
          </button>
        </div>
      </div>

      {error && <div className="banner banner-error">{error}</div>}
      {notice && <div className="banner banner-info">{notice}</div>}
      {loading && <p className="muted">Carregando…</p>}

      {!loading && (
        <>
          <p className="muted">
            {filtered.length} de {items.length} recursos
          </p>
          <table className="inventory-table">
            <thead>
              <tr>
                <th>Nome</th>
                <th>Tipo</th>
                <th>Fonte</th>
                <th>Responsável</th>
                <th>Detalhes</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((item) => (
                <tr key={item.id}>
                  <td>{item.name}</td>
                  <td>
                    <code>{item.resource_type}</code>
                  </td>
                  <td>{item.source}</td>
                  <td>{item.owner_hint ?? <span className="muted">—</span>}</td>
                  <td>
                    <details>
                      <summary>ver</summary>
                      <pre>{JSON.stringify(item.metadata, null, 2)}</pre>
                    </details>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  )
}
