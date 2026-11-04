package ai.saarthi.companion

import android.app.Notification
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import org.json.JSONObject

/** With the driver's permission, messages from family/office (WhatsApp, Telegram, SMS) are read aloud. */
class NotificationReader : NotificationListenerService() {
    private val apps = mapOf(
        "com.whatsapp" to "WhatsApp", "com.whatsapp.w4b" to "WhatsApp", "org.telegram.messenger" to "Telegram",
        "com.google.android.apps.messaging" to "SMS", "com.samsung.android.messaging" to "SMS",
    )
    private val recent = ArrayDeque<String>()

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        val app = apps[sbn.packageName] ?: return
        val n = sbn.notification ?: return
        if ((n.flags and Notification.FLAG_GROUP_SUMMARY) != 0 || (n.flags and Notification.FLAG_ONGOING_EVENT) != 0) return
        val title = n.extras.getCharSequence(Notification.EXTRA_TITLE)?.toString() ?: return
        val text = n.extras.getCharSequence(Notification.EXTRA_TEXT)?.toString() ?: return
        val key = "$title|$text"
        if (key in recent) return            // apps re-post the same message
        recent.addLast(key); if (recent.size > 30) recent.removeFirst()
        if (app == "SMS" && (text.contains("FASTag", true) || text.contains("OTP", true))) return  // not read aloud
        Bridge.toPage(JSONObject().put("type", "notification").put("app", app).put("title", title).put("text", text.take(300)))
    }
}
