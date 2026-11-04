package ai.saarthi.companion

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.provider.Telephony
import org.json.JSONObject

/** FASTag debit SMS -> server -> matched to the GPS toll crossing (actual toll replaces the estimate). */
class SmsReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        if (intent.action != Telephony.Sms.Intents.SMS_RECEIVED_ACTION) return
        val body = Telephony.Sms.Intents.getMessagesFromIntent(intent).joinToString("") { it.messageBody ?: "" }
        val isToll = body.contains("FASTag", ignoreCase = true) || body.contains("toll", ignoreCase = true)
        val isDebit = Regex("(?i)(debited|deducted|paid|Rs\\.?|INR|₹)").containsMatchIn(body)
        if (isToll && isDebit) Net.post(ctx, "/api/fastag/sms", JSONObject().put("text", body))
    }
}
