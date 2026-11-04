package ai.saarthi.companion

import android.content.Context
import android.util.Log
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors

/** Small fire-and-forget JSON POSTs to the Saarthi server (location, FASTag SMS). */
object Net {
    private val pool = Executors.newSingleThreadExecutor()

    fun post(ctx: Context, path: String, body: JSONObject) {
        val base = Config.base(ctx) ?: return
        val auth = Config.auth(ctx) ?: return
        pool.execute {
            try {
                val conn = URL("$base$path?$auth").openConnection() as HttpURLConnection
                conn.requestMethod = "POST"
                conn.connectTimeout = 10_000
                conn.readTimeout = 10_000
                conn.doOutput = true
                conn.setRequestProperty("Content-Type", "application/json")
                conn.outputStream.use { it.write(body.toString().toByteArray()) }
                conn.responseCode
                conn.disconnect()
            } catch (e: Exception) {
                Log.w("Saarthi", "POST $path failed: ${e.message}")
            }
        }
    }
}
