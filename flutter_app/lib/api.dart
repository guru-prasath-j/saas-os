import 'dart:convert';
import 'dart:io';
import 'package:http/http.dart' as http;
import 'config.dart';

/// Talks to the Amy (PersonalOS) FastAPI backend.
/// Base URL + auth token come from [Config] (editable on the Settings screen).
class AmyApi {
  AmyApi();

  String get baseUrl => Config.baseUrl;

  Future<Map<String, dynamic>> stats() async {
    final r = await http.get(Uri.parse('$baseUrl/api/stats'), headers: Config.authHeaders());
    return jsonDecode(r.body) as Map<String, dynamic>;
  }

  Future<Map<String, dynamic>> query(String text, {String channel = 'text'}) async {
    final r = await http.post(
      Uri.parse('$baseUrl/api/query'),
      headers: Config.authHeaders({'Content-Type': 'application/json'}),
      body: jsonEncode({'text': text, 'channel': channel}),
    );
    return jsonDecode(r.body) as Map<String, dynamic>;
  }

  /// Multi-agent collaborative answer (SaaS): routes to several domain agents +
  /// planner and merges. Returns {answer, domains, sections, sources}.
  Future<Map<String, dynamic>> collabAsk(String text) async {
    final r = await http.post(
      Uri.parse('$baseUrl/api/collab/ask'),
      headers: Config.authHeaders({'Content-Type': 'application/json'}),
      body: jsonEncode({'text': text}),
    );
    if (r.statusCode >= 400) throw Exception('collab ${r.statusCode}');
    return jsonDecode(r.body) as Map<String, dynamic>;
  }

  // --- goals (collaboration) ------------------------------------------------
  Future<List<Map<String, dynamic>>> listGoals() async {
    final r = await http.get(Uri.parse('$baseUrl/api/goals'), headers: Config.authHeaders());
    final d = jsonDecode(r.body) as Map<String, dynamic>;
    return (d['goals'] as List).cast<Map<String, dynamic>>();
  }

  Future<void> createGoal(String title, {String domain = 'general'}) async {
    await http.post(Uri.parse('$baseUrl/api/goals'),
        headers: Config.authHeaders({'Content-Type': 'application/json'}),
        body: jsonEncode({'title': title, 'domain': domain}));
  }

  Future<void> addMilestone(String goalId, String title) async {
    await http.post(Uri.parse('$baseUrl/api/goals/$goalId/milestones'),
        headers: Config.authHeaders({'Content-Type': 'application/json'}),
        body: jsonEncode({'title': title}));
  }

  Future<void> completeMilestone(String milestoneId, bool done) async {
    await http.post(Uri.parse('$baseUrl/api/milestones/$milestoneId/complete?done=$done'),
        headers: Config.authHeaders());
  }

  /// Confirm a pending write proposal (personal backend only; SaaS agents are
  /// read-only so this is rarely used). Safe to call; errors are surfaced.
  Future<Map<String, dynamic>> confirm(String id) async {
    final r = await http.post(
      Uri.parse('$baseUrl/api/confirm'),
      headers: Config.authHeaders({'Content-Type': 'application/json'}),
      body: jsonEncode({'id': id}),
    );
    return jsonDecode(r.body) as Map<String, dynamic>;
  }

  // --- auth (SaaS) ----------------------------------------------------------
  Future<Map<String, dynamic>> _auth(String path, String email, String password) async {
    final r = await http.post(
      Uri.parse('$baseUrl$path'),
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({'email': email, 'password': password}),
    );
    final body = jsonDecode(r.body) as Map<String, dynamic>;
    if (r.statusCode >= 400) {
      throw Exception(body['detail'] ?? 'auth failed (${r.statusCode})');
    }
    return body;
  }

  Future<Map<String, dynamic>> signup(String email, String password) =>
      _auth('/auth/signup', email, password);

  Future<Map<String, dynamic>> login(String email, String password) =>
      _auth('/auth/login', email, password);

  Future<Map<String, dynamic>> me() async {
    final r = await http.get(Uri.parse('$baseUrl/api/me'), headers: Config.authHeaders());
    return jsonDecode(r.body) as Map<String, dynamic>;
  }

  Future<void> setOpenAiKey(String key) async {
    final r = await http.post(
      Uri.parse('$baseUrl/api/settings/openai-key'),
      headers: Config.authHeaders({'Content-Type': 'application/json'}),
      body: jsonEncode({'key': key}),
    );
    if (r.statusCode >= 400) throw Exception('failed to save key (${r.statusCode})');
  }

  Future<void> setPrivateFolders(List<String> folders) async {
    final r = await http.put(
      Uri.parse('$baseUrl/api/settings/private-folders'),
      headers: Config.authHeaders({'Content-Type': 'application/json'}),
      body: jsonEncode({'folders': folders}),
    );
    if (r.statusCode >= 400) throw Exception('failed to save folders (${r.statusCode})');
  }

