"""分析剩余标签，找出可合并的变体对。"""
import frontmatter as fm
import pathlib
from collections import Counter

tags = Counter()
for p in pathlib.Path('D:/my_kb/processed').glob('*.md'):
    tags.update(fm.loads(p.read_text(encoding='utf-8')).metadata.get('tags', []))

sorted_tags = sorted(tags.items(), key=lambda x: -x[1])

print(f"Total unique tags: {len(tags)}")
print(f"Single-use: {sum(1 for v in tags.values() if v==1)}")
print()

# 1. 共享前缀的标签 (3+ chars prefix, at least 2 tags share it)
from collections import defaultdict
by_prefix = defaultdict(list)
for t, c in sorted_tags:
    for prefix_len in range(3, min(len(t), 8)):
        p = t[:prefix_len]
        by_prefix[p].append((t, c))

print("=== 共享前缀 >= 3 个标签 ===")
for prefix, items in sorted(by_prefix.items(), key=lambda x: -len(x[1])):
    if len(items) >= 3 and max(c for _,c in items) >= 2:
        print(f"  [{prefix}] ({len(items)}个): {dict(items)}")
print()

# 2. 包含相同核心词的标签
keywords = ['交易','投资','策略','分析','基金','股票','指数','趋势','操作','管理','系统','模型','因子','周期']
print("=== 包含相同核心词的标签 (合并建议) ===")
for kw in keywords:
    related = [(t,c) for t,c in sorted_tags if kw in t]
    if len(related) >= 3:
        top = max(related, key=lambda x: x[1])
        rest = [t for t,c in related if t != top[0]]
        print(f"  [{kw}] 核心: {top[0]}({top[1]}) 变体: {rest}")

# 3. 同义词对检测 (word-level overlap)
import jieba
print()
print("=== 分词重叠建议 ===")
seen = set()
for t1, c1 in sorted_tags:
    if t1 in seen: continue
    w1 = set(jieba.lcut(t1))
    if not w1: continue
    for t2, c2 in sorted_tags:
        if t2 in seen or t2 == t1: continue
        w2 = set(jieba.lcut(t2))
        if not w2: continue
        overlap = len(w1 & w2) / max(len(w1 | w2), 1)
        if overlap >= 0.6 and (c1 >= 2 or c2 >= 2):
            print(f"  {t1} ~ {t2} (overlap={overlap:.2f}, freq={c1}/{c2})")
            seen.add(t1)
            seen.add(t2)
            break
