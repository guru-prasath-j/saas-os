import Flutter
import Foundation

// Real Meta Wearables Device Access Toolkit (DAT) bridge for iOS.
//
// The DAT SDK is added via Swift Package Manager (Xcode → Add Package →
// https://github.com/facebook/meta-wearables-dat-ios → MWDATCore +
// MWDATCamera [+ MWDATMockDevice for testing]). Until that package is added,
// the canImport guards below compile this file into a stub that answers
// every call with reason=sdk_not_bundled — so plain `flutter build ios`
// keeps working unchanged. See flutter_app/META_GLASSES.md.
//
// Dart-facing contract (identical to the Android bridge):
//   MethodChannel  amy/meta_glasses
//     isAvailable / connect / startStream{quality,frameRate} / stopStream /
//     triggerCapture / disconnect / mockPair
//   EventChannel   amy/meta_glasses/events:
//     {type: sessionState|streamState|registrationState, state: <lowercase>}
//     {type: capture, bytes: FlutterStandardTypedData(JPEG),
//      takenAt: nil, lat: nil, lon: nil}
//     {type: error, code, message}
//
// API surface verified against facebook/meta-wearables-dat-ios
// (samples/CameraAccess + plugins/mwdat-ios/skills). Knowingly unverified
// spots are marked TODO(DAT-verify).

#if canImport(MWDATCore)
import MWDATCore
#endif
#if canImport(MWDATCamera)
import MWDATCamera
#endif
#if canImport(MWDATMockDevice)
import MWDATMockDevice
#endif

final class MetaGlassesBridge: NSObject, FlutterStreamHandler {
  private var eventSink: FlutterEventSink?

  static func register(messenger: FlutterBinaryMessenger) {
    let bridge = MetaGlassesBridge()
    let method = FlutterMethodChannel(name: "amy/meta_glasses", binaryMessenger: messenger)
    let events = FlutterEventChannel(name: "amy/meta_glasses/events", binaryMessenger: messenger)
    events.setStreamHandler(bridge)
    method.setMethodCallHandler { call, result in
      bridge.handle(call, result: result)
    }
    // Keep the bridge alive for the app's lifetime.
    objc_setAssociatedObject(method, "amy_glasses_bridge", bridge, .OBJC_ASSOCIATION_RETAIN)
  }

  // --- FlutterStreamHandler ------------------------------------------------
  func onListen(withArguments arguments: Any?, eventSink events: @escaping FlutterEventSink) -> FlutterError? {
    eventSink = events
    return nil
  }

  func onCancel(withArguments arguments: Any?) -> FlutterError? {
    eventSink = nil
    return nil
  }

  private func emit(_ event: [String: Any?]) {
    DispatchQueue.main.async { [weak self] in
      self?.eventSink?(event as [String: Any?])
    }
  }

  private func emitError(_ code: String, _ message: String) {
    emit(["type": "error", "code": code, "message": message])
  }

  private func ok() -> [String: Any?] { ["ok": true] }

  private func fail(_ reason: String, _ message: String) -> [String: Any?] {
    ["ok": false, "reason": reason, "message": message]
  }

#if canImport(MWDATCore) && canImport(MWDATCamera)
  // ==========================================================================
  // REAL IMPLEMENTATION (SPM package present)
  // ==========================================================================
  private static var configured = false

  // TODO(DAT-verify): session type is `DeviceSession` per DAT docs; adjust if
  // the SPM module names it differently.
  private var session: DeviceSession?
  private var stream: MWDATCamera.Stream?
  private var sessionStateTask: Task<Void, Never>?
  private var stateListenerToken: AnyListenerToken?
  private var errorListenerToken: AnyListenerToken?
  private var photoDataListenerToken: AnyListenerToken?

  private func handle(_ call: FlutterMethodCall, result: @escaping FlutterResult) {
    switch call.method {
    case "isAvailable":
      result(["available": true])
    case "connect":
      connect(result)
    case "startStream":
      let args = call.arguments as? [String: Any] ?? [:]
      startStream(quality: args["quality"] as? String ?? "medium",
                  frameRate: args["frameRate"] as? Int ?? 15, result: result)
    case "stopStream":
      stopStreamInternal()
      result(ok())
    case "triggerCapture":
      triggerCapture(result)
    case "disconnect":
      teardown()
      result(ok())
    case "mockPair":
      mockPair(result)
    default:
      result(FlutterMethodNotImplemented)
    }
  }

