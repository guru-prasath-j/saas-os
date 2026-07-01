import 'dart:io';
import 'package:flutter/material.dart';
import 'package:image_picker/image_picker.dart';
import 'package:geolocator/geolocator.dart';
import 'api.dart';

/// Take or pick a photo, attach time + GPS, and send it to Amy's vault.
class CaptureScreen extends StatefulWidget {
  const CaptureScreen({super.key});
  @override
  State<CaptureScreen> createState() => _CaptureScreenState();
}

class _CaptureScreenState extends State<CaptureScreen> {
  final api = AmyApi();
  final picker = ImagePicker();
  final note = TextEditingController();
  final tags = TextEditingController();

  File? _image;
  bool _busy = false;
  String _status = '';
  Map<String, dynamic>? _result;

  Future<void> _pick(ImageSource src) async {
    final x = await picker.pickImage(source: src, imageQuality: 85, maxWidth: 2000);
    if (x != null) {
      setState(() {
        _image = File(x.path);
        _result = null;
        _status = '';
      });
    }
  }

  Future<Position?> _location() async {
    try {
      if (!await Geolocator.isLocationServiceEnabled()) return null;
      var perm = await Geolocator.checkPermission();
      if (perm == LocationPermission.denied) {
        perm = await Geolocator.requestPermission();
      }
      if (perm == LocationPermission.denied || perm == LocationPermission.deniedForever) {
        return null;
      }
      return await Geolocator.getCurrentPosition();
    } catch (_) {
      return null;
    }
  }

  Future<void> _upload() async {
    if (_image == null) return;
    setState(() {
      _busy = true;
      _status = 'Getting location…';
    });
    final pos = await _location();
    setState(() => _status = 'Uploading & analyzing…');
    try {
      final res = await api.uploadCapture(
        _image!,
        lat: pos?.latitude,
        lon: pos?.longitude,
        takenAt: DateTime.now().toIso8601String(),
        source: 'in-app-camera',
        note: note.text,
        tags: tags.text,
      );
      setState(() {
        _result = res;
        _status = res['duplicate'] == true ? 'Already captured earlier.' : 'Saved to your vault ✓';
        if (res['duplicate'] != true) {
          _image = null;
          note.clear();
          tags.clear();
        }
      });
    } catch (e) {
      setState(() => _status = 'Error: $e');
    } finally {
      setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('New capture')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          if (_image != null)
            ClipRRect(
              borderRadius: BorderRadius.circular(12),
              child: Image.file(_image!, height: 240, width: double.infinity, fit: BoxFit.cover),
            )
          else
            Container(
              height: 200,
              decoration: BoxDecoration(
                color: const Color(0xFF171B22),
                borderRadius: BorderRadius.circular(12),
                border: Border.all(color: const Color(0xFF262C36)),
              ),
              child: const Center(child: Text('No photo selected', style: TextStyle(color: Colors.white54))),
            ),
          const SizedBox(height: 12),
          Row(children: [
            Expanded(
              child: OutlinedButton.icon(
                onPressed: _busy ? null : () => _pick(ImageSource.camera),
                icon: const Icon(Icons.camera_alt),
                label: const Text('Camera'),
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: OutlinedButton.icon(
                onPressed: _busy ? null : () => _pick(ImageSource.gallery),
                icon: const Icon(Icons.photo_library),
                label: const Text('Gallery'),
              ),
            ),
          ]),
          const SizedBox(height: 16),
          TextField(
            controller: note,
            decoration: const InputDecoration(labelText: 'Note (optional)', border: OutlineInputBorder()),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: tags,
            decoration: const InputDecoration(
              labelText: 'Tags (comma-separated, optional)',
              border: OutlineInputBorder(),
            ),
          ),
          const SizedBox(height: 16),
          FilledButton.icon(
            onPressed: (_image == null || _busy) ? null : _upload,
            icon: _busy
                ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2))
                : const Icon(Icons.cloud_upload),
            label: Text(_busy ? 'Working…' : 'Send to Amy'),
          ),
          if (_status.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 16),
              child: Text(_status, style: const TextStyle(color: Colors.white70)),
            ),
          if (_result != null && _result!['duplicate'] != true) ...[
            const SizedBox(height: 12),
            Card(
              color: const Color(0xFF171B22),
              child: Padding(
                padding: const EdgeInsets.all(12),
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  if ((_result!['caption'] ?? '').toString().isNotEmpty)
                    Text('Caption: ${_result!['caption']}'),
                  if ((_result!['place'] ?? '').toString().isNotEmpty)
                    Padding(
                      padding: const EdgeInsets.only(top: 6),
                      child: Text('Place: ${_result!['place']}', style: const TextStyle(color: Colors.white54)),
                    ),
                  if ((_result!['ocr'] ?? '').toString().isNotEmpty)
                    Padding(
                      padding: const EdgeInsets.only(top: 6),
                      child: Text('Text: ${_result!['ocr']}', style: const TextStyle(color: Colors.white54)),
                    ),
                ]),
              ),
            ),
          ],
        ],
      ),
    );
  }

  @override
  void dispose() {
    note.dispose();
    tags.dispose();
    super.dispose();
  }
}
