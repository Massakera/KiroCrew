/**
 * CrewChatPage — point the shared chat surface at a remote crew.
 *
 * This file is deliberately thin. All of the chat UX lives in `ChatSurface`,
 * which knows nothing about "remote": it issues LOGICAL paths only. The single
 * thing this wrapper contributes is the answer to "which gateway answers them" —
 * `AppApiProvider`'s `basePath` prefixes every request with
 * `/api/instances/<id>/proxy`, so the transcript, the slot list and the turn all
 * live on the crew and the hub keeps no copy. That is what makes the
 * remote-content-in-local-memory leak structurally impossible rather than
 * policy-dependent.
 *
 * Because the surface is origin-agnostic, a local page can mount the same
 * component with no `basePath` and get identical behaviour against the local
 * gateway — which is the point: one chat view, origin as data.
 *
 * The declared API scope stays the narrow pair the backend allowlist forwards
 * (`api/chat`, `api/stream`). `basePath` does not widen it — `createScopedApi`
 * checks the LOGICAL path before the prefix is applied — so this page cannot
 * reach the peer's control plane even by accident.
 */
import { Navigate, useNavigate, useParams } from 'react-router-dom'
import { AppApiProvider } from '../app-sdk'
import ChatSurface, { Unreachable } from './chat/ChatSurface'
import { usePreviewFlag } from '../hooks/usePreviewFlag'
import { PREVIEW_REMOTE_CREW_CHAT } from '../utils/previewFlags'

export default function CrewChatPage() {
  const { crewId, sessionId } = useParams<{ crewId: string; sessionId?: string }>()
  const navigate = useNavigate()
  // Hard preview gate: remote-crew chat is unreleased, so unless an operator has
  // opted in from Developer → Feature Previews the page does not resolve at all —
  // not even via a bookmarked URL. Redirect (replace) rather than render an empty
  // shell so the address bar reflects that the page is not here. Runs before the
  // `!crewId` guard and before any conditional so hook order stays fixed.
  const crewChatOn = usePreviewFlag(PREVIEW_REMOTE_CREW_CHAT)
  if (!crewChatOn) {
    return <Navigate to="/chat" replace />
  }
  // A missing :crewId cannot happen via the route, but an empty basePath would
  // silently address the LOCAL gateway — the one failure this design refuses to
  // make possible — so fail closed instead of proxying to ourselves.
  if (!crewId) {
    return <Unreachable label="" detail="No crew was named in the URL." onRetry={null} />
  }
  const basePath = `/api/instances/${encodeURIComponent(crewId)}/proxy`
  return (
    <AppApiProvider
      appName="remote-crew-chat"
      allowedApiPaths={['/api/chat', '/api/stream']}
      allowedEvents={[]}
      basePath={basePath}
      subscribeFn={() => () => {}}
      navigateFn={() => {}}
      notifyFn={() => {}}
    >
      <ChatSurface
        origin={{ kind: 'crew', label: crewId }}
        streamBase={basePath}
        slotKey={sessionId}
        onOpenSlot={key => navigate(`/crew/${encodeURIComponent(crewId)}/chat/${encodeURIComponent(key)}`)}
      />
    </AppApiProvider>
  )
}
