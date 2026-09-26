import { useEffect, useRef, useState } from 'react'
import { AlertTriangle, Check, FlaskConical, RotateCw, Save, Trash2, Zap } from 'lucide-react'
import { getConfig, patchConfig, storeCredential } from '@/services/config-api'
import { postLlmTest, postLlmTestCompletion } from '@/services/llm-test-api'
import {
  getReembedStatus,
  postEmbeddingModels,
  postEmbeddingTest,
  startReembed,
} from '@/services/embedding-api'
import { startVaultReembed } from '@/services/vault-api'
import { resetVault } from '@/services/reset-api'
import { listProviders } from '@/services/provider-api'
import type {
  AppConfig,
  AppliedKind,
  CapacityLane,
  CapacityReport,
  ConsolidationConfig,
  ConfigScope,
  EmbeddingConfig,
  FolderWatchConfig,
  IngestConfig,
  LLMDefaults,
  LlmParameterValue,
  ParameterPlan,
  PrefilterConfig,
  ProviderProfile,
  ReembedStatus,
  StepLLM,
  UpkeepConfig,
} from '@/types'
import { Badge, ErrorBox, NumberField, Select, Spinner, TextArea } from '@/components/ui'
import { Plus, X as XIcon } from 'lucide-react'
import { useApp } from '@/store/app'
import { ProvidersPanel } from './ProvidersPanel'

// ── types ─────────────────────────────────────────────────────────────────────

type StepKey = 'extraction' | 'judge' | 'curator' | 'relation_curator' | 'ask'
type AnyStep = 'defaults' | StepKey
type ConfigTab = 'providers' | 'llm' | 'embedding' | 'curation' | 'ingestion' | 'vault'

// Draft stores all steps as StepLLM so every field is uniformly nullable.
// LLMDefaults is structurally assignable to StepLLM (string ⊂ string|null, etc.).
interface DraftLLM {
  allow_remote: boolean
  defaults: StepLLM
  extraction: StepLLM
  judge: StepLLM
  curator: StepLLM
  relation_curator: StepLLM
  ask: StepLLM
  // step_default_prompts carried along read-only for reset buttons
  step_default_prompts: {
    extraction: string
    judge: string
    curator: string
    relation_curator: string
    ask: string
  }
}

type Draft = {
  embedding: EmbeddingConfig
  llm: DraftLLM
  consolidation: ConsolidationConfig
  upkeep: UpkeepConfig
  folder_watch: FolderWatchConfig
  ingest: IngestConfig
  packs: string[]
}

// ── constants ─────────────────────────────────────────────────────────────────

const CONFIG_TABS: ReadonlyArray<{ id: ConfigTab; label: string }> = [
  { id: 'providers', label: 'Providers' },
  { id: 'llm', label: 'LLM' },
  { id: 'embedding', label: 'Embedding' },
  { id: 'curation', label: 'Curation' },
  { id: 'ingestion', label: 'Ingestion' },
  { id: 'vault', label: 'Vault' },
]
const APPLICATION_CONFIG_TABS = CONFIG_TABS.filter(
  (tab) => tab.id !== 'vault',
)

const LITELLM_CHAT_PROVIDER_VALUES = [
  'ai21',
  'ai21_chat',
  'amazon_nova',
  'anthropic',
  'anthropic_text',
  'azure',
  'azure_ai',
  'azure_text',
  'baseten',
  'bedrock',
  'bytez',
  'cerebras',
  'chatgpt',
  'clarifai',
  'cloudflare',
  'codestral',
  'cohere',
  'cohere_chat',
  'cometapi',
  'custom',
  'custom_openai',
  'dashscope',
  'databricks',
  'datarobot',
  'deepinfra',
  'deepseek',
  'docker_model_runner',
  'empower',
  'featherless_ai',
  'fireworks_ai',
  'friendliai',
  'galadriel',
  'gemini',
  'gigachat',
  'github',
  'github_copilot',
  'gradient_ai',
  'groq',
  'helicone',
  'heroku',
  'hosted_vllm',
  'huggingface',
  'inception',
  'lambda_ai',
  'lemonade',
  'litellm_proxy',
  'llamafile',
  'lm_studio',
  'maritalk',
  'meta_llama',
  'mistral',
  'moonshot',
  'morph',
  'nebius',
  'nlp_cloud',
  'novita',
  'nscale',
  'nvidia_nim',
  'oci',
  'ollama',
  'ollama_chat',
  'oobabooga',
  'openai',
  'openai_like',
  'openrouter',
  'ovhcloud',
  'perplexity',
  'petals',
  'predibase',
  'publicai',
  'replicate',
  'sagemaker',
  'sagemaker_chat',
  'sagemaker_nova',
  'sambanova',
  'text-completion-codestral',
  'text-completion-inception',
  'text-completion-openai',
  'together_ai',
  'triton',
  'v0',
  'vercel_ai_gateway',
  'vertex_ai',
  'vertex_ai_beta',
  'vllm',
  'volcengine',
  'wandb',
  'watsonx',
  'watsonx_text',
  'xai',
] as const

