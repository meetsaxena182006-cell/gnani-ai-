package ai.saarthi.companion

import android.content.Context
import android.net.Uri

/** The companion link given by the owner: https://<server>/driver?v=<truck>&k=<key> */
object Config {
    private const val PREFS = "saarthi"
    private const val KEY_URL = "companion_url"

    fun companionUrl(ctx: Context): String? =
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY_URL, null)

    fun save(ctx: Context, url: String): Boolean {
        val u = Uri.parse(url.trim())
        val scheme = u.scheme ?: return false
        if ((scheme != "http" && scheme != "https") || u.getQueryParameter("v").isNullOrBlank() ||
            u.getQueryParameter("k").isNullOrBlank()) return false
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putString(KEY_URL, url.trim()).apply()
        return true
    }

    /** https://host:port */
    fun base(ctx: Context): String? = companionUrl(ctx)?.let {
        val u = Uri.parse(it)
        "${u.scheme}://${u.encodedAuthority}"
    }

    /** "v=..&k=.." for authenticated API calls */
    fun auth(ctx: Context): String? = companionUrl(ctx)?.let {
        val u = Uri.parse(it)
        "v=${Uri.encode(u.getQueryParameter("v"))}&k=${Uri.encode(u.getQueryParameter("k"))}"
    }
}
