package com.personalos.amy_app

// Real Meta Wearables Device Access Toolkit (DAT) bridge — compiled only when
// the metaDat.enabled gradle property is true (see app/build.gradle.kts).
//
// Dart-facing contract (identical to the iOS bridge and the stub):
//   MethodChannel  amy/meta_glasses
//     isAvailable            -> {available: bool, reason?: str}
//     connect                -> {ok: bool, reason?: str, message?: str}
//     startStream {quality, frameRate} -> {ok, reason?}
//     stopStream             -> {ok}
//     triggerCapture         -> {ok, reason?}   (photo arrives as an event)
//     disconnect             -> {ok}
//     mockPair               -> {ok, reason?}   (Mock Device Kit, dev only)
//   EventChannel   amy/meta_glasses/events, map events:
//     {type: sessionState|streamState|registrationState, state: str-lowercase}
//     {type: capture, bytes: ByteArray(JPEG), takenAt: null, lat: null, lon: null}
//     {type: error, code: str, message: str}
//
// API surface verified against facebook/meta-wearables-dat-android v0.8.0
// (samples/CameraAccess + plugins/mwdat-android/skills). Two knowingly
// unverified spots are marked TODO(DAT-verify) below.

import android.app.Activity
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import java.io.ByteArrayOutputStream
import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.common.EventChannel
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import com.meta.wearable.dat.camera.Stream
import com.meta.wearable.dat.camera.addStream
import com.meta.wearable.dat.camera.types.PhotoData
import com.meta.wearable.dat.camera.types.StreamConfiguration
import com.meta.wearable.dat.camera.types.VideoQuality
import com.meta.wearable.dat.core.Wearables
import com.meta.wearable.dat.core.selectors.AutoDeviceSelector
import com.meta.wearable.dat.core.session.DeviceSession
import com.meta.wearable.dat.core.types.Permission
import com.meta.wearable.dat.core.types.RegistrationState