function providerLabel(value: string): string {
  const known: Record<string, string> = {
    ai21: 'AI21',
    ai21_chat: 'AI21 Chat',
    azure: 'Azure OpenAI',
    azure_ai: 'Azure AI',
    azure_text: 'Azure Text',
    claude_cli: 'Claude Code CLI (subscription)',
    pi_cli: 'pi CLI (local multi-provider)',
    codex_cli: 'Codex CLI (subscription)',
    lm_studio: 'LM Studio',
    nvidia_nim: 'NVIDIA NIM',
    oci: 'OCI',
    openai: 'OpenAI / compatible',
    openai_like: 'OpenAI-like',
    xai: 'xAI',
  }
  if (known[value]) return known[value]
  return value
    .split(/[_-]/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ')
}

// Model-id syntax varies by CLI provider and none of them expose a real
// catalog worth defaulting to — shown as the Model field's placeholder.
function modelSyntaxHint(provider: string): string {
  const hints: Record<string, string> = {
    claude_cli: 'e.g. sonnet, haiku, opus, fable',
    pi_cli: 'e.g. anthropic/claude-haiku-4-5 (provider/id)',
    codex_cli: 'e.g. gpt-5.5 (plain model id — no catalog to discover)',
  }
  return hints[provider] ?? ''
}

// claude_cli, pi_cli, and codex_cli are special non-litellm providers that
// shell out to a local CLI binary. They ignore Base URL, API key, and every
// sampling knob (max tokens, temperature, top_p/k, min_p, presence penalty,
// thinking) — none of the CLIs expose a generation-parameter surface.
// Validate checks the local binary instead of probing an HTTP endpoint.
const CLI_PROVIDERS = new Set(['claude_cli', 'pi_cli', 'codex_cli'])
const MANAGED_API_KEY_PROVIDERS = new Set([
  'anthropic',
  'custom_openai',
  'gemini',
  'litellm_proxy',
  'lm_studio',
  'ollama',
  'openai',
  'openai_like',
  'openrouter',
])

const PROVIDERS = [
  { value: 'stub', label: 'Stub (testing)' },
  { value: 'claude_cli', label: providerLabel('claude_cli') },
  { value: 'pi_cli', label: providerLabel('pi_cli') },
  { value: 'codex_cli', label: providerLabel('codex_cli') },
  ...LITELLM_CHAT_PROVIDER_VALUES.map((value) => ({ value, label: providerLabel(value) })),
]

// Mirrors the backend _EMBEDDING_PROVIDERS allowlist (config/_vault.py).
const EMBEDDING_PROVIDERS = [
  { value: 'fastembed', label: 'FastEmbed (local)' },
  { value: 'stub', label: 'Stub (testing)' },
  { value: 'sentence-transformers', label: 'Sentence Transformers (local)' },
  { value: 'azure', label: providerLabel('azure') },
  { value: 'azure_ai', label: providerLabel('azure_ai') },
  { value: 'bedrock', label: providerLabel('bedrock') },
  { value: 'cohere', label: providerLabel('cohere') },
  { value: 'databricks', label: providerLabel('databricks') },
  { value: 'fireworks_ai', label: providerLabel('fireworks_ai') },
  { value: 'gemini', label: providerLabel('gemini') },
  { value: 'gigachat', label: providerLabel('gigachat') },
  { value: 'github_copilot', label: providerLabel('github_copilot') },
  { value: 'llamagate', label: providerLabel('llamagate') },
  { value: 'libertai', label: providerLabel('libertai') },
  { value: 'lm_studio', label: providerLabel('lm_studio') },
  { value: 'litellm_proxy', label: providerLabel('litellm_proxy') },
  { value: 'mistral', label: providerLabel('mistral') },
  { value: 'nebius', label: providerLabel('nebius') },
  { value: 'novita', label: providerLabel('novita') },
  { value: 'oci', label: providerLabel('oci') },
  { value: 'ollama', label: providerLabel('ollama') },
  { value: 'openai', label: providerLabel('openai') },
  { value: 'perplexity', label: providerLabel('perplexity') },
  { value: 'scaleway', label: providerLabel('scaleway') },
  { value: 'snowflake', label: providerLabel('snowflake') },
  { value: 'together_ai', label: providerLabel('together_ai') },
  { value: 'vercel_ai_gateway', label: providerLabel('vercel_ai_gateway') },
  { value: 'vertex_ai', label: providerLabel('vertex_ai') },
  { value: 'volcengine', label: providerLabel('volcengine') },
  { value: 'voyage', label: providerLabel('voyage') },
]

// Mirrors onboarding.MANAGED_EMBEDDING_API_KEY_PROVIDERS. Providers that use
// cloud credential chains or an interactive login intentionally stay on the
// advanced environment-reference path.
const MANAGED_EMBEDDING_API_KEY_PROVIDERS = new Set([
  'azure',
  'azure_ai',
  'cohere',
  'databricks',
  'fireworks_ai',
  'gemini',
  'gigachat',
  'llamagate',
  'libertai',
  'lm_studio',
  'mistral',
  'nebius',
  'novita',
  'ollama',
  'openai',
  'perplexity',
  'scaleway',
  'together_ai',
  'vercel_ai_gateway',
  'volcengine',
  'voyage',
])

// In-process providers: no network endpoint, no Test probe needed.
const LOCAL_EMBEDDING_PROVIDERS = new Set(['stub', 'fastembed', 'sentence-transformers'])

const inputCls =
  'rounded-lg border border-surface-700 bg-surface-900 px-3 py-2 text-sm text-surface-100 placeholder:text-surface-600 focus:border-accent-600 focus:outline-none w-full'

// ── tiny layout helpers ───────────────────────────────────────────────────────

function Field({
  label,
  hint,
  span,
  children,
}: {
  label: string
  hint?: string
  span?: boolean
  children: React.ReactNode
}) {
  return (
    <div className={`flex flex-col gap-1 ${span ? 'col-span-2' : ''}`}>
      <span className="text-xs font-medium text-surface-300">{label}</span>
      {children}
      {hint && <span className="text-[11px] text-surface-500">{hint}</span>}
    </div>
  )
}

function FolderWatchRootsEditor({
  roots,
  onChange,
}: {
  roots: string[]
  onChange: (roots: string[]) => void
}) {
  const [newRoot, setNewRoot] = useState('')
  const add = () => {
    const path = newRoot.trim()
    if (!path || roots.includes(path)) return
    onChange([...roots, path])
    setNewRoot('')
  }
  return (
    <div className="flex flex-col gap-2">
      {roots.length > 0 && (
        <ul className="flex flex-col gap-1">
          {roots.map((root) => (
            <li
              key={root}
              className="flex items-center justify-between gap-2 rounded-lg border border-surface-700 bg-surface-950 px-3 py-1.5 text-xs text-surface-300"
            >
              <span className="truncate font-mono">{root}</span>
              <button
                type="button"
                onClick={() => onChange(roots.filter((r) => r !== root))}
                className="shrink-0 rounded p-0.5 text-surface-500 hover:bg-surface-800 hover:text-rose-300"
                aria-label={`Remove ${root}`}
              >
                <XIcon size={14} />
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="flex items-center gap-2">
        <input
          value={newRoot}
          onChange={(e) => setNewRoot(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              add()
            }
          }}
          placeholder="/absolute/path/to/folder"
          className="h-9 min-w-0 flex-1 rounded-lg border border-surface-700 bg-surface-950 px-3 text-xs text-surface-100 outline-none placeholder:text-surface-600 focus:border-accent-600"
        />
        <button
          type="button"
          onClick={add}
          className="inline-flex h-9 items-center gap-1 rounded-lg border border-surface-700 bg-surface-900 px-3 text-xs text-surface-300 hover:bg-surface-800"
        >
          <Plus size={14} />
          Add
        </button>
      </div>
    </div>
  )
}

type LegacyParameterKey =
  | 'max_tokens'
  | 'temperature'
  | 'top_p'
  | 'top_k'
  | 'min_p'
  | 'presence_penalty'
  | 'enable_thinking'

const LEGACY_PARAMETER_KEYS: readonly LegacyParameterKey[] = [
  'max_tokens',
  'temperature',
  'top_p',
  'top_k',
  'min_p',
  'presence_penalty',
  'enable_thinking',
]

function configuredParameters(block: StepLLM | LLMDefaults): Record<string, LlmParameterValue> {
  const values: Record<string, LlmParameterValue> = {}
  for (const key of LEGACY_PARAMETER_KEYS) {
    const value = block[key]
    if (value !== null && value !== undefined) values[key] = value
  }
  return values
}

// ── raw sampling-payload editor (bound to sampling_payload) ────────────────
//
// Mirrors RESERVED_SAMPLING_PAYLOAD_KEYS in src/okto_neuron/config/_vault.py.
// Two categories, two messages — the server is still the authority (a save
// that slips past this check is rejected server-side and that error is
// surfaced verbatim), this is a client-side UX assist only.
const CONNECTION_OWNED_SAMPLING_KEYS: ReadonlySet<string> = new Set([
  'api_base',
  'api_key',
  'drop_params',
  'messages',
  'model',
  'timeout',
])
const RESPONSE_SHAPE_SAMPLING_KEYS: ReadonlySet<string> = new Set([
  'extra_body',
  'n',
  'stream',
  'tools',
])

function samplingPayloadKeyViolation(name: string): string | null {
  if (CONNECTION_OWNED_SAMPLING_KEYS.has(name)) {
    return `"${name}" is owned by the connection config (model/messages/api_base/api_key/drop_params/timeout) and cannot be set here.`
  }
  if (RESPONSE_SHAPE_SAMPLING_KEYS.has(name)) {
    return `"${name}" controls the response shape and is owned by Okto Neuron, not sampling — it cannot be set here.`
  }
  return null
}

type SamplingPayloadParseResult =
  | { ok: true; value: Record<string, LlmParameterValue> }
  | { ok: false; error: string }

function parseSamplingPayload(text: string): SamplingPayloadParseResult {
  const trimmed = text.trim()
  if (trimmed === '') return { ok: true, value: {} }
  let parsed: unknown
  try {
    parsed = JSON.parse(trimmed)
  } catch {
    return {
      ok: false,
      error: 'Invalid JSON. Enter an object of key/value pairs (e.g. {"typical_p": 0.9}), or leave empty for provider defaults.',
    }
  }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return {
      ok: false,
      error: 'sampling_payload must be a JSON object, e.g. {"typical_p": 0.9} — not a list or a bare value.',
    }
  }
  const violation = Object.keys(parsed as Record<string, unknown>)
    .map(samplingPayloadKeyViolation)
    .find((msg): msg is string => msg !== null)
  if (violation) return { ok: false, error: violation }
  return { ok: true, value: parsed as Record<string, LlmParameterValue> }
}

function SamplingPayloadEditor({
  value,
  onChange,
}: {
  value: Record<string, LlmParameterValue>
  onChange: (value: Record<string, LlmParameterValue>) => void
}) {
  const serialized = Object.keys(value).length > 0 ? JSON.stringify(value, null, 2) : ''
  const [text, setText] = useState(serialized)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    setText(serialized)
    setError(null)
  }, [serialized])
  return (
    <div>
      <textarea
        className={`${inputCls} min-h-24 py-2 font-mono ${
          error ? 'border-rose-600 focus:border-rose-500' : ''
        }`}
        value={text}
        spellCheck={false}
        placeholder='{"typical_p": 0.9, "stop_token_ids": [151643]}'
        onChange={(event) => {
          const next = event.target.value
          setText(next)
          const result = parseSamplingPayload(next)
          if (result.ok) {
            setError(null)
            onChange(result.value)
          } else {
            setError(result.error)
          }
        }}
      />
      {error ? (
        <p className="mt-1 flex items-start gap-1 text-[11px] text-rose-300">
          <AlertTriangle size={11} className="mt-0.5 shrink-0" />
          {error}
        </p>
      ) : text.trim() === '' ? (
        <p className="mt-1 text-[11px] text-surface-500">Empty — uses provider defaults.</p>
      ) : (
        <p className="flex items-center gap-1 mt-1 text-[11px] text-emerald-400">
          <Check size={11} /> Valid JSON.
        </p>
      )}
    </div>
  )
}

// ── read-only backend capacity (configured vs effective) ──────────────────────

/**
 * Surfaces the clamp applied by `okto_neuron.config._capacity`. Purely
 * informational: the number fields below still show and PATCH the *configured*
 * value, because the stored config is deliberately kept verbatim. This panel is
 * what makes a configured-but-clamped value visible instead of silently
 * mismatching what the backend actually runs.
 */
function CapacityLaneRow({ label, lane }: { label: string; lane: CapacityLane }) {
  const configured = lane.configured ?? 1
  const clamped = configured > lane.effective
  return (
    <div className="flex items-baseline justify-between gap-3 py-1">
      <span className="text-[11px] text-surface-400">{label}</span>
      <span className="font-mono text-[11px] text-surface-200">
        configured {configured} →{' '}
        <span className={clamped ? 'text-amber-400' : 'text-surface-200'}>
          effective {lane.effective}
        </span>
      </span>
    </div>
  )
}

function CapacityNotice({ capacity }: { capacity?: CapacityReport }) {
  if (!capacity) return null
  const notices = [capacity.extraction.notice, capacity.curation.notice].filter(
    (n): n is string => Boolean(n),
  )
  return (
    <div className="mb-4 rounded border border-surface-700 bg-surface-800/40 p-3">
      <h3 className="text-[11px] font-semibold uppercase tracking-wide text-surface-300">
        Effective LLM concurrency
      </h3>
      <p className="mt-0.5 text-[11px] text-surface-500">
        Only models listed in <code>llm.parallel_capable_models</code> may exceed one
        in-flight request. Everything else is clamped to 1 at the point the value is
        used, so this is what the backend actually runs.
      </p>
      <div className="mt-2 border-t border-surface-700/60 pt-1">
        <CapacityLaneRow label="Extraction" lane={capacity.extraction} />
        <CapacityLaneRow label="Curation" lane={capacity.curation} />
      </div>
      {notices.length > 0 && (
        <ul className="mt-2 space-y-1">
          {notices.map((n) => (
            <li key={n} className="text-[11px] text-amber-400">
              {n}
            </li>
          ))}
        </ul>
      )}
      <p className="mt-2 text-[11px] text-surface-500">
        Parallel-capable:{' '}
        <code>{capacity.parallel_capable_models.join(', ') || 'none'}</code>
      </p>
    </div>
  )
}

// ── per-step LLM card ─────────────────────────────────────────────────────────

interface StepCardProps {
  title: string
  subtitle?: string
  step: AnyStep
  data: StepLLM
  // Resolved defaults used for placeholder text and inherited values.
  defaults: LLMDefaults
  // Built-in default prompt for this step (shown as textarea placeholder; absent for Defaults card).
  defaultPrompt?: string
  onChange: (patch: Partial<StepLLM>) => void
  credentialStatus?: Record<string, boolean>
  onCredentialStored?: (apiKeyEnv: string) => void
  probeDisabled?: boolean
  managedCredentialStorage?: boolean
  configScope?: ConfigScope
  providers?: ProviderProfile[]
}

function StepCard({
  title,
  subtitle,
  step,
  data,
  defaults,
  defaultPrompt,
  onChange,
  credentialStatus = {},
  onCredentialStored,
  probeDisabled = false,
  managedCredentialStorage = false,
  configScope = 'vault',
  providers = [],
}: StepCardProps) {
  const isDefaults = step === 'defaults'
  const [discovering, setDiscovering] = useState(false)
  const [discovered, setDiscovered] = useState<string[] | null>(null)
  const [testError, setTestError] = useState<string | null>(null)
  const [completing, setCompleting] = useState(false)
  const [completionResult, setCompletionResult] = useState<{
    reply: string
    duration_s: number | null
    parameter_plan: ParameterPlan | null
  } | null>(null)
  const [completionError, setCompletionError] = useState<string | null>(null)
  const [apiKey, setApiKey] = useState('')
  const [storingCredential, setStoringCredential] = useState(false)
  const [credentialError, setCredentialError] = useState<string | null>(null)
  const [storedCredentialEnv, setStoredCredentialEnv] = useState<string | null>(null)

  const inlineEndpointOverride = !isDefaults && (
    (data.provider !== null && data.provider !== defaults.provider) ||
    (data.api_base !== null && data.api_base !== defaults.api_base)
  )
  const effectiveProviderRef = isDefaults
    ? (data.provider_ref ?? null)
    : (data.provider_ref ?? (inlineEndpointOverride ? null : defaults.provider_ref) ?? null)
  const effectiveProfile = providers.find((provider) => provider.id === effectiveProviderRef)
  const hasExplicitProfile = data.provider_ref !== null && data.provider_ref !== undefined
  const effectiveProbeDisabled = !effectiveProfile && probeDisabled
  const defaultParameters = configuredParameters(defaults)
  const ownParameters = configuredParameters(data)
  // Typed legacy sampler fields only now (the free-form `parameters` map was
  // removed, decision A, 2026-09-15) — a step's own non-null values win over
  // the default's, matching StepLLM's per-field inherit rule.
  const effectiveParameters: Record<string, LlmParameterValue> = isDefaults
    ? { ...ownParameters }
    : { ...defaultParameters, ...ownParameters }

  // Resolved effective value: provider profiles own connection fields; generation
  // fields remain on the LLM step/default configuration.
  const eff = (k: keyof LLMDefaults): string | number | boolean | null => {
    if (effectiveProfile) {
      if (k === 'provider') return effectiveProfile.driver
      if (k === 'api_base') return effectiveProfile.api_base
      if (k === 'api_key_env') return effectiveProfile.api_key_env
    }
    const v = (data as unknown as Record<string, unknown>)[k as string]
    if (v !== null && v !== undefined) return v as string | number | boolean
    return (defaults as unknown as Record<string, unknown>)[k as string] as string | number | boolean
  }

  // Placeholder hint for per-step text fields.
  const ph = (k: keyof LLMDefaults): string => {
    if (isDefaults) return ''
    const v = (defaults as unknown as Record<string, unknown>)[k as string]
    return v !== null && v !== undefined ? `inherits: ${v}` : ''
  }

  const stepEndpointChanged = !isDefaults && (hasExplicitProfile || inlineEndpointOverride)

  // Common request fields for both LLM-probe endpoints.
  const llmTestFields = () => {
    return {
      provider: String(eff('provider') ?? ''),
      model: String(eff('model') ?? ''),
      api_base: String(eff('api_base') ?? '') || undefined,
      api_key_env: (
        effectiveProfile
          ? effectiveProfile.api_key_env
          : stepEndpointChanged && data.api_key_env === null
          ? undefined
          : (isDefaults ? data.api_key_env : (data.api_key_env ?? defaults.api_key_env))
      ) ?? undefined,
    }
  }

  const storeApiKey = async () => {
    if (!isDefaults || !apiKey) return
    setStoringCredential(true)
    setCredentialError(null)
    setStoredCredentialEnv(null)
    try {
      const res = await storeCredential({
        kind: 'llm',
        provider: String(eff('provider') ?? ''),
        api_base: String(eff('api_base') ?? '') || undefined,
        api_key: apiKey,
      })
      onChange({ api_key_env: res.api_key_env })
      onCredentialStored?.(res.api_key_env)
      setApiKey('')
      setStoredCredentialEnv(res.api_key_env)
    } catch (e) {
      setCredentialError(e instanceof Error ? e.message : 'could not store API key')
    } finally {
      setStoringCredential(false)
    }
  }

  // Validate — cheap reachability/binary check + model discovery. Never calls the model.
  const runValidate = async () => {
    setDiscovering(true)
    setTestError(null)
    setDiscovered(null)
    try {
      const { provider, model, api_base, api_key_env } = llmTestFields()
      if (!provider) {
        setTestError('Provider is required to validate.')
        return
      }
      const res = await postLlmTest(
        { provider, model, api_base, api_key_env },
        configScope,
      )
      if (res.ok) {
        setDiscovered(res.models)
      } else {
        setTestError(res.error ?? 'probe failed')
      }
    } catch (e) {
      setTestError(e instanceof Error ? e.message : 'validate failed')
    } finally {
      setDiscovering(false)
    }
  }

  // Test — round-trips one real completion through the configured provider/model.
  const runTestCompletion = async () => {
    setCompleting(true)
    setCompletionError(null)
    setCompletionResult(null)
    try {
      const { provider, model, api_base, api_key_env } = llmTestFields()
      if (!provider || !model) {
        setCompletionError('Provider and model are required to test.')
        return
      }
      const res = await postLlmTestCompletion(
        {
          provider,
          provider_ref: effectiveProviderRef,
          model,
          api_base,
          api_key_env,
          parameters: effectiveParameters,
        },
        configScope,
      )
      if (res.ok && res.reply !== null) {
        setCompletionResult({
          reply: res.reply,
          duration_s: res.duration_s,
          parameter_plan: res.parameter_plan ?? null,
        })
      } else {
        setCompletionError(res.error ?? 'completion failed')
      }
    } catch (e) {
      setCompletionError(e instanceof Error ? e.message : 'test failed')
    } finally {
      setCompleting(false)
    }
  }

  // model value (empty string when null, triggers placeholder on text input)

  // Model field: text input pre-test; select populated after test succeeds.
  const modelVal = data.model ?? ''
  const providerVal = String(eff('provider') ?? '')
  const isCliProvider = CLI_PROVIDERS.has(providerVal)
  const providerSupportsManagedApiKey = MANAGED_API_KEY_PROVIDERS.has(providerVal)
  const canStoreManagedApiKey = managedCredentialStorage && providerSupportsManagedApiKey
  const canValidate = providerVal === 'stub' || isCliProvider || providerSupportsManagedApiKey
  const effectiveCredentialEnv = isDefaults
    ? data.api_key_env
    : (data.api_key_env ?? defaults.api_key_env)
  const credentialConfigured = Boolean(
    effectiveCredentialEnv && credentialStatus[effectiveCredentialEnv],
  ) || Boolean(effectiveCredentialEnv && storedCredentialEnv === effectiveCredentialEnv)

  return (
    <section
      data-testid={`llm-step-${step}`}
      className="rounded-xl border border-surface-800 bg-surface-900/40 p-5"
    >
      {/* Card header */}
      <div className="mb-4 flex items-start justify-between gap-4">
        <div>
          <h2 className="text-sm font-semibold text-surface-100">{title}</h2>
          {subtitle && <p className="mt-0.5 text-[11px] text-surface-500">{subtitle}</p>}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button
            type="button"
            onClick={() => void runValidate()}
            disabled={discovering || effectiveProbeDisabled || !canValidate}
            title={
              effectiveProbeDisabled
                ? 'Save the Allow remote change before testing this endpoint.'
                : canValidate
                  ? 'Cheap check: confirms the endpoint/binary is reachable and lists known models. Does not call the model.'
                  : 'This provider has no reliable model-discovery protocol; use Test for an end-to-end check.'
            }
            className="flex items-center gap-1.5 rounded-lg border border-surface-700 bg-surface-800 px-3 py-1.5 text-xs text-surface-300 hover:bg-surface-700 disabled:opacity-40"
          >
            {discovering ? (
              <RotateCw size={12} className="animate-spin" />
            ) : (
              <FlaskConical size={12} />
            )}
            Validate
          </button>
          <button
            type="button"
            onClick={() => void runTestCompletion()}
            disabled={completing || effectiveProbeDisabled}
            title={
              effectiveProbeDisabled
                ? 'Save the Allow remote change before testing this endpoint.'
                : 'Sends a real prompt to the configured provider/model and shows the reply.'
            }
            className="flex items-center gap-1.5 rounded-lg border border-surface-700 bg-surface-800 px-3 py-1.5 text-xs text-surface-300 hover:bg-surface-700 disabled:opacity-40"
          >
            {completing ? (
              <RotateCw size={12} className="animate-spin" />
            ) : (
              <Zap size={12} />
            )}
            Test
          </button>
        </div>
      </div>

      {testError && (
        <div className="mb-3 rounded-lg border border-red-900/60 bg-red-950/40 px-3 py-2 text-xs text-red-300">
          {testError}
        </div>
      )}

      {completionError && (
        <div className="mb-3 rounded-lg border border-red-900/60 bg-red-950/40 px-3 py-2 text-xs text-red-300">
          {completionError}
        </div>
      )}

      {completionResult && (
        <div className="mb-3 rounded-lg border border-accent-900/60 bg-accent-950/20 px-3 py-2 text-xs text-accent-300">
          <div className="flex items-center gap-1.5">
            <Check size={12} />
            Replied{completionResult.duration_s !== null ? ` in ${completionResult.duration_s}s` : ''}:{' '}
            <span className="text-surface-200">&ldquo;{completionResult.reply}&rdquo;</span>
          </div>
          {completionResult.parameter_plan && (
            <div className="mt-2 border-t border-accent-900/40 pt-2 text-[11px] text-surface-400">
              <span>Sent: {completionResult.parameter_plan.sent.join(', ') || 'none'}</span>
              {completionResult.parameter_plan.extra_body.length > 0 && (
                <span> · local extras: {completionResult.parameter_plan.extra_body.join(', ')}</span>
              )}
              {Object.keys(completionResult.parameter_plan.omitted).length > 0 && (
                <span> · omitted: {Object.keys(completionResult.parameter_plan.omitted).join(', ')}</span>
              )}
            </div>
          )}
        </div>
      )}

      {providerVal === 'claude_cli' && (
        <div className="mb-3 rounded-lg border border-surface-700 bg-surface-800/60 px-3 py-2 text-xs text-surface-400">
          Uses the local Claude Code CLI and its existing login (subscription) — no
          Base URL or API key needed. <span className="text-surface-300">Model</span> is passed
          verbatim to <code>--model</code> (e.g. sonnet, haiku, opus, fable). Sampling
          (temperature, top_p/k, thinking) is ignored. Test checks the <code>claude</code> binary.
        </div>
      )}

      {providerVal === 'pi_cli' && (
        <div className="mb-3 rounded-lg border border-surface-700 bg-surface-800/60 px-3 py-2 text-xs text-surface-400">
          Uses the local <code>pi</code> binary to dispatch to whatever provider it's
          configured with — no Base URL or API key needed here.{' '}
          <span className="text-surface-300">Model</span> is passed verbatim to{' '}
          <code>--model</code> (provider/id, e.g. anthropic/claude-haiku-4-5). Sampling
          (temperature, top_p/k, thinking) is ignored. Test checks the <code>pi</code> binary.
        </div>
      )}

      {providerVal === 'codex_cli' && (
        <div className="mb-3 rounded-lg border border-surface-700 bg-surface-800/60 px-3 py-2 text-xs text-surface-400">
          Uses the local Codex CLI and its existing login (ChatGPT subscription or API
          key) — no Base URL or API key needed here.{' '}
          <span className="text-surface-300">Model</span> is passed verbatim to{' '}
          <code>-m</code>. Sampling (temperature, top_p/k, thinking) is ignored. Test
          checks the <code>codex</code> binary.
        </div>
      )}

      <div className="grid grid-cols-2 gap-4">
        <Field
          label="Provider connection"
          hint={effectiveProfile ? `${effectiveProfile.driver}${effectiveProfile.api_base ? ` · ${effectiveProfile.api_base}` : ''}` : undefined}
        >
          <Select
            value={data.provider_ref ?? ''}
            onChange={(value) => onChange({ provider_ref: value || null })}
            options={[
              {
                value: '',
                label: isDefaults
                  ? '— inline / legacy fields —'
                  : `— inherit (${providers.find((provider) => provider.id === defaults.provider_ref)?.name ?? providerLabel(defaults.provider)}) —`,
              },
              ...providers
                .filter((provider) => provider.uses.includes('llm'))
                .map((provider) => ({ value: provider.id, label: provider.name })),
            ]}
          />
        </Field>

        {/* Provider */}
        {(!effectiveProfile || (!isDefaults && !hasExplicitProfile)) && (
        <Field label="Provider" hint={!isDefaults ? ph('provider') : undefined}>
          {isDefaults ? (
            <Select
              value={data.provider ?? defaults.provider}
              onChange={(v) => onChange({ provider: v, api_key_env: null })}
              options={PROVIDERS}
            />
          ) : (
            <Select
              value={data.provider ?? ''}
              onChange={(v) => onChange({ provider: v || null, api_key_env: null })}
              options={[
                { value: '', label: `— inherit (${defaults.provider}) —` },
                ...PROVIDERS,
              ]}
            />
          )}
        </Field>
        )}

        {/* Model — text input before test; select dropdown after test */}
        <Field label="Model" hint={!isDefaults && !data.model ? ph('model') : undefined}>
          {discovered && discovered.length > 0 ? (
            <>
              <Select
                value={modelVal || (isDefaults ? defaults.model : '')}
                onChange={(v) => onChange({ model: v || null })}
                options={[
                  ...(!isDefaults ? [{ value: '', label: `— inherit (${defaults.model}) —` }] : []),
                  ...discovered.map((m) => ({ value: m, label: m })),
                ]}
              />
              <span className="mt-0.5 flex items-center gap-1 text-[11px] text-accent-400">
                <Check size={10} /> {discovered.length} model{discovered.length !== 1 ? 's' : ''} found
              </span>
            </>
          ) : (
            <input
              className={inputCls}
              value={modelVal}
              placeholder={
                !isDefaults
                  ? `inherits: ${defaults.model}`
                  : modelSyntaxHint(providerVal)
              }
              onChange={(e) => onChange({ model: e.target.value || null })}
            />
          )}
        </Field>

        {step === 'extraction' && (
          <div className="col-span-2 rounded-lg border border-surface-800 bg-surface-950/30 p-3">
            <Field
              label="Concurrent chunks"
              hint="Okto Neuron execution policy, not a model parameter. Blank uses 1 (sequential); up to 32 chunks may run at once when the provider has capacity."
            >
              <NumberField
                value={data.max_concurrent ?? null}
                min={1}
                max={32}
                step={1}
                placeholder="1"
                onChange={(value) => onChange({
                  max_concurrent: value === null
                    ? null
                    : Math.min(32, Math.max(1, Math.round(value))),
                })}
              />
            </Field>
          </div>
        )}

        {/* CLI-shell providers (claude_cli/pi_cli/codex_cli) ignore Base URL, API
            key, and every sampling knob below — hidden rather than shown-disabled
            so the card only asks for what that provider actually uses. */}
        {!isCliProvider && (
          <>
            {/* api_base */}
            {!effectiveProfile && (
            <Field label="Base URL" hint={!isDefaults ? ph('api_base') : 'Loopback only unless allow remote is on.'}>
              <input
                className={inputCls}
                value={data.api_base ?? ''}
                placeholder={!isDefaults ? `inherits: ${defaults.api_base}` : ''}
                onChange={(e) => onChange({
                  api_base: e.target.value || null,
                  api_key_env: null,
                })}
              />
            </Field>
            )}

            {!effectiveProfile && (isDefaults && canStoreManagedApiKey ? (
              <Field
                label="API key"
                hint={
                  credentialConfigured && effectiveCredentialEnv
                    ? `Available to this daemon as ${effectiveCredentialEnv}. The value is never returned to the browser or written to vault YAML.`
                    : 'Stored locally with owner-only permissions. The value is never written to vault YAML.'
                }
              >
                <div className="flex gap-2">
                  <input
                    data-testid="llm-default-api-key"
                    type="password"
                    autoComplete="off"
                    spellCheck={false}
                    className={inputCls}
                    value={apiKey}
                    placeholder={credentialConfigured ? 'Stored — paste to replace' : 'Paste API key'}
                    onChange={(e) => {
                      setApiKey(e.target.value)
                      setCredentialError(null)
                      setStoredCredentialEnv(null)
                    }}
                  />
                  <button
                    data-testid="llm-default-api-key-save"
                    type="button"
                    onClick={() => void storeApiKey()}
                    disabled={!apiKey || storingCredential}
                    className="shrink-0 rounded-lg border border-surface-700 bg-surface-800 px-3 py-2 text-xs text-surface-200 hover:bg-surface-700 disabled:opacity-40"
                  >
                    {storingCredential ? 'Saving…' : credentialConfigured ? 'Replace key' : 'Save key'}
                  </button>
                </div>
                {credentialError && (
                  <p className="mt-1 text-[11px] text-red-300">{credentialError}</p>
                )}
                {storedCredentialEnv === effectiveCredentialEnv && effectiveCredentialEnv && (
                  <p className="mt-1 text-[11px] text-accent-400">
                    Key stored as {effectiveCredentialEnv} and attached to this draft.
                  </p>
                )}
              </Field>
            ) : !isDefaults ? (
              <Field
                label="API key env override"
                hint={
                  stepEndpointChanged
                    ? 'This step uses a different endpoint, so blank means no key. Set an explicit reference if that endpoint needs one.'
                    : 'Leave blank to inherit the default credential reference.'
                }
              >
                <input
                  className={inputCls}
                  value={data.api_key_env ?? ''}
                  placeholder={defaults.api_key_env ? `inherits: ${defaults.api_key_env}` : 'OKTO_NEURON_…'}
                  onChange={(e) => onChange({ api_key_env: e.target.value || null })}
                />
              </Field>
            ) : (
              <Field
                label="Provider credentials"
                hint={
                  providerSupportsManagedApiKey
                    ? 'Direct managed-key storage is unavailable on this platform. Set a OKTO_NEURON_* environment reference under Advanced below.'
                    : 'This provider uses provider-specific authentication. Set its OKTO_NEURON_* environment reference under Advanced below.'
                }
              >
                <div className="rounded-lg border border-surface-800 bg-surface-950/40 px-3 py-2 text-xs text-surface-400">
                  {providerSupportsManagedApiKey
                    ? 'Use an external environment credential.'
                    : `Managed single-key storage is not available for ${providerLabel(providerVal)}.`}
                </div>
              </Field>
            ))}

            {isDefaults && !effectiveProfile && (
              <details className="col-span-2 rounded-lg border border-surface-800 bg-surface-950/30 px-3 py-2">
                <summary className="cursor-pointer text-xs text-surface-400">
                  Advanced credential reference
                </summary>
                <div className="mt-3">
                  <Field
                    label="API key env var"
                    hint="Use an existing OKTO_NEURON_* environment variable instead of storing a key here."
                  >
                    <input
                      className={inputCls}
                      value={data.api_key_env ?? ''}
                      placeholder="OKTO_NEURON_…"
                      onChange={(e) => onChange({ api_key_env: e.target.value || null })}
                    />
                  </Field>
                </div>
              </details>
            )}

            <details className="col-span-2 rounded-lg border border-surface-800 bg-surface-950/30 px-3 py-2">
              <summary className="flex cursor-pointer items-center justify-between gap-3 text-xs text-surface-300">
                <span className="flex items-center gap-1.5">
                  Sampling payload (raw JSON)
                  {!isDefaults && (
                    data.sampling_payload === null ? (
                      <Badge>inherits default</Badge>
                    ) : (
                      <Badge tone="accent">standalone</Badge>
                    )
                  )}
                </span>
                <span className="text-[11px] font-normal text-surface-500">
                  Empty uses provider defaults
                </span>
              </summary>
              <div className="mt-3 border-t border-surface-800 pt-3">
                <p className="mb-2 text-[11px] text-surface-500">
                  Any key LiteLLM/the model host understands (<code>typical_p</code>,{' '}
                  <code>stop_token_ids</code>, a nested <code>grammar</code> object, …) — no
                  whitelist, no range check, the backend validates it. Connection fields
                  (model/messages/api_base/api_key/drop_params/timeout) and response-shape
                  fields (stream/n/tools/extra_body) are owned by Okto Neuron and rejected here.
                </p>
                {!isDefaults && data.sampling_payload === null ? (
                  <div>
                    <p className="text-[11px] text-surface-500">
                      {Object.keys(defaults.sampling_payload).length > 0
                        ? `Inheriting the default payload (${Object.keys(defaults.sampling_payload).length} key${Object.keys(defaults.sampling_payload).length === 1 ? '' : 's'}, read-only below).`
                        : 'Inheriting the default payload — currently empty (provider defaults).'}
                    </p>
                    {Object.keys(defaults.sampling_payload).length > 0 && (
                      <pre className="mt-2 overflow-x-auto rounded-lg border border-surface-800 bg-surface-950 px-3 py-2 text-[11px] text-surface-400">
                        {JSON.stringify(defaults.sampling_payload, null, 2)}
                      </pre>
                    )}
                    <button
                      type="button"
                      onClick={() => onChange({ sampling_payload: { ...defaults.sampling_payload } })}
                      className="mt-3 inline-flex h-8 items-center gap-1 rounded-lg border border-surface-700 bg-surface-800 px-3 text-xs text-surface-200 hover:bg-surface-700"
                    >
                      Override for this step
                    </button>
                  </div>
                ) : (
                  <div>
                    <SamplingPayloadEditor
                      value={data.sampling_payload ?? {}}
                      onChange={(next) => onChange({ sampling_payload: next })}
                    />
                    {!isDefaults && (
                      <button
                        type="button"
                        onClick={() => onChange({ sampling_payload: null })}
                        className="mt-2 text-[11px] text-surface-400 hover:text-surface-200"
                      >
                        Reset to inherit default
                      </button>
                    )}
                  </div>
                )}
              </div>
            </details>
          </>
        )}

        {/* system_prompt — per-step cards only; Defaults has no system_prompt server-side */}
        {!isDefaults && (
          <div className="col-span-2 flex flex-col gap-1">
            <div className="flex items-center justify-between">
              <span className="text-xs font-medium text-surface-300">System prompt</span>
              {defaultPrompt && (
                <button
                  type="button"
                  onClick={() => onChange({ system_prompt: null })}
                  disabled={data.system_prompt === null}
                  className="text-[11px] text-surface-500 hover:text-surface-300 disabled:opacity-40"
                >
                  Reset to default
                </button>
              )}
            </div>
            <TextArea
              value={data.system_prompt}
              onChange={(v) => onChange({ system_prompt: v })}
              placeholder={defaultPrompt ?? undefined}
              rows={5}
            />
            {data.system_prompt === null && defaultPrompt && (
              <span className="text-[11px] italic text-surface-500">Using built-in default prompt.</span>
            )}
          </div>
        )}
      </div>
    </section>
  )
}

function effectiveStepLabel(step: StepLLM, defaults: LLMDefaults): string {
  const provider = step.provider ?? defaults.provider
  const model = step.model ?? defaults.model
  return `${providerLabel(provider)} · ${model}`
}

function AgenticWorkflowMap({ llm, defaults }: { llm: DraftLLM; defaults: LLMDefaults }) {
  const rows = [
    {
      phase: '01',
      title: 'Extraction LLM',
      value: effectiveStepLabel(llm.extraction, defaults),
      detail: 'Block text -> node, edge, and literal-claim candidates.',
    },
    {
      phase: '02',
      title: 'Deterministic resolver',
      value: 'no LLM',
      detail: 'Exact match, embedding band, endpoint, and graph correlations become evidence.',
    },
    {
      phase: '03',
      title: 'Merge Judge LLM',
      value: effectiveStepLabel(llm.judge, defaults),
      detail: 'Adjudicates likely duplicate pairs when deterministic signals are not enough.',
    },
    {
      phase: '04',
      title: 'Candidate Curator LLM',
      value: effectiveStepLabel(llm.curator, defaults),
      detail: 'Reviews each unique proposed node, including deterministic-remap audit cases.',
    },
    {
      phase: '05',
      title: 'Relationship Curator LLM',
      value: effectiveStepLabel(llm.relation_curator, defaults),
      detail: 'Reviews each proposed topology edge and literal claim before graph write.',
    },
    {
      phase: '06',
      title: 'Commit planner',
      value: 'no LLM',
      detail: 'Applies graph writes only from accepted, queued, superseded, or dead-lettered ledger state.',
    },
  ]

  return (
    <section className="rounded-xl border border-surface-800 bg-surface-900/40 p-5">
      <div className="mb-4">
        <h2 className="text-sm font-semibold text-surface-100">Agentic ingest workflow</h2>
        <p className="mt-0.5 text-[11px] text-surface-500">
          Resolver output is evidence. Curator verdicts decide candidate and relationship fate.
        </p>
      </div>
      <div className="divide-y divide-surface-800">
        {rows.map((row) => (
          <div key={row.phase} className="grid grid-cols-[2.5rem_minmax(0,1fr)] gap-3 py-3 first:pt-0 last:pb-0">
            <div className="flex h-7 w-7 items-center justify-center rounded-md border border-surface-700 bg-surface-950 text-[11px] font-semibold text-surface-400">
              {row.phase}
            </div>
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-medium text-surface-100">{row.title}</span>
                <span className="rounded-md border border-surface-700 px-2 py-0.5 text-[11px] text-surface-300">
                  {row.value}
                </span>
              </div>
              <p className="mt-1 text-[11px] leading-5 text-surface-500">{row.detail}</p>
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}

// ── main panel ────────────────────────────────────────────────────────────────

function toDraftLLM(src: AppConfig['llm']): DraftLLM {
  // LLMDefaults is structurally assignable to StepLLM (string ⊂ string|null, etc.)
  return {
    allow_remote: src.allow_remote,
    defaults: src.defaults as unknown as StepLLM,
    extraction: src.extraction,
    judge: src.judge,
    curator: src.curator,
    relation_curator: src.relation_curator,
    ask: src.ask,
    step_default_prompts: src.step_default_prompts,
  }
}

export function ConfigPanel() {
  const mock = useApp((s) => s.mock)
  const dataVersion = useApp((s) => s.dataVersion)
  const selectedVaultPath = useApp((s) => s.selectedVaultPath)
  const clearIngestQueue = useApp((s) => s.clearIngestQueue)
  const bumpData = useApp((s) => s.bumpData)
  const selectNode = useApp((s) => s.selectNode)
  const isApplicationDefaults = selectedVaultPath === null
  const configScope: ConfigScope = isApplicationDefaults ? 'application' : 'vault'
  const visibleTabs = isApplicationDefaults
    ? APPLICATION_CONFIG_TABS
    : CONFIG_TABS

  const [config, setConfig] = useState<AppConfig | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [providerProfiles, setProviderProfiles] = useState<ProviderProfile[]>([])
  const [activeTab, setActiveTab] = useState<ConfigTab>('llm')
  const configScrollRef = useRef<HTMLDivElement>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [result, setResult] = useState<{
    applied: AppliedKind
    notes: string[]
    changed: string[]
  } | null>(null)
  const [confirmReembed, setConfirmReembed] = useState(false)
  const [confirmWipe, setConfirmWipe] = useState(false)
  const [wiping, setWiping] = useState(false)
  const [wipeError, setWipeError] = useState<string | null>(null)
  const [wiped, setWiped] = useState(false)
  // Embedding card: test probe + re-embed job
  const [embTesting, setEmbTesting] = useState(false)
  const [embDiscovering, setEmbDiscovering] = useState(false)
  const [embModels, setEmbModels] = useState<string[] | null>(null)
  const [embDiscoveryError, setEmbDiscoveryError] = useState<string | null>(null)
  const [embTestError, setEmbTestError] = useState<string | null>(null)
  const [embTestDimension, setEmbTestDimension] = useState<number | null>(null)
  const [embTestVectors, setEmbTestVectors] = useState<number | null>(null)
  const [embApiKey, setEmbApiKey] = useState('')
  const [embCredentialError, setEmbCredentialError] = useState<string | null>(null)
  const [embStoringCredential, setEmbStoringCredential] = useState(false)
  const [embStoredCredentialEnv, setEmbStoredCredentialEnv] = useState<string | null>(null)
  const [reembedding, setReembedding] = useState(false)
  const [reembedStatus, setReembedStatus] = useState<ReembedStatus | null>(null)
  const [reembedError, setReembedError] = useState<string | null>(null)

  const load = () => {
    setLoading(true)
    setError(null)
    getConfig(configScope)
      .then((c) => {
        setConfig(c)
        setDraft({
          embedding: c.embedding,
          llm: toDraftLLM(c.llm),
          consolidation: c.consolidation,
          upkeep: c.upkeep,
          folder_watch: c.folder_watch,
          ingest: c.ingest,
          packs: c.packs,
        })
      })
      .catch((e) => setError(e instanceof Error ? e.message : 'failed to load config'))
      .finally(() => setLoading(false))
  }
  useEffect(load, [mock, dataVersion, configScope])
  useEffect(() => {
    listProviders().then(setProviderProfiles).catch(() => setProviderProfiles([]))
  }, [mock, dataVersion])
  useEffect(() => {
    if (!visibleTabs.some((tab) => tab.id === activeTab)) setActiveTab('llm')
  }, [activeTab, visibleTabs])

  const embeddingProfile = providerProfiles.find(
    (provider) => provider.id === draft?.embedding.provider_ref,
  )
  const embeddingProvider = embeddingProfile?.driver ?? draft?.embedding.provider ?? ''
  const embeddingApiBase = embeddingProfile?.api_base ?? draft?.embedding.api_base
  const embeddingApiKeyEnv = embeddingProfile?.api_key_env ?? draft?.embedding.api_key_env
  const embeddingAllowRemote = embeddingProfile?.allow_remote
    ?? draft?.embedding.allow_remote
    ?? false

  useEffect(() => {
    if (!draft || embeddingProvider !== 'litellm_proxy') {
      setEmbDiscovering(false)
      setEmbModels(null)
      setEmbDiscoveryError(null)
      return
    }

    let cancelled = false
    setEmbDiscovering(true)
    setEmbModels(null)
    setEmbDiscoveryError(null)
    const timer = window.setTimeout(() => {
      void postEmbeddingModels(
        {
          provider_ref: draft.embedding.provider_ref,
          provider: embeddingProvider,
          api_base: embeddingApiBase,
          api_key_env: embeddingApiKeyEnv,
          allow_remote: embeddingAllowRemote,
        },
        configScope,
      )
        .then((response) => {
          if (cancelled) return
          if (!response.ok) {
            setEmbDiscoveryError(response.error ?? 'model discovery failed')
            return
          }
          setEmbModels(response.models)
          if (response.models.length === 0) {
            setEmbDiscoveryError('The provider advertised no embedding models.')
          }
        })
        .catch((caught: unknown) => {
          if (!cancelled) {
            setEmbDiscoveryError(
              caught instanceof Error ? caught.message : 'model discovery failed',
            )
          }
        })
        .finally(() => {
          if (!cancelled) setEmbDiscovering(false)
        })
    }, 250)

    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [
    configScope,
    draft?.embedding.provider_ref,
    embeddingAllowRemote,
    embeddingApiBase,
    embeddingApiKeyEnv,
    embeddingProvider,
  ])

  if (loading) return <div className="p-8"><Spinner label="loading config…" /></div>
  if (error) return <div className="p-8"><ErrorBox message={error} /></div>
  if (!config || !draft) return null

  // ── updaters ────────────────────────────────────────────────────────────────

  const updEmbedding = (patch: Partial<EmbeddingConfig>) =>
    setDraft((d) => (d ? { ...d, embedding: { ...d.embedding, ...patch } } : d))

  const updAllowRemote = (v: boolean) =>
    setDraft((d) => (d ? { ...d, llm: { ...d.llm, allow_remote: v } } : d))

  const updStep = (step: AnyStep, patch: Partial<StepLLM>) =>
    setDraft((d) =>
      d
        ? {
            ...d,
            llm: {
              ...d.llm,
              [step]: { ...(d.llm[step] as StepLLM), ...patch },
            },
          }
        : d,
    )

  const updConsolidation = (patch: Partial<Draft['consolidation']>) =>
    setDraft((d) => (d ? { ...d, consolidation: { ...d.consolidation, ...patch } } : d))

  const updPrefilter = (patch: Partial<PrefilterConfig>) =>
    setDraft((d) =>
      d
        ? {
            ...d,
            consolidation: {
              ...d.consolidation,
              prefilter: { ...d.consolidation.prefilter, ...patch },
            },
          }
        : d,
    )

  const updUpkeep = (patch: Partial<UpkeepConfig>) =>
    setDraft((d) => (d ? { ...d, upkeep: { ...d.upkeep, ...patch } } : d))

  const updFolderWatch = (patch: Partial<FolderWatchConfig>) =>
    setDraft((d) => (d ? { ...d, folder_watch: { ...d.folder_watch, ...patch } } : d))

  const updIngest = (patch: Partial<IngestConfig>) =>
    setDraft((d) => (d ? { ...d, ingest: { ...d.ingest, ...patch } } : d))

  // Bounds mirror the backend pydantic constraints (config/_vault.py
  // ConsolidationConfig) so an out-of-range value never reaches the PATCH.
  const clampInt = (n: number | null, lo: number, hi: number, fallback: number) =>
    n === null || Number.isNaN(n) ? fallback : Math.min(hi, Math.max(lo, Math.round(n)))
  const clampFloat = (n: number | null, lo: number, hi: number, fallback: number) =>
    n === null || Number.isNaN(n) ? fallback : Math.min(hi, Math.max(lo, n))

  const isLocalEmbedder = LOCAL_EMBEDDING_PROVIDERS.has(embeddingProvider)
  const embeddingSupportsManagedApiKey = MANAGED_EMBEDDING_API_KEY_PROVIDERS.has(
    embeddingProvider,
  )
  const canStoreEmbeddingApiKey = Boolean(
    !embeddingProfile &&
    !isLocalEmbedder &&
    embeddingSupportsManagedApiKey &&
    config.managed_credentials_supported,
  )
  const embeddingCredentialConfigured = Boolean(
    embeddingApiKeyEnv &&
    (
      config.credential_status?.[embeddingApiKeyEnv] ||
      embStoredCredentialEnv === embeddingApiKeyEnv
    ),
  )

  const resetEmbeddingCredentialDraft = () => {
    setEmbApiKey('')
    setEmbCredentialError(null)
    setEmbStoredCredentialEnv(null)
  }

  const storeEmbeddingApiKey = async () => {
    if (!embApiKey || !canStoreEmbeddingApiKey) return
    setEmbStoringCredential(true)
    setEmbCredentialError(null)
    setEmbStoredCredentialEnv(null)
    try {
      const res = await storeCredential({
        kind: 'embedding',
        provider: embeddingProvider,
        api_base: embeddingApiBase || undefined,
        api_key: embApiKey,
      })
      updEmbedding({ api_key_env: res.api_key_env })
      setConfig((current) =>
        current
          ? {
              ...current,
              credential_status: {
                ...(current.credential_status ?? {}),
                [res.api_key_env]: true,
              },
            }
          : current,
      )
      setEmbApiKey('')
      setEmbStoredCredentialEnv(res.api_key_env)
    } catch (e) {
      setEmbCredentialError(e instanceof Error ? e.message : 'could not store API key')
    } finally {
      setEmbStoringCredential(false)
    }
  }

  const runEmbeddingTest = async () => {
    setEmbTesting(true)
    setEmbTestError(null)
    setEmbTestDimension(null)
    setEmbTestVectors(null)
    try {
      const res = await postEmbeddingTest(
        {
          provider: embeddingProvider,
          model: draft.embedding.model,
          dimension: draft.embedding.dimension ?? 384,
          api_base: embeddingApiBase || undefined,
          api_key_env: embeddingApiKeyEnv || undefined,
          allow_remote: embeddingProfile?.allow_remote ?? (draft.embedding.allow_remote ?? false),
        },
        configScope,
      )
      if (res.ok) {
        setEmbTestDimension(res.dimension ?? null)
        setEmbTestVectors(res.vectors ?? null)
      }
      else setEmbTestError(res.error ?? 'probe failed')
    } catch (e) {
      setEmbTestError(e instanceof Error ? e.message : 'test failed')
    } finally {
      setEmbTesting(false)
    }
  }

  const runReembed = async () => {
    setReembedding(true)
    setReembedError(null)
    setReembedStatus(null)
    try {
      await startReembed()
      // Poll the status file until the background job finishes (running clears).
      for (;;) {
        await new Promise((r) => setTimeout(r, 700))
        const s = await getReembedStatus()
        setReembedStatus(s)
        if (!s.running && ['complete', 'failed', 'idle'].includes(s.phase)) break
      }
      bumpData() // graph changed under us — refresh dependent views
    } catch (e) {
      setReembedError(e instanceof Error ? e.message : 'reembed failed')
    } finally {
      setReembedding(false)
    }
  }

  // ── change detection ────────────────────────────────────────────────────────

  const configSnapshot = {
    embedding: config.embedding,
    llm: toDraftLLM(config.llm),
    consolidation: config.consolidation,
    upkeep: config.upkeep,
    folder_watch: config.folder_watch,
    ingest: config.ingest,
    packs: config.packs,
  }
  const hasChanges = JSON.stringify(draft) !== JSON.stringify(configSnapshot)
  const embedderChanged =
    (draft.embedding.provider_ref ?? null) !== (config.embedding.provider_ref ?? null) ||
    draft.embedding.provider !== config.embedding.provider ||
    draft.embedding.model !== config.embedding.model ||
    (draft.embedding.dimension ?? 384) !== (config.embedding.dimension ?? 384)
  const semanticStagesChanged =
    draft.consolidation.type_adjudication_enabled !==
      config.consolidation.type_adjudication_enabled ||
    draft.consolidation.relation_curator_enabled !==
      config.consolidation.relation_curator_enabled

  // ── save ────────────────────────────────────────────────────────────────────

  const save = async (reembedAfterSave = false) => {
    setSaving(true)
    setError(null)
    setResult(null)

    const patch: Record<string, unknown> = {}

    if (JSON.stringify(draft.embedding) !== JSON.stringify(config.embedding)) {
      patch.embedding = draft.embedding.provider_ref
        ? {
            provider_ref: draft.embedding.provider_ref,
            model: draft.embedding.model,
            dimension: draft.embedding.dimension,
            batch_size: draft.embedding.batch_size,
            max_concurrent_batches: draft.embedding.max_concurrent_batches,
          }
        : draft.embedding
    }

    // Field-level LLM diff
    const llmPatch: Record<string, unknown> = {}
    const srcLlm = toDraftLLM(config.llm)
    if (draft.llm.allow_remote !== srcLlm.allow_remote) {
      llmPatch.allow_remote = draft.llm.allow_remote
    }
    for (const step of ['defaults', 'extraction', 'judge', 'curator', 'relation_curator', 'ask'] as const) {
      const dStep = draft.llm[step] as unknown as Record<string, unknown>
      const cStep = srcLlm[step] as unknown as Record<string, unknown>
      const stepPatch: Record<string, unknown> = {}
      for (const k of Object.keys(dStep)) {
        if (JSON.stringify(dStep[k]) !== JSON.stringify(cStep[k])) {
          stepPatch[k] = dStep[k]
        }
      }
      if (Object.keys(stepPatch).length > 0) {
        llmPatch[step] = stepPatch
      }
    }
    if (Object.keys(llmPatch).length > 0) {
      patch.llm = llmPatch
    }

    if (JSON.stringify(draft.consolidation) !== JSON.stringify(config.consolidation)) {
      patch.consolidation = draft.consolidation
    }
    if (JSON.stringify(draft.upkeep) !== JSON.stringify(config.upkeep)) {
      patch.upkeep = draft.upkeep
    }
    if (JSON.stringify(draft.folder_watch) !== JSON.stringify(config.folder_watch)) {
      patch.folder_watch = draft.folder_watch
    }
    if (JSON.stringify(draft.ingest) !== JSON.stringify(config.ingest)) {
      patch.ingest = draft.ingest
    }
    if (JSON.stringify(draft.packs) !== JSON.stringify(config.packs)) {
      patch.packs = draft.packs
    }

    try {
      const res = await patchConfig(patch, configScope)
      setConfig(res.config)
      setDraft({
        embedding: res.config.embedding,
        llm: toDraftLLM(res.config.llm),
        consolidation: res.config.consolidation,
        upkeep: res.config.upkeep,
        folder_watch: res.config.folder_watch,
        ingest: res.config.ingest,
        packs: res.config.packs,
      })
      setResult({ applied: res.applied, notes: res.notes, changed: res.changed ?? Object.keys(patch) })
      if (reembedAfterSave && res.applied === 'reembed') {
        if (isApplicationDefaults) {
          const affectedVaults = res.affected_vaults ?? []
          await Promise.all(affectedVaults.map((vault) => startVaultReembed(vault)))
          setResult({
            applied: res.applied,
            changed: res.changed ?? Object.keys(patch),
            notes: [
              ...res.notes,
              `Started re-embed for ${affectedVaults.length} inheriting vault(s).`,
            ],
          })
        } else {
          await runReembed()
        }
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'save failed')
    } finally {
      setSaving(false)
    }
  }

  // ── wipe ────────────────────────────────────────────────────────────────────

  const wipe = async () => {
    setWiping(true)
    setWipeError(null)
    try {
      await resetVault()
      clearIngestQueue()
      selectNode(null)
      bumpData()
      setWiped(true)
      setConfirmWipe(false)
    } catch (e) {
      setWipeError(e instanceof Error ? e.message : 'reset failed')
    } finally {
      setWiping(false)
    }
  }

  // ── derived ─────────────────────────────────────────────────────────────────

  const { defaults, extraction, judge, curator, relation_curator, ask, step_default_prompts } = draft.llm
  // Resolved defaults as LLMDefaults (concrete values) for placeholder/inherit resolution.
  const resolvedDefaults = defaults as unknown as LLMDefaults
  const probeDisabled = draft.llm.allow_remote !== config.llm.allow_remote
  const markCredentialStored = (apiKeyEnv: string) =>
    setConfig((current) =>
      current
        ? {
            ...current,
            credential_status: {
              ...(current.credential_status ?? {}),
              [apiKeyEnv]: true,
            },
          }
        : current,
    )
  const selectTab = (tab: ConfigTab) => {
    setActiveTab(tab)
    configScrollRef.current?.scrollTo({ top: 0 })
  }

  // ── render ──────────────────────────────────────────────────────────────────

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <header className="flex items-center justify-between border-b border-surface-800 px-6 py-4">
        <div>
          <h1 className="text-base font-semibold">Config</h1>
          <p className="text-xs text-surface-500">
            {isApplicationDefaults
              ? 'Application defaults inherited by new vaults and vaults without local overrides.'
              : `Vault overrides for ${selectedVaultPath}.`}
          </p>
        </div>
        <button
          onClick={load}
          className="flex items-center gap-1.5 rounded-lg border border-surface-700 px-3 py-1.5 text-xs text-surface-300 hover:bg-surface-800"
        >
          <RotateCw size={13} /> Reload
        </button>
      </header>

      <nav
        role="tablist"
        aria-label="Configuration sections"
        className="flex shrink-0 gap-1 overflow-x-auto border-b border-surface-800 px-6"
      >
        {visibleTabs.map((tab, index) => (
          <button
            key={tab.id}
            id={`config-tab-${tab.id}`}
            type="button"
            role="tab"
            aria-selected={activeTab === tab.id}
            aria-controls="config-panel"
            tabIndex={activeTab === tab.id ? 0 : -1}
            onClick={() => selectTab(tab.id)}
            onKeyDown={(event) => {
              let nextIndex: number | null = null
              if (event.key === 'ArrowRight') nextIndex = (index + 1) % visibleTabs.length
              if (event.key === 'ArrowLeft') {
                nextIndex = (index - 1 + visibleTabs.length) % visibleTabs.length
              }
              if (event.key === 'Home') nextIndex = 0
              if (event.key === 'End') nextIndex = visibleTabs.length - 1
              if (nextIndex === null) return

              event.preventDefault()
              const nextTab = visibleTabs[nextIndex]
              selectTab(nextTab.id)
              document.getElementById(`config-tab-${nextTab.id}`)?.focus()
            }}
            className={`shrink-0 border-b-2 px-3 py-3 text-xs font-medium transition-colors ${
              activeTab === tab.id
                ? 'border-accent-500 text-accent-300'
                : 'border-transparent text-surface-400 hover:text-surface-200'
            }`}
          >
            {tab.label}
          </button>
        ))}
      </nav>

      <div ref={configScrollRef} className="min-h-0 flex-1 overflow-y-auto p-6">
        <div
          id="config-panel"
          role="tabpanel"
          aria-labelledby={`config-tab-${activeTab}`}
          className="mx-auto flex max-w-3xl flex-col gap-5"
        >

          {activeTab === 'providers' && (
            <ProvidersPanel onProvidersChanged={setProviderProfiles} />
          )}

          {activeTab === 'llm' && (
            <>

          {/* Allow remote toggle */}
          <section className="rounded-xl border border-surface-800 bg-surface-900/40 p-4">
            <label className="flex items-center justify-between gap-4 cursor-pointer">
              <div>
                <span className="text-sm font-medium text-surface-100">
                  Allow remote for legacy inline endpoints
                </span>
                <p className="mt-0.5 text-[11px] text-surface-500">
                  Provider connections own this permission in the Providers tab. This toggle only
                  applies to older inline LLM Base URL fields.
                </p>
              </div>
              <input
                type="checkbox"
                checked={draft.llm.allow_remote}
                onChange={(e) => updAllowRemote(e.target.checked)}
                className="h-5 w-5 shrink-0 accent-accent-500"
              />
            </label>
            {probeDisabled && (
              <p className="mt-3 text-[11px] text-amber-300">
                Save this security setting before using Validate or Test.
              </p>
            )}
          </section>

          {/* LLM step cards */}
          <AgenticWorkflowMap llm={draft.llm} defaults={resolvedDefaults} />

          <StepCard
            title="LLM Defaults"
            subtitle="Base values inherited by all steps unless explicitly overridden."
            step="defaults"
            data={defaults}
            defaults={resolvedDefaults}
            onChange={(p) => updStep('defaults', p)}
            credentialStatus={config.credential_status}
            onCredentialStored={markCredentialStored}
            probeDisabled={probeDisabled}
            managedCredentialStorage={config.managed_credentials_supported ?? false}
            configScope={configScope}
            providers={providerProfiles}
          />

          <StepCard
            title="Extraction LLM"
            subtitle="Per-block candidate proposal: nodes, topology edges, and literal claims."
            step="extraction"
            data={extraction}
            defaults={resolvedDefaults}
            defaultPrompt={step_default_prompts.extraction}
            onChange={(p) => updStep('extraction', p)}
            probeDisabled={probeDisabled}
            configScope={configScope}
            providers={providerProfiles}
          />

          <StepCard
            title="Merge Judge LLM"
            subtitle="Same-vs-distinct adjudication for likely duplicates; not the whole resolver."
            step="judge"
            data={judge}
            defaults={resolvedDefaults}
            defaultPrompt={step_default_prompts.judge}
            onChange={(p) => updStep('judge', p)}
            probeDisabled={probeDisabled}
            configScope={configScope}
            providers={providerProfiles}
          />

          <StepCard
            title="Candidate Curator LLM"
            subtitle="Evaluates each unique proposed node, including deterministic-remap audit cases."
            step="curator"
            data={curator}
            defaults={resolvedDefaults}
            defaultPrompt={step_default_prompts.curator}
            onChange={(p) => updStep('curator', p)}
            probeDisabled={probeDisabled}
            configScope={configScope}
            providers={providerProfiles}
          />

          <StepCard
            title="Relationship Curator LLM"
            subtitle="Evaluates each proposed topology edge and literal claim before graph write."
            step="relation_curator"
            data={relation_curator}
            defaults={resolvedDefaults}
            defaultPrompt={step_default_prompts.relation_curator}
            onChange={(p) => updStep('relation_curator', p)}
            probeDisabled={probeDisabled}
            configScope={configScope}
            providers={providerProfiles}
          />

          <StepCard
            title="Ask / Synthesis LLM"
            subtitle="Query synthesis over grounded context. Null fields inherit from Defaults."
            step="ask"
            data={ask}
            defaults={resolvedDefaults}
            defaultPrompt={step_default_prompts.ask}
            onChange={(p) => updStep('ask', p)}
            probeDisabled={probeDisabled}
            configScope={configScope}
            providers={providerProfiles}
          />
            </>
          )}

          {/* Embedding */}
          {activeTab === 'embedding' && (
          <section className="rounded-xl border border-surface-800 bg-surface-900/40 p-5">
            <div className="mb-4 flex items-start justify-between gap-4">
              <div>
                <h2 className="text-sm font-semibold text-surface-100">Embedding</h2>
                <p className="mt-0.5 text-[11px] text-surface-500">
                  Local (fastembed) or an external endpoint. Changing provider / model / dimension
                  needs a re-embed.
                </p>
              </div>
              <button
                type="button"
                onClick={() => void runEmbeddingTest()}
                disabled={embTesting || isLocalEmbedder || !draft.embedding.model.trim()}
                title={
                  isLocalEmbedder
                    ? 'Local provider — no endpoint to test'
                    : !draft.embedding.model.trim()
                      ? 'Choose an embedding model first.'
                      : undefined
                }
                className="flex shrink-0 items-center gap-1.5 rounded-lg border border-surface-700 bg-surface-800 px-3 py-1.5 text-xs text-surface-300 hover:bg-surface-700 disabled:opacity-40"
              >
                {embTesting ? (
                  <RotateCw size={12} className="animate-spin" />
                ) : (
                  <FlaskConical size={12} />
                )}
                Test
              </button>
            </div>

            {embTestError && (
              <div className="mb-3 rounded-lg border border-red-900/60 bg-red-950/40 px-3 py-2 text-xs text-red-300">
                {embTestError}
              </div>
            )}
            {embTestDimension !== null && (
              <div className="mb-3 flex items-center gap-2 rounded-lg border border-emerald-800/60 bg-emerald-950/30 px-3 py-2 text-xs text-emerald-300">
                <Check size={12} /> Batch test passed · {embTestVectors ?? 2} vectors ·{' '}
                {embTestDimension} dimensions
              </div>
            )}

            <div className="grid grid-cols-2 gap-4">
              <Field
                label="Provider connection"
                hint={embeddingProfile ? `${embeddingProfile.driver}${embeddingProfile.api_base ? ` · ${embeddingProfile.api_base}` : ''}` : undefined}
              >
                <Select
                  value={draft.embedding.provider_ref ?? ''}
                  onChange={(value) => {
                    updEmbedding({ provider_ref: value || null, model: '' })
                    resetEmbeddingCredentialDraft()
                    setEmbModels(null)
                    setEmbTestError(null)
                    setEmbTestDimension(null)
                    setEmbTestVectors(null)
                  }}
                  options={[
                    { value: '', label: '— inline / legacy fields —' },
                    ...providerProfiles
                      .filter((provider) => provider.uses.includes('embedding'))
                      .map((provider) => ({ value: provider.id, label: provider.name })),
                  ]}
                />
              </Field>
              {!embeddingProfile && (
              <Field label="Provider">
                <Select
                  value={draft.embedding.provider}
                  onChange={(v) => {
                    updEmbedding({ provider: v, model: '', api_key_env: null })
                    resetEmbeddingCredentialDraft()
                    setEmbModels(null)
                    setEmbTestError(null)
                    setEmbTestDimension(null)
                    setEmbTestVectors(null)
                  }}
                  options={EMBEDDING_PROVIDERS}
                />
              </Field>
              )}
              <Field label="Model">
                {embDiscovering ? (
                  <div className={`${inputCls} flex items-center gap-2 text-surface-400`}>
                    <RotateCw size={12} className="animate-spin" /> Loading embedding models…
                  </div>
                ) : embModels && embModels.length > 0 ? (
                  <>
                    <Select
                      value={draft.embedding.model}
                      onChange={(v) => updEmbedding({ model: v })}
                      options={[
                        { value: '', label: 'Select an embedding model' },
                        ...(draft.embedding.model && !embModels.includes(draft.embedding.model)
                          ? [{
                              value: draft.embedding.model,
                              label: `${draft.embedding.model} (saved)`,
                            }]
                          : []),
                        ...embModels.map((model) => ({ value: model, label: model })),
                      ]}
                    />
                    <span className="mt-0.5 flex items-center gap-1 text-[11px] text-accent-400">
                      <Check size={10} /> {embModels.length} model
                      {embModels.length !== 1 ? 's' : ''} from LiteLLM Gateway
                    </span>
                  </>
                ) : (
                  <input
                    className={inputCls}
                    value={draft.embedding.model}
                    onChange={(e) => updEmbedding({ model: e.target.value })}
                  />
                )}
                {embDiscoveryError && (
                  <span className="mt-0.5 text-[11px] text-amber-300">
                    {embDiscoveryError}
                  </span>
                )}
              </Field>

              <Field label="Dimension" hint="Vector width. Must match the model. Re-embed on change.">
                <NumberField
                  value={draft.embedding.dimension ?? 384}
                  onChange={(n) => updEmbedding({ dimension: n ?? 384 })}
                  min={1}
                  step={1}
                />
              </Field>

              <details className="col-span-2 rounded-lg border border-surface-800 bg-surface-950/50 px-3 py-2">
                <summary className="cursor-pointer text-xs text-surface-400">
                  Advanced execution
                </summary>
                <div className="mt-3 grid grid-cols-2 gap-4">
                  <Field
                    label="Batch size"
                    hint="Texts per provider request. 32 is the portable default; range 1–256."
                  >
                    <NumberField
                      value={draft.embedding.batch_size ?? 32}
                      onChange={(n) => updEmbedding({
                        batch_size: clampInt(n, 1, 256, 32),
                      })}
                      min={1}
                      max={256}
                      step={1}
                    />
                  </Field>
                  <Field
                    label="Concurrent batches"
                    hint="Provider requests in flight. Starts at 1; range 1–32."
                  >
                    <NumberField
                      value={draft.embedding.max_concurrent_batches ?? 1}
                      onChange={(n) => updEmbedding({
                        max_concurrent_batches: clampInt(n, 1, 32, 1),
                      })}
                      min={1}
                      max={32}
                      step={1}
                    />
                  </Field>
                  <p className="col-span-2 text-[11px] text-surface-500">
                    These settings change request scheduling only. They apply live and never
                    require re-embedding stored vectors.
                  </p>
                </div>
              </details>

              {!embeddingProfile && !isLocalEmbedder && (
                <Field label="Base URL" hint="Loopback only unless allow remote is on.">
                  <input
                    className={inputCls}
                    value={draft.embedding.api_base ?? ''}
                    placeholder="http://127.0.0.1:1234/v1"
                    onChange={(e) => {
                      updEmbedding({ api_base: e.target.value || null, api_key_env: null })
                      resetEmbeddingCredentialDraft()
                      setEmbModels(null)
                    }}
                  />
                </Field>
              )}

              {canStoreEmbeddingApiKey && (
                <Field
                  label="API key"
                  hint={
                    embeddingCredentialConfigured && draft.embedding.api_key_env
                      ? `Available to this daemon as ${draft.embedding.api_key_env}. The value is never returned to the browser or written to vault YAML.`
                      : 'Stored locally with owner-only permissions. The value is never written to vault YAML.'
                  }
                >
                  <div className="flex gap-2">
                    <input
                      data-testid="embedding-api-key"
                      type="password"
                      autoComplete="off"
                      spellCheck={false}
                      className={inputCls}
                      value={embApiKey}
                      placeholder={embeddingCredentialConfigured ? 'Stored — paste to replace' : 'Paste API key'}
                      onChange={(event) => {
                        setEmbApiKey(event.target.value)
                        setEmbCredentialError(null)
                        setEmbStoredCredentialEnv(null)
                      }}
                    />
                    <button
                      data-testid="embedding-api-key-save"
                      type="button"
                      onClick={() => void storeEmbeddingApiKey()}
                      disabled={!embApiKey || embStoringCredential}
                      className="shrink-0 rounded-lg border border-surface-700 bg-surface-800 px-3 py-2 text-xs text-surface-200 hover:bg-surface-700 disabled:opacity-40"
                    >
                      {embStoringCredential
                        ? 'Saving…'
                        : embeddingCredentialConfigured
                          ? 'Replace key'
                          : 'Save key'}
                    </button>
                  </div>
                  {embCredentialError && (
                    <p className="mt-1 text-[11px] text-red-300">{embCredentialError}</p>
                  )}
                  {embStoredCredentialEnv === draft.embedding.api_key_env && draft.embedding.api_key_env && (
                    <p className="mt-1 text-[11px] text-accent-400">
                      Key stored as {draft.embedding.api_key_env} and attached to this draft.
                    </p>
                  )}
                </Field>
              )}

              {!embeddingProfile && !isLocalEmbedder && !canStoreEmbeddingApiKey && (
                <Field
                  label="Provider credentials"
                  hint={
                    embeddingSupportsManagedApiKey
                      ? 'Managed key storage is unavailable on this platform; use an external environment credential.'
                      : 'This provider uses its native credential chain; configure that outside Okto Neuron.'
                  }
                >
                  <div className="rounded-lg border border-surface-800 bg-surface-950 px-3 py-2 text-xs text-surface-400">
                    No raw secret is stored in this vault.
                  </div>
                </Field>
              )}

              {!embeddingProfile && !isLocalEmbedder && (
                <details className="col-span-2 rounded-lg border border-surface-800 bg-surface-950/50 px-3 py-2">
                  <summary className="cursor-pointer text-xs text-surface-400">
                    Advanced credential reference
                  </summary>
                  <div className="mt-3">
                    <Field label="API key environment variable" hint="Reference only — never paste a secret here.">
                      <input
                        className={inputCls}
                        value={draft.embedding.api_key_env ?? ''}
                        placeholder="OKTO_NEURON_…"
                        onChange={(event) => {
                          updEmbedding({ api_key_env: event.target.value || null })
                          setEmbCredentialError(null)
                          setEmbStoredCredentialEnv(null)
                        }}
                      />
                    </Field>
                  </div>
                </details>
              )}

              {!embeddingProfile && !isLocalEmbedder && (
                <Field label="Allow remote endpoint">
                  <label className="flex items-center gap-2 py-2 text-sm text-surface-300">
                    <input
                      type="checkbox"
                      checked={draft.embedding.allow_remote ?? false}
                      onChange={(e) => updEmbedding({ allow_remote: e.target.checked })}
                      className="h-4 w-4 accent-accent-500"
                    />
                    Permit a non-loopback Base URL
                  </label>
                </Field>
              )}

              {embedderChanged && (
                <div className="col-span-2 flex items-start gap-3 rounded-lg border border-amber-600/50 bg-amber-950/40 p-3 text-sm text-amber-200">
                  <AlertTriangle size={18} className="mt-0.5 shrink-0 text-amber-400" />
                  <div>
                    <p className="font-medium">Changing the embedder invalidates stored embeddings.</p>
                    <p className="mt-1 text-amber-300/80">
                      {isApplicationDefaults
                        ? 'Saving will re-embed every inheriting vault whose effective model or width changes.'
                        : "Existing vectors were written in the current model's space. Saving will immediately re-embed every vector at the new model / width."}
                    </p>
                  </div>
                </div>
              )}
            </div>

            {/* Re-embed now */}
            {!isApplicationDefaults && (
            <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-surface-800 pt-4">
              <button
                type="button"
                onClick={() => void runReembed()}
                disabled={reembedding}
                className="flex items-center gap-2 rounded-lg border border-accent-700 bg-accent-900/30 px-4 py-2 text-sm font-medium text-accent-200 transition-colors hover:bg-accent-900/60 disabled:opacity-40"
              >
                {reembedding ? <RotateCw size={15} className="animate-spin" /> : <RotateCw size={15} />}
                Re-embed now
              </button>
              <p className="text-[11px] text-surface-500">
                Recomputes every vector from the stored graph (no re-extraction). Save config first.
              </p>
              {reembedStatus && (
                <span className="text-xs text-surface-300">
                  {reembedStatus.phase === 'complete'
                    ? `done — ${reembedStatus.nodes ?? 0} nodes (${reembedStatus.recomputed ?? 0} re-embedded) at dim ${reembedStatus.embedding_dim ?? '?'}`
                    : reembedStatus.phase === 'embedding'
                      ? `embedding ${reembedStatus.nodes_done ?? 0}/${reembedStatus.nodes_total ?? 0}…`
                      : `${reembedStatus.phase}…`}
                </span>
              )}
              {reembedError && <span className="text-xs text-red-300">{reembedError}</span>}
            </div>
            )}
          </section>
          )}

          {/* Consolidation */}
          {activeTab === 'curation' && (
            <>
          <section className="rounded-xl border border-surface-800 bg-surface-900/40 p-5">
            <div className="mb-4">
              <h2 className="text-sm font-semibold text-surface-100">
                Semantic model stages
              </h2>
              <p className="mt-0.5 text-[11px] text-surface-500">
                Advanced ablation controls. Enabled is the production default; disabled
                stages make zero model calls and route affected candidates to review.
              </p>
            </div>
            <div className="grid grid-cols-2 gap-4">
              <Field label="Type adjudication">
                <label className="flex items-center gap-2 py-2 text-sm text-surface-300">
                  <input
                    type="checkbox"
                    checked={draft.consolidation.type_adjudication_enabled}
                    onChange={(e) =>
                      updConsolidation({ type_adjudication_enabled: e.target.checked })
                    }
                    className="h-4 w-4 accent-accent-500"
                  />
                  Resolve exact-name primitive type conflicts
                </label>
              </Field>
              <Field label="Relationship curator">
                <label className="flex items-center gap-2 py-2 text-sm text-surface-300">
                  <input
                    type="checkbox"
                    checked={draft.consolidation.relation_curator_enabled}
                    onChange={(e) =>
                      updConsolidation({ relation_curator_enabled: e.target.checked })
                    }
                    className="h-4 w-4 accent-accent-500"
                  />
                  Validate relationships before graph materialization
                </label>
              </Field>
            </div>
            {semanticStagesChanged && (
              <div className="mt-4 flex gap-2 rounded-lg border border-amber-700/60 bg-amber-950/30 p-3 text-xs text-amber-200">
                <AlertTriangle size={16} className="mt-0.5 shrink-0" />
                <span>
                  This changes semantic materialization policy. Saving does not rewrite the
                  current graph; rebuild existing sources before evaluating or using it as
                  output from the new policy.
                </span>
              </div>
            )}
          </section>

          <section className="rounded-xl border border-surface-800 bg-surface-900/40 p-5">
            <h2 className="mb-4 text-sm font-semibold text-surface-100">
              Consolidation gate (live)
            </h2>
            <div className="grid grid-cols-2 gap-4">
              <Field
                label={`Auto-commit threshold: ${draft.consolidation.auto_commit_threshold.toFixed(2)}`}
                hint="0–1. Above this, claims auto-commit; below routes to review."
              >
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.01}
                  value={draft.consolidation.auto_commit_threshold}
                  onChange={(e) =>
                    updConsolidation({ auto_commit_threshold: Number(e.target.value) })
                  }
                  className="accent-accent-500"
                />
              </Field>
              <Field label="Review on contradiction">
                <label className="flex items-center gap-2 py-2 text-sm text-surface-300">
                  <input
                    type="checkbox"
                    checked={draft.consolidation.review_on_contradiction}
                    onChange={(e) =>
                      updConsolidation({ review_on_contradiction: e.target.checked })
                    }
                    className="h-4 w-4 accent-accent-500"
                  />
                  Route contradictions to the review queue
                </label>
              </Field>
              <Field label="Audit superseded nodes with LLM">
                <label className="flex items-center gap-2 py-2 text-sm text-surface-300">
                  <input
                    type="checkbox"
                    checked={draft.consolidation.audit_superseded_nodes_with_llm}
                    onChange={(e) =>
                      updConsolidation({ audit_superseded_nodes_with_llm: e.target.checked })
                    }
                    className="h-4 w-4 accent-accent-500"
                  />
                  Review removed or remapped node candidates before commit
                </label>
              </Field>
              <Field label="Audit superseded relations with LLM">
                <label className="flex items-center gap-2 py-2 text-sm text-surface-300">
                  <input
                    type="checkbox"
                    checked={draft.consolidation.audit_superseded_relations_with_llm}
                    onChange={(e) =>
                      updConsolidation({
                        audit_superseded_relations_with_llm: e.target.checked,
                      })
                    }
                    className="h-4 w-4 accent-accent-500"
                  />
                  Review removed or remapped relation candidates before commit
                </label>
              </Field>
            </div>
          </section>

          {/* Consolidation / Performance (ADR 0015) */}
          <section className="rounded-xl border border-surface-800 bg-surface-900/40 p-5">
            <div className="mb-4">
              <h2 className="text-sm font-semibold text-surface-100">
                Consolidation / Performance
              </h2>
              <p className="mt-0.5 text-[11px] text-surface-500">
                ADR 0015 ingest-throughput knobs. Defaults preserve today's sequential
                behavior; raising them only pays on a multi-slot inference server.
              </p>
            </div>
            <CapacityNotice capacity={config?.capacity} />
            <div className="grid grid-cols-2 gap-4">
              <Field
                label="Max concurrent curator calls"
                hint="Parallel curator calls (needs a multi-slot inference server). 1 = sequential. 1–32."
              >
                <NumberField
                  value={draft.consolidation.curation_max_concurrent}
                  onChange={(n) =>
                    updConsolidation({ curation_max_concurrent: clampInt(n, 1, 32, 1) })
                  }
                  min={1}
                  max={32}
                  step={1}
                />
              </Field>
              <Field
                label="Curation batch size"
                hint="Candidates judged per LLM call — 10 proved ~5× fewer tokens. 1 = per-candidate calls. 1–32."
              >
                <NumberField
                  value={draft.consolidation.curation_batch_size}
                  onChange={(n) =>
                    updConsolidation({ curation_batch_size: clampInt(n, 1, 32, 1) })
                  }
                  min={1}
                  max={32}
                  step={1}
                />
              </Field>
              <Field
                label="Curation wait timeout (s)"
                hint="Optional scheduler wait deadline. Empty waits for the provider result; provider request timeout is configured on the provider connection."
              >
                <NumberField
                  value={draft.consolidation.curation_call_timeout_s}
                  onChange={(n) =>
                    updConsolidation({
                      curation_call_timeout_s: n === null || Number.isNaN(n) || n <= 0 ? null : n,
                    })
                  }
                  min={1}
                  step={10}
                />
              </Field>
              <div /> {/* grid spacer */}
              <Field label="Deterministic pre-filter">
                <label className="flex items-center gap-2 py-2 text-sm text-surface-300">
                  <input
                    type="checkbox"
                    checked={draft.consolidation.prefilter.enabled}
                    onChange={(e) => updPrefilter({ enabled: e.target.checked })}
                    className="h-4 w-4 accent-accent-500"
                  />
                  Demote low-value candidates (generic predicates, trivial nodes) to review instead of an LLM verdict
                </label>
              </Field>
              <Field label="Established-entity fast path">
                <label className="flex items-center gap-2 py-2 text-sm text-surface-300">
                  <input
                    type="checkbox"
                    checked={draft.consolidation.prefilter.established_entity_fastpath}
                    onChange={(e) =>
                      updPrefilter({ established_entity_fastpath: e.target.checked })
                    }
                    className="h-4 w-4 accent-accent-500"
                  />
                  Skip the curator when a candidate merges into a live node with no novel content
                </label>
              </Field>
            </div>
          </section>
            </>
          )}

          {/* Ingest chunking (ADR 0038) + folder watch (ADR 0025) */}
          {activeTab === 'ingestion' && (
            <>
              <section className="rounded-xl border border-surface-800 bg-surface-900/40 p-5">
                <div className="mb-4">
                  <h2 className="text-sm font-semibold text-surface-100">Text chunking</h2>
                  <p className="mt-0.5 text-[11px] text-surface-500">
                    Byte-anchored extraction windows. Whole lines are kept intact.
                  </p>
                </div>
                <div className="grid grid-cols-2 gap-4">
                  <Field
                    label="Chunk size (bytes)"
                    hint="Maximum target size per extraction chunk. Very long individual lines remain whole. 256–1,000,000."
                  >
                    <NumberField
                      value={draft.ingest.chunk_size_bytes}
                      onChange={(n) => {
                        const size = clampInt(n, 256, 1_000_000, 12_000)
                        updIngest({
                          chunk_size_bytes: size,
                          chunk_overlap_bytes: Math.min(
                            draft.ingest.chunk_overlap_bytes,
                            size - 1,
                          ),
                        })
                      }}
                      min={256}
                      max={1_000_000}
                      step={256}
                    />
                  </Field>
                  <Field
                    label="Chunk overlap (bytes)"
                    hint="Up to this many bytes of whole trailing lines are repeated in the next chunk. Must be smaller than chunk size."
                  >
                    <NumberField
                      value={draft.ingest.chunk_overlap_bytes}
                      onChange={(n) =>
                        updIngest({
                          chunk_overlap_bytes: clampInt(
                            n,
                            0,
                            draft.ingest.chunk_size_bytes - 1,
                            0,
                          ),
                        })
                      }
                      min={0}
                      max={draft.ingest.chunk_size_bytes - 1}
                      step={256}
                    />
                  </Field>
                </div>
                <div className="mt-4 flex gap-2 rounded-lg border border-amber-700/60 bg-amber-950/30 p-3 text-xs text-amber-200">
                  <AlertTriangle size={15} className="mt-0.5 shrink-0" />
                  <span>
                    Changes apply to the next ingest. Reingest existing sources to rebuild
                    their Blocks and extraction results with the new partition.
                  </span>
                </div>
              </section>

              {!isApplicationDefaults && (
                <section className="rounded-xl border border-surface-800 bg-surface-900/40 p-5">
                  <div className="mb-4">
                    <h2 className="text-sm font-semibold text-surface-100">Folder watch</h2>
                    <p className="mt-0.5 text-[11px] text-surface-500">
                      Continuously monitor local folders and auto-ingest changed files.
                    </p>
                  </div>
                  <div className="grid grid-cols-2 gap-4">
                    <Field label="Enable folder watch">
                      <label className="flex items-center gap-2 py-2 text-sm text-surface-300">
                        <input
                          type="checkbox"
                          checked={draft.folder_watch.enabled}
                          onChange={(e) => updFolderWatch({ enabled: e.target.checked })}
                          className="h-4 w-4 accent-accent-500"
                        />
                        Poll watched roots and auto-ingest settled changes
                      </label>
                    </Field>
                    <div /> {/* grid spacer */}
                    <Field label="Poll interval (s)" hint="How often each watched root is re-scanned.">
                      <NumberField
                        value={draft.folder_watch.poll_interval_s}
                        onChange={(n) => updFolderWatch({ poll_interval_s: n === null || n <= 0 ? 5 : n })}
                        min={1}
                        step={1}
                      />
                    </Field>
                    <Field label="Quiet debounce (s)" hint="A file must stop changing for this long before ingest.">
                      <NumberField
                        value={draft.folder_watch.quiet_debounce_s}
                        onChange={(n) => updFolderWatch({ quiet_debounce_s: n === null || n < 0 ? 3 : n })}
                        min={0}
                        step={1}
                      />
                    </Field>
                    <Field label="Min interval between ingests (s)" hint="Minimum gap before the same file is re-ingested.">
                      <NumberField
                        value={draft.folder_watch.min_interval_s}
                        onChange={(n) => updFolderWatch({ min_interval_s: n === null || n < 0 ? 10 : n })}
                        min={0}
                        step={1}
                      />
                    </Field>
                    <Field label="Recursive">
                      <label className="flex items-center gap-2 py-2 text-sm text-surface-300">
                        <input
                          type="checkbox"
                          checked={draft.folder_watch.recursive}
                          onChange={(e) => updFolderWatch({ recursive: e.target.checked })}
                          className="h-4 w-4 accent-accent-500"
                        />
                        Walk subdirectories
                      </label>
                    </Field>
                    <Field label="Watched roots" span hint="Absolute folder paths polled for changes.">
                      <FolderWatchRootsEditor
                        roots={draft.folder_watch.roots}
                        onChange={(roots) => updFolderWatch({ roots })}
                      />
                    </Field>
                  </div>
                </section>
              )}
            </>
          )}

          {/* Predicate upkeep (ADR 0017) */}
          {activeTab === 'curation' && (
          <section className="rounded-xl border border-surface-800 bg-surface-900/40 p-5">
            <div className="mb-4">
              <h2 className="text-sm font-semibold text-surface-100">Predicate upkeep</h2>
              <p className="mt-0.5 text-[11px] text-surface-500">
                Bounded predicate canonicalization sweeps. Propose is read-only; apply writes the alias ledger.
              </p>
            </div>
            <div className="grid grid-cols-2 gap-4">
              <Field label="Enable predicate upkeep">
                <label className="flex items-center gap-2 py-2 text-sm text-surface-300">
                  <input
                    type="checkbox"
                    checked={draft.upkeep.enabled}
                    onChange={(e) => updUpkeep({ enabled: e.target.checked })}
                    className="h-4 w-4 accent-accent-500"
                  />
                  Allow scheduled predicate propose sweeps
                </label>
              </Field>
              <Field label="Max pairs per run" hint="Judge budget cap per propose run. 1–200.">
                <NumberField
                  value={draft.upkeep.max_pairs_per_run}
                  onChange={(n) =>
                    updUpkeep({ max_pairs_per_run: clampInt(n, 1, 200, 30) })
                  }
                  min={1}
                  max={200}
                  step={1}
                />
              </Field>
              <Field label="Minimum support" hint="Predicate uses required to anchor a pair. 1–100.">
                <NumberField
                  value={draft.upkeep.min_support}
                  onChange={(n) => updUpkeep({ min_support: clampInt(n, 1, 100, 2) })}
                  min={1}
                  max={100}
                  step={1}
                />
              </Field>
              <Field
                label={`Cluster threshold: ${draft.upkeep.cluster_threshold.toFixed(2)}`}
                hint="Embedding similarity needed to candidate predicates together. 0.50–0.99."
              >
                <input
                  type="range"
                  min={0.5}
                  max={0.99}
                  step={0.01}
                  value={draft.upkeep.cluster_threshold}
                  onChange={(e) =>
                    updUpkeep({
                      cluster_threshold: clampFloat(Number(e.target.value), 0.5, 0.99, 0.8),
                    })
                  }
                  className="accent-accent-500"
                />
              </Field>
              <Field
                label={`Auto-fold threshold: ${draft.upkeep.auto_fold_threshold.toFixed(2)}`}
                hint="Same-predicate confidence required for auto status. 0.50–1.00."
              >
                <input
                  type="range"
                  min={0.5}
                  max={1}
                  step={0.01}
                  value={draft.upkeep.auto_fold_threshold}
                  onChange={(e) =>
                    updUpkeep({
                      auto_fold_threshold: clampFloat(Number(e.target.value), 0.5, 1, 0.85),
                    })
                  }
                  className="accent-accent-500"
                />
              </Field>
            </div>
          </section>
          )}

          {/* Packs (read-only) */}
          {activeTab === 'vault' && (
            <>
          <section className="flex flex-wrap items-center gap-2 text-xs text-surface-500">
            <span>packs:</span>
            {config.packs.map((p) => (
              <Badge key={p}>{p}</Badge>
            ))}
          </section>

          {/* Danger zone */}
          <section className="rounded-xl border border-rose-800/60 bg-rose-950/20 p-5">
            <div className="mb-3 flex items-center gap-2">
              <h2 className="text-sm font-semibold text-rose-200">Danger zone</h2>
              <Badge tone="danger">irreversible</Badge>
            </div>
            <p className="mb-4 text-xs text-rose-300/70">
              Start fresh permanently deletes all notes, ingested sources, derived state, and
              the graph. Your config (okto-neuron.yaml) is preserved.
            </p>
            {wipeError && (
              <div className="mb-3">
                <ErrorBox message={wipeError} />
              </div>
            )}
            {wiped ? (
              <div className="flex items-center gap-2 text-sm text-rose-200">
                <Trash2 size={15} />
                Vault wiped. Everything is blank.
              </div>
            ) : (
              <button
                onClick={() => setConfirmWipe(true)}
                disabled={wiping}
                className="flex items-center gap-2 rounded-lg border border-rose-700 bg-rose-900/40 px-4 py-2 text-sm font-medium text-rose-200 transition-colors hover:bg-rose-900/70 disabled:opacity-40"
              >
                {wiping ? <RotateCw size={15} className="animate-spin" /> : <Trash2 size={15} />}
                Start fresh
              </button>
            )}
          </section>
            </>
          )}

          {/* Save result */}
          {result && (
            <div className="rounded-lg border border-surface-700 bg-surface-900/60 p-4">
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <span className="text-surface-400">Applied:</span>
                <Badge tone={result.applied === 'live' ? 'accent' : 'warn'}>{result.applied}</Badge>
                {result.changed.length > 0 && (
                  <span className="text-xs text-surface-500">
                    changed: {result.changed.join(', ')}
                  </span>
                )}
              </div>
              {result.notes.length > 0 && (
                <ul className="mt-2 list-disc pl-5 text-xs text-surface-400">
                  {result.notes.map((n, i) => (
                    <li key={i}>{n}</li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Save bar */}
      <div className="flex items-center justify-end gap-3 border-t border-surface-800 px-6 py-4">
        {hasChanges && <span className="text-xs text-amber-400">unsaved changes</span>}
        <button
          onClick={() =>
            embedderChanged && !semanticStagesChanged
              ? setConfirmReembed(true)
              : void save()
          }
          disabled={!hasChanges || saving || reembedding}
          className="flex items-center gap-2 rounded-lg bg-accent-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-accent-500 disabled:opacity-40"
        >
          {saving ? <RotateCw size={15} className="animate-spin" /> : <Save size={15} />}
          {semanticStagesChanged
            ? (isApplicationDefaults ? 'Save defaults — rebuild required' : 'Save — rebuild required')
            : embedderChanged
            ? (isApplicationDefaults ? 'Save defaults & re-embed' : 'Save & re-embed')
            : (isApplicationDefaults ? 'Save defaults' : 'Save changes')}
        </button>
      </div>

      {/* Confirm embedding-space migration */}
      {confirmReembed && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <div className="w-full max-w-md rounded-xl border border-amber-700/60 bg-surface-900 p-6 shadow-2xl">
            <div className="mb-3 flex items-center gap-2">
              <AlertTriangle size={18} className="text-amber-400" />
              <h2 className="text-base font-semibold text-amber-200">Change embedding space?</h2>
            </div>
            <p className="text-sm text-surface-300">
              {isApplicationDefaults
                ? 'Changing the provider, model, or dimension will update every inheriting vault without a local override. Confirming will save the defaults and start a full vector re-embed for each affected vault.'
                : 'Changing the provider, model, or dimension makes the stored vectors incompatible. Confirming will save the configuration and immediately start a full vector re-embed. Ingestion and semantic queries remain unavailable until it completes.'}
            </p>
            <div className="mt-6 flex items-center justify-end gap-3">
              <button
                onClick={() => setConfirmReembed(false)}
                disabled={saving || reembedding}
                className="rounded-lg border border-surface-700 px-4 py-2 text-sm text-surface-300 hover:bg-surface-800 disabled:opacity-40"
              >
                Cancel
              </button>
              <button
                onClick={() => {
                  setConfirmReembed(false)
                  void save(true)
                }}
                disabled={saving || reembedding}
                className="flex items-center gap-2 rounded-lg bg-amber-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-amber-500 disabled:opacity-40"
              >
                {(saving || reembedding) && <RotateCw size={15} className="animate-spin" />}
                {isApplicationDefaults ? 'Save defaults & re-embed' : 'Save & re-embed'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Confirm wipe modal */}
      {confirmWipe && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <div className="w-full max-w-md rounded-xl border border-rose-800/60 bg-surface-900 p-6 shadow-2xl">
            <div className="mb-3 flex items-center gap-2">
              <AlertTriangle size={18} className="text-rose-400" />
              <h2 className="text-base font-semibold text-rose-200">Start fresh?</h2>
            </div>
            <p className="text-sm text-surface-300">
              This permanently deletes all notes, ingested sources, derived state, and the graph.
              Irreversible.
            </p>
            {wipeError && (
              <div className="mt-4">
                <ErrorBox message={wipeError} />
              </div>
            )}
            <div className="mt-6 flex items-center justify-end gap-3">
              <button
                onClick={() => setConfirmWipe(false)}
                disabled={wiping}
                className="rounded-lg border border-surface-700 px-4 py-2 text-sm text-surface-300 hover:bg-surface-800 disabled:opacity-40"
              >
                Cancel
              </button>
              <button
                onClick={() => void wipe()}
                disabled={wiping}
                className="flex items-center gap-2 rounded-lg bg-rose-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-rose-500 disabled:opacity-40"
              >
                {wiping ? <RotateCw size={15} className="animate-spin" /> : <Trash2 size={15} />}
                Confirm
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
