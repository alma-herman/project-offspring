"""memory.py full test suite — Tick 9"""
import sys
sys.path.insert(0, "/home/hermine/workspace/project_offspring")
import offspring.memory as memory
import os

TEST_DB = "/tmp/fen_memory_test.db"
TEST_DB2 = "/tmp/fen_memory_test2.db"

# Ensure clean state
for p in (TEST_DB, TEST_DB2):
    if os.path.exists(p):
        os.unlink(p)

db = memory.connect(TEST_DB)

# 1. store + get_recent
memory.store(db, [
    {'content': 'Alpha fact', 'importance': 7, 'context': 'test'},
    {'content': 'Beta fact', 'importance': 3, 'context': 'test'},
    {'content': 'Gamma fact', 'importance': 9, 'tags': 'pinned,critical'},
], 'sess001')

recent = memory.get_recent(db, limit=5)
assert len(recent) == 3, f"Expected 3 recent, got {len(recent)}"
contents = {r['content'] for r in recent}
assert 'Alpha fact' in contents and 'Beta fact' in contents and 'Gamma fact' in contents
print("1. get_recent: PASS")

# 2. get_important — Gamma first (importance=9)
important = memory.get_important(db, limit=3)
assert important[0]['content'] == 'Gamma fact', f"Top importance should be Gamma, got: {important[0]['content']}"
assert important[0]['importance'] == 9
print("2. get_important: PASS")

# 3. search by content
results = memory.search(db, 'Alpha', limit=5)
assert len(results) == 1, f"Expected 1, got {len(results)}"
assert results[0]['content'] == 'Alpha fact'

# search by tag
results_tag = memory.search(db, 'pinned', limit=5)
assert any(r['content'] == 'Gamma fact' for r in results_tag), "Tag search failed"
print("3. search: PASS")

# 4. get_session
session_mems = memory.get_session(db, 'sess001')
assert len(session_mems) == 3, f"Expected 3, got {len(session_mems)}"
print("4. get_session: PASS")

# 5. second session isolation
memory.store(db, [{'content': 'Delta fact', 'importance': 6}], 'sess002')
s1 = memory.get_session(db, 'sess001')
s2 = memory.get_session(db, 'sess002')
assert len(s1) == 3
assert len(s2) == 1
print("5. session isolation: PASS")

# 6. dict shape completeness
r = memory.get_recent(db, limit=1)[0]
for key in ('id', 'content', 'context', 'importance', 'tags', 'session_id', 'created_at', 'source'):
    assert key in r, f"Missing key: {key}"
print("6. dict shape: PASS")

# 7. Prescribed test from CURRENT_STATE.md (verbatim)
db2 = memory.connect(TEST_DB2)
memory.store(db2, [{'content': 'Test fact', 'importance': 7, 'context': 'test'}], 'sess001')
recent2 = memory.get_recent(db2, limit=5)
assert any(m['content'] == 'Test fact' for m in recent2), "Store/retrieve failed"
results2 = memory.search(db2, 'Test', limit=5)
assert any(m['content'] == 'Test fact' for m in results2), "Search failed"
print("7. prescribed test: PASS")

# Cleanup
for p in (TEST_DB, TEST_DB2):
    if os.path.exists(p):
        os.unlink(p)

print("\nmemory.py tests pass")
