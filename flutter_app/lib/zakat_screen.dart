import 'package:flutter/material.dart';
import 'api.dart';

/// Zakat status — the full working, not just a number: live gold/silver
/// nisab, wealth breakdown with exclusions explained (custodial money held
/// in trust never counts), hawl progress on the Hijri calendar, and — when
/// due — one tap to park the payment in the Approval Inbox.
class ZakatScreen extends StatefulWidget {
  const ZakatScreen({super.key});
  @override
  State<ZakatScreen> createState() => _ZakatScreenState();
}

class _ZakatScreenState extends State<ZakatScreen> {
  final api = AmyApi();
  Map<String, dynamic>? report;
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
      report = await api.zakatReport();
      error = null;
    } catch (e) {
      error = '$e';
    }
    if (mounted) setState(() => loading = false);
  }

  Future<void> _propose() async {
    try {
      await api.zakatPropose();
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
          content: Text('Zakat payment parked in the Approval Inbox')));
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text('$e')));
    }
  }

  String _money(num? v) {
    if (v == null) return '—';
    final cur = report?['currency'] ?? '';
    return '$cur ${v.toStringAsFixed(2).replaceAllMapped(RegExp(r'\B(?=(\d{3})+(?!\d))'), (m) => ',')}';
  }

  Widget _section(String title, List<Widget> children) => Card(
        margin: const EdgeInsets.only(bottom: 12),
        child: Padding(
          padding: const EdgeInsets.all(14),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(title,
                  style: const TextStyle(
                      fontWeight: FontWeight.w700, fontSize: 15)),
              const SizedBox(height: 8),
              ...children,
            ],
          ),
        ),
      );

  Widget _kv(String k, String v, {Color? color}) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 3),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Expanded(
                flex: 2,
                child: Text(k,
                    style: TextStyle(fontSize: 13, color: Colors.grey[400]))),
            Expanded(
                flex: 3,
                child: Text(v,
                    style: TextStyle(fontSize: 13, color: color))),
          ],
        ),
      );

  @override
  Widget build(BuildContext context) {
    final r = report;
    final hawl = (r?['hawl'] as Map?)?.cast<String, dynamic>() ?? {};
    final wealth = (r?['wealth'] as Map?)?.cast<String, dynamic>() ?? {};
    final nisab = (r?['nisab'] as Map?)?.cast<String, dynamic>() ?? {};
    final due = r?['zakat_due_now'] == true;

    return Scaffold(
      appBar: AppBar(title: const Text('Zakat')),
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
                : ListView(
                    padding: const EdgeInsets.all(12),
                    children: [
                      // verdict banner
                      Card(
                        color: due
                            ? Colors.orange.withValues(alpha: .15)
                            : Colors.green.withValues(alpha: .10),
                        child: Padding(
                          padding: const EdgeInsets.all(16),
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(due ? 'ZAKAT DUE' : 'NO ZAKAT DUE NOW',
                                  style: TextStyle(
                                      fontWeight: FontWeight.w800,
                                      letterSpacing: 1.2,
                                      color: due
                                          ? Colors.orangeAccent
                                          : Colors.greenAccent)),
                              const SizedBox(height: 6),
                              Text(r?['verdict'] ?? '',
                                  style: const TextStyle(fontSize: 14)),
                              const SizedBox(height: 6),
                              Text(
                                  '${r?['computed_on']} · ${r?['computed_on_hijri']}',
                                  style: TextStyle(
                                      fontSize: 12, color: Colors.grey[500])),
                            ],
                          ),
                        ),
                      ),
                      const SizedBox(height: 12),
                      _section('Nisab (threshold)', [
                        _kv('Standard used', r?['threshold_standard'] ?? ''),
                        _kv('Threshold', _money(r?['threshold_used'] as num?)),
                        if (nisab['gold'] != null)
                          _kv('Gold (85g)',
                              _money((nisab['gold'] as Map)['threshold'] as num?)),
                        if (nisab['silver'] != null)
                          _kv('Silver (595g)',
                              _money((nisab['silver'] as Map)['threshold'] as num?)),
                        _kv('Price source',
                            nisab['live'] == true
                                ? 'live spot price (${(nisab['fetched_at'] ?? '').toString().split('T').first})'
                                : (nisab['note'] ?? 'reference value'),
                            color: nisab['live'] == true
                                ? Colors.greenAccent
                                : Colors.orangeAccent),
                      ]),
                      _section('Qualifying wealth', [
                        for (final a
                            in (wealth['accounts'] as List? ?? []).cast<Map>())
                          _kv('${a['account']}', _money(a['balance'] as num?)),
                        if ((wealth['investment_holdings']
                                    as Map?)?['value'] !=
                                null &&
                            ((wealth['investment_holdings'] as Map)['value']
                                    as num) >
                                0)
                          _kv('Investment holdings',
                              _money((wealth['investment_holdings']
                                  as Map)['value'] as num?)),
                        const Divider(),
                        _kv('Total', _money(wealth['total'] as num?),
                            color: Colors.white),
                        const SizedBox(height: 6),
                        for (final e
                            in (wealth['excluded'] as List? ?? []).cast<Map>())
                          _kv('${e['account']} (excluded)',
                              '${e['excluded_because']}',
                              color: Colors.grey[600]),
                      ]),
                      _section('Hawl (one lunar year above nisab)', [
                        _kv('Status', '${hawl['state'] ?? '—'}'),
                        if (hawl['crossed_nisab_on'] != null)
                          _kv('Crossed nisab',
                              '${hawl['crossed_nisab_on']} (${hawl['crossed_nisab_on_hijri']})'),
                        if (hawl['hawl_completes_on'] != null)
                          _kv('Hawl completes',
                              '${hawl['hawl_completes_on']} (${hawl['hawl_completes_on_hijri']})'),
                        if (hawl['days_remaining'] != null)
                          _kv('Days remaining', '${hawl['days_remaining']}'),
                        if (hawl['note'] != null)
                          _kv('Note', '${hawl['note']}',
                              color: Colors.grey[500]),
                      ]),
                      if (due)
                        Padding(
                          padding: const EdgeInsets.symmetric(vertical: 8),
                          child: FilledButton.icon(
                            icon: const Icon(Icons.volunteer_activism),
                            label: Text(
                                'Propose payment of ${_money(r?['estimated_liability'] as num?)}'),
                            onPressed: _propose,
                          ),
                        ),
                      Padding(
                        padding: const EdgeInsets.all(8),
                        child: Text(r?['disclaimer'] ?? '',
                            style: TextStyle(
                                fontSize: 11, color: Colors.grey[600])),
                      ),
                    ],
                  ),
      ),
    );
  }
}
