# Amy — Flutter Client (PersonalOS)

Mobile frontend for the **same Amy/Jarvis backend** that powers the web dashboard.
It does two things:

1. **Chat / voice with Amy** — talks to the existing `/ws` + `/api/query` endpoints.
2. **Photo capture** — take or pick a photo; the backend captions it, OCRs it,
   records date/time + GPS, and writes a note into your Obsidian vault under
   `08_Captures/`. Amy can then answer questions about your photos.

---

## 1. Run the backend (with the new capture endpoints)

From `_Amy/`:

```bash
pip install -r requirements.txt        # also: pip install python-multipart  (needed for upload)
python main.py --mode personal --host 0.0.0.0 --port 8848
```

> Use `--host 0.0.0.0` (not 127.0.0.1) so your phone can reach it over the network.

New endpoints added for mobile:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/captures` | Upload a photo (multipart `file` + optional `lat,lon,taken_at,note,tags`) |
| `GET`  | `/api/captures?limit=50` | List recent captures |
| `GET`  | `/api/captures/image?path=08_Captures/attachments/<file>` | Serve a stored image |

Captures are **disabled in public mode** and the caption/OCR step uses your existing `OPENAI_API_KEY`.

Quick test without the app:

```bash
curl -F "file=@some_photo.jpg" -F "note=test" http://localhost:8848/api/captures
# then ask Amy in the web app: "what photos did I take today?"
```

---

## 2. Run the app

**Easiest — one command (Windows):**

```powershell
cd flutter_app
powershell -ExecutionPolicy Bypass -File .\setup_mobile.ps1
flutter run
```

`setup_mobile.ps1` runs `flutter create .`, patches the Android manifest
(permissions + cleartext + share intent), runs `flutter pub get`, and installs
the backend's `python-multipart` dep.

**Or manually:**

```bash
cd flutter_app
flutter create .          # generates android/ ios/ etc. (keeps existing lib/)
flutter pub get
flutter run
```

Then open **Settings** (gear icon, top-right) and set the **Server URL**:

- Android emulator → `http://10.0.2.2:8848`
- Real phone on same Wi-Fi → `http://<your-PC-LAN-IP>:8848` (e.g. `http://192.168.1.20:8848`)
- On the go → expose the backend with Cloudflare Tunnel / Tailscale and use that URL
- Set the **token** only if you set `AMY_AUTH_TOKEN` on the backend.

---

## 3. Required permissions (add after `flutter create .`)

`flutter create` writes default platform files **without** camera/location/cleartext.
Add these or the camera, GPS, and `http://` LAN calls will fail.

### Android — `android/app/src/main/AndroidManifest.xml`

> The setup script does all of this automatically. Only needed if patching by hand.

Inside `<manifest>` (above `<application>`):

```xml
<uses-permission android:name="android.permission.INTERNET"/>
<uses-permission android:name="android.permission.CAMERA"/>
<uses-permission android:name="android.permission.ACCESS_FINE_LOCATION"/>
<uses-permission android:name="android.permission.ACCESS_COARSE_LOCATION"/>
<uses-permission android:name="android.permission.READ_MEDIA_IMAGES"/>
<uses-permission android:name="android.permission.READ_EXTERNAL_STORAGE"/>
<uses-permission android:name="android.permission.ACCESS_MEDIA_LOCATION"/>
```

On the `<application ...>` tag add (needed to call an `http://` backend):

```xml
android:usesCleartextTraffic="true"
```

Inside the launcher `<activity>` (so photos can be **shared** to Amy):

```xml
<intent-filter>
    <action android:name="android.intent.action.SEND"/>
    <category android:name="android.intent.category.DEFAULT"/>
    <data android:mimeType="image/*"/>
</intent-filter>
<intent-filter>
    <action android:name="android.intent.action.SEND_MULTIPLE"/>
    <category android:name="android.intent.category.DEFAULT"/>
    <data android:mimeType="image/*"/>
</intent-filter>
```

### iOS — `ios/Runner/Info.plist`

> The setup script adds these automatically (camera, photo, location usage strings
> + an `NSAppTransportSecurity` arbitrary-loads exception for `http://` backends).
> Shown here for reference / manual patching.

```xml
<key>NSCameraUsageDescription</key>
<string>Take photos to save into your PersonalOS vault.</string>
<key>NSPhotoLibraryUsageDescription</key>
<string>Pick or sync photos into your PersonalOS vault.</string>
<key>NSLocationWhenInUseUsageDescription</key>
<string>Tag captures with where they were taken.</string>
<key>NSAppTransportSecurity</key>
<dict>
    <key>NSAllowsArbitraryLoads</key>
    <true/>
</dict>
```

**iOS build requires a Mac with Xcode** — the project + `Info.plist` are generated on
Windows, but `flutter build ios` / `flutter run` on an iPhone only works from macOS.
**Share-to-Amy on iOS** additionally needs a *Share Extension* target added in Xcode
(see the `receive_sharing_intent` docs); camera, gallery sync, and chat work without it.

---

## Files

| File | Role |
|---|---|
| `lib/config.dart` | Server URL + token, persisted (shared_preferences) |
| `lib/api.dart` | REST client (stats, query, **captures upload/list/image**) |
| `lib/ws.dart` | WebSocket chat client (sends token first if set) |
| `lib/main.dart` | Chat + voice home screen; nav to capture / captures / settings |
| `lib/capture_screen.dart` | Camera/gallery + GPS → upload (mode B) |
| `lib/captures_screen.dart` | Browse stored captures |
| `lib/settings_screen.dart` | Backend connection settings |
| `lib/share_handler.dart` | Share-to-Amy from any app (mode C) |
| `lib/gallery_sync.dart` | Auto-ingest new gallery photos (mode A) |

Existing chat features are unchanged: text + push-to-talk voice, spoken replies using
the backend's redacted `voice_safe` text, live header stats.

---

## Capture modes (all three implemented)

- **B — In-app camera/gallery**: camera icon → take or pick → upload.
- **C — Share-to-Amy**: in any app, Share → Amy. Handled on cold start and while running.
- **A — Gallery sync**: the sync icon opens "Gallery sync". Toggle **Auto-sync on app open**
  to ingest new photos each time you open the app, or tap **Sync now**. Only photos newer
  than the last sync are uploaded; duplicates are skipped by the backend.

## Roadmap (next)

- **True background ingestion** for mode A: a native `workmanager` task so photos sync
  even when the app is closed (Android; iOS is OS-restricted).
- **Offline queue**: hold captures (sqflite) and upload when connectivity returns.

See `../../PersonalOS_Mobile_Architecture.md` for the full design.
