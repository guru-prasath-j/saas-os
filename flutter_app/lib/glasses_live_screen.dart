import 'dart:async';
import 'package:flutter/foundation.dart' show kDebugMode;
import 'package:flutter/material.dart';
import 'meta_glasses_service.dart';

/// Live capture from Meta Ray-Ban glasses (Wearables Device Access Toolkit).
///
/// Feature-flagged (Settings → "Meta glasses live capture", default OFF) and
/// only functional in builds made with the DAT SDK bundled — otherwise the
/// native bridge reports sdk_not_bundled and this screen explains that.
/// Captures ride the existing pipeline: bytes → POST /api/captures with
/// source='meta-glasses' → vault note + photo memory, like every capture.
class GlassesLiveScreen extends StatefulWidget {
  const GlassesLiveScreen({super.key});
  @override
  State<GlassesLiveScreen> createState() => _GlassesLiveScreenState();
}

class _GlassesLiveScreenState extends State<GlassesLiveScreen> {
  final svc = MetaGlassesService.instance;

  bool? _available; // null = probing
  bool _busy = false;
  String _sessionState = 'disconnected';
  String _streamState = 'stopped';
  String _notice = '';
  String _uploadLine = '';
  String _quality = 'medium';

  final _subs = <StreamSubscription>[];

  @override
  void initState() {
    super.initState();
    _probe();
    _subs.add(svc.sessionStateStream.listen((s) {
      if (mounted) setState(() => _sessionState = s);
    }));
    _subs.add(svc.streamStateStream.listen((s) {
      if (mounted) setState(() => _streamState = s);
    }));
    _subs.add(svc.errorStream.listen((e) {
      if (mounted) setState(() => _notice = e);
    }));
    _subs.add(svc.captureStream.listen((_) {
      if (mounted) setState(() => _uploadLine = 'Captured — syncing…');
    }));
    _subs.add(svc.uploadStream.listen((u) {
      if (!mounted) return;
      setState(() => _uploadLine = switch (u.status) {
            'saved' => 'Saved to your vault ✓'
                '${u.message.isNotEmpty ? ' — ${u.message}' : ''}',
            'duplicate' => 'Already captured earlier.',
            _ => 'Upload error: ${u.message}',
          });
    }));
  }

  Future<void> _probe() async {
    final ok = await svc.isAvailable();
    if (mounted) setState(() => _available = ok);
  }

  bool get _streaming => _streamState == 'streaming';

  Future<void> _connect() async {
    setState(() {
      _busy = true;
      _notice = '';
    });
    final r = await svc.connect();
    if (!mounted) return;
    setState(() {
      _busy = false;
      if (!r.ok) {
        _notice = switch (r.reason) {
          'not_registered' =>
            'Approve Amy in the Meta AI app (App connections), then tap Connect again.',
          'permission_denied' =>
            'Camera access for Amy is off — enable it in the Meta AI app under App connections.',
          'no_device' => 'No glasses found — make sure they are paired and worn.',
          _ => r.message.isNotEmpty ? r.message : 'Could not connect (${r.reason}).',
        };
      } else {
        _sessionState = 'starting';
      }
    });
  }

  Future<void> _startStream() async {
    setState(() => _busy = true);
    final r = await svc.startStream(quality: _quality, frameRate: 15);
    if (!mounted) return;
    setState(() {
      _busy = false;
      if (!r.ok) _notice = r.message.isNotEmpty ? r.message : r.reason;
    });
  }

  Future<void> _capture() async {
    final r = await svc.triggerCapture();
    if (!mounted) return;
    if (!r.ok) setState(() => _notice = r.message.isNotEmpty ? r.message : r.reason);
  }

  Future<void> _disconnect() async {
    await svc.disconnect();
    if (mounted) {
      setState(() {
        _sessionState = 'disconnected';
        _streamState = 'stopped';
      });
    }
  }

