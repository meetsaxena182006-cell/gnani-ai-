package ai.saarthi.companion

import android.os.Handler
import android.os.Looper
import android.webkit.WebView
import org.json.JSONObject
import java.lang.ref.WeakReference

/** Native events (incoming call, message, ...) -> companion page -> Saarthi server over the WebRTC data channel. */
object Bridge {
    private var web: WeakReference<WebView>? = null
    private val main = Handler(Looper.getMainLooper())
    private val pending = ArrayDeque<String>()

    fun attach(view: WebView) {
        web = WeakReference(view)
        while (pending.isNotEmpty()) deliver(pending.removeFirst())
    }

    fun toPage(event: JSONObject) {
        val js = event.toString()
        if (web?.get() == null) {
            if (pending.size > 20) pending.removeFirst()
            pending.addLast(js)
        } else deliver(js)
    }

    private fun deliver(json: String) {
        main.post {
            web?.get()?.evaluateJavascript("window.saarthiNative && window.saarthiNative($json)", null)
        }
    }
}
