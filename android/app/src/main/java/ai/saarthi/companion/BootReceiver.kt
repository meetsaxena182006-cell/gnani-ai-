package ai.saarthi.companion

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat

/** After a phone restart Android does not allow starting the microphone in the background, so we ask for one tap. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED || Config.companionUrl(ctx) == null) return
        val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            nm.createNotificationChannel(NotificationChannel("saarthi_boot", "Saarthi start", NotificationManager.IMPORTANCE_HIGH))
        }
        val open = PendingIntent.getActivity(ctx, 1, Intent(ctx, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        nm.notify(7, NotificationCompat.Builder(ctx, "saarthi_boot")
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentTitle("Saarthi shuru karein")
            .setContentText("Ek baar dabaiye — phir phone chhoone ki zaroorat nahi")
            .setContentIntent(open).setAutoCancel(true).build())
    }
}
