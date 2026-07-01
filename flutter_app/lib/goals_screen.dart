import 'package:flutter/material.dart';
import 'api.dart';

/// Goals + milestones + progress (Planner). Mirrors the web Goals tab.
class GoalsScreen extends StatefulWidget {
  const GoalsScreen({super.key});
  @override
  State<GoalsScreen> createState() => _GoalsScreenState();
}

class _GoalsScreenState extends State<GoalsScreen> {
  final api = AmyApi();
  final _title = TextEditingController();
  late Future<List<Map<String, dynamic>>> _future;

  @override
  void initState() {
    super.initState();
    _future = api.listGoals();
  }

  void _refresh() => setState(() => _future = api.listGoals());

  Future<void> _create() async {
    if (_title.text.trim().isEmpty) return;
    await api.createGoal(_title.text.trim());
    _title.clear();
    _refresh();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Goals')),
      body: Column(children: [
        Padding(
          padding: const EdgeInsets.all(12),
          child: Row(children: [
            Expanded(child: TextField(controller: _title,
                decoration: const InputDecoration(hintText: 'New goal', border: OutlineInputBorder()))),
            const SizedBox(width: 8),
            FilledButton(onPressed: _create, child: const Text('Add')),
          ]),
        ),
        Expanded(
          child: FutureBuilder<List<Map<String, dynamic>>>(
            future: _future,
            builder: (_, snap) {
              if (snap.connectionState != ConnectionState.done) {
                return const Center(child: CircularProgressIndicator());
              }
              final goals = snap.data ?? [];
              if (goals.isEmpty) return const Center(child: Text('No goals yet.'));
              return ListView(
                padding: const EdgeInsets.all(12),
                children: goals.map(_goalCard).toList(),
              );
            },
          ),
        ),
      ]),
    );
  }

  Widget _goalCard(Map<String, dynamic> g) {
    final progress = (g['progress'] ?? 0).toDouble();
    final milestones = (g['milestones'] as List?) ?? [];
    final msCtl = TextEditingController();
    return Card(
      color: const Color(0xFF171B22),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text('${g['title']}  ·  ${g['domain']} · ${g['status']}',
              style: const TextStyle(fontWeight: FontWeight.bold)),
          const SizedBox(height: 6),
          LinearProgressIndicator(value: progress / 100),
          Text('${progress.toStringAsFixed(0)}%', style: const TextStyle(color: Colors.white54, fontSize: 12)),
          ...milestones.map((m) => Row(children: [
                Icon(m['done'] == 1 ? Icons.check_box : Icons.check_box_outline_blank, size: 18),
                const SizedBox(width: 6),
                Expanded(child: Text(m['title'] ?? '')),
                TextButton(
                  onPressed: () async {
                    await api.completeMilestone(m['id'], m['done'] != 1);
                    _refresh();
                  },
                  child: Text(m['done'] == 1 ? 'undo' : 'done'),
                ),
              ])),
          Row(children: [
            Expanded(child: TextField(controller: msCtl,
                decoration: const InputDecoration(hintText: 'new milestone', isDense: true))),
            IconButton(
              icon: const Icon(Icons.add),
              onPressed: () async {
                if (msCtl.text.trim().isEmpty) return;
                await api.addMilestone(g['id'], msCtl.text.trim());
                _refresh();
              },
            ),
          ]),
        ]),
      ),
    );
  }

  @override
  void dispose() {
    _title.dispose();
    super.dispose();
  }
}
