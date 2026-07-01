import 'dart:async';
import 'dart:io';
import 'package:receive_sharing_intent/receive_sharing_intent.dart';
import 'api.dart';

/// Share-to-Amy (capture mode C).
///
/// Lets you select photos in any app, tap "Share", choose Amy, and have them
/// ingested into the vault. Handles both cold-start (app launched via share)
/// and warm shares (app already running).
class ShareHandler {
  static final _api = AmyApi();
  static StreamSubscription? _sub;

  /// [onUploaded] is called after each share batch with (uploaded, skipped).
  static void init({void Function(int uploaded, int skipped)? onUploaded}) {
    // app already running
    _sub = ReceiveSharingIntent.instance.getMediaStream().listen(
      (files) => _handle(files, onUploaded),
      onError: (_) {},
    );
    // app launched from a share
    ReceiveSharingIntent.instance.getInitialMedia().then((files) {
      _handle(files, onUploaded);
      ReceiveSharingIntent.instance.reset();
    });
  }

  static Future<void> _handle(
    List<SharedMediaFile> files,
    void Function(int, int)? onUploaded,
  ) async {
    if (files.isEmpty) return;
    int up = 0, skip = 0;
    for (final f in files) {
      if (f.type != SharedMediaType.image) {
        skip++;
        continue;
      }
      try {
        final res = await _api.uploadCapture(
          File(f.path),
          takenAt: DateTime.now().toIso8601String(),
          source: 'share',
        );
        res['duplicate'] == true ? skip++ : up++;
      } catch (_) {
        skip++;
      }
    }
    onUploaded?.call(up, skip);
  }

  static void dispose() => _sub?.cancel();
}