  private func connect(_ result: @escaping FlutterResult) {
    Task { [weak self] in
      guard let self else { return }
      do {
        if !Self.configured {
          Wearables.configure()   // must run before any other DAT call
          Self.configured = true
        }
        let wearables = Wearables.shared

        // Registration gate: first value of the registration state stream.
        var registered = false
        for await state in wearables.registrationStateStream() {
          registered = (state == .registered)
          self.emit(["type": "registrationState",
                     "state": String(describing: state).lowercased()])
          break
        }
        if !registered {
          // Deeplinks to the Meta AI app; user approves there and this app
          // appears under the glasses' "App connections".
          try? await wearables.startRegistration()
          result(self.fail("not_registered",
                           "Finish the approval in the Meta AI app, then tap Connect again."))
          return
        }

        // NOTE: unlike Android, the iOS sample exposes no explicit wearables
        // camera-permission check API — permission problems surface as the
        // session/stream pausing or erroring, which we forward as events.
        if self.session == nil {
          let selector = AutoDeviceSelector(wearables: wearables)
          let s = try wearables.createSession(deviceSelector: selector)
          self.session = s
          self.sessionStateTask = Task { [weak self] in
            for await st in s.stateStream() {
              self?.emit(["type": "sessionState",
                          "state": String(describing: st).lowercased()])
            }
          }
          try s.start()
        }
        result(self.ok())
      } catch {
        self.emitError("connect_failed", error.localizedDescription)
        result(self.fail("sdk_error", error.localizedDescription))
      }
    }
  }

  private func resolution(_ name: String) -> StreamingResolution {
    switch name.lowercased() {
    case "high": return .high
    case "low": return .low
    default: return .medium
    }
  }

  private func startStream(quality: String, frameRate: Int, result: @escaping FlutterResult) {
    guard let session else {
      result(fail("no_session", "Call connect first."))
      return
    }
    do {
      // Valid frame rates per DAT docs: 2, 7, 15, 24, 30.
      let valid = [2, 7, 15, 24, 30]
      let fr = valid.min(by: { abs($0 - frameRate) < abs($1 - frameRate) }) ?? 15
      let config = StreamConfiguration(videoCodec: .raw,
                                       resolution: resolution(quality),
                                       frameRate: fr)
      guard let newStream = try session.addStream(config: config) else {
        result(fail("sdk_error", "addStream returned nil"))
        return
      }
      stream = newStream
      stateListenerToken = newStream.statePublisher.listen { [weak self] state in
        self?.emit(["type": "streamState",
                    "state": String(describing: state).lowercased()])
      }
      photoDataListenerToken = newStream.photoDataPublisher.listen { [weak self] photoData in
        // DAT photos carry no GPS / wall-clock taken-at: pass nils through,
        // never fabricate device-clock values (same rule as Android).
        self?.emit(["type": "capture",
                    "bytes": FlutterStandardTypedData(bytes: photoData.data),
                    "takenAt": nil, "lat": nil, "lon": nil])
      }
      newStream.start()
      result(ok())
    } catch {
      result(fail("sdk_error", error.localizedDescription))
    }
  }

  private func triggerCapture(_ result: @escaping FlutterResult) {
    guard let stream else {
      result(fail("no_stream", "Start the stream first."))
      return
    }
    let accepted = stream.capturePhoto(format: .jpeg)
    if accepted {
      result(ok())   // photo arrives via photoDataPublisher -> capture event
    } else {
      result(fail("capture_failed", "capturePhoto was not accepted by the stream"))
    }
  }

  private func stopStreamInternal() {
    stateListenerToken = nil
    errorListenerToken = nil
    photoDataListenerToken = nil
    stream?.stop()
    stream = nil
  }

  private func teardown() {
    stopStreamInternal()
    sessionStateTask?.cancel()
    sessionStateTask = nil
    session?.stop()
    // Stopped sessions are terminal in DAT: create a new one next connect.
    session = nil
  }

  private func mockPair(_ result: @escaping FlutterResult) {
#if canImport(MWDATMockDevice)
    // TODO(DAT-verify): the sample injects a MockDeviceKitInterface; direct
    // construction assumed here — adjust to the package's factory if needed.
    let kit = MockDeviceKit()
    kit.enable()
    _ = kit.pairGlasses(model: .rayBanMeta)
    result(ok())
#else
    result(fail("sdk_not_bundled", "Add the MWDATMockDevice product to Runner to use the mock device kit."))
#endif
  }

#else
  // ==========================================================================
  // STUB (SPM package not added) — keeps plain builds green; Dart sees the
  // feature as unavailable with reason=sdk_not_bundled.
  // ==========================================================================
  private func handle(_ call: FlutterMethodCall, result: @escaping FlutterResult) {
    switch call.method {
    case "isAvailable":
      result(["available": false, "reason": "sdk_not_bundled"])
    case "connect", "startStream", "stopStream", "triggerCapture",
         "disconnect", "mockPair":
      result(fail("sdk_not_bundled",
                  "This build was made without the Meta DAT SDK (add the SPM package in Xcode)."))
    default:
      result(FlutterMethodNotImplemented)
    }
  }
#endif
}
