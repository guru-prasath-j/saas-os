package com.personalos.amy_app

import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine

class MainActivity : FlutterActivity() {
    // Real DAT implementation or no-op stub, chosen at build time by the
    // metaDat.enabled gradle property (see app/build.gradle.kts sourceSets).
    private var glassesBridge: MetaGlassesBridge? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        glassesBridge = MetaGlassesBridge(this).also {
            it.register(flutterEngine.dartExecutor.binaryMessenger)
        }
    }

    override fun onDestroy() {
        glassesBridge?.dispose()
        glassesBridge = null
        super.onDestroy()
    }
}
