import { Folder, GitBranch } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { SubagentActivity, SubagentRunContext } from '../../types'
import { isAwaitingSpawnApproval } from '../../store/chatSlice'
import { sanitizeLlmOutput } from '../../utils/sanitize'
import { i18nT } from '../../i18n/t'

export function subagentStatusLabel(agent?: SubagentActivity): string {
  if (!agent) return i18nT('pages.chat.subagentLocation.status_unknown')
  if (isAwaitingSpawnApproval(agent)) return i18nT('pages.chat.activityViewer.pending_approval')
  switch (agent.status) {
    case 'running':
    case 'tool': return i18nT('pages.chat.activityViewer.running')
    case 'pending': return i18nT('pages.chat.subagentRunCard.waiting')
    case 'done': return i18nT('pages.chat.activityViewer.complete')
    case 'stopped': return i18nT('pages.chat.activityViewer.stopped')
    case 'error': return i18nT('pages.chat.activityViewer.error')
  }
}

export default function SubagentLocation({ workspace, compact = false }: {
  workspace?: SubagentRunContext['workspace']
  compact?: boolean
}) {
  const { t } = useTranslation()
  const cwd = sanitizeLlmOutput(workspace?.cwd || '')
  if (!cwd) return <div className="text-[11px] text-muted">{t('pages.chat.subagentLocation.unavailable')}</div>
  const branch = workspace?.branch
    ? sanitizeLlmOutput(workspace.branch)
    : workspace?.head ? t('pages.chat.subagentLocation.detached', { head: sanitizeLlmOutput(workspace.head) }) : ''
  const worktree = sanitizeLlmOutput(workspace?.worktree || '')
  return (
    <div className="min-w-0 text-[11px] text-muted space-y-0.5">
      <div>{t('pages.chat.subagentLocation.at_start')}</div>
      <div className="flex items-start gap-1 min-w-0">
        <Folder size={12} className="shrink-0 mt-0.5" aria-hidden />
        <span className={compact ? 'font-mono truncate min-w-0' : 'font-mono break-all min-w-0'} title={cwd}>{cwd}</span>
      </div>
      {branch && <div className="flex items-start gap-1 min-w-0">
        <GitBranch size={12} className="shrink-0 mt-0.5" aria-hidden />
        <span className="font-mono break-all min-w-0">{branch}</span>
      </div>}
      {!compact && worktree && <div className="break-all">
        {t('pages.chat.subagentLocation.worktree', { path: worktree })}
      </div>}
    </div>
  )
}
