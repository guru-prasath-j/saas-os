import Flutter
import UIKit

@main
@objc class AppDelegate: FlutterAppDelegate, FlutterImplicitEngineDelegate {
  override func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
  ) -> Bool {
    return super.application(application, didFinishLaunchingWithOptions: launchOptions)
  }

  func didInitializeImplicitFlutterEngine(_ engineBridge: FlutterImplicitEngineBridge) {
    GeneratedPluginRegistrant.register(with: engineBridge.pluginRegistry)
    // Meta glasses live capture (DAT) — stub behavior until the MWDAT SPM
    // package is added in Xcode; see flutter_app/META_GLASSES.md.
    let registrar = engineBridge.pluginRegistry.registrar(forPlugin: "MetaGlassesBridge")
    if let messenger = registrar?.messenger() {
      MetaGlassesBridge.register(messenger: messenger)
    }
  }
}
