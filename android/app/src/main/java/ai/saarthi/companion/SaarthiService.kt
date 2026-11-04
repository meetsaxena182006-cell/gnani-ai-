package ai.saarthi.companion

import android.Manifest
import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Build
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.telephony.PhoneStateListener
import android.telephony.TelephonyManager
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import org.json.JSONObject

/**
 * Keeps Saarthi alive with the screen off (foreground service: microphone + location), sends GPS to the server
 * every ~15 s (no GPS hardware needed), and announces incoming calls to the companion so the driver can answer
 * by voice ("Sharma ji ka call aa raha hai, uthaun?").
 */
class SaarthiService : Service() {
    private var wake: PowerManager.WakeLock? = null
    private var phoneListener: PhoneStateListener? = null

    private val locationListener = LocationListener { loc: Location ->
        Net.post(this, "/api/location", JSONObject()
            .put("lat", loc.latitude).put("lon", loc.longitude)
            .put("speed", if (loc.hasSpeed()) loc.speed.toDouble() else JSONObject.NULL)
            .put("acc", loc.accuracy.toDouble()))
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        startAsForeground()
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        wake = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "saarthi:companion").apply { acquire() }
        startLocation()
        listenCalls()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    override fun onDestroy() {
        (getSystemService(Context.LOCATION_SERVICE) as LocationManager).removeUpdates(locationListener)
        @Suppress("DEPRECATION")
        phoneListener?.let { (getSystemService(Context.TELEPHONY_SERVICE) as TelephonyManager).listen(it, PhoneStateListener.LISTEN_NONE) }
        wake?.let { if (it.isHeld) it.release() }
        super.onDestroy()
    }

    private fun startAsForeground() {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            nm.createNotificationChannel(NotificationChannel(CHANNEL, "Saarthi companion", NotificationManager.IMPORTANCE_LOW))
        }
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val n: Notification = NotificationCompat.Builder(this, CHANNEL)
            .setContentTitle("Saarthi sun raha hai 🎧")
            .setContentText("Aapka co-driver saath hai. Bas boliye: \"Saarthi…\"")
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setOngoing(true)
            .setContentIntent(open)
            .build()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            var types = ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
            if (granted(Manifest.permission.ACCESS_FINE_LOCATION)) types = types or ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION
            startForeground(NOTIF_ID, n, types)
        } else {
            startForeground(NOTIF_ID, n)
        }
    }

    private fun granted(p: String) = ContextCompat.checkSelfPermission(this, p) == PackageManager.PERMISSION_GRANTED

    @SuppressLint("MissingPermission")
    private fun startLocation() {
        if (!granted(Manifest.permission.ACCESS_FINE_LOCATION)) return
        val lm = getSystemService(Context.LOCATION_SERVICE) as LocationManager
        for (provider in listOf(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER)) {
            if (lm.isProviderEnabled(provider)) {
                lm.requestLocationUpdates(provider, 15_000L, 25f, locationListener, Looper.getMainLooper())
            }
        }
    }

    @Suppress("DEPRECATION")
    private fun listenCalls() {
        if (!granted(Manifest.permission.READ_PHONE_STATE)) return
        val tm = getSystemService(Context.TELEPHONY_SERVICE) as TelephonyManager
        phoneListener = object : PhoneStateListener() {
            override fun onCallStateChanged(state: Int, number: String?) {
                when (state) {
                    TelephonyManager.CALL_STATE_RINGING -> {
                        val name = PhoneControl.contactName(this@SaarthiService, number)
                        Bridge.toPage(JSONObject().put("type", "incoming_call")
                            .put("name", name ?: JSONObject.NULL).put("known", name != null))
                    }
                    TelephonyManager.CALL_STATE_OFFHOOK -> Bridge.toPage(JSONObject().put("type", "call_active"))
                    TelephonyManager.CALL_STATE_IDLE -> {
                        PhoneControl.speaker(this@SaarthiService, false)
                        Bridge.toPage(JSONObject().put("type", "call_ended"))
                    }
                }
            }
        }
        tm.listen(phoneListener, PhoneStateListener.LISTEN_CALL_STATE)
    }

    companion object {
        private const val CHANNEL = "saarthi_companion"
        private const val NOTIF_ID = 42

        fun start(ctx: Context) {
            ContextCompat.startForegroundService(ctx, Intent(ctx, SaarthiService::class.java))
        }
    }
}