  Widget _stateChip(String label, String value, {bool live = false}) => Chip(
        avatar: Icon(Icons.circle,
            size: 10, color: live ? Colors.greenAccent : Colors.white38),
        label: Text('$label: $value', style: const TextStyle(fontSize: 12)),
        visualDensity: VisualDensity.compact,
      );

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Glasses live capture')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          if (_available == null)
            const Center(child: CircularProgressIndicator())
          else if (_available == false) ...[
            const Icon(Icons.visibility_off, size: 42, color: Colors.white38),
            const SizedBox(height: 12),
            const Text(
              'This build was made without the Meta Wearables SDK.\n\n'
              'Live glasses capture needs an internal/dev build with the DAT '
              'SDK bundled (see flutter_app/META_GLASSES.md). Photos from '
              'your glasses can still reach Amy via the camera-roll gallery '
              'sync or by sharing them into the app.',
              style: TextStyle(color: Colors.white70, height: 1.5),
            ),
          ] else ...[
            Wrap(spacing: 8, runSpacing: 8, children: [
              _stateChip('session', _sessionState,
                  live: _sessionState == 'started'),
              _stateChip('stream', _streamState, live: _streaming),
            ]),
            const SizedBox(height: 16),
            Row(children: [
              const Text('Quality:  '),
              DropdownButton<String>(
                value: _quality,
                items: const [
                  DropdownMenuItem(value: 'low', child: Text('Low (360p)')),
                  DropdownMenuItem(value: 'medium', child: Text('Medium (504p)')),
                  DropdownMenuItem(value: 'high', child: Text('High (720p)')),
                ],
                onChanged: _streaming
                    ? null
                    : (v) => setState(() => _quality = v ?? 'medium'),
              ),
            ]),
            const SizedBox(height: 8),
            Row(children: [
              Expanded(
                child: OutlinedButton.icon(
                  onPressed: _busy
                      ? null
                      : (_sessionState == 'started' || _streaming)
                          ? _disconnect
                          : _connect,
                  icon: const Icon(Icons.sensors),
                  label: Text((_sessionState == 'started' || _streaming)
                      ? 'Disconnect'
                      : 'Connect'),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: OutlinedButton.icon(
                  onPressed: (_busy || _sessionState != 'started' || _streaming)
                      ? null
                      : _startStream,
                  icon: const Icon(Icons.play_arrow),
                  label: const Text('Start stream'),
                ),
              ),
            ]),
            const SizedBox(height: 16),
            FilledButton.icon(
              // Capture only while the stream is actually STREAMING.
              onPressed: _streaming ? _capture : null,
              icon: const Icon(Icons.camera),
              label: const Text('Capture from glasses'),
            ),
            if (kDebugMode) ...[
              const SizedBox(height: 8),
              TextButton.icon(
                onPressed: () async {
                  final r = await svc.mockPair();
                  if (mounted) {
                    setState(() => _notice = r.ok
                        ? 'Mock glasses paired — connect as usual.'
                        : 'Mock kit: ${r.message}');
                  }
                },
                icon: const Icon(Icons.bug_report, size: 18),
                label: const Text('Pair mock glasses (dev)'),
              ),
            ],
            if (_uploadLine.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 16),
                child: Text(_uploadLine,
                    style: const TextStyle(color: Colors.white70)),
              ),
            if (_notice.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 12),
                child: Text(_notice,
                    style: const TextStyle(color: Colors.orangeAccent)),
              ),
            const SizedBox(height: 24),
            const Text(
              'Wearing your glasses? Connect, start the stream, then capture. '
              'Removing the glasses or closing the hinge pauses the session — '
              'that is normal, reconnect when ready. Every capture lands in '
              'your vault with caption, text and place extracted, and Amy can '
              'answer questions about it right away.',
              style: TextStyle(color: Colors.white54, height: 1.5),
            ),
          ],
        ],
      ),
    );
  }

  @override
  void dispose() {
    for (final s in _subs) {
      s.cancel();
    }
    // Leave any active session running only while the screen is open.
    svc.disconnect();
    super.dispose();
  }
}
