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
  ///
  /// [onPickDisbursement], if given, is offered a chance to link a shared
  /// screenshot (e.g. a UPI/NEFT confirmation) to a specific custodial
  /// disbursement — called with the list of recently-confirmed-but-not-yet-
  /// screenshotted beneficiaries; return the chosen one's transaction_id, or
  /// null to skip linking. Left null (the default), sharing behaves exactly
  /// as before — plain vault capture, no finance linkage, no extra API call.
  static void init({
    void Function(int uploaded, int skipped)? onUploaded,
    Future<String?> Function(List<Map<String, dynamic>> pending)? onPickDisbursement,
  }) {
    // app already running
    _sub = ReceiveSharingIntent.instance.getMediaStream().listen(
      (files) => _handle(files, onUploaded, onPickDisbursement),
      onError: (_) {},
    );
    // app launched from a share
    ReceiveSharingIntent.instance.getInitialMedia().then((files) {
      _handle(files, onUploaded, onPickDisbursement);
      ReceiveSharingIntent.instance.reset();
    });
  }

  static Future<void> _handle(
    List<SharedMediaFile> files,
    void Function(int, int)? onUploaded,
    Future<String?> Function(List<Map<String, dynamic>>)? onPickDisbursement,
  ) async {
    final images = files.where((f) => f.type == SharedMediaType.image).toList();
    int up = 0, skip = files.length - images.length;
    if (images.isEmpty) {
      onUploaded?.call(up, skip);
      return;
    }

    // Ask at most once per share batch, not once per image.
    String? linkTxn;
    if (onPickDisbursement != null) {
      try {
        final accountId = await _api.custodialAccountId();
        if (accountId != null) {
          final pending = await _api.custodialPendingScreenshots(accountId);
          if (pending.isNotEmpty) {
            linkTxn = await onPickDisbursement(pending);
          }
        }
      } catch (_) {
        // no custodial account, or the check failed — fall through to a
        // plain capture, same as if onPickDisbursement were never given.
      }
    }

    for (final f in images) {
      try {
        final res = await _api.uploadCapture(
          File(f.path),
          takenAt: DateTime.now().toIso8601String(),
          source: 'share',
          linkDisbursementTxn: linkTxn,
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
