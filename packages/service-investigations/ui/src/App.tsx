import React, { useState } from 'react'
import { ChatPanel, useAppApi, useNavBadge } from '@kirocrew/app-sdk'
import { Badge, Btn, Card, ErrorNotice, Input, PageHeader } from '@kirocrew/app-sdk/ui'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Search, Settings2, Square, Play, KeyRound } from 'lucide-react'

const endpoint = '/api/apps/service-investigations/investigations'
const queryKey = ['service-investigations', 'index']
type Service = Record<string, string>
type Run = {
  id: string; slot_key: string; service: Service; question: string; status: string
  created_at: number; updated_at: number; report: Record<string, string>
  identity: Record<string, unknown>; auth_message: string; chat_ready: boolean
  timeline: { at: number; text: string }[]
}
type Index = { services: Service[]; runs: Run[] }
const emptyService: Service = {
  name: '', repository: '', aws_profile: '', aws_account: '', kube_context: '',
  kube_server: '', namespaces: '', log_sources: '', database: '', instructions: '',
}
const pt = {
  title: 'Investigações', subtitle: 'Entenda o que está acontecendo. Decida o que mudar.',
  services: 'Serviços', newRun: 'Nova investigação', start: 'Investigar', question: 'O que você quer entender?',
  placeholder: 'Ex.: por que a latência aumentou depois do último deploy?', choose: 'Escolha um serviço',
  attention: 'Precisa de você', active: 'Em andamento', history: 'Histórico', all: 'Todos os serviços',
  empty: 'Comece com uma pergunta sobre um serviço.', setup: 'Cadastre o contexto de um serviço para começar.',
  save: 'Salvar serviço', back: 'Voltar', add: 'Novo serviço', cancel: 'Cancelar investigação', resume: 'Retomar',
  reconnect: 'Reconectar AWS SSO', loading: 'Carregando…', timeline: 'Atividade', chat: 'Conversa e aprovações',
  findings: 'Achados', noFindings: 'O agente está reunindo evidências. Acompanhe a conversa abaixo.',
  access: 'Use os acessos locais que você já tem. Informe referências de conexão, nunca senhas ou tokens.',
  policy: 'Leituras passam por avaliação automática. Mudanças e operações ambíguas pedem aprovação. Essa avaliação é best effort; não é um bloqueio de escrita na infraestrutura.',
  local: 'V1 · Kiro CLI. A execução continua ao sair desta página; mantenha o gateway ligado.',
  auth: 'Abra o endereço abaixo no navegador e use o código exibido. A investigação retoma após o login.',
  name: 'Nome do serviço', repository: 'Repositório local', aws_profile: 'Perfil AWS', aws_account: 'Conta AWS esperada',
  kube_context: 'Contexto Kubernetes', kube_server: 'API server esperado (https://…)', namespaces: 'Namespaces',
  log_sources: 'Fontes de logs', database: 'Referência local do banco', instructions: 'Instruções de investigação',
  summary: 'Resumo', evidence: 'Evidências', hypotheses: 'Hipóteses', gaps: 'O que falta confirmar',
  recommendation: 'Recomendação', decisions: 'Decisões que precisam de você', verified: 'Identidade verificada',
  checking: 'Conferindo acesso', running: 'Investigando', waiting_approval: 'Aguardando aprovação',
  waiting_auth: 'Acesso expirado', connecting: 'Reconectando', completed: 'Concluída',
  interrupted: 'Interrompida', failed: 'Falha', needs_attention: 'Revisão necessária', cancelled: 'Cancelada',
}
const en: typeof pt = {
  title: 'Investigations', subtitle: 'Understand what is happening. Decide what to change.',
  services: 'Services', newRun: 'New investigation', start: 'Investigate', question: 'What do you want to understand?',
  placeholder: 'E.g. why did latency increase after the latest deployment?', choose: 'Choose a service',
  attention: 'Needs you', active: 'In progress', history: 'History', all: 'All services',
  empty: 'Start with a question about a service.', setup: 'Save a service context to get started.',
  save: 'Save service', back: 'Back', add: 'New service', cancel: 'Cancel investigation', resume: 'Resume',
  reconnect: 'Reconnect AWS SSO', loading: 'Loading…', timeline: 'Activity', chat: 'Conversation and approvals',
  findings: 'Findings', noFindings: 'The agent is gathering evidence. Follow the conversation below.',
  access: 'Use your existing local access. Enter connection references, never passwords or tokens.',
  policy: 'Reads receive automatic review. Changes and ambiguous operations need approval. Review is best effort; it does not enforce read-only infrastructure access.',
  local: 'V1 · Kiro CLI. Execution continues after leaving this page; keep the gateway running.',
  auth: 'Open the address below in your browser and enter the displayed code. Investigation resumes after sign-in.',
  name: 'Service name', repository: 'Local repository', aws_profile: 'AWS profile', aws_account: 'Expected AWS account',
  kube_context: 'Kubernetes context', kube_server: 'Expected API server (https://…)', namespaces: 'Namespaces',
  log_sources: 'Log sources', database: 'Local database reference', instructions: 'Investigation instructions',
  summary: 'Summary', evidence: 'Evidence', hypotheses: 'Hypotheses', gaps: 'Unconfirmed',
  recommendation: 'Recommendation', decisions: 'Decisions needing you', verified: 'Verified identity',
  checking: 'Checking access', running: 'Investigating', waiting_approval: 'Awaiting approval',
  waiting_auth: 'Authentication required', connecting: 'Reconnecting', completed: 'Completed',
  interrupted: 'Interrupted', failed: 'Failed', needs_attention: 'Needs review', cancelled: 'Cancelled',
}
const attention = new Set(['waiting_approval', 'waiting_auth', 'needs_attention', 'failed', 'interrupted'])
const active = new Set(['checking', 'running', 'connecting'])
const styles = `
.investigations{display:flex;flex-direction:column;min-height:0;height:100%;color:var(--text)}
.investigations .content{padding:0 24px 32px;overflow:auto;flex:1;min-height:0}
.investigations .wrap{max-width:1120px;margin:auto;display:grid;gap:20px}
.investigations .row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.investigations .between{justify-content:space-between}.investigations .muted{color:var(--muted);font-size:13px}
.investigations h2{font-size:17px;font-weight:600;margin:0 0 12px}.investigations h3{font-size:14px;margin:0 0 8px}
.investigations .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}
.investigations label{display:grid;gap:6px;font-size:13px}.investigations select,.investigations textarea{border:1px solid var(--border);border-radius:8px;background:var(--bg);color:var(--text);padding:10px;font:inherit;width:100%}
.investigations textarea{resize:vertical;min-height:96px}.investigations .stack{display:grid;gap:14px}
.investigations .run{width:100%;text-align:left;display:flex;align-items:flex-start;justify-content:space-between;gap:18px;padding:16px;height:auto;white-space:normal}
.investigations .run strong{display:block;font-weight:500;margin:5px 0}.investigations .prose{white-space:pre-wrap;overflow-wrap:anywhere;font-size:14px;line-height:1.65}
.investigations .chat{height:650px;min-height:420px;border:1px solid var(--border);border-radius:12px;overflow:hidden;display:flex;flex-direction:column}
.investigations pre{white-space:pre-wrap;overflow-wrap:anywhere;padding:12px;background:var(--bg);border-radius:8px;font-size:12px}
.investigations details summary{cursor:pointer;font-weight:500;padding:8px 0}.investigations .activity{border-left:2px solid var(--border);padding-left:14px;margin:12px 0}
@media(max-width:600px){.investigations .content{padding:0 12px 20px}.investigations .grid{grid-template-columns:1fr}.investigations .chat{height:70vh}}
`

