# -*- coding: utf-8 -*-
"""体检：重复记忆的量级（精确重复 / 前缀重复 / 同 session 连写）"""
import psycopg

c = psycopg.connect("postgresql://postgres@127.0.0.1:5432/hermes_data")
cur = c.cursor()

cur.execute("SELECT count(*), count(DISTINCT content) FROM boshi.memories WHERE is_latest")
n, u = cur.fetchone()
print("latest 总条数 %d / 去重正文 %d / 精确重复 %d 条" % (n, u, n - u))

cur.execute("""
  SELECT count(*) FROM (
    SELECT left(content,120) p, count(*) k FROM boshi.memories
    WHERE is_latest GROUP BY 1 HAVING count(*) > 1
  ) t""")
print("前120字相同的组数:", cur.fetchone()[0])

cur.execute("""
  SELECT sum(k) FROM (
    SELECT left(content,120) p, count(*) k FROM boshi.memories
    WHERE is_latest GROUP BY 1 HAVING count(*) > 1
  ) t""")
print("这些组共占条数:", cur.fetchone()[0])

cur.execute("""
  SELECT left(content,60) p, count(*) k FROM boshi.memories
  WHERE is_latest GROUP BY 1 HAVING count(*) > 3 ORDER BY k DESC LIMIT 8""")
print("--- 重复最多的正文开头 ---")
for r in cur.fetchall():
    print(r[1], "x", r[0].replace("\n", " ")[:60])
