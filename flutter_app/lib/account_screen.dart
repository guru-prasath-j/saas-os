import 'package:flutter/material.dart';
import 'api.dart';
import 'config.dart';

/// Account settings: OpenAI key (BYO), private folders, and logout.
class AccountScreen extends StatefulWidget {
  const AccountScreen({super.key, required this.onLoggedOut});
  final VoidCallback onLoggedOut;

  @override
  State<AccountScreen> createState() => _AccountScreenState();
}

class _AccountScreenState extends State<AccountScreen> {
  final api = AmyApi();
  final keyCtl = TextEditingController();
  final foldersCtl = TextEditingController();
  String _email = '';
  bool _hasKey = false;
  String _status = '';

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final m = await api.me();
      setState(() {
        _email = m['email'] ?? '';
        _hasKey = m['has_openai_key'] == true;
      });
    } catch (_) {}
  }

  Future<void> _saveKey() async {
    try {
      await api.setOpenAiKey(keyCtl.text.trim());
      keyCtl.clear();
      setState(() {
        _hasKey = true;
        _status = 'OpenAI key saved ✓';
      });
    } catch (e) {
      setState(() => _status = 'Error: $e');
    }
  }

  Future<void> _saveFolders() async {
    final folders = foldersCtl.text.split(',').map((s) => s.trim()).where((s) => s.isNotEmpty).toList();
    try {
      await api.setPrivateFolders(folders);
      setState(() => _status = 'Private folders saved ✓');
    } catch (e) {
      setState(() => _status = 'Error: $e');
    }
  }

  Future<void> _logout() async {
    await Config.logout();
    widget.onLoggedOut();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Account')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          ListTile(
            leading: const Icon(Icons.person),
            title: Text(_email.isEmpty ? '—' : _email),
            subtitle: Text(_hasKey ? 'OpenAI key set' : 'No OpenAI key yet'),
          ),
          const Divider(),
          const Text('Your OpenAI key', style: TextStyle(fontWeight: FontWeight.bold)),
          const SizedBox(height: 4),
          const Text('Your notes are only ever sent to your own key. Without a key, '
              'answers use the local/template model.', style: TextStyle(color: Colors.white54)),
          const SizedBox(height: 8),
          TextField(
            controller: keyCtl,
            obscureText: true,
            decoration: const InputDecoration(
              labelText: 'sk-...', border: OutlineInputBorder()),
          ),
          const SizedBox(height: 8),
          FilledButton(onPressed: _saveKey, child: const Text('Save key')),
          const SizedBox(height: 24),
          const Text('Private folders', style: TextStyle(fontWeight: FontWeight.bold)),
          const SizedBox(height: 4),
          const Text('Comma-separated folder names. Notes in these stay on the local '
              'model and are never sent to the cloud.', style: TextStyle(color: Colors.white54)),
          const SizedBox(height: 8),
          TextField(
            controller: foldersCtl,
            decoration: const InputDecoration(
              labelText: 'e.g. Finance, Family', border: OutlineInputBorder()),
          ),
          const SizedBox(height: 8),
          FilledButton(onPressed: _saveFolders, child: const Text('Save private folders')),
          if (_status.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 16),
              child: Text(_status, style: const TextStyle(color: Colors.white70)),
            ),
          const SizedBox(height: 32),
          OutlinedButton.icon(
            onPressed: _logout,
            icon: const Icon(Icons.logout),
            label: const Text('Log out'),
          ),
        ],
      ),
    );
  }

  @override
  void dispose() {
    keyCtl.dispose();
    foldersCtl.dispose();
    super.dispose();
  }
}
