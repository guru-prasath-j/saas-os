import 'package:flutter/material.dart';
import 'api.dart';

/// Approval Inbox — every write an agent wants to make waits here with its
/// reasoning. Approving from the phone is the whole human-in-the-loop
/// governance model in one screen: AI proposes, human decides.
class ApprovalsScreen extends StatefulWidget {
  const ApprovalsScreen({super.key});
  @override
  State<ApprovalsScreen> createState() => _ApprovalsScreenState();
}

class _ApprovalsScreenState extends State<ApprovalsScreen> {
  final api = AmyApi();
  List<Map<String, dynamic>> items = [];
  bool loading = true;
  String? error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() => loading = true);
    try {
      items = await api.pendingApprovals();
      error = null;
    } catch (e) {
      error = '$e';
    }
    if (mounted) setState(() => loading = false);
  }

  Future<void> _decide(Map<String, dynamic> a, String verb) async {
    try {
      final d = await api.decideApproval(a['id'] as String, verb);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text(verb == 'approve'
              ? 'Approved — ${d['status'] ?? 'executed'}'
              : 'Rejected')));
      _load();
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text('$e')));
    }
  }

  Color _riskColor(String? risk) => switch (risk) {
        'destructive' => Colors.redAccent,
        'write' => Colors.orangeAccent,
        _ => Colors.greenAccent,
      };

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Approval Inbox')),
      body: RefreshIndicator(
        onRefresh: _load,
        child: loading
            ? const Center(child: CircularProgressIndicator())
            : error != null
                ? ListView(children: [
                    Padding(
                        padding: const EdgeInsets.all(24),
                        child: Text(error!, textAlign: TextAlign.center))
                  ])
                : items.isEmpty
                    ? ListView(children: const [
                        Padding(
                          padding: EdgeInsets.all(32),
                          child: Text(
                              'Nothing waiting.\n\nAgents park every proposed '
                              'change here — nothing runs until you approve it.',
                              textAlign: TextAlign.center),
                        )
                      ])
                    : ListView.builder(
                        padding: const EdgeInsets.all(12),
                        itemCount: items.length,
                        itemBuilder: (_, i) {
                          final a = items[i];
                          final payload = a['payload'];
                          return Card(
                            margin: const EdgeInsets.only(bottom: 12),
                            child: Padding(
                              padding: const EdgeInsets.all(14),
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  Row(children: [
                                    Expanded(
                                        child: Text(a['title'] ?? '',
                                            style: const TextStyle(
                                                fontWeight: FontWeight.w600))),
                                    Container(
                                      padding: const EdgeInsets.symmetric(
                                          horizontal: 8, vertical: 2),
                                      decoration: BoxDecoration(
                                        border: Border.all(
                                            color: _riskColor(
                                                a['risk'] as String?)),
                                        borderRadius: BorderRadius.circular(99),
                                      ),
                                      child: Text(a['risk'] ?? 'write',
                                          style: TextStyle(
                                              fontSize: 11,
                                              color: _riskColor(
                                                  a['risk'] as String?))),
                                    ),
                                  ]),
                                  if ((a['reasoning'] ?? a['body']) != null) ...[
                                    const SizedBox(height: 8),
                                    Text('Why: ${a['reasoning'] ?? a['body']}',
                                        style: TextStyle(
                                            fontSize: 13,
                                            color: Colors.grey[400])),
                                  ],
                                  if (payload is Map && payload['tool'] != null)
                                    Padding(
                                      padding: const EdgeInsets.only(top: 6),
                                      child: Text('${payload['tool']}',
                                          style: TextStyle(
                                              fontSize: 12,
                                              fontFamily: 'monospace',
                                              color: Colors.grey[500])),
                                    ),
                                  const SizedBox(height: 6),
                                  Text(
                                      'from ${a['source'] ?? 'agent'} · '
                                      '${(a['created_at'] ?? '').toString().replaceFirst('T', ' ').split('.').first}',
                                      style: TextStyle(
                                          fontSize: 11,
                                          color: Colors.grey[600])),
                                  const SizedBox(height: 10),
                                  Row(children: [
                                    Expanded(
                                      child: FilledButton.icon(
                                        icon: const Icon(Icons.check, size: 18),
                                        label: const Text('Approve'),
                                        onPressed: () => _decide(a, 'approve'),
                                      ),
                                    ),
                                    const SizedBox(width: 10),
                                    Expanded(
                                      child: OutlinedButton.icon(
                                        icon: const Icon(Icons.close, size: 18),
                                        label: const Text('Reject'),
                                        onPressed: () => _decide(a, 'reject'),
                                      ),
                                    ),
                                  ]),
                                ],
                              ),
                            ),
                          );
                        },
                      ),
      ),
    );
  }
}
