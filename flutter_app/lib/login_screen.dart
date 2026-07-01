import 'package:flutter/material.dart';
import 'api.dart';
import 'config.dart';

/// Sign in / sign up against the PersonalOS SaaS backend.
/// On success the JWT is stored and [onLoggedIn] is called.
class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key, required this.onLoggedIn, this.onLocal});
  final VoidCallback onLoggedIn;
  final VoidCallback? onLocal;  // continue without login (personal/local backend)

  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final api = AmyApi();
  final url = TextEditingController(text: Config.baseUrl);
  final email = TextEditingController();
  final password = TextEditingController();
  bool _signup = false;
  bool _busy = false;
  String _error = '';

  Future<void> _submit() async {
    setState(() {
      _busy = true;
      _error = '';
    });
    try {
      await Config.save(url.text, Config.token); // persist server URL first
      final res = _signup
          ? await api.signup(email.text.trim(), password.text)
          : await api.login(email.text.trim(), password.text);
      await Config.setToken(res['token'] as String);
      widget.onLoggedIn();
    } catch (e) {
      setState(() => _error = '$e'.replaceFirst('Exception: ', ''));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Center(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(24),
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 420),
            child: Column(mainAxisSize: MainAxisSize.min, children: [
              const Text('PersonalOS', style: TextStyle(fontSize: 30, fontWeight: FontWeight.bold)),
              const SizedBox(height: 4),
              const Text('Sign in to Amy', style: TextStyle(color: Colors.white54)),
              const SizedBox(height: 28),
              TextField(
                controller: url,
                keyboardType: TextInputType.url,
                decoration: const InputDecoration(labelText: 'Server URL', border: OutlineInputBorder()),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: email,
                keyboardType: TextInputType.emailAddress,
                decoration: const InputDecoration(labelText: 'Email', border: OutlineInputBorder()),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: password,
                obscureText: true,
                onSubmitted: (_) => _submit(),
                decoration: const InputDecoration(labelText: 'Password', border: OutlineInputBorder()),
              ),
              if (_error.isNotEmpty)
                Padding(
                  padding: const EdgeInsets.only(top: 12),
                  child: Text(_error, style: const TextStyle(color: Colors.redAccent)),
                ),
              const SizedBox(height: 20),
              SizedBox(
                width: double.infinity,
                child: FilledButton(
                  onPressed: _busy ? null : _submit,
                  child: _busy
                      ? const SizedBox(height: 18, width: 18, child: CircularProgressIndicator(strokeWidth: 2))
                      : Text(_signup ? 'Create account' : 'Sign in'),
                ),
              ),
              TextButton(
                onPressed: _busy ? null : () => setState(() => _signup = !_signup),
                child: Text(_signup ? 'Have an account? Sign in' : "New here? Create an account"),
              ),
              if (widget.onLocal != null)
                TextButton(
                  onPressed: _busy
                      ? null
                      : () async {
                          await Config.save(url.text, '');
                          widget.onLocal!();
                        },
                  child: const Text('Use a local backend (no login)'),
                ),
            ]),
          ),
        ),
      ),
    );
  }

  @override
  void dispose() {
    url.dispose();
    email.dispose();
    password.dispose();
    super.dispose();
  }
}
