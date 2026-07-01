import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'api.dart';

/// Browse recent photo captures stored in the vault.
class CapturesScreen extends StatefulWidget {
  const CapturesScreen({super.key});
  @override
  State<CapturesScreen> createState() => _CapturesScreenState();
}

class _CapturesScreenState extends State<CapturesScreen> {
  final api = AmyApi();
  late Future<List<Map<String, dynamic>>> _future;

  @override
  void initState() {
    super.initState();
    _future = api.listCaptures();
  }

  void _refresh() => setState(() => _future = api.listCaptures());

  /// list endpoint returns image as "attachments/<name>"; the vault-relative
  /// path the image endpoint expects is "08_Captures/attachments/<name>".
  String _fullImagePath(String image) => '08_Captures/$image';

  String _when(String iso) {
    final d = DateTime.tryParse(iso);
    return d == null ? iso : DateFormat('d MMM yyyy, h:mm a').format(d.toLocal());
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Captures'),
        actions: [IconButton(onPressed: _refresh, icon: const Icon(Icons.refresh))],
      ),
      body: FutureBuilder<List<Map<String, dynamic>>>(
        future: _future,
        builder: (_, snap) {
          if (snap.connectionState != ConnectionState.done) {
            return const Center(child: CircularProgressIndicator());
          }
          if (snap.hasError) {
            return Center(child: Padding(padding: const EdgeInsets.all(24), child: Text('Error: ${snap.error}')));
          }
          final items = snap.data ?? [];
          if (items.isEmpty) {
            return const Center(child: Text('No captures yet. Tap the camera to add one.'));
          }
          return RefreshIndicator(
            onRefresh: () async => _refresh(),
            child: ListView.separated(
              itemCount: items.length,
              separatorBuilder: (_, __) => const Divider(height: 1),
              itemBuilder: (_, i) {
                final c = items[i];
                final image = (c['image'] ?? '').toString();
                return ListTile(
                  leading: image.isEmpty
                      ? const Icon(Icons.image_not_supported)
                      : ClipRRect(
                          borderRadius: BorderRadius.circular(8),
                          child: Image.network(
                            api.imageUrl(_fullImagePath(image)),
                            width: 52,
                            height: 52,
                            fit: BoxFit.cover,
                            errorBuilder: (_, __, ___) => const Icon(Icons.broken_image),
                          ),
                        ),
                  title: Text(c['title'] ?? '(capture)'),
                  subtitle: Text([
                    _when((c['created'] ?? '').toString()),
                    if ((c['place'] ?? '').toString().isNotEmpty) c['place'],
                  ].join(' · ')),
                  onTap: () => _showDetail(c),
                );
              },
            ),
          );
        },
      ),
    );
  }

  void _showDetail(Map<String, dynamic> c) {
    final image = (c['image'] ?? '').toString();
    showModalBottomSheet(
      context: context,
      isScrollControlled: true,
      builder: (_) => Padding(
        padding: const EdgeInsets.all(16),
        child: SingleChildScrollView(
          child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
            if (image.isNotEmpty)
              ClipRRect(
                borderRadius: BorderRadius.circular(12),
                child: Image.network(
                  api.imageUrl(_fullImagePath(image)),
                  errorBuilder: (_, __, ___) => const SizedBox.shrink(),
                ),
              ),
            const SizedBox(height: 12),
            Text(c['title'] ?? '', style: const TextStyle(fontSize: 18, fontWeight: FontWeight.bold)),
            const SizedBox(height: 4),
            Text(_when((c['created'] ?? '').toString()), style: const TextStyle(color: Colors.white54)),
            if ((c['place'] ?? '').toString().isNotEmpty)
              Padding(padding: const EdgeInsets.only(top: 4), child: Text(c['place'])),
            if ((c['tags'] as List?)?.isNotEmpty ?? false)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Wrap(
                  spacing: 6,
                  children: (c['tags'] as List)
                      .map((t) => Chip(label: Text('$t'), visualDensity: VisualDensity.compact))
                      .toList(),
                ),
              ),
          ]),
        ),
      ),
    );
  }
}