export default function Investigations() {
  const api = useAppApi()
  const client = useQueryClient()
  const setBadge = useNavBadge()
  const locale = document.documentElement.lang || navigator.language || 'en'
  const t = locale.startsWith('pt') ? pt : en
  const label = (key: string) => t[key as keyof typeof t] || key
  const [selected, setSelected] = useState(new URLSearchParams(window.location.search).get('run') || '')
  const [settings, setSettings] = useState(false)
  const [draft, setDraft] = useState<Service>({ ...emptyService })
  const [serviceId, setServiceId] = useState('')
  const [filter, setFilter] = useState('')
  const [question, setQuestion] = useState('')
  const index = useQuery({ queryKey, queryFn: () => api.get<Index>(endpoint), refetchInterval: 3000 })
  const runs = index.data?.runs || []
  const services = index.data?.services || []
  const run = runs.find(row => row.id === selected)
  React.useEffect(() => setBadge(runs.filter(row => attention.has(row.status)).length), [index.data, setBadge])
  const chooseRun = (id: string) => {
    setSelected(id)
    const url = new URL(window.location.href)
    if (id) url.searchParams.set('run', id)
    else url.searchParams.delete('run')
    window.history.replaceState(null, '', url)
  }
  const action = useMutation({
    mutationFn: (body: Record<string, unknown>) => api.post<Run & Service>(endpoint, body),
    onSuccess: (data, variables) => {
      if (variables.action === 'start') { chooseRun(data.id); setQuestion('') }
      if (variables.action === 'save_service') { setDraft(data); setServiceId(data.id); setSettings(false) }
      void client.invalidateQueries({ queryKey })
    },
  })
  const opened = useQuery({
    queryKey: ['service-investigations', 'open', selected],
    queryFn: () => api.post<Run>(endpoint, { action: 'open', id: selected }),
    enabled: Boolean(run?.slot_key && !['checking', 'connecting'].includes(run.status)),
    retry: false,
  })
  const error = index.error || action.error || opened.error
  const date = (value: number) => new Intl.DateTimeFormat(locale, { dateStyle: 'short', timeStyle: 'short' }).format(value * 1000)
  const status = (row: Run) => <Badge variant={attention.has(row.status) ? 'warn' : row.status === 'completed' ? 'ok' : 'muted'}>{label(row.status)}</Badge>
  return <div className="investigations">
    <style>{styles}</style>
    <PageHeader title={t.title} subtitle={t.subtitle} />
    <div className="content"><div className="wrap">
      <div className="row between">
        <div className="row">
          {(selected || settings) && <Btn onClick={() => { chooseRun(''); setSettings(false) }}><ArrowLeft size={15} /> {t.back}</Btn>}
          <span className="muted">{t.local}</span>
        </div>
        <Btn onClick={() => { setSettings(!settings); chooseRun('') }}><Settings2 size={15} /> {t.services}</Btn>
      </div>
      {/* No hand-off: service settings and the investigation question may be unsaved. */}
      <ErrorNotice message={error instanceof Error ? error.message : null} />
      {index.isPending ? <p aria-live="polite">{t.loading}</p> : settings ? <Card>
        <div className="stack"><h2>{t.services}</h2><p className="muted">{t.access}</p>
          <div className="row"><select aria-label={t.services} value={draft.id || ''} onChange={e => setDraft({ ...(services.find(s => s.id === e.target.value) || emptyService) })}>
            <option value="">{t.add}</option>{services.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select></div>
          <form className="stack" onSubmit={e => { e.preventDefault(); action.mutate({ action: 'save_service', service: draft }) }}>
            <div className="grid">{Object.keys(emptyService).map(key => <label key={key}>{label(key)}
              {['instructions', 'log_sources', 'database'].includes(key)
                ? <textarea value={draft[key]} onChange={e => setDraft({ ...draft, [key]: e.target.value })} />
                : <Input required={key === 'name'} value={draft[key]} onChange={e => setDraft({ ...draft, [key]: e.target.value })} />}
            </label>)}</div>
            <div><Btn primary disabled={action.isPending} type="submit">{t.save}</Btn></div>
          </form>
        </div>
      </Card> : run ? <>
        <div className="row between"><div><span className="muted">{run.service.name} · {date(run.created_at)}</span><h2>{run.question}</h2></div>{status(run)}</div>
        <div className="row">
          {!active.has(run.status) && run.status !== 'waiting_approval' && <Btn disabled={action.isPending} onClick={() => action.mutate({ action: 'resume', id: run.id })}><Play size={14} /> {t.resume}</Btn>}
          {run.status === 'waiting_auth' && run.service.aws_profile && <Btn primary disabled={action.isPending} onClick={() => action.mutate({ action: 'reconnect', id: run.id })}><KeyRound size={14} /> {t.reconnect}</Btn>}
          {(active.has(run.status) || run.status === 'waiting_approval') && <Btn danger disabled={action.isPending} onClick={() => action.mutate({ action: 'cancel', id: run.id })}><Square size={14} /> {t.cancel}</Btn>}
        </div>
        {run.auth_message && <Card><p>{t.auth}</p><pre>{run.auth_message}</pre></Card>}
        <Card><h2>{t.findings}</h2>{!Object.keys(run.report).length ? <p className="muted">{t.noFindings}</p> : <div className="stack">
          {['summary', 'recommendation', 'decisions', 'evidence', 'hypotheses', 'gaps'].map(key => run.report[key] && <section key={key}><h3>{label(key)}</h3><div className="prose">{run.report[key]}</div></section>)}
        </div>}</Card>
        <details><summary>{t.verified}</summary><pre>{JSON.stringify(run.identity, null, 2)}</pre></details>
        <details open={attention.has(run.status)}><summary>{t.timeline}</summary>{run.timeline.map((event, i) => <div className="activity" key={i}><span className="muted">{date(event.at)}</span><div className="prose">{event.text}</div></div>)}</details>
        <section><h2>{t.chat}</h2>{(run.chat_ready || opened.data?.chat_ready) && <div className="chat"><ChatPanel key={run.slot_key} slotKey={run.slot_key} conversationOnly /></div>}</section>
      </> : <>
        <Card><h2>{t.newRun}</h2>{services.length ? <form className="stack" onSubmit={e => { e.preventDefault(); action.mutate({ action: 'start', service_id: serviceId || services[0].id, question }) }}>
          <label>{t.choose}<select value={serviceId || services[0].id} onChange={e => setServiceId(e.target.value)}>{services.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
          <label>{t.question}<textarea required maxLength={16000} value={question} placeholder={t.placeholder} onChange={e => setQuestion(e.target.value)} /></label>
          <div><Btn primary type="submit" disabled={action.isPending || !question.trim()}><Search size={15} /> {t.start}</Btn></div>
        </form> : <><p className="muted">{t.setup}</p><Btn primary onClick={() => setSettings(true)}>{t.add}</Btn></>}</Card>
        {runs.length > 0 && <select aria-label={t.services} value={filter} onChange={e => setFilter(e.target.value)}><option value="">{t.all}</option>{services.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select>}
        {(['attention', 'active', 'history'] as const).map(group => {
          const items = runs.filter(r => (!filter || r.service.id === filter) && (group === 'attention' ? attention.has(r.status) : group === 'active' ? active.has(r.status) : !attention.has(r.status) && !active.has(r.status)))
          return items.length > 0 && <section key={group}><h2>{t[group]} · {items.length}</h2><div className="stack">{items.map(row => <Btn className="run" key={row.id} onClick={() => chooseRun(row.id)}><span><span className="muted">{row.service.name}</span><strong>{row.question}</strong><span className="muted">{date(row.updated_at)}</span></span>{status(row)}</Btn>)}</div></section>
        })}
      </>}
      <p className="muted">{t.policy}</p>
    </div></div>
  </div>
}
