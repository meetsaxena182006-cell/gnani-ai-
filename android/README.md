# Saarthi driver app (Android)

The driver's companion as a real phone app: it keeps running with the **screen off** and the phone in the dashboard holder.

| Capability | How |
|---|---|
| Always-on voice companion | Foreground service (microphone + location) + the companion page (WebRTC to the Saarthi server, Gnani Prisma/Timbre) |
| GPS without hardware | Native location every ~15 s → `POST /api/location` |
| Calls by voice | Incoming call announced by name ("Sharma ji ka call aa raha hai, uthaun?") → *haan* = answered on speaker, *nahi* = rejected. Dial mechanic/112 only after the driver's yes |
| Messages read aloud | WhatsApp / Telegram / SMS from contacts (driver enables notification access) |
| FASTag tolls | FASTag debit SMS → `POST /api/fastag/sms` → matched to the GPS toll crossing |
| Music | Songs open straight in YouTube Music |
| After restart | One tap on the "Saarthi shuru karein" notification (Android does not allow background mic start at boot) |

**Get the APK:** every push builds it on GitHub Actions (Actions → "Android APK" → artifact `saarthi-companion-apk`). Install it on the phone (allow "install unknown apps").
**Setup:** open the app, paste the companion link the owner got from the seeder (`https://…/driver?v=…&k=…`), or open `saarthi://setup?url=<link>`. Grant the permissions once.

Build locally: Android Studio → open `android/` → Run. Requires JDK 17.
Play Store note: SMS and call-log permissions are restricted on Google Play; this build is for direct install (pilot). A Play Store build would read FASTag data via statement upload instead.
