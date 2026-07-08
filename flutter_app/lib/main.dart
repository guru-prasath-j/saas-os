import 'package:flutter/material.dart';
import 'package:speech_to_text/speech_to_text.dart';
import 'package:flutter_tts/flutter_tts.dart';
import 'api.dart';
import 'config.dart';
import 'capture_screen.dart';
import 'captures_screen.dart';
import 'glasses_live_screen.dart';
import 'settings_screen.dart';
import 'gallery_sync.dart';
import 'share_handler.dart';
import 'login_screen.dart';
import 'account_screen.dart';
import 'goals_screen.dart';
import 'approvals_screen.dart';
import 'zakat_screen.dart';
import 'package:flutter_markdown/flutter_markdown.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await Config.load();
  runApp(const AmyApp());
}

class AmyApp extends StatelessWidget {
  const AmyApp({super.key});
  @override
  Widget build(BuildContext context) => MaterialApp(
        title: 'Amy',
        theme: ThemeData.dark(useMaterial3: true)
            .copyWith(scaffoldBackgroundColor: const Color(0xFF0E1116)),
        home: const Root(),
      );
}

/// Decides between the login screen and the app based on auth state.
class Root extends StatefulWidget {
  const Root({super.key});
  @override
  State<Root> createState() => _RootState();
}

class _RootState extends State<Root> {
  bool _entered = Config.isLoggedIn;

  @override
  Widget build(BuildContext context) {
    if (_entered) {
      return HomePage(onLogout: () => setState(() => _entered = false));
    }
    return LoginScreen(
      onLoggedIn: () => setState(() => _entered = true),
      onLocal: () => setState(() => _entered = true),
    );
  }
}

class Msg {
  Msg(this.who, this.text, {this.meta = '', this.confirmId, this.sources = const []});
  final String who, text, meta;
  final List<String> sources;
  final String? confirmId; // if set, render a Confirm button
}

class HomePage extends StatefulWidget {
  const HomePage({super.key, this.onLogout});
  final VoidCallback? onLogout;
  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  final api = AmyApi();
  final stt = SpeechToText();
  final tts = FlutterTts();
  final input = TextEditingController();
  final msgs = <Msg>[];
  Map<String, dynamic> stats = {};
  bool sttReady = false, listening = false, lastSpoken = false;

  @override
  void initState() {
    super.initState();
    _init();
  }

  Future<void> _init() async {
    sttReady = await stt.initialize();
    try {
      stats = await api.stats(); // REST just for the header cards
    } catch (_) {}
    setState(() {});
    // capture mode C: photos shared into the app
    ShareHandler.init(
      onUploaded: (up, skip) {
        if (mounted && up > 0) {
          ScaffoldMessenger.of(context)
              .showSnackBar(SnackBar(content: Text('Shared $up photo(s) to Amy')));
        }
      },
      onPickDisbursement: (pending) => _pickDisbursementForScreenshot(pending),
    );
    // capture mode A: auto-ingest new gallery photos (if enabled in Gallery sync)
    GallerySync.maybeAutoSync();
  }