class MetaGlassesBridge(private val activity: Activity) :
    MethodChannel.MethodCallHandler, EventChannel.StreamHandler {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private var events: EventChannel.EventSink? = null
    private var methodChannel: MethodChannel? = null
    private var eventChannel: EventChannel? = null

    private var session: DeviceSession? = null
    private var stream: Stream? = null
    private var sessionStateJob: Job? = null
    private var streamStateJob: Job? = null
    private var streamErrorJob: Job? = null

    fun register(messenger: BinaryMessenger) {
        methodChannel = MethodChannel(messenger, "amy/meta_glasses").also {
            it.setMethodCallHandler(this)
        }
        eventChannel = EventChannel(messenger, "amy/meta_glasses/events").also {
            it.setStreamHandler(this)
        }
    }

    fun dispose() {
        teardown()
        scope.cancel()
        methodChannel?.setMethodCallHandler(null)
        eventChannel?.setStreamHandler(null)
        events = null
    }

    // --- EventChannel ------------------------------------------------------
    override fun onListen(arguments: Any?, sink: EventChannel.EventSink?) {
        events = sink
    }

    override fun onCancel(arguments: Any?) {
        events = null
    }

    private fun emit(event: Map<String, Any?>) {
        // Always deliver on the platform main thread (scope is Main-bound).
        scope.launch { events?.success(event) }
    }

    private fun emitError(code: String, message: String) =
        emit(mapOf("type" to "error", "code" to code, "message" to message))

    // --- MethodChannel -----------------------------------------------------
    override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
        try {
            when (call.method) {
                "isAvailable" -> result.success(mapOf("available" to true))
                "connect" -> connect(result)
                "startStream" -> startStream(
                    call.argument<String>("quality") ?: "medium",
                    call.argument<Int>("frameRate") ?: 15, result)
                "stopStream" -> { stopStreamInternal(); result.success(mapOf("ok" to true)) }
                "triggerCapture" -> triggerCapture(result)
                "disconnect" -> { teardown(); result.success(mapOf("ok" to true)) }
                "mockPair" -> mockPair(result)
                else -> result.notImplemented()
            }
        } catch (t: Throwable) {
            // A DAT exception must never crash the app: fail the call instead.
            result.success(mapOf("ok" to false, "reason" to "sdk_error",
                                 "message" to (t.message ?: t.toString())))
        }
    }

    // --- connect: registration -> permission -> session --------------------
    private fun connect(result: MethodChannel.Result) {
        scope.launch {
            try {
                if (Wearables.registrationState.value != RegistrationState.REGISTERED) {
                    // Deeplinks into the Meta AI app; the user approves there
                    // and this app shows up under glasses "App connections".
                    Wearables.startRegistration(activity)
                    result.success(mapOf(
                        "ok" to false, "reason" to "not_registered",
                        "message" to "Finish the approval in the Meta AI app, then tap Connect again."))
                    return@launch
                }

                // TODO(DAT-verify): the sample app only *checks* the wearables
                // camera permission; granting happens through the Meta AI
                // deeplink flow (startRegistration covers first-time grants).
                // If the SDK adds an explicit requestPermission API, call it
                // here on the denied path.
                var granted = false
                var checkFailed: String? = null
                Wearables.checkPermissionStatus(Permission.CAMERA)
                    .onSuccess { status ->
                        granted = status.toString().lowercase().contains("grant")
                    }
                    .onFailure { error, _ -> checkFailed = error.toString() }
                if (checkFailed != null) {
                    result.success(mapOf("ok" to false, "reason" to "sdk_error",
                                         "message" to "Permission check failed: $checkFailed"))
                    return@launch
                }
                if (!granted) {
                    result.success(mapOf(
                        "ok" to false, "reason" to "permission_denied",
                        "message" to "Grant camera access for this app in the Meta AI app (App connections)."))
                    return@launch
                }

                if (session == null) {
                    val s = Wearables.createSession(AutoDeviceSelector())
                    session = s
                    sessionStateJob = scope.launch {
                        s.state.collect { st ->
                            emit(mapOf("type" to "sessionState",
                                       "state" to st.toString().lowercase()))
                        }
                    }
                    s.start()
                }
                result.success(mapOf("ok" to true))
            } catch (t: Throwable) {
                emitError("connect_failed", t.message ?: t.toString())
                result.success(mapOf("ok" to false, "reason" to "sdk_error",
                                     "message" to (t.message ?: t.toString())))
            }
        }
    }

    // --- stream -------------------------------------------------------------
    private fun quality(name: String): VideoQuality = when (name.lowercase()) {
        "high" -> VideoQuality.HIGH
        "low" -> VideoQuality.LOW
        else -> VideoQuality.MEDIUM
    }

    private fun startStream(qualityName: String, frameRate: Int, result: MethodChannel.Result) {
        val s = session
        if (s == null) {
            result.success(mapOf("ok" to false, "reason" to "no_session",
                                 "message" to "Call connect first."))
            return
        }
        scope.launch {
            try {
                // Valid frame rates per DAT docs: 2, 7, 15, 24, 30.
                val fr = intArrayOf(2, 7, 15, 24, 30)
                    .minByOrNull { kotlin.math.abs(it - frameRate) } ?: 15
                s.addStream(StreamConfiguration(
                    videoQuality = quality(qualityName), frameRate = fr))
                    .onSuccess { added ->
                        stream = added
                        streamStateJob = scope.launch {
                            added.state.collect { st ->
                                emit(mapOf("type" to "streamState",
                                           "state" to st.toString().lowercase()))
                            }
                        }
                        streamErrorJob = scope.launch {
                            added.errorStream.collect { err ->
                                emitError("stream_error", err.toString())
                            }
                        }
                        added.start()
                        result.success(mapOf("ok" to true))
                    }
                    .onFailure { error, _ ->
                        result.success(mapOf("ok" to false, "reason" to "sdk_error",
                                             "message" to "addStream failed: $error"))
                    }
            } catch (t: Throwable) {
                result.success(mapOf("ok" to false, "reason" to "sdk_error",
                                     "message" to (t.message ?: t.toString())))
            }
        }
    }

    private fun stopStreamInternal() {
        streamStateJob?.cancel(); streamStateJob = null
        streamErrorJob?.cancel(); streamErrorJob = null
        try { stream?.stop() } catch (_: Throwable) {}
        stream = null
    }

    // --- capture ------------------------------------------------------------
    private fun triggerCapture(result: MethodChannel.Result) {
        val st = stream
        if (st == null) {
            result.success(mapOf("ok" to false, "reason" to "no_stream",
                                 "message" to "Start the stream first."))
            return
        }
        scope.launch {
            try {
                st.capturePhoto()
                    .onSuccess { photo -> handlePhoto(photo) }
                    .onFailure { error, _ -> emitError("capture_failed", error.toString()) }
                result.success(mapOf("ok" to true))
            } catch (t: Throwable) {
                result.success(mapOf("ok" to false, "reason" to "sdk_error",
                                     "message" to (t.message ?: t.toString())))
            }
        }
    }

    private fun handlePhoto(photo: PhotoData) {
        try {
            // DAT PhotoData carries no GPS and no wall-clock taken-at; per the
            // capture pipeline's rules we pass nulls through rather than
            // fabricating device-clock values.
            val jpeg: ByteArray? = when (photo) {
                is PhotoData.Bitmap -> {
                    val out = ByteArrayOutputStream()
                    photo.bitmap.compress(Bitmap.CompressFormat.JPEG, 90, out)
                    out.toByteArray()
                }
                // TODO(DAT-verify): HEIC payload field is `data` per the DAT
                // docs ("PhotoData exposes: data"); adjust here if 0.8.x names
                // it differently.
                is PhotoData.HEIC -> {
                    val bmp = BitmapFactory.decodeByteArray(photo.data, 0, photo.data.size)
                    if (bmp != null) {
                        val out = ByteArrayOutputStream()
                        bmp.compress(Bitmap.CompressFormat.JPEG, 90, out)
                        out.toByteArray()
                    } else photo.data // backend sniffs magic bytes; pass through
                }
            }
            if (jpeg == null || jpeg.isEmpty()) {
                emitError("capture_failed", "empty photo payload")
                return
            }
            emit(mapOf("type" to "capture", "bytes" to jpeg,
                       "takenAt" to null, "lat" to null, "lon" to null))
        } catch (t: Throwable) {
            emitError("capture_failed", t.message ?: t.toString())
        }
    }

    // --- mock device kit (dev/CI without physical glasses) ------------------
    private fun mockPair(result: MethodChannel.Result) {
        scope.launch {
            try {
                val kit = com.meta.wearable.dat.mockdevice.MockDeviceKit
                    .getInstance(activity.applicationContext)
                kit.enable()
                kit.pairGlasses(
                    com.meta.wearable.dat.mockdevice.api.GlassesModel.RAYBAN_META)
                result.success(mapOf("ok" to true))
            } catch (t: Throwable) {
                result.success(mapOf("ok" to false, "reason" to "sdk_error",
                                     "message" to (t.message ?: t.toString())))
            }
        }
    }

    // --- teardown ------------------------------------------------------------
    private fun teardown() {
        stopStreamInternal()
        sessionStateJob?.cancel(); sessionStateJob = null
        try { session?.stop() } catch (_: Throwable) {}
        // Stopped sessions are terminal in DAT: create a new one next connect.
        session = null
    }
}
