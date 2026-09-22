/**
 * Fires a browser `Notification` whenever a new unacked notification lands in
 * the Redux store. Used by `App.tsx` to surface macOS notification-center
 * toasts.
 *
 * A muted channel's notes arrive with `silenced: true` and `priority:
 * "passive"` (`ChannelSettings.apply()`, `kiro_crew/notifications/settings.py`)
 * -- the backend's stated contract is that every attention surface (badge
 * count, sound, native banner, feed styling) skips them. The in-app feed
 * (`NotificationFeed.tsx`) already reads `silenced` for its styling; this hook
 * must exclude the same notes from BOTH its unread count and its
 * latest-note pick, or a muted note still increments the count and fires the
 * native banner even though the in-app row correctly shows "muted".
 *
 * A shared hook so the regression tests in
 * `integration/AppNotification.integration.test.tsx` exercise *this* code —
 * if the effect regresses, tests and production break together.
 */
import { useEffect, useRef } from 'react'
import { useAppSelector } from '../store'
// Shared with the tab-title attention count rather than kept file-local: two
// attention surfaces that spell this rule separately can drift apart, and the
// backend states it once for all of them.
import { isSilencedNote } from '../store/notificationsSlice'
// A note's `body` is MARKDOWN by contract -- the detail panel renders it as
// markdown and producers write `**name** -- description`, `_italics_`,
// `**Triggers:**` (see `_pending_skill_notification`, dashboard/server.py).
// An OS notification body is plain text: macOS Notification Center paints the
// asterisks and underscores literally. Reuse the same flattener the in-app feed
// row uses so both previews read identically.
import { stripMd } from '../components/notifications/notifMeta'
import { i18nT } from '../i18n/t'
import { requestNativeNotificationPermission, showNativeNotification } from '../lib/nativeNotify'

export function useNativeNotification(botName: string, avatar: string) {
  const notifCount = useAppSelector(
    (s) => s.notifications.items.filter((n) => !n.acked && !isSilencedNote(n)).length,
  )
  const latestNotif = useAppSelector((s) => {
    const unacked = s.notifications.items.filter((n) => !n.acked && !isSilencedNote(n))
    return unacked.length > 0 ? unacked[unacked.length - 1] : null
  })

  const prev = useRef(0)
  useEffect(() => {
    if (notifCount > prev.current) {
      const delta = notifCount - prev.current
      // stripMd only on the note's own markdown body; the generic fallback and
      // the title are plain text already (the feed renders the title verbatim,
      // never as markdown).
      const noteBody = latestNotif?.body ? stripMd(latestNotif.body) : ''
      // The fallback goes through the catalog's pluralized count key rather than
      // choosing a form in JS: Italian and Russian have categories English does
      // not, so a `delta > 1` ternary is wrong in them however it is worded.
      const body = noteBody || i18nT('app.notification_count', { count: delta })
      // `showNativeNotification` owns the permission check, the try/catch, the
      // silent default (WebAudio is the single source of notification sound),
      // and the embedded-pane relay — in a remote-instance pane the
      // page-context constructor is refused by Electron's main-frame-only
      // gate, so this has to go through the parent. See lib/nativeNotify.ts.
      showNativeNotification({
        title: latestNotif?.title || botName,
        body,
        icon: avatar,
        tag:
          latestNotif?.approval_id ||
          latestNotif?.job_id ||
          latestNotif?.task_id ||
          'kirocrew-notif',
      })
      // Best-effort only: browsers refuse a prompt with no user gesture behind
      // it, and this fires from an effect. The two places that ask FROM a
      // gesture are Settings › Notifications ("Allow system notifications",
      // `SystemNotificationsRow`) and the bell popover's hint row
      // (`NotificationPermissionHint`), both through
      // `useNotificationPermission().request`.
      requestNativeNotificationPermission()
    }
    prev.current = notifCount
  }, [notifCount, botName, avatar, latestNotif])
}