  /// Shown when a screenshot is shared in and there's at least one recently-
  /// confirmed custodial disbursement without a screenshot yet. Returns the
  /// chosen beneficiary's transaction_id, or null if the user picks "Skip".
  Future<String?> _pickDisbursementForScreenshot(
      List<Map<String, dynamic>> pending) async {
    if (!mounted) return null;
    return showModalBottomSheet<String?>(
      context: context,
      builder: (ctx) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Padding(
              padding: EdgeInsets.all(16),
              child: Text('Attach this screenshot to:',
                  style: TextStyle(fontWeight: FontWeight.bold)),
            ),
            for (final b in pending)
              ListTile(
                title: Text(b['name'] as String? ?? ''),
                subtitle: b['last_amount'] != null
                    ? Text('₹${b['last_amount']} · ${b['last_date'] ?? ''}')
                    : null,
                onTap: () => Navigator.pop(ctx, b['transaction_id'] as String?),
              ),
            ListTile(
              title: const Text('Skip — just save the photo'),
              onTap: () => Navigator.pop(ctx, null),
            ),
          ],
        ),
      ),
    );
  }

  void _onReply(Map<String, dynamic> r) {
    final conf = r['confidence'];
    final meta =
        '${r['route'] ?? r['intent']} · ${r['model']}${conf != null ? ' · ${conf}%' : ''}${r['sensitive'] == true ? ' · 🔒' : ''}';
    final needsConfirm = r['needs_confirmation'] == true && r['proposal'] != null;
    final sources = ((r['sources'] as List?) ?? []).map((e) => e.toString()).toList();
    setState(() => msgs.add(Msg('amy', r['answer'] ?? '',
        meta: meta, sources: sources,
        confirmId: needsConfirm ? r['proposal']['id'] as String : null)));
    if (lastSpoken) {
      tts.speak((r['voice_safe'] ?? r['answer'] ?? '') as String); // redacted for voice
    }
  }

  Future<void> _send(String text, {bool spoken = false}) async {
    if (text.trim().isEmpty) return;
    lastSpoken = spoken;
    setState(() => msgs.add(Msg('you', text)));
    input.clear();
    // Prefer the collaborative multi-agent path; fall back to /api/query
    // (personal backend, or if collab isn't available).
    try {
      final r = await api.collabAsk(text);
      _onReply(_fromCollab(r));
    } catch (e) {
      // 401 / expired session -> bounce to login
      if (e.toString().contains('401') || e.toString().toLowerCase().contains('unauthor')) {
        await Config.logout();
        widget.onLogout?.call();
        return;
      }
      try {
        _onReply(await api.query(text, channel: spoken ? 'voice' : 'text'));
      } catch (e2) {
        setState(() => msgs.add(Msg('amy', 'Error: $e2')));
      }
    }
  }

  /// Normalize a collaborative response into the shape _onReply expects.
  Map<String, dynamic> _fromCollab(Map<String, dynamic> r) {
    final domains = (r['domains'] as List?)?.join(', ') ?? '';
    return {
      'answer': r['answer'] ?? '',
      'route': 'collab',
      'model': domains,           // shows which agents collaborated
      'sensitive': false,
      'voice_safe': r['answer'] ?? '',
      'needs_confirmation': false,
      'proposal': null,
      'sources': r['sources'] ?? [],
    };
  }

  Future<void> _mic() async {
    if (!sttReady) return;
    if (listening) {
      await stt.stop();
      setState(() => listening = false);
      return;
    }
    setState(() => listening = true);
    await stt.listen(onResult: (res) {
      if (res.finalResult) {
        setState(() => listening = false);
        _send(res.recognizedWords, spoken: true);
      }
    });
  }

  Widget _statCards() {
    final by = (stats['by_category'] as Map?) ?? {};
    final cards = <Widget>[
      _card('Notes', '${stats['notes'] ?? '–'}'),
      ...by.entries.map((e) => _card(e.key, '${e.value}')),
    ];
    return SizedBox(
      height: 78,
      child: ListView(scrollDirection: Axis.horizontal, padding: const EdgeInsets.all(8), children: cards),
    );
  }

  Widget _card(String h, String v) => Container(
        width: 110,
        margin: const EdgeInsets.symmetric(horizontal: 4),
        padding: const EdgeInsets.all(10),
        decoration: BoxDecoration(
            color: const Color(0xFF171B22),
            borderRadius: BorderRadius.circular(12),
            border: Border.all(color: const Color(0xFF262C36))),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(h, style: const TextStyle(fontSize: 11, color: Colors.white54)),
          const Spacer(),
          Text(v, style: const TextStyle(fontSize: 22, fontWeight: FontWeight.bold)),
        ]),
      );

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Amy'),
        actions: [
          IconButton(
            tooltip: 'New capture',
            icon: const Icon(Icons.camera_alt),
            onPressed: () async {
              await Navigator.push(context, MaterialPageRoute(builder: (_) => const CaptureScreen()));
              stats = await api.stats();
              if (mounted) setState(() {});
            },
          ),
          IconButton(
            tooltip: 'Captures',
            icon: const Icon(Icons.photo_library),
            onPressed: () =>
                Navigator.push(context, MaterialPageRoute(builder: (_) => const CapturesScreen())),
          ),
          // Feature-flagged (Settings → Experimental): Meta glasses live
          // capture via the Wearables Device Access Toolkit. Flag OFF
          // (default) = this button doesn't exist and nothing else changes.
          if (Config.glassesLiveCapture)
            IconButton(
              tooltip: 'Glasses live capture',
              icon: const Icon(Icons.remove_red_eye_outlined),
              onPressed: () => Navigator.push(context,
                  MaterialPageRoute(builder: (_) => const GlassesLiveScreen())),
            ),
          IconButton(
            tooltip: 'Gallery sync',
            icon: const Icon(Icons.sync),
            onPressed: () async {
              await Navigator.push(context, MaterialPageRoute(builder: (_) => const GallerySyncScreen()));
              stats = await api.stats();
              if (mounted) setState(() {});
            },
          ),
          IconButton(
            tooltip: 'Account',
            icon: const Icon(Icons.account_circle),
            onPressed: () => Navigator.push(
              context,
              MaterialPageRoute(
                builder: (_) => AccountScreen(
                  onLoggedOut: () {
                    Navigator.pop(context);
                    widget.onLogout?.call();
                  },
                ),
              ),
            ),
          ),
          IconButton(
            tooltip: 'Approvals — agent actions waiting on you',
            icon: const Icon(Icons.fact_check),
            onPressed: () => Navigator.push(context,
                MaterialPageRoute(builder: (_) => const ApprovalsScreen())),
          ),
          IconButton(
            tooltip: 'Zakat',
            icon: const Icon(Icons.volunteer_activism),
            onPressed: () => Navigator.push(context,
                MaterialPageRoute(builder: (_) => const ZakatScreen())),
          ),
          IconButton(
            tooltip: 'Goals',
            icon: const Icon(Icons.flag),
            onPressed: () => Navigator.push(
                context, MaterialPageRoute(builder: (_) => const GoalsScreen())),
          ),
          IconButton(
            tooltip: 'Settings',
            icon: const Icon(Icons.settings),
            onPressed: () async {
              await Navigator.push(context, MaterialPageRoute(builder: (_) => const SettingsScreen()));
              try {
                stats = await api.stats();
              } catch (_) {}
              if (mounted) setState(() {});
            },
          ),
        ],
        bottom: PreferredSize(
          preferredSize: const Size.fromHeight(22),
          child: Text(stats.isEmpty ? 'connecting…' : '${stats['notes']} notes · ${stats['index_backend']} index'),
        ),
      ),
      body: Column(children: [
        _statCards(),
        const Divider(height: 1),
        Expanded(
          child: ListView.builder(
            padding: const EdgeInsets.all(12),
            itemCount: msgs.length,
            itemBuilder: (_, i) {
              final m = msgs[i];
              final me = m.who == 'you';
              return Align(
                alignment: me ? Alignment.centerRight : Alignment.centerLeft,
                child: Container(
                  margin: const EdgeInsets.symmetric(vertical: 4),
                  padding: const EdgeInsets.all(10),
                  decoration: BoxDecoration(
                    color: me ? const Color(0xFF4F8CFF) : const Color(0xFF171B22),
                    borderRadius: BorderRadius.circular(10),
                  ),
                  child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    me
                        ? Text(m.text)
                        : MarkdownBody(
                            data: m.text.isEmpty ? '_(no answer)_' : m.text,
                            shrinkWrap: true,
                          ),
                    if (m.sources.isNotEmpty)
                      Padding(
                        padding: const EdgeInsets.only(top: 6),
                        child: Wrap(
                          spacing: 4, runSpacing: 4,
                          children: m.sources
                              .map((s) => Chip(
                                    label: Text(s.split('/').last,
                                        style: const TextStyle(fontSize: 10)),
                                    visualDensity: VisualDensity.compact,
                                    materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
                                  ))
                              .toList(),
                        ),
                      ),
                    if (m.meta.isNotEmpty)
                      Padding(
                        padding: const EdgeInsets.only(top: 4),
                        child: Text(m.meta, style: const TextStyle(fontSize: 11, color: Colors.white54)),
                      ),
                    if (m.confirmId != null)
                      Padding(
                        padding: const EdgeInsets.only(top: 8),
                        child: ElevatedButton(
                          onPressed: () async {
                            try {
                              _onReply(await api.confirm(m.confirmId!));
                            } catch (_) {}
                          },
                          child: const Text('Confirm change'),
                        ),
                      ),
                  ]),
                ),
              );
            },
          ),
        ),
        SafeArea(
          child: Padding(
            padding: const EdgeInsets.all(10),
            child: Row(children: [
              IconButton(
                onPressed: _mic,
                icon: Icon(listening ? Icons.mic : Icons.mic_none, color: listening ? Colors.red : null),
              ),
              Expanded(
                child: TextField(
                  controller: input,
                  onSubmitted: _send,
                  decoration: const InputDecoration(hintText: 'Ask Amy…'),
                ),
              ),
              IconButton(onPressed: () => _send(input.text), icon: const Icon(Icons.send)),
            ]),
          ),
        ),
      ]),
    );
  }

  @override
  void dispose() {
    ShareHandler.dispose();
    super.dispose();
  }
}
