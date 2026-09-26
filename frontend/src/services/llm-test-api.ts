// LLM test / model-discovery endpoint.
import { isMock } from '@/lib/mode'
import { apiFetch } from './http'
import type {
  LlmTestCompletionRequest,
  LlmTestCompletionResponse,
  LlmTestRequest,
  LlmTestResponse,
  ConfigScope,
} from '@/types'

export function postLlmTest(
  body: LlmTestRequest,
  scope: ConfigScope = 'vault',
): Promise<LlmTestResponse> {
  if (isMock()) {
    // In mock mode: succeed immediately and return a plausible model list.
    const models = [body.model, 'llama3:8b', 'mistral:7b', 'qwen2.5:14b'].filter(
      (m): m is string => !!m,
    )
    return Promise.resolve({ ok: true, models, error: null })
  }
  const path = scope === 'application' ? '/config/defaults/llm/test' : '/llm/test'
  return apiFetch<LlmTestResponse>(path, {
    method: 'POST',
    body: JSON.stringify(body),
    vaultScoped: scope === 'vault',
  })
}

// Round-trips a real completion through the configured provider/model.
export function postLlmTestCompletion(
  body: LlmTestCompletionRequest,
  scope: ConfigScope = 'vault',
): Promise<LlmTestCompletionResponse> {
  if (isMock()) {
    return Promise.resolve({
      ok: true,
      reply: 'pong',
      error: null,
      duration_s: 0.1,
      parameter_plan: { sent: [], extra_body: [], omitted: {} },
    })
  }
  const path = scope === 'application'
    ? '/config/defaults/llm/test-completion'
    : '/llm/test-completion'
  return apiFetch<LlmTestCompletionResponse>(path, {
    method: 'POST',
    body: JSON.stringify(body),
    vaultScoped: scope === 'vault',
  })
}
