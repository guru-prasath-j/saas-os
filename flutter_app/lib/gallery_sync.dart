import 'dart:io';
import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:photo_manager/photo_manager.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'api.dart';

/// Gallery auto-watch (capture mode A).
///
/// Scans the device's photo library for images newer than the last sync and
/// uploads them to Amy (with each photo's own date/time + GPS from EXIF).
/// This runs while the app is in the foreground (on open + manual "Sync now").
/// True background ingestion needs a native WorkManager task — see roadmap.
class GallerySync {
  static const _kLast = 'galleryLastSync';
  static const _kAuto = 'galleryAutoSync';
  static final api = AmyApi();

  static Future<bool> autoEnabled() async {
    final p = await SharedPreferences.getInstance();
    return p.getBool(_kAuto) ?? false;
  }

  static Future<void> setAuto(bool v) async {
    final p = await SharedPreferences.getInstance();
    await p.setBool(_kAuto, v);
  }

  static Future<DateTime?> lastSync() async {
    final p = await SharedPreferences.getInstance();
    final s = p.getString(_kLast);
    return s == null ? null : DateTime.tryParse(s);
  }

  static Future<void> _setLast(DateTime t) async {
    final p = await SharedPreferences.getInstance();
    await p.setString(_kLast, t.toIso8601String());
  }

  /// Returns (uploaded, skipped). [onProgress] reports (done, total).
  static Future<(int, int)> sync({
    int maxBatch = 30,
    void Function(int done, int total)? onProgress,
  }) async {
    final ps = await PhotoManager.requestPermissionExtend();
    if (!ps.isAuth && !ps.hasAccess) {
      throw Exception('Photo permission denied');
    }

    final albums = await PhotoManager.getAssetPathList(onlyAll: true, type: RequestType.image);
    if (albums.isEmpty) return (0, 0);

    final since = await lastSync();
    // newest first; pull a window and keep only those newer than last sync
    final recent = await albums.first.getAssetListPaged(page: 0, size: 200);
    final fresh = recent
        .where((a) => since == null ? true : a.createDateTime.isAfter(since))
        .toList()
      ..sort((a, b) => a.createDateTime.compareTo(b.createDateTime)); // oldest -> newest

    final batch = fresh.take(maxBatch).toList();
    int uploaded = 0, skipped = 0;
    DateTime? newest = since;

    for (var i = 0; i < batch.length; i++) {
      final a = batch[i];
      onProgress?.call(i + 1, batch.length);
      final File? f = await a.file;
      if (f == null) {
        skipped++;
        continue;
      }
      try {
        final res = await api.uploadCapture(
          f,
          lat: a.latitude == 0 ? null : a.latitude,
          lon: a.longitude == 0 ? null : a.longitude,
          takenAt: a.createDateTime.toIso8601String(),
          source: 'gallery-auto',
        );
        if (res['duplicate'] == true) {
          skipped++;
        } else {
          uploaded++;
        }
      } catch (_) {
        skipped++;
      }
      if (newest == null || a.createDateTime.isAfter(newest)) {
        newest = a.createDateTime;
      }
    }
    if (newest != null) await _setLast(newest);
    return (uploaded, skipped);
  }

  /// Called on app open: only runs if the user enabled auto-sync.
  static Future<void> maybeAutoSync() async {
    if (await autoEnabled()) {
      try {
        await sync();
      } catch (_) {/* silent on startup */}
    }
  }
}

class GallerySyncScreen extends StatefulWidget {
  const GallerySyncScreen({super.key});
  @override
  State<GallerySyncScreen> createState() => _GallerySyncScreenState();
}

class _GallerySyncScreenState extends State<GallerySyncScreen> {
  bool _auto = false;
  bool _busy = false;
  String _status = '';
  DateTime? _last;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    _auto = await GallerySync.autoEnabled();
    _last = await GallerySync.lastSync();
    if (mounted) setState(() {});
  }

  Future<void> _syncNow() async {
    setState(() {
      _busy = true;
      _status = 'Scanning gallery…';
    });
    try {
      final (up, skip) = await GallerySync.sync(
        onProgress: (d, t) => setState(() => _status = 'Uploading $d / $t…'),
      );
      _last = await GallerySync.lastSync();
      setState(() => _status = 'Done · $up new, $skip skipped');
    } catch (e) {
      setState(() => _status = 'Error: $e');
    } finally {
      setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final lastStr = _last == null ? 'never' : DateFormat('d MMM yyyy, h:mm a').format(_last!.toLocal());
    return Scaffold(
      appBar: AppBar(title: const Text('Gallery sync')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          SwitchListTile(
            title: const Text('Auto-sync on app open'),
            subtitle: const Text('Ingest new photos every time you open the app'),
            value: _auto,
            onChanged: _busy
                ? null
                : (v) async {
                    await GallerySync.setAuto(v);
                    setState(() => _auto = v);
                  },
          ),
          const Divider(),
          ListTile(
            title: const Text('Last synced'),
            subtitle: Text(lastStr),
          ),
          const SizedBox(height: 8),
          FilledButton.icon(
            onPressed: _busy ? null : _syncNow,
            icon: _busy
                ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2))
                : const Icon(Icons.sync),
            label: Text(_busy ? 'Working…' : 'Sync now'),
          ),
          if (_status.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 16),
              child: Text(_status, style: const TextStyle(color: Colors.white70)),
            ),
          const SizedBox(height: 24),
          const Text(
            'Only photos newer than the last sync are uploaded, in batches. '
            'Each photo keeps its own date/time and GPS. Duplicates are skipped '
            'automatically by the backend.',
            style: TextStyle(color: Colors.white54, height: 1.5),
          ),
        ],
      ),
    );
  }
}
