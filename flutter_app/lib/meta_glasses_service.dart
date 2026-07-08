import 'dart:async';
import 'package:flutter/services.dart';
import 'api.dart';

/// Platform-agnostic Dart face of the Meta glasses (DAT) native bridges.
///
/// Both the Kotlin bridge (android/app/src/dat) and the Swift bridge
/// (ios/Runner/MetaGlassesBridge.swift) implement the same MethodChannel /
/// EventChannel contract, so this class contains zero platform branching.
/// Builds made without the DAT SDK answer with reason `sdk_not_bundled`,
/// which the UI shows as "not available in this build".
///
/// Every photo event is auto-uploaded through the existing capture pipeline
/// ([AmyApi.uploadCaptureBytes] → POST /api/captures) with
/// source='meta-glasses' — the backend then does dedup, caption/OCR,
/// vault note, journaling and photo-memory exactly like any other capture.

class GlassesResult {
  GlassesResult(this.ok, {this.reason = '', this.message = ''});
  final bool ok;
  final String reason; // not_registered | permission_denied | no_device |
                       // no_session | no_stream | sdk_not_bundled | sdk_error
  final String message;

  static GlassesResult from(dynamic raw) {
    final m = (raw as Map?)?.cast<String, dynamic>() ?? {};
    return GlassesResult(m['ok'] == true,
        reason: (m['reason'] ?? '').toString(),
        message: (m['message'] ?? '').toString());
  }
}

class GlassesCapture {
  GlassesCapture(this.bytes, {this.takenAt, this.lat, this.lon});
  final Uint8List bytes;
  // Passed through from the device only if DAT provided them; never
  // fabricated from the phone clock.
  final String? takenAt;
  final double? lat;
  final double? lon;
}

/// Result of the automatic upload that follows each capture.
class GlassesUpload {
  GlassesUpload(this.status, {this.message = ''});
  final String status; // saved | duplicate | error
  final String message;
}

class MetaGlassesService {
  MetaGlassesService._();
  static final MetaGlassesService instance = MetaGlassesService._();

  static const _method = MethodChannel('amy/meta_glasses');
  static const _events = EventChannel('amy/meta_glasses/events');

  final api = AmyApi();

  final _sessionState = StreamController<String>.broadcast();
  final _streamState = StreamController<String>.broadcast();
  final _errors = StreamController<String>.broadcast();
  final _captures = StreamController<GlassesCapture>.broadcast();
  final _uploads = StreamController<GlassesUpload>.broadcast();

  Stream<String> get sessionStateStream => _sessionState.stream;
  Stream<String> get streamStateStream => _streamState.stream;
  Stream<String> get errorStream => _errors.stream;
  Stream<GlassesCapture> get captureStream => _captures.stream;
  Stream<GlassesUpload> get uploadStream => _uploads.stream;

  StreamSubscription? _eventSub;

  void _ensureListening() {
    _eventSub ??= _events.receiveBroadcastStream().listen(_onEvent,
        onError: (e) => _errors.add(e.toString()));
  }

  void _onEvent(dynamic raw) {
    final ev = (raw as Map?)?.cast<String, dynamic>() ?? {};
    switch (ev['type']) {
      case 'sessionState':
        _sessionState.add((ev['state'] ?? '').toString());
      case 'streamState':
        _streamState.add((ev['state'] ?? '').toString());
      case 'error':
        _errors.add('${ev['code']}: ${ev['message']}');
      case 'capture':
        final bytes = ev['bytes'];
        if (bytes is Uint8List && bytes.isNotEmpty) {
          final cap = GlassesCapture(bytes,
              takenAt: ev['takenAt'] as String?,
              lat: (ev['lat'] as num?)?.toDouble(),
              lon: (ev['lon'] as num?)?.toDouble());
          _captures.add(cap);
          _upload(cap);
        }
    }
  }

  Future<void> _upload(GlassesCapture cap) async {
    try {
      final res = await api.uploadCaptureBytes(cap.bytes,
          takenAt: cap.takenAt, lat: cap.lat, lon: cap.lon,
          source: 'meta-glasses');
      _uploads.add(GlassesUpload(
          res['duplicate'] == true ? 'duplicate' : 'saved',
          message: (res['caption'] ?? '').toString()));
    } catch (e) {
      _uploads.add(GlassesUpload('error', message: e.toString()));
    }
  }

  /// False on builds made without the DAT SDK (stub bridge).
  Future<bool> isAvailable() async {
    try {
      final r = await _method.invokeMethod('isAvailable');
      return ((r as Map?)?.cast<String, dynamic>() ?? {})['available'] == true;
    } catch (_) {
      return false;
    }
  }

  /// Registration + permission + session start. Returns a reason code the UI
  /// can explain (not_registered → finish approval in the Meta AI app, etc.).
  Future<GlassesResult> connect() async {
    _ensureListening();
    try {
      return GlassesResult.from(await _method.invokeMethod('connect'));
    } on PlatformException catch (e) {
      return GlassesResult(false, reason: 'sdk_error', message: e.message ?? '$e');
    }
  }

  Future<GlassesResult> startStream(
      {String quality = 'medium', int frameRate = 15}) async {
    _ensureListening();
    try {
      return GlassesResult.from(await _method.invokeMethod(
          'startStream', {'quality': quality, 'frameRate': frameRate}));
    } on PlatformException catch (e) {
      return GlassesResult(false, reason: 'sdk_error', message: e.message ?? '$e');
    }
  }

  Future<void> stopStream() async {
    try {
      await _method.invokeMethod('stopStream');
    } on PlatformException catch (_) {}
  }

  Future<GlassesResult> triggerCapture() async {
    try {
      return GlassesResult.from(await _method.invokeMethod('triggerCapture'));
    } on PlatformException catch (e) {
      return GlassesResult(false, reason: 'sdk_error', message: e.message ?? '$e');
    }
  }

  Future<void> disconnect() async {
    try {
      await _method.invokeMethod('disconnect');
    } on PlatformException catch (_) {}
  }

  /// Pair a simulated device (Mock Device Kit) — dev/CI without hardware.
  Future<GlassesResult> mockPair() async {
    _ensureListening();
    try {
      return GlassesResult.from(await _method.invokeMethod('mockPair'));
    } on PlatformException catch (e) {
      return GlassesResult(false, reason: 'sdk_error', message: e.message ?? '$e');
    }
  }
}
