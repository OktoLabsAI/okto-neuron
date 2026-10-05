import { useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { Check, FlaskConical, Pencil, Plus, RotateCw, Trash2 } from 'lucide-react'
import { postLlmTest } from '@/services/llm-test-api'
import {
  createCredential,
  createProvider,
  deleteCredential,
  deleteProvider,
  listCredentials,
  listProviders,
  listProviderTypes,
  updateCredential,
  updateProvider,
  type ProviderDraft,
} from '@/services/provider-api'
import type { NamedCredential, ProviderProfile, ProviderType } from '@/types'

const inputCls =
  'w-full rounded-lg border border-surface-700 bg-surface-900 px-3 py-2 text-sm text-surface-100 placeholder:text-surface-600 focus:border-accent-600 focus:outline-hidden'

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1 text-xs text-surface-400">
      {label}
      {children}
    </label>
  )
}

export function ProvidersPanel({
  onProvidersChanged,
}: {
  onProvidersChanged?: (providers: ProviderProfile[]) => void
}) {
  const [credentials, setCredentials] = useState<NamedCredential[]>([])
  const [providers, setProviders] = useState<ProviderProfile[]>([])
  const [providerTypes, setProviderTypes] = useState<ProviderType[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [credentialName, setCredentialName] = useState('')
  const [credentialKey, setCredentialKey] = useState('')
  const [rotatingId, setRotatingId] = useState<string | null>(null)
  const [rotationKey, setRotationKey] = useState('')
  const [savingCredential, setSavingCredential] = useState(false)
  const [editingProviderId, setEditingProviderId] = useState<string | null>(null)
  const [providerDraft, setProviderDraft] = useState<ProviderDraft>({
    name: '',
    driver: '',
    api_base: null,
    allow_remote: true,
    credential_id: null,
    parameter_mode: 'safe',
    request_timeout_s: null,
  })
  const [savingProvider, setSavingProvider] = useState(false)
  const [testingId, setTestingId] = useState<string | null>(null)
  const [testResults, setTestResults] = useState<Record<string, string>>({})

  const selectedType = useMemo(
    () => providerTypes.find((item) => item.driver === providerDraft.driver),
    [providerDraft.driver, providerTypes],
  )

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      const [nextCredentials, nextProviders, nextTypes] = await Promise.all([
        listCredentials(),
        listProviders(),
        listProviderTypes(),
      ])
      setCredentials(nextCredentials)
      setProviders(nextProviders)
      setProviderTypes(nextTypes)
      onProvidersChanged?.(nextProviders)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'failed to load providers')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
  }, [])

  const addCredential = async () => {
    if (!credentialName.trim() || !credentialKey) return
    setSavingCredential(true)
    setError(null)
    try {
      await createCredential(credentialName.trim(), credentialKey)
      setCredentialName('')
      setCredentialKey('')
      await load()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'could not save credential')
    } finally {
      setSavingCredential(false)
    }
  }

  const rotateCredential = async (id: string) => {
    if (!rotationKey) return
    setSavingCredential(true)
    setError(null)
    try {
      await updateCredential(id, { api_key: rotationKey })
      setRotatingId(null)
      setRotationKey('')
      await load()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'could not rotate credential')
    } finally {
      setSavingCredential(false)
    }
  }

  const removeCredential = async (credential: NamedCredential) => {
    if (!window.confirm(`Delete credential “${credential.name}”?`)) return
    setError(null)
    try {
      await deleteCredential(credential.id)
      await load()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'could not delete credential')
    }
  }

  const resetProviderDraft = () => {
    setEditingProviderId(null)
    setProviderDraft({
      name: '',
      driver: '',
      api_base: null,
      allow_remote: true,
      credential_id: null,
      parameter_mode: 'safe',
      request_timeout_s: null,
    })
  }

  const saveProvider = async () => {
    if (!providerDraft.name.trim() || !providerDraft.driver) return
    setSavingProvider(true)
    setError(null)
    try {
      if (editingProviderId) await updateProvider(editingProviderId, providerDraft)
      else await createProvider(providerDraft)
      resetProviderDraft()
      await load()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'could not save provider')
    } finally {
      setSavingProvider(false)
    }
  }

  const editProvider = (provider: ProviderProfile) => {
    setEditingProviderId(provider.id)
    setProviderDraft({
      name: provider.name,
      driver: provider.driver,
      api_base: provider.api_base,
      allow_remote: provider.allow_remote,
      credential_id: provider.credential_id,
      parameter_mode: provider.parameter_mode,
      request_timeout_s: provider.request_timeout_s,
    })
  }

  const removeProvider = async (provider: ProviderProfile) => {
    if (!window.confirm(`Delete provider “${provider.name}”?`)) return
    setError(null)
    try {
      await deleteProvider(provider.id)
      await load()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'could not delete provider')
    }
  }

  const testProvider = async (provider: ProviderProfile) => {
    setTestingId(provider.id)
    setError(null)
    try {
      const result = await postLlmTest(
        {
          provider_ref: provider.id,
          provider: provider.driver,
          api_base: provider.api_base,
          api_key_env: provider.api_key_env,
        },
        'application',
      )
      setTestResults((current) => ({
        ...current,
        [provider.id]: result.ok
          ? `${result.models.length} model${result.models.length === 1 ? '' : 's'} found`
          : result.error ?? 'test failed',
      }))
    } catch (cause) {
      setTestResults((current) => ({
        ...current,
        [provider.id]: cause instanceof Error ? cause.message : 'test failed',
      }))
    } finally {
      setTestingId(null)
    }
  }

  if (loading) {
    return <div className="py-8 text-sm text-surface-400">Loading providers…</div>
  }

  return (
    <div className="flex flex-col gap-5">
      <section className="rounded-xl border border-surface-800 bg-surface-900/40 p-5">
        <div className="mb-4 flex items-start justify-between gap-3">
          <div>
            <h2 className="text-sm font-semibold text-surface-100">API credentials</h2>
            <p className="mt-1 text-[11px] text-surface-500">
              Application-wide, write-only secrets. Values are never returned to the browser.
            </p>
          </div>
          <button type="button" onClick={() => void load()} className="text-surface-400">
            <RotateCw size={14} />
          </button>
        </div>

        <div className="grid gap-3 md:grid-cols-[1fr_1.4fr_auto]">
          <Field label="Name">
            <input className={inputCls} value={credentialName} onChange={(event) => setCredentialName(event.target.value)} placeholder="Gemini personal" />
          </Field>
          <Field label="API key">
            <input className={inputCls} type="password" autoComplete="off" value={credentialKey} onChange={(event) => setCredentialKey(event.target.value)} placeholder="Paste once" />
          </Field>
          <button type="button" onClick={() => void addCredential()} disabled={!credentialName.trim() || !credentialKey || savingCredential} className="mt-5 flex items-center gap-1.5 rounded-lg bg-accent-700 px-3 py-2 text-xs text-white disabled:opacity-40">
            <Plus size={13} /> Add
          </button>
        </div>

        <div className="mt-4 divide-y divide-surface-800">
          {credentials.length === 0 && <p className="py-3 text-xs text-surface-500">No named credentials yet.</p>}
          {credentials.map((credential) => (
            <div key={credential.id} className="py-3">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <div className="flex items-center gap-2 text-sm text-surface-100">
                    {credential.name}
                    <span className={`text-[10px] ${credential.configured ? 'text-accent-400' : 'text-amber-300'}`}>
                      {credential.configured ? 'configured' : 'missing'}
                    </span>
                  </div>
                  <p className="text-[11px] text-surface-600">{credential.id}</p>
                </div>
                <div className="flex gap-2">
                  <button type="button" onClick={() => { setRotatingId(credential.id); setRotationKey('') }} className="rounded-md border border-surface-700 px-2 py-1 text-xs text-surface-300">Replace key</button>
                  <button type="button" aria-label={`Delete ${credential.name}`} onClick={() => void removeCredential(credential)} className="rounded-md border border-red-900/70 p-1.5 text-red-300"><Trash2 size={13} /></button>
                </div>
              </div>
              {rotatingId === credential.id && (
                <div className="mt-2 flex gap-2">
                  <input className={inputCls} type="password" autoComplete="off" value={rotationKey} onChange={(event) => setRotationKey(event.target.value)} placeholder="New API key" />
                  <button type="button" onClick={() => void rotateCredential(credential.id)} disabled={!rotationKey || savingCredential} className="rounded-lg bg-accent-700 px-3 text-xs text-white disabled:opacity-40">Replace</button>
                </div>
              )}
            </div>
          ))}
        </div>
      </section>

      <section className="rounded-xl border border-surface-800 bg-surface-900/40 p-5">
        <div className="mb-4">
          <h2 className="text-sm font-semibold text-surface-100">Provider connections</h2>
          <p className="mt-1 text-[11px] text-surface-500">
            Reusable connection, endpoint, credential, and request compatibility policy.
          </p>
        </div>

        <div className="grid gap-3 md:grid-cols-2">
          <Field label="Name">
            <input className={inputCls} value={providerDraft.name} onChange={(event) => setProviderDraft((current) => ({ ...current, name: event.target.value }))} placeholder="Gemini via LiteLLM" />
          </Field>
          <Field label="Driver">
            <select className={inputCls} value={providerDraft.driver} onChange={(event) => {
              const next = providerTypes.find((item) => item.driver === event.target.value)
              setProviderDraft((current) => ({ ...current, driver: event.target.value, api_base: next?.default_api_base ?? null, parameter_mode: 'safe' }))
            }}>
              <option value="">Select a driver</option>
              {providerTypes.map((type) => <option key={type.driver} value={type.driver}>{type.label}</option>)}
            </select>
          </Field>
          <Field label="Base URL">
            <input className={inputCls} value={providerDraft.api_base ?? ''} onChange={(event) => setProviderDraft((current) => ({ ...current, api_base: event.target.value || null }))} placeholder="Optional for native/CLI providers" />
          </Field>
          <Field label="Allow remote endpoint">
            <label className="flex items-center gap-2 py-2 text-sm text-surface-300">
              <input
                type="checkbox"
                checked={providerDraft.allow_remote}
                onChange={(event) => setProviderDraft((current) => ({
                  ...current,
                  allow_remote: event.target.checked,
                }))}
                className="h-4 w-4 accent-accent-500"
              />
              Permit private-LAN or HTTPS endpoints
            </label>
          </Field>
          <Field label="Credential">
            <select className={inputCls} value={providerDraft.credential_id ?? ''} onChange={(event) => setProviderDraft((current) => ({ ...current, credential_id: event.target.value || null }))}>
              <option value="">No managed API key</option>
              {credentials.map((credential) => <option key={credential.id} value={credential.id}>{credential.name}</option>)}
            </select>
          </Field>
          <Field label="Parameter compatibility">
            <select className={inputCls} value={providerDraft.parameter_mode} onChange={(event) => setProviderDraft((current) => ({ ...current, parameter_mode: event.target.value as ProviderDraft['parameter_mode'] }))}>
              <option value="safe">Safe — capability-gated</option>
              {selectedType?.local_extended_allowed && <option value="local_extended">Local extended — raw sampler fields</option>}
            </select>
          </Field>
          <Field label="Request timeout (seconds)">
            <input
              className={inputCls}
              type="number"
              min={1}
              step={30}
              value={providerDraft.request_timeout_s ?? ''}
              onChange={(event) => setProviderDraft((current) => ({
                ...current,
                request_timeout_s: event.target.value ? Number(event.target.value) : null,
              }))}
              placeholder="No Okto Neuron deadline"
            />
            <span className="text-[10px] text-surface-600">
              Empty delegates timeout policy to LiteLLM and the provider; LiteLLM may still apply its own default. Stop still cancels active ingest calls.
            </span>
          </Field>
          <div className="flex items-end gap-2">
            <button type="button" onClick={() => void saveProvider()} disabled={!providerDraft.name.trim() || !providerDraft.driver || savingProvider} className="flex items-center gap-1.5 rounded-lg bg-accent-700 px-3 py-2 text-xs text-white disabled:opacity-40">
              {editingProviderId ? <Check size={13} /> : <Plus size={13} />}
              {editingProviderId ? 'Save provider' : 'Add provider'}
            </button>
            {editingProviderId && <button type="button" onClick={resetProviderDraft} className="rounded-lg border border-surface-700 px-3 py-2 text-xs text-surface-300">Cancel</button>}
          </div>
        </div>

        <div className="mt-5 divide-y divide-surface-800">
          {providers.length === 0 && <p className="py-3 text-xs text-surface-500">No provider connections yet.</p>}
          {providers.map((provider) => (
            <div key={provider.id} className="flex items-center justify-between gap-3 py-3">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2 text-sm text-surface-100">
                  {provider.name}
                  {provider.uses.map((use) => <span key={use} className="rounded-sm border border-surface-700 px-1.5 py-0.5 text-[10px] text-surface-400">{use}</span>)}
                  <span className="text-[10px] text-surface-500">{provider.parameter_mode}</span>
                  <span className="text-[10px] text-surface-500">
                    {provider.allow_remote ? 'remote allowed' : 'loopback only'}
                  </span>
                </div>
                <p className="truncate text-[11px] text-surface-500">{provider.driver}{provider.api_base ? ` · ${provider.api_base}` : ''}</p>
                <p className="text-[11px] text-surface-600">Credential: {provider.credential_name ?? 'external / none'}</p>
                <p className="text-[11px] text-surface-600">
                  Request timeout: {provider.request_timeout_s == null ? 'LiteLLM / provider default' : `${provider.request_timeout_s}s`}
                </p>
                {testResults[provider.id] && <p className="mt-1 text-[11px] text-accent-400">{testResults[provider.id]}</p>}
              </div>
              <div className="flex shrink-0 gap-2">
                {provider.uses.includes('llm') && <button type="button" onClick={() => void testProvider(provider)} disabled={testingId === provider.id} className="flex items-center gap-1 rounded-md border border-surface-700 px-2 py-1 text-xs text-surface-300"><FlaskConical size={12} /> Test</button>}
                <button type="button" aria-label={`Edit ${provider.name}`} onClick={() => editProvider(provider)} className="rounded-md border border-surface-700 p-1.5 text-surface-300"><Pencil size={13} /></button>
                <button type="button" aria-label={`Delete ${provider.name}`} onClick={() => void removeProvider(provider)} className="rounded-md border border-red-900/70 p-1.5 text-red-300"><Trash2 size={13} /></button>
              </div>
            </div>
          ))}
        </div>
      </section>

      {error && <div className="rounded-lg border border-red-900/60 bg-red-950/40 px-3 py-2 text-xs text-red-300">{error}</div>}
    </div>
  )
}
