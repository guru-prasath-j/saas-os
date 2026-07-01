"""Tests for the real-time background file watcher in Engine."""
import time
import tempfile
from pathlib import Path
from amy.engine import Engine
from amy.vault import Note


def test_live_watcher_flow():
    with tempfile.TemporaryDirectory(prefix="amy_test_vault_") as tmp_dir:
        vault = Path(tmp_dir)
        
        # 1. Create initial notes
        note1_path = vault / "Note 1.md"
        note1_path.write_text("---\ntitle: Note One\ncategory: info\n---\nHello World", encoding="utf-8")
        
        note2_path = vault / "Note 2.md"
        note2_path.write_text("---\ntitle: Note Two\ncategory: resource\n---\nObsidian Brain", encoding="utf-8")
        
        # Start Engine on the temp vault
        engine = Engine(vault_path=str(vault), index_dir=str(vault / ".amy_index"))
        
        # Verify initial load
        assert len(engine.notes) == 2
        paths = {n.path for n in engine.notes}
        assert "Note 1.md" in paths
        assert "Note 2.md" in paths
        
        # 2. Add a new note dynamically
        note3_path = vault / "Note 3.md"
        note3_path.write_text("---\ntitle: Note Three\ncategory: project\n---\nGenAI app", encoding="utf-8")
        
        # Wait for watcher poll (polls every 2s)
        time.sleep(2.5)
        
        # Verify added note
        assert len(engine.notes) == 3
        paths = {n.path for n in engine.notes}
        assert "Note 3.md" in paths
        note3 = next(n for n in engine.notes if n.path == "Note 3.md")
        assert note3.title == "Note Three"
        assert note3.body == "GenAI app"
        
        # 3. Modify an existing note
        note1_path.write_text("---\ntitle: Note One\ncategory: info\n---\nHello World Modified", encoding="utf-8")
        
        time.sleep(2.5)
        
        # Verify note reload
        note1 = next(n for n in engine.notes if n.path == "Note 1.md")
        assert note1.body == "Hello World Modified"
        
        # 4. Delete a note
        note2_path.unlink()
        
        time.sleep(2.5)
        
        # Verify deleted note removal
        assert len(engine.notes) == 2
        paths = {n.path for n in engine.notes}
        assert "Note 2.md" not in paths
