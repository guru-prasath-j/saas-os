import 'package:flutter/material.dart';
import 'config.dart';

/// Configure the backend connection (server URL + optional auth token).
class SettingsScreen extends StatefulWidget {
  const SettingsScreen({super.key});
  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  late final TextEditingController _url = TextEditingController(text: Config.baseUrl);
  late final TextEditingController _token = TextEditingController(text: Config.token);
  bool _saved = false;

  Future<void> _save() async {
    await Config.save(_url.text, _token.text);
    setState(() => _saved = true);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Settings')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          const Text('Backend connection', style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
          const SizedBox(height: 12),
          TextField(
            controller: _url,
            keyboardType: TextInputType.url,
            decoration: const InputDecoration(
              labelText: 'Server URL',
              hintText: 'http://192.168.1.20:8848',
              border: OutlineInputBorder(),
            ),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _token,
            obscureText: true,
            decoration: const InputDecoration(
              labelText: 'Auth token (optional)',
              hintText: 'matches AMY_AUTH_TOKEN on the backend',
              border: OutlineInputBorder(),
            ),
          ),
          const SizedBox(height: 16),
          FilledButton.icon(onPressed: _save, icon: const Icon(Icons.save), label: const Text('Save')),
          if (_saved)
            const Padding(
              padding: EdgeInsets.only(top: 12),
              child: Text('Saved. Restart chat or pull-to-refresh to reconnect.',
                  style: TextStyle(color: Colors.white54)),
            ),
          const SizedBox(height: 24),
          const Text(
            'Tips:\n'
            '• Android emulator → use http://10.0.2.2:8848\n'
            '• Real phone on same Wi-Fi → use the PC LAN IP (e.g. 192.168.x.x:8848)\n'
            '• On the go → expose the backend via Cloudflare Tunnel / Tailscale and use that URL\n'
            '• Set a token only if the backend has AMY_AUTH_TOKEN set.',
            style: TextStyle(color: Colors.white54, height: 1.5),
          ),
        ],
      ),
    );
  }

  @override
  void dispose() {
    _url.dispose();
    _token.dispose();
    super.dispose();
  }
}
