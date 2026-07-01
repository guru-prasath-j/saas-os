import 'dart:async';
import 'dart:convert';
import 'package:web_socket_channel/web_socket_channel.dart';
import 'config.dart';

/// WebSocket client for the Amy (PersonalOS) backend (/ws).
/// URL + token come from [Config]. When a token is set, the backend expects it
/// as the first message, so we send {"token": ...} immediately on connect.
class AmySocket {
  WebSocketChannel? _ch;
  final _controller = StreamController<Map<String, dynamic>>.broadcast();

  Stream<Map<String, dynamic>> get stream => _controller.stream;

  void connect() {
    _ch = WebSocketChannel.connect(Uri.parse(Config.wsUrl));
    if (Config.token.isNotEmpty) {
      _ch!.sink.add(jsonEncode({'token': Config.token}));
    }
    _ch!.stream.listen((data) {
      _controller.add(jsonDecode(data as String) as Map<String, dynamic>);
    }, onError: (_) {}, onDone: () {});
  }

  /// Reconnect (e.g. after changing the server URL in Settings).
  void reconnect() {
    _ch?.sink.close();
    connect();
  }

  void ask(String text, {String channel = 'text'}) =>
      _ch?.sink.add(jsonEncode({'text': text, 'channel': channel}));

  void confirm(String id) => _ch?.sink.add(jsonEncode({'confirm': id}));

  void dispose() {
    _ch?.sink.close();
    _controller.close();
  }
}
