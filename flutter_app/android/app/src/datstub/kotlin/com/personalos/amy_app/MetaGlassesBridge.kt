package com.personalos.amy_app

// Stub MetaGlassesBridge — compiled when the metaDat.enabled gradle property
// is false (the default), so normal builds never depend on the DAT SDK's
// GitHub Packages repo. Same channel contract as the real bridge
// (src/dat/kotlin): every call succeeds with reason=sdk_not_bundled and the
// Dart layer shows the feature as unavailable.

import android.app.Activity
import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.common.EventChannel
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel

class MetaGlassesBridge(@Suppress("UNUSED_PARAMETER") activity: Activity) :
    MethodChannel.MethodCallHandler, EventChannel.StreamHandler {

    private var methodChannel: MethodChannel? = null
    private var eventChannel: EventChannel? = null

    fun register(messenger: BinaryMessenger) {
        methodChannel = MethodChannel(messenger, "amy/meta_glasses").also {
            it.setMethodCallHandler(this)
        }
        eventChannel = EventChannel(messenger, "amy/meta_glasses/events").also {
            it.setStreamHandler(this)
        }
    }

    fun dispose() {
        methodChannel?.setMethodCallHandler(null)
        eventChannel?.setStreamHandler(null)
    }

    override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
        when (call.method) {
            "isAvailable" -> result.success(
                mapOf("available" to false, "reason" to "sdk_not_bundled"))
            "connect", "startStream", "stopStream", "triggerCapture",
            "disconnect", "mockPair" -> result.success(
                mapOf("ok" to false, "reason" to "sdk_not_bundled",
                      "message" to "This build was made without the Meta DAT SDK "
                          + "(gradle -PmetaDat.enabled=true)."))
            else -> result.notImplemented()
        }
    }

    override fun onListen(arguments: Any?, sink: EventChannel.EventSink?) {}
    override fun onCancel(arguments: Any?) {}
}
