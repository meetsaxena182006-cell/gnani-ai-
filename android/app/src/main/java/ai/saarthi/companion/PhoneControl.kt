package ai.saarthi.companion

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.AudioManager
import android.net.Uri
import android.os.Build
import android.provider.ContactsContract
import android.telecom.TelecomManager
import androidx.core.content.ContextCompat

/** Hands-free phone control. Every action here is triggered by the driver's spoken "haan". */
object PhoneControl {
    private fun has(ctx: Context, perm: String) =
        ContextCompat.checkSelfPermission(ctx, perm) == PackageManager.PERMISSION_GRANTED

    fun dial(ctx: Context, phone: String) {
        val clean = phone.filter { it.isDigit() || it == '+' }
        val action = if (has(ctx, Manifest.permission.CALL_PHONE)) Intent.ACTION_CALL else Intent.ACTION_DIAL
        ctx.startActivity(Intent(action, Uri.parse("tel:$clean")).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        speaker(ctx, true)
    }

    @SuppressLint("MissingPermission")
    fun answer(ctx: Context) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && has(ctx, Manifest.permission.ANSWER_PHONE_CALLS)) {
            (ctx.getSystemService(Context.TELECOM_SERVICE) as TelecomManager).acceptRingingCall()
            speaker(ctx, true)
        }
    }

    @SuppressLint("MissingPermission")
    @Suppress("DEPRECATION")
    fun end(ctx: Context) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P && has(ctx, Manifest.permission.ANSWER_PHONE_CALLS)) {
            (ctx.getSystemService(Context.TELECOM_SERVICE) as TelecomManager).endCall()
        }
    }

    @Suppress("DEPRECATION")
    fun speaker(ctx: Context, on: Boolean) {
        val am = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
        am.isSpeakerphoneOn = on
    }

    fun contactName(ctx: Context, number: String?): String? {
        if (number.isNullOrBlank() || !has(ctx, Manifest.permission.READ_CONTACTS)) return null
        val uri = Uri.withAppendedPath(ContactsContract.PhoneLookup.CONTENT_FILTER_URI, Uri.encode(number))
        return try {
            ctx.contentResolver.query(uri, arrayOf(ContactsContract.PhoneLookup.DISPLAY_NAME), null, null, null)?.use {
                if (it.moveToFirst()) it.getString(0) else null
            }
        } catch (e: Exception) { null }
    }

    fun openUrl(ctx: Context, url: String) {
        if (!url.startsWith("https://")) return
        ctx.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
    }
}
