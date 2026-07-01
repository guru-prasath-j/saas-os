import 'package:shared_preferences/shared_preferences.dart';

/// App-wide connection config: backend base URL + optional auth token.
/// Persisted with shared_preferences and edited on the Settings screen.
///
/// Defaults to 10.0.2.2 (the host machine as seen from the Android emulator).
/// On a real phone, set this to your backend's LAN IP or public/Tailscale URL
/// on the Settings screen, e.g. http://192.168.1.20:8848
class Config {
  static String baseUrl = 'http://10.0.2.2:8848';
  static String token = '';

  static Future<void> load() async {
    final p = await SharedPreferences.getInstance();
    baseUrl = p.getString('baseUrl') ?? baseUrl;
    token = p.getString('token') ?? '';
  }

  static Future<void> save(String url, String tok) async {
    baseUrl = url.trim().replaceAll(RegExp(r'/+$'), '');
    token = tok.trim();
    final p = await SharedPreferences.getInstance();
    await p.setString('baseUrl', baseUrl);
    await p.setString('token', token);
  }

  static bool get isLoggedIn => token.isNotEmpty;

  static Future<void> setToken(String tok) async {
    token = tok.trim();
    final p = await SharedPreferences.getInstance();
    await p.setString('token', token);
  }

  static Future<void> logout() async {
    token = '';
    final p = await SharedPreferences.getInstance();
    await p.remove('token');
  }

  static String get wsUrl {
    final ws = baseUrl.replaceFirst('https://', 'wss://').replaceFirst('http://', 'ws://');
    return '$ws/ws';
  }

  static Map<String, String> authHeaders([Map<String, String>? base]) {
    final h = <String, String>{...?base};
    if (token.isNotEmpty) h['Authorization'] = 'Bearer $token';
    return h;
  }
}
