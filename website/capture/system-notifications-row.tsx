/**
 * Isolated capture entry for Settings › Notifications › **System notifications**
 * — the row whose `host-managed` state this PR adds.
 *
 * WHY ISOLATED: the rest of `NotificationsPanel` fetches notification sources
 * against a live gateway, so mounting the whole page here would put a red
 * "couldn't load sources" banner next to the thing being photographed. This
 * mounts the REAL `SystemNotificationsRow` — not a re-composition of its markup
 * — because the evidence is precisely which permission state shows which copy,
 * and a hand-copied fixture could drift from that mapping silently.
 *
 * WHY ONE STATE PER PAGE LOAD: `Notification.permission` is a read-only
 * platform value, so a document can only be stubbed to one verdict. Each state
 * is therefore its own navigation (`?permission=`), and the capture script
 * screenshots them in turn.
 *
 * WHY AN IFRAME FOR host-managed: that state is not a value anything sets. It is
 * derived by `isEmbeddedPane()` from `window.self !== window.top`, so the only
 * honest way to photograph it is to BE in a frame. `?embed=1` renders a wrapper
 * whose iframe loads this same page, reaching the state the way a real
 * remote-instance pane does. Stubbing the hook would photograph the stub — and
 * note the inner frame is stubbed to `denied`, which is what Electron really
 * pins a pane to, so the frame also shows the row NOT saying "blocked".
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6842 --strictPort    # in another shell
 *   node scripts/capture-system-notifications-row.mjs http://127.0.0.1:6842 ../temp-screenshots/system-notifications-row
 */
import { createRoot } from 'react-dom/client'

// Initialise i18next exactly as main.tsx does: importing the module only
// DEFINES initI18n, and without calling it every label renders blank.
import { initI18n } from '../src/i18n'
import { SettingsSection, SettingsCard } from '../src/components/settings'
import { SystemNotificationsRow } from '../src/pages/settings/NotificationsPanel'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const permission = params.get('permission') || 'default'
const embed = params.get('embed') === '1'

// Stub the platform value before the row reads it. A headless browser grants
// this page nothing, so without the stub every state would render as `default`.
Object.defineProperty(window, 'Notification', {
  configurable: true,
  writable: true,
  value: Object.assign(
    function StubNotification() {
      /* never constructed here — the row only reads `.permission` */
    },
    { permission, requestPermission: async () => permission },
  ),
})

initI18n('en')
document.documentElement.setAttribute('data-theme', theme)

function Row() {
  return (
    <div className="bg-bg p-6" style={{ width: 560 }}>
      <SettingsSection title="Desktop alerts">
        <SettingsCard>
          <SystemNotificationsRow />
        </SettingsCard>
      </SettingsSection>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  embed ? (
    // The wrapper contributes no chrome of its own: the frame IS the pane, and
    // everything visible inside it is the row as an embedded instance renders it.
    // Sized inline rather than with Tailwind arbitrary values — `capture/` is not
    // in the Tailwind content globs, so `h-[200px]` here generates no CSS and the
    // frame silently falls back to the 150px default, clipping the card.
    <iframe
      title="remote-instance pane"
      src={`${location.pathname}?theme=${theme}&permission=denied`}
      style={{ width: 560, height: 200, border: 0, display: 'block' }}
    />
  ) : (
    <Row />
  ),
)