  /// Upload a photo to be ingested into the vault (08_Captures/) and indexed.
  /// [linkDisbursementTxn] optionally attaches this image (e.g. a UPI/NEFT
  /// confirmation screenshot) to a specific custodial disbursement transaction.
  Future<Map<String, dynamic>> uploadCapture(
    File image, {
    double? lat,
    double? lon,
    String? takenAt,
    String source = 'mobile',
    String note = '',
    String tags = '',
    String? linkDisbursementTxn,
  }) async {
    final req = http.MultipartRequest('POST', Uri.parse('$baseUrl/api/captures'));
    req.headers.addAll(Config.authHeaders());
    req.files.add(await http.MultipartFile.fromPath('file', image.path));
    if (lat != null) req.fields['lat'] = lat.toString();
    if (lon != null) req.fields['lon'] = lon.toString();
    if (takenAt != null) req.fields['taken_at'] = takenAt;
    req.fields['source'] = source;
    if (note.isNotEmpty) req.fields['note'] = note;
    if (tags.isNotEmpty) req.fields['tags'] = tags;
    if (linkDisbursementTxn != null) {
      req.fields['link_disbursement_txn'] = linkDisbursementTxn;
    }

    final streamed = await req.send();
    final body = await streamed.stream.bytesToString();
    if (streamed.statusCode >= 400) {
      throw Exception('Upload failed (${streamed.statusCode}): $body');
    }
    return jsonDecode(body) as Map<String, dynamic>;
  }

  /// First custodial-type finance account, if any — used by share_handler.dart
  /// to decide whether to offer the "attach to disbursement" picker at all.
  Future<String?> custodialAccountId() async {
    final r = await http.get(Uri.parse('$baseUrl/api/finance/accounts'), headers: Config.authHeaders());
    if (r.statusCode >= 400) return null;
    final d = jsonDecode(r.body) as Map<String, dynamic>;
    final accounts = (d['accounts'] as List? ?? []).cast<Map<String, dynamic>>();
    for (final a in accounts) {
      if (a['account_type'] == 'custodial') return a['id'] as String;
    }
    return null;
  }

  /// Recently-confirmed custodial disbursements for a beneficiary picker
  /// when a screenshot is shared in (share_handler.dart). Returns
  /// [{beneficiary_id, name, last_amount, last_date}, ...] for beneficiaries
  /// with a logged transfer but no screenshot attached yet — pass the
  /// custodial account id.
  Future<List<Map<String, dynamic>>> custodialPendingScreenshots(String accountId) async {
    final r = await http.get(
      Uri.parse('$baseUrl/api/finance/custodial/$accountId/next-cycle-prefill'),
      headers: Config.authHeaders(),
    );
    if (r.statusCode >= 400) return [];
    final d = jsonDecode(r.body) as Map<String, dynamic>;
    final beneficiaries = (d['beneficiaries'] as List? ?? []).cast<Map<String, dynamic>>();
    return beneficiaries
        .where((b) => b['transaction_id'] != null && b['has_screenshot'] != true)
        .toList();
  }

  /// Recent capture notes (newest first).
  Future<List<Map<String, dynamic>>> listCaptures({int limit = 50}) async {
    final r = await http.get(
      Uri.parse('$baseUrl/api/captures?limit=$limit'),
      headers: Config.authHeaders(),
    );
    final data = jsonDecode(r.body) as Map<String, dynamic>;
    return (data['captures'] as List).cast<Map<String, dynamic>>();
  }

  /// Full URL to a stored capture image (vault-relative [path]).
  String imageUrl(String path) =>
      '$baseUrl/api/captures/image?path=${Uri.encodeQueryComponent(path)}';

  // --- agent approvals (AI governance: human approves every agent write) ---

  /// Pending Approval Inbox items — actions agents proposed and are waiting on.
  Future<List<Map<String, dynamic>>> pendingApprovals() async {
    final r = await http.get(
      Uri.parse('$baseUrl/api/automation/approvals?status=pending'),
      headers: Config.authHeaders(),
    );
    if (r.statusCode >= 400) throw Exception('approvals ${r.statusCode}');
    final d = jsonDecode(r.body) as Map<String, dynamic>;
    return (d['approvals'] as List? ?? []).cast<Map<String, dynamic>>();
  }

  /// verb: 'approve' (executes the parked action) or 'reject'.
  Future<Map<String, dynamic>> decideApproval(String id, String verb) async {
    final r = await http.post(
      Uri.parse('$baseUrl/api/automation/approvals/$id/$verb'),
      headers: Config.authHeaders({'Content-Type': 'application/json'}),
      body: '{}',
    );
    final d = jsonDecode(r.body) as Map<String, dynamic>;
    if (r.statusCode >= 400) throw Exception(d['detail'] ?? 'HTTP ${r.statusCode}');
    return d;
  }

  // --- zakat (live nisab + hawl on the Hijri calendar) ----------------------

  Future<Map<String, dynamic>> zakatReport() async {
    final r = await http.get(Uri.parse('$baseUrl/api/obligations/zakat'),
        headers: Config.authHeaders());
    final d = jsonDecode(r.body) as Map<String, dynamic>;
    if (r.statusCode >= 400) throw Exception(d['detail'] ?? 'HTTP ${r.statusCode}');
    return d;
  }

  /// Parks the computed zakat payment in the Approval Inbox.
  Future<Map<String, dynamic>> zakatPropose() async {
    final r = await http.post(Uri.parse('$baseUrl/api/obligations/zakat/propose'),
        headers: Config.authHeaders({'Content-Type': 'application/json'}));
    final d = jsonDecode(r.body) as Map<String, dynamic>;
    if (r.statusCode >= 400) throw Exception(d['detail'] ?? 'HTTP ${r.statusCode}');
    return d;
  }
}
