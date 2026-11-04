package ai.saarthi.companion

import android.Manifest
import android.annotation.SuppressLint
import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.view.Gravity
import android.view.ViewGroup
import android.webkit.GeolocationPermissions
import android.webkit.JavascriptInterface
import android.webkit.PermissionRequest
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat

/**
 * Saarthi driver app. One-time setup (owner's link + permissions), then the companion runs hands-free:
 * the screen can be off, the phone stays in the dashboard holder.
 */
class MainActivity : AppCompatActivity() {
    private var web: WebView? = null

    private val perms: Array<String> by lazy {
        val p = mutableListOf(
            Manifest.permission.RECORD_AUDIO, Manifest.permission.ACCESS_FINE_LOCATION,
            Manifest.permission.ACCESS_COARSE_LOCATION, Manifest.permission.READ_PHONE_STATE,
            Manifest.permission.READ_CALL_LOG, Manifest.permission.CALL_PHONE, Manifest.permission.READ_CONTACTS,
            Manifest.permission.RECEIVE_SMS,
        )
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) p += Manifest.permission.ANSWER_PHONE_CALLS
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) p += Manifest.permission.POST_NOTIFICATIONS
        p.toTypedArray()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        handleSetupLink(intent)
        if (Config.companionUrl(this) == null) showSetup() else askPermissionsThenStart()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        if (handleSetupLink(intent)) recreate()
    }

    /** saarthi://setup?url=https://server/driver?v=..&k=.. */
    private fun handleSetupLink(i: Intent?): Boolean {
        val data = i?.data ?: return false
        if (data.scheme != "saarthi") return false
        val url = data.getQueryParameter("url") ?: return false
        return Config.save(this, url).also { if (!it) Toast.makeText(this, "Link galat hai", Toast.LENGTH_LONG).show() }
    }

    private fun showSetup() {
        val pad = (24 * resources.displayMetrics.density).toInt()
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL; gravity = Gravity.CENTER; setPadding(pad, pad, pad, pad)
            setBackgroundColor(Color.parseColor("#0B1F20"))
        }
        val title = TextView(this).apply {
            text = "सारथी\nमालिक से मिला लिंक यहाँ डालिए"; textSize = 22f; setTextColor(Color.WHITE); gravity = Gravity.CENTER
        }
        val input = EditText(this).apply {
            hint = "https://…/driver?v=…&k=…"; setTextColor(Color.WHITE); setHintTextColor(Color.GRAY)
        }
        val go = Button(this).apply {
            text = "शुरू करें"
            setOnClickListener {
                if (Config.save(this@MainActivity, input.text.toString())) askPermissionsThenStart()
                else Toast.makeText(this@MainActivity, "Link galat hai", Toast.LENGTH_LONG).show()
            }
        }
        box.addView(title); box.addView(input, ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT); box.addView(go)
        setContentView(box)
    }

    private fun askPermissionsThenStart() {
        val missing = perms.filter { ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED }
        if (missing.isEmpty()) start() else ActivityCompat.requestPermissions(this, missing.toTypedArray(), 1)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, results: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, results)
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) start()
        else Toast.makeText(this, "Saarthi ko mic ki permission chahiye", Toast.LENGTH_LONG).show()
    }

    private fun start() {
        SaarthiService.start(this)
        askBatteryExemption()
        maybeAskNotificationAccess()
        showCompanion()
    }

    @SuppressLint("BatteryLife")
    private fun askBatteryExemption() {
        val pm = getSystemService(POWER_SERVICE) as PowerManager
        if (!pm.isIgnoringBatteryOptimizations(packageName)) {
            try {
                startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
            } catch (e: Exception) { }
        }
    }

    private fun maybeAskNotificationAccess() {
        val enabled = Settings.Secure.getString(contentResolver, "enabled_notification_listeners") ?: ""
        val me = ComponentName(this, NotificationReader::class.java).flattenToString()
        val prefs = getSharedPreferences("saarthi", MODE_PRIVATE)
        if (!enabled.contains(me) && !prefs.getBoolean("asked_notif", false)) {
            prefs.edit().putBoolean("asked_notif", true).apply()
            Toast.makeText(this, "Messages padhkar sunane ke liye 'Saarthi' ko allow kariye", Toast.LENGTH_LONG).show()
            try { startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)) } catch (e: Exception) { }
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun showCompanion() {
        if (web != null) return
        val view = WebView(this)
        view.settings.javaScriptEnabled = true
        view.settings.domStorageEnabled = true
        view.settings.mediaPlaybackRequiresUserGesture = false
        view.settings.setGeolocationEnabled(true)
        view.webViewClient = WebViewClient()
        view.webChromeClient = object : WebChromeClient() {
            override fun onPermissionRequest(request: PermissionRequest) {
                runOnUiThread { request.grant(request.resources) }   // mic for the companion page
            }
            override fun onGeolocationPermissionsShowPrompt(origin: String, callback: GeolocationPermissions.Callback) {
                callback.invoke(origin, true, false)
            }
        }
        view.addJavascriptInterface(JsBridge(), "SaarthiAndroid")
        setContentView(view)
        web = view
        Bridge.attach(view)
        view.loadUrl(Config.companionUrl(this)!!)
    }

    // Keep the companion running when the screen turns off: do NOT pause the WebView in onPause.
    override fun onPause() { super.onPause(); web?.resumeTimers() }

    override fun onDestroy() {
        web?.destroy(); web = null
        super.onDestroy()
    }

    /** Called from the companion page (window.SaarthiAndroid.*) after the driver says "haan". */
    inner class JsBridge {
        @JavascriptInterface fun isNative(): Boolean = true
        @JavascriptInterface fun dial(phone: String) = runOnUiThread { PhoneControl.dial(this@MainActivity, phone) }
        @JavascriptInterface fun answerCall() = runOnUiThread { PhoneControl.answer(this@MainActivity) }
        @JavascriptInterface fun endCall() = runOnUiThread { PhoneControl.end(this@MainActivity) }
        @JavascriptInterface fun openUrl(url: String) = runOnUiThread { PhoneControl.openUrl(this@MainActivity, url) }
        @JavascriptInterface fun resetLink() = runOnUiThread {
            getSharedPreferences("saarthi", MODE_PRIVATE).edit().clear().apply(); recreate()
        }
    }
}
