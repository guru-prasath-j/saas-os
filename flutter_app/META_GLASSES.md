# Meta Glasses Live Capture (Wearables Device Access Toolkit)

Real-time photo capture from Ray-Ban Meta glasses straight into the Amy
vault, as a new capture **source** (`meta-glasses`) on the existing
`POST /api/captures` pipeline. No backend changes — dedup, caption/OCR,
geocoding, vault note, journaling and photo memory all apply automatically.

## Status / distribution

- **Feature flag:** Settings → Experimental → "Meta glasses live capture"
  (`glassesLiveCapture`, default **OFF**). Flag off = app behavior unchanged.
- **SDK gating:** normal builds do NOT bundle the DAT SDK. Without it, the
  native bridges compile as stubs and the UI reports "not available in this
  build".
- **Distribution:** debug + internal release (TestFlight/ad-hoc, Meta release
  channels, Play internal testing) are fine on both platforms. **Do not
  submit to the public App Store** — Apple currently rejects DAT integrations
  over the ExternalAccessory/MFi conflict (`com.meta.ar.wearable` protocol in
  Info.plist); public Play publishing is separately partner-gated by Meta.

## Architecture

```
Dart (platform-agnostic)                 Native (per platform)
lib/meta_glasses_service.dart            android/app/src/dat/kotlin/...        (real, gradle-gated)
  MetaGlassesService                     android/app/src/datstub/kotlin/...    (stub, default)
  connect/startStream/triggerCapture/    ios/Runner/MetaGlassesBridge.swift    (real+stub via canImport)
  disconnect + captureStream,
  sessionStateStream, uploadStream
        │  MethodChannel  amy/meta_glasses
        │  EventChannel   amy/meta_glasses/events
        ▼
  auto-upload each capture → AmyApi.uploadCaptureBytes(source: 'meta-glasses')
UI: lib/glasses_live_screen.dart (flag-gated app-bar entry in main.dart)
```

Session model (both platforms): register via Meta AI deeplink → camera
permission (granted in Meta AI, "once"/"always") → create+start session →
add camera stream (default MEDIUM / 15fps, quality configurable in the UI)
→ `capturePhoto()` → JPEG bytes arrive as a `capture` event. PAUSED/STOPPED
states (glasses removed, hinge closed, permission revoked) are normal and
surface as state chips, never crashes. Stopped sessions are terminal —
reconnect creates a new one. Only one third-party dev-mode app can be
registered per Meta account at a time.

## Building WITH the SDK

### Android

1. GitHub personal access token with `read:packages`.
2. Build with:
   ```
   flutter build apk --debug \
     --dart-define-from-file=none \
     -P metaDat.enabled=true            # via android/gradle.properties or -P
   ```
   In practice: add to `android/gradle.properties` (or CI env):
   ```
   metaDat.enabled=true
   metaDat.githubUser=<your-github-username>
   metaDat.githubToken=<token>          # or export GITHUB_TOKEN
   # from the Meta Wearables developer center (internal-release builds):
   metaDat.applicationId=<app id>       # default "0" = development mode
   metaDat.clientToken=<client token>
   ```
3. This flips the compiled source set from `src/datstub` to `src/dat` and
   adds `com.meta.wearable:mwdat-{core,camera,mockdevice}:0.8.0`.

### iOS

1. Xcode → Runner → Add Package Dependency →
   `https://github.com/facebook/meta-wearables-dat-ios` → add **MWDATCore**
   and **MWDATCamera** (plus **MWDATMockDevice** for mock testing) to Runner.
2. Set the iOS deployment target to **16.0+** for the Runner target.
3. `MetaGlassesBridge.swift` switches from stub to real automatically via
   `#if canImport(MWDATCore)`. Info.plist already carries the URL scheme
   (`amyapp`), `MWDAT` dict (`MetaAppID` "0" = development), external
   accessory protocol and background modes.

## Testing without glasses (Mock Device Kit)

Debug builds show "Pair mock glasses (dev)" on the live-capture screen —
this enables MockDeviceKit and pairs a simulated Ray-Ban Meta; connect and
capture as if real hardware were present. Works on both platforms when the
mock product is bundled.

## Known TODO(DAT-verify) points

The DAT SDK is a preview; two spots were implemented from documented-but-
thin API surface and are marked in code:
- Android: no explicit `requestPermission` API in the sample — denied
  permission routes the user to the Meta AI app; revisit if the SDK adds one.
  HEIC payload field assumed `data`.
- iOS: `DeviceSession` type name and direct `MockDeviceKit()` construction
  assumed; the sample injects interfaces. iOS URL-callback forwarding for the
  registration deeplink may need `onOpenURL`-equivalent wiring in
  SceneDelegate if registration state doesn't refresh after returning from
  Meta AI.
First compile against the real SDK will confirm or correct these in minutes.
