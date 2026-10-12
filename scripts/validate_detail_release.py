"""用只读数据库快照校验详情发布，并输出线上抽测样本。"""
import json
import time

from newgame_monitor import webapp


def main():
    conn = webapp._read_conn()
    try:
        conn.execute("BEGIN")
        assert conn.execute("SELECT COUNT(*) FROM canonical_games").fetchone()[0], "目录不能为空"
        catalog = webapp._load_catalog(conn)
        samples = {}
        for game in sorted(catalog, key=lambda item: len(item["members"]), reverse=True)[:5]:
            samples[game["id"]] = game
        sources = set()
        for game in catalog:
            game_sources = {row["source"] for row in game["members"]}
            if game_sources - sources:
                samples[game["id"]] = game
                sources.update(game_sources)
        for game in catalog[::max(1, len(catalog) // 20)]:
            if len(samples) >= 20:
                break
            samples[game["id"]] = game
        result = []
        for original in samples.values():
            start = time.perf_counter()
            current = webapp._load_game(conn, game_uuid=original["game_uuid"])
            payload = webapp._serialize(current)
            payload["latest_intro"] = webapp._latest_game_intro(current)
            elapsed = (time.perf_counter() - start) * 1000
            expected = webapp._serialize(original)
            expected["latest_intro"] = webapp._latest_game_intro(original)
            assert payload == expected, f"详情不一致：{original['game_uuid']}"
            result.append({"uuid": original["game_uuid"], "name": original["name"],
                           "milliseconds": round(elapsed, 2), "payload": payload})
        print(json.dumps({"compared": len(result), "sources": sorted(sources), "samples": result}, ensure_ascii=True))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
