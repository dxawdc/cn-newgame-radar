import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
import xml.etree.ElementTree as ET

from PIL import Image

from newgame_monitor.enrichment import (
    _parse_haoyou_detail_html,
    _parse_4399_info_data,
    _parse_huawei_public_detail,
    _parse_xiaomi_public_detail,
    _parse_vivo_appointment_detail,
    _honor_cache_metadata,
    _parse_haoyou_search_html,
    _repair_huawei_names_from_official_details,
    enrich_9game_screenshots,
    enrich_name_lookup_fallback,
    enrich_oppo_offline_media,
    enrich_oppo_ui_snapshots,
)
from newgame_monitor.app_cache_collectors import (
    _OPPO_OFFLINE_BLOB_CACHE,
    _attach_oppo_offline_metadata,
    _clean_ui_text,
    _capture_oppo_gallery,
    _dump_ui,
    _enrich_visible_oppo_details,
    _huawei_event_fields,
    _oppo_network_response_data,
    _oppo_offline_blobs,
    _oppo_screenshot_urls,
    _parse_honor_list,
    _oppo_explicit_date,
    _parse_oppo_today,
    _parse_oppo_timeline,
    _wait_for_text_node,
    _xiaomi_event_time,
)
from newgame_monitor.db import connect, upsert_items


class HaoYouDetailTest(unittest.TestCase):
    @staticmethod
    def _protobuf_varint(value):
        output = bytearray()
        while value > 0x7f:
            output.append((value & 0x7f) | 0x80)
            value >>= 7
        output.append(value)
        return bytes(output)

    @classmethod
    def _protobuf_field(cls, number, value):
        if isinstance(value, int):
            return cls._protobuf_varint(number << 3) + cls._protobuf_varint(value)
        payload = value.encode("utf-8") if isinstance(value, str) else value
        return (
            cls._protobuf_varint((number << 3) | 2)
            + cls._protobuf_varint(len(payload)) + payload
        )

    @classmethod
    def _oppo_resource_card(
        cls, app_id, name, package_name, *, with_link=True, link_id=None,
    ):
        fields = [
            cls._protobuf_field(127, "com.heytap.cdo.common.domain.dto.ResourceDto"),
            cls._protobuf_field(1, app_id),
            cls._protobuf_field(3, name),
            cls._protobuf_field(7, package_name),
            cls._protobuf_field(14, f"https://gc-image.heytapimage.com/{app_id}/icon.png"),
            cls._protobuf_field(26, f"{name}的一句话玩法介绍"),
            cls._protobuf_field(30, "角色扮演"),
        ]
        fields.extend(
            cls._protobuf_field(
                32, f"https://gc-image.heytapimage.com/{app_id}/{index}.jpg",
            )
            for index in range(1, 6)
        )
        if with_link:
            fields.append(cls._protobuf_field(60, f"oap://gc/dt?id={link_id or app_id}"))
        return b"".join(fields)

    def test_oppo_java_network_response_extracts_business_data(self):
        payload = self._oppo_resource_card(36309165, "粒粒的小人国", "com.tencent.lifeH")
        java_array = (
            b"\xac\xed\x00\x05com.nearme.network.internal.NetworkResponse"
            b"\x75\x72\x00\x02[B"
            b"\xac\xf3\x17\xf8\x06\x08\x54\xe0\x02\x00\x00\x78\x70"
            + len(payload).to_bytes(4, "big") + payload + b"tail"
        )
        self.assertEqual(_oppo_network_response_data(java_array), payload)
        self.assertIsNone(_oppo_network_response_data(payload))

    @patch("newgame_monitor.app_cache_collectors._oppo_network_response_data")
    @patch("newgame_monitor.app_cache_collectors._read_root_file")
    @patch("newgame_monitor.app_cache_collectors._adb")
    def test_oppo_offline_cache_remembers_negative_and_refreshes_changed_hash(
        self, adb, read_root, extract,
    ):
        state = {"digest": "a" * 64}

        def adb_result(*args):
            if "ls -1t" in args[-1]:
                return b"/cache/sample.0\n"
            return f"{state['digest']}  /cache/sample.0\n".encode()

        adb.side_effect = adb_result
        read_root.side_effect = [b"first", b"second"]
        extract.side_effect = [None, b"changed-payload"]
        _OPPO_OFFLINE_BLOB_CACHE.clear()
        self.assertEqual(_oppo_offline_blobs(), [])
        self.assertEqual(_oppo_offline_blobs(), [])
        self.assertEqual(read_root.call_count, 1)
        state["digest"] = "b" * 64
        self.assertEqual(_oppo_offline_blobs(), [b"changed-payload"])
        self.assertEqual(read_root.call_count, 2)
        _OPPO_OFFLINE_BLOB_CACHE.clear()

    def test_oppo_resource_fields_keep_adjacent_cards_isolated(self):
        blob = b"".join([
            self._protobuf_field(
                10, self._oppo_resource_card(36309165, "粒粒的小人国", "com.tencent.lifeH"),
            ),
            self._protobuf_field(
                10, self._oppo_resource_card(37361733, "斗罗大陆:零", "com.tencent.tmgp.osgame"),
            ),
            self._protobuf_field(7, "oap://gc/dt?id=99999999"),
            self._protobuf_field(14, "https://example.com/wrapper-banner.jpg"),
        ])
        items = [
            {"name": "粒粒的小人国", "raw": {}, "tags": []},
            {"name": "斗罗大陆:零", "raw": {}, "tags": []},
        ]
        _attach_oppo_offline_metadata(items, [blob])
        first = items[0]["raw"]["oppo_offline_detail"]
        second = items[1]["raw"]["oppo_offline_detail"]
        self.assertEqual(first["app_id"], "36309165")
        self.assertEqual(first["package_name"], "com.tencent.lifeH")
        self.assertEqual(second["app_id"], "37361733")
        self.assertEqual(second["package_name"], "com.tencent.tmgp.osgame")
        self.assertEqual(len(first["screenshot_urls"]), 5)
        self.assertNotIn(first["icon_url"], first["screenshot_urls"])
        self.assertIn("玩法介绍", items[0]["gameplay_intro"])

    @patch("newgame_monitor.app_cache_collectors._oppo_offline_blobs")
    def test_oppo_offline_media_backfills_intro_and_gallery(self, offline_blobs):
        offline_blobs.return_value = [
            self._oppo_resource_card(36309165, "粒粒的小人国", "com.tencent.lifeH")
        ]
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "oppo-offline.db")
            upsert_items(conn, [{
                "source": "oppo_gamecenter", "source_item_id": "oppo-1",
                "name": "粒粒的小人国", "event_type": "beta",
                "event_time": "2026-08-27", "raw": {},
            }], "2026-08-27T10:00:00+08:00")
            result = enrich_oppo_offline_media(conn)
            row = conn.execute(
                "SELECT package_name,gameplay_intro,detail_url,category,raw_json "
                "FROM source_items WHERE source='oppo_gamecenter'"
            ).fetchone()
            self.assertEqual(result["matched"], 1)
            self.assertEqual(row["package_name"], "com.tencent.lifeH")
            self.assertIn("玩法介绍", row["gameplay_intro"])
            self.assertEqual(row["detail_url"], "oaps://gc/dt?id=36309165")
            self.assertEqual(row["category"], "角色扮演")
            self.assertEqual(
                len(json.loads(row["raw_json"])["oppo_offline_detail"]["screenshot_urls"]), 5,
            )
            conn.close()

    def test_oppo_resource_app_id_does_not_require_deep_link(self):
        item = {"name": "无链接卡片", "raw": {}, "tags": []}
        blob = self._oppo_resource_card(
            36969909, "无链接卡片", "ShinchanMatchDaily.nearme.gamecenter",
            with_link=False,
        )
        _attach_oppo_offline_metadata([item], [blob])
        detail = item["raw"]["oppo_offline_detail"]
        self.assertEqual(detail["app_id"], "36969909")
        self.assertEqual(detail["package_name"], "ShinchanMatchDaily.nearme.gamecenter")

    def test_oppo_resource_rejects_app_id_link_mismatch(self):
        item = {"name": "错配卡片", "raw": {}, "tags": []}
        blob = self._oppo_resource_card(
            37361733, "错配卡片", "com.example.mismatch", link_id=36309165,
        )
        _attach_oppo_offline_metadata([item], [blob])
        self.assertNotIn("oppo_offline_detail", item["raw"])

    def test_oppo_same_name_with_different_app_ids_is_ambiguous(self):
        item = {"name": "同名游戏", "raw": {}, "tags": []}
        blob = b"".join([
            self._protobuf_field(
                10, self._oppo_resource_card(10001, "同名游戏", "com.example.first"),
            ),
            self._protobuf_field(
                10, self._oppo_resource_card(10002, "同名游戏", "com.example.second"),
            ),
        ])
        _attach_oppo_offline_metadata([item], [blob])
        self.assertNotIn("oppo_offline_detail", item["raw"])

    @patch("newgame_monitor.app_cache_collectors._tap_node")
    @patch("newgame_monitor.app_cache_collectors._ui_detail_enabled", return_value=False)
    def test_oppo_beta_does_not_open_detail_without_explicit_backfill(
        self, _enabled, tap,
    ):
        xml = (
            b'<hierarchy><node resource-id="com.nearme.gamecenter:id/appName" '
            b'text="beta game" bounds="[0,0][100,100]" /></hierarchy>'
        )
        _enrich_visible_oppo_details(
            xml, [{"name": "beta game", "event_type": "beta", "raw": {}}], set(),
        )
        tap.assert_not_called()

    def test_oppo_snapshot_enrichment_preserves_captured_gallery(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / "oppo-snapshot.db")
            upsert_items(conn, [{
                "source": "oppo_gamecenter", "source_item_id": "oppo-1",
                "name": "图集保留测试", "event_type": "beta",
                "event_time": "2026-08-27", "raw": {
                    "ui_detail": {"screenshot_urls": [
                        "local-screenshot://gallery/oppo_gamecenter/sample.webp"
                    ]},
                },
            }], "2026-08-27T10:00:00+08:00")
            key = hashlib.sha1("图集保留测试".encode()).hexdigest()[:10]
            path = f"/sdcard/oppo-detail-{key}-expanded.xml"
            xml = (
                '<hierarchy><node resource-id="com.nearme.gamecenter:id/introduction_tv" '
                'text="完整游戏介绍" /></hierarchy>'
            ).encode("utf-8")

            def fake_adb(*args):
                return (path + "\n").encode() if args[0] == "shell" else xml

            with patch("newgame_monitor.app_cache_collectors._adb", side_effect=fake_adb):
                enrich_oppo_ui_snapshots(conn)
            raw = json.loads(conn.execute(
                "SELECT raw_json FROM source_items WHERE source_item_id='oppo-1'"
            ).fetchone()[0])
            self.assertEqual(raw["ui_detail"]["screenshot_urls"], [
                "local-screenshot://gallery/oppo_gamecenter/sample.webp"
            ])
            conn.close()

    def test_oppo_gallery_captures_local_screenshot_asset(self):
        root = ET.fromstring("""
        <hierarchy><node resource-id="com.nearme.gamecenter:id/screenshots_view"
          class="android.widget.HorizontalScrollView" bounds="[0,600][1440,1400]">
          <node class="android.widget.ImageView" content-desc="图片"
            bounds="[64,700][1376,1300]" />
        </node></hierarchy>
        """.encode("utf-8"))
        image = Image.new("RGB", (1440, 2560), (40, 90, 160))
        buffer = BytesIO()
        image.save(buffer, "PNG")
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ, {"NEWGAME_ICON_DIR": folder}, clear=False,
        ), patch(
            "newgame_monitor.app_cache_collectors._capture_screen",
            return_value=buffer.getvalue(),
        ):
            urls = _capture_oppo_gallery("图集测试游戏", root, max_images=1)
            relative = urls[0].removeprefix("local-screenshot://")
            self.assertTrue((Path(folder) / relative).is_file())

    def test_ui_dump_retries_transient_adb_failure(self):
        failure = subprocess.CalledProcessError(137, ["adb", "uiautomator", "dump"])
        with patch(
            "newgame_monitor.app_cache_collectors._adb",
            side_effect=[failure, b"dumped", b"<hierarchy />"],
        ) as adb, patch("newgame_monitor.app_cache_collectors.time.sleep") as sleep:
            xml = _dump_ui("oppo-launch-1")

        self.assertEqual(xml, b"<hierarchy />")
        self.assertEqual(adb.call_count, 3)
        sleep.assert_called_once_with(1.5)

    def test_honor_home_waits_for_new_game_tab(self):
        loading = b'<hierarchy><node package="com.hihonor.gamecenter" /></hierarchy>'
        ready = (
            b'<hierarchy><node package="com.hihonor.gamecenter" text="\xe6\x96\xb0\xe6\xb8\xb8" '
            b'bounds="[150,240][278,392]" /></hierarchy>'
        )

        with patch(
            "newgame_monitor.app_cache_collectors._dump_ui", return_value=ready,
        ) as dump_ui, patch("newgame_monitor.app_cache_collectors.time.sleep"):
            node, latest = _wait_for_text_node(
                loading, "新游", dump_prefix="honor-home-ready", max_top=500,
            )

        self.assertIsNotNone(node)
        self.assertEqual(latest, ready)
        dump_ui.assert_called_once_with("honor-home-ready-1")

    def test_huawei_game_event_uses_recruitment_type_and_start_time(self):
        event_type, event_time, status = _huawei_event_fields({
            "gcode": "GameEvent",
            "name": "先锋测试招募开启",
            "typeName": "先锋测试",
            "startTime": 1787538600000,
        }, 1787550000000)
        self.assertEqual(event_type, "recruiting_beta")
        self.assertTrue(event_time.startswith("2026-08-24T10:30:00"))
        self.assertEqual(status, "先锋测试招募开启")

    def test_huawei_official_name_survives_later_cache_upsert(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = connect(Path(directory) / "huawei-name.db")
            observed = "2026-08-24T10:00:00+08:00"
            base = {
                "source": "huawei_gamecenter", "source_item_id": "C115042561",
                "name": "先锋测试招募开启", "event_type": "reservation",
                "event_time": "", "raw": {
                    "official_public_detail": {"name": "粒粒的小人国"},
                },
            }
            upsert_items(conn, [base], observed)
            self.assertEqual(_repair_huawei_names_from_official_details(conn), 1)
            upsert_items(conn, [{**base, "raw": {"name": "先锋测试招募开启"}}], observed)
            row = conn.execute(
                "SELECT name FROM source_items WHERE source_item_id='C115042561'"
            ).fetchone()
            self.assertEqual(row["name"], "粒粒的小人国")
            conn.close()

    def test_9game_screenshot_backfill_prefers_retained_raw_html(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conn = connect(root / "ninegame.db")
            upsert_items(conn, [{
                "source": "uc_9game",
                "source_item_id": "3132949",
                "name": "点点英雄",
                "detail_url": "https://www.9game.cn/diandianyingxiong/",
                "event_type": "launch",
                "event_time": "2026-08-21",
                "raw": {},
            }], "2026-08-24T10:00:00+08:00")
            raw_file = root / "raw" / "2026-08-21" / "9game" / "120000-detail-3132949.raw"
            raw_file.parent.mkdir(parents=True)
            raw_file.write_bytes(b"""
                <div class=\"special-img\">
                  <img src=\"https://example.com/1.jpg\" alt=\"screenshot 1\">
                  <img src=\"//example.com/2.jpg\" alt=\"screenshot 2\">
                </div>
            """)

            result = enrich_9game_screenshots(conn, root / "raw", delay=0)
            row = conn.execute(
                "SELECT raw_json FROM source_items WHERE source='uc_9game'"
            ).fetchone()
            screenshots = json.loads(row["raw_json"])["detail"]["screenshot_urls"]

            self.assertEqual(result["updated"], 1)
            self.assertEqual(result["raw_hits"], 1)
            self.assertEqual(result["fetched"], 0)
            self.assertEqual(screenshots, [
                "https://example.com/1.jpg",
                "https://example.com/2.jpg",
            ])
            conn.close()

    def test_xiaomi_reservation_ignores_hidden_placeholder_timestamp(self):
        self.assertEqual(
            _xiaomi_event_time({}, {"t": 1809050400, "text": "敬请期待"}),
            "",
        )
        self.assertTrue(
            _xiaomi_event_time({}, {"t": 1787619600, "text": "8月25日"}).startswith("2026-08-25")
        )
        self.assertTrue(
            _xiaomi_event_time({"begin": 1787191200}, {"t": 1809050400, "text": "敬请期待"}).startswith("2026-08-20")
        )

    def test_honor_card_uses_explicit_launch_date(self):
        observed = datetime.fromisoformat("2026-08-20T13:58:46+08:00")
        xml = """
        <hierarchy><node resource-id="com.hihonor.gamecenter:id/layout_provider_content"
          bounds="[0,1232][1440,1552]">
          <node text="绝境猎人" resource-id="com.hihonor.gamecenter:id/tv_app_name" />
          <node text="1万人已预约" resource-id="com.hihonor.gamecenter:id/tv_download_info" />
          <node text="8月27日上线" resource-id="com.hihonor.gamecenter:id/tv_desc" />
        </node></hierarchy>
        """.encode("utf-8")

        parsed = _parse_honor_list(xml, "beta", observed)
        self.assertEqual(parsed[0]["event_time"], "2026-08-27")
        self.assertEqual(parsed[0]["event_type"], "launch")
        self.assertEqual(parsed[0]["status"], "8月27日上线")

    def test_oppo_today_card_prefers_explicit_launch_date(self):
        observed = datetime.fromisoformat("2026-08-20T11:04:03+08:00")
        xml = """
        <hierarchy><node bounds="[0,0][1440,2560]">
          <node bounds="[64,1117][1376,1449]">
            <node text="永远的蔚蓝星球" resource-id="com.nearme.gamecenter:id/tv_name"
                  bounds="[296,1201][1040,1285]" />
            <node text="8月21日首发" resource-id="com.nearme.gamecenter:id/tv_des"
                  bounds="[296,1285][1040,1342]" />
          </node>
        </node></hierarchy>
        """.encode("utf-8")

        self.assertEqual(_oppo_explicit_date("8月21日首发", observed), "2026-08-21")
        self.assertEqual(_oppo_explicit_date("招募至8月24日结束", observed), "")
        parsed = _parse_oppo_today(xml, observed)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["event_time"], "2026-08-21")
        self.assertEqual(parsed[0]["raw"]["event_date_source"], "status")

    def test_oppo_timeline_keeps_inherited_date_above_next_header(self):
        xml = """
        <hierarchy><node bounds="[0,0][1440,2560]">
          <node bounds="[0,100][1440,400]">
            <node text="上一组游戏" resource-id="com.nearme.gamecenter:id/appName"
                  bounds="[572,180][900,250]" />
            <node text="首发 10:00 开启" resource-id="com.nearme.gamecenter:id/tagTextView"
                  bounds="[572,260][900,310]" />
          </node>
          <node text="08/26" bounds="[50,500][200,560]" />
          <node bounds="[0,580][1440,900]">
            <node text="下一组游戏" resource-id="com.nearme.gamecenter:id/appName"
                  bounds="[572,620][900,690]" />
            <node text="首发 10:00 开启" resource-id="com.nearme.gamecenter:id/tagTextView"
                  bounds="[572,700][900,750]" />
          </node>
        </node></hierarchy>
        """.encode("utf-8")

        parsed, inherited = _parse_oppo_timeline(xml, "launch", "2026-08-25")
        self.assertEqual(
            [(item["name"], item["event_time"]) for item in parsed],
            [("上一组游戏", "2026-08-25"), ("下一组游戏", "2026-08-26")],
        )
        self.assertEqual(inherited, "2026-08-26")

    def test_search_fallback_only_accepts_exact_normalized_name(self):
        html = """
        <ul>
          <li><p class="top"><a href="//www.3839.com/a/123.htm">
            <span class="sp-name">样例游戏(官服)</span></a></p></li>
          <li><p class="top"><a href="//www.3839.com/a/456.htm">
            <span class="sp-name">样例游戏2</span></a></p></li>
        </ul>
        """.encode("utf-8")
        matches = _parse_haoyou_search_html(html, "样例游戏")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["source_item_id"], "123")
        self.assertEqual(matches[0]["detail_url"], "https://m.3839.com/a/123.htm")

    def test_reuses_collected_taptap_metadata_before_online_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = connect(Path(directory) / "fallback.db")
            observed = "2026-08-21T10:00:00+08:00"
            description = "这是一段来自TapTap的完整产品介绍，包含世界观、核心玩法、成长路线、多人合作内容以及长期养成目标。"
            upsert_items(conn, [{
                "source": "taptap", "source_item_id": "tap-1", "name": "样例游戏",
                "developer": "样例开发公司", "full_description": description,
                "icon_url": "https://example.com/taptap-icon.png",
                "detail_url": "https://www.taptap.cn/app/1", "event_type": "launch",
                "event_time": "2026-08-21", "raw": {},
            }, {
                "source": "oppo_gamecenter", "source_item_id": "oppo-1",
                "name": "样例游戏(官服)", "event_type": "beta",
                "event_time": "2026-08-22", "raw": {},
            }], observed)
            result = enrich_name_lookup_fallback(conn, online=False)
            row = conn.execute(
                "SELECT developer,full_description,icon_url,raw_json FROM source_items "
                "WHERE source='oppo_gamecenter'"
            ).fetchone()
            self.assertEqual(row["developer"], "样例开发公司")
            self.assertEqual(row["full_description"], description)
            self.assertEqual(row["icon_url"], "https://example.com/taptap-icon.png")
            self.assertEqual(result["developer_updated"], 1)
            self.assertEqual(result["icon_updated"], 1)
            trace = json.loads(row["raw_json"])["name_fallback_enrichment"]
            self.assertEqual(trace["developer"]["source"], "taptap")
            conn.close()

    def test_4399_structured_detail_fields(self):
        html = b'<script>window.infoData = {"kind":"RPG","appinfo":"Full<br>detail","review":"Brief","devname":"Developer","pic":"https://example.com/icon.png","tag":[{"name":"Adventure"}],"screenpath":["game-shot-1.jpg","game-shot-2.jpg"]}; window.ageData=[];</script>'
        parsed = _parse_4399_info_data(html)
        self.assertEqual(parsed["category"], "RPG")
        self.assertEqual(parsed["developer"], "Developer")
        self.assertIn("detail", parsed["description"])
        self.assertEqual(parsed["screenshot_urls"], [
            "https://f1.img4399.com/sj~game-shot-1.jpg",
            "https://f1.img4399.com/sj~game-shot-2.jpg",
        ])

    def test_oppo_offline_screenshots_stop_before_next_game_card(self):
        blob = (
            b"current-game-name com.example.nearme.gamecenter "
            b"https://gc-image.heytapimage.com/img/icon.png "
            b"https://gc-image.heytapimage.com/img/1.jpg "
            b"https://gc-image.heytapimage.com/img/2.jpg "
            b"https://gc-image.heytapimage.com/img/3.jpg "
            b"https://gc-image.heytapimage.com/img/4.jpg "
            b"https://gc-image.heytapimage.com/img/5.jpg "
            b"oap://gc/dt?id=123456 "
            b"https://gc-image.heytapimage.com/img/other.jpg"
        )
        start = blob.index(b"current-game-name") + len(b"current-game-name")
        self.assertEqual(_oppo_screenshot_urls(blob, start), [
            f"https://gc-image.heytapimage.com/img/{index}.jpg" for index in range(1, 6)
        ])

    def test_ui_description_removes_visual_line_wrapping(self):
        self.assertEqual(
            _clean_ui_text("随机召唤英雄、\n随机搭配技能\r\n\r\n合作破敌"),
            "随机召唤英雄、随机搭配技能\n\n合作破敌",
        )

    def test_extracts_description_and_publisher(self):
        html = """
        <img src="//img.71acg.net/kbyx/sample-icon.png" alt="样例游戏下载">
        <div class="wrap mt20">
          <div class="titHd"><em>游戏介绍</em></div>
          <div class="game-desc over" id="zinfo3">
            <p>这是一段足够长的游戏背景介绍，用于说明世界观和核心目标。</p>
            <p>玩家可以组队挑战副本、收集装备并参与多人竞技。</p><a id="btn_zhan3">更多</a>
          </div>
          <img src="//img.71acg.net/game/shot1.jpg" alt="样例游戏截图1">
          <img src="//img.71acg.net/game/shot2.jpg" alt="样例游戏截图2">
          <ul class="game-data"><li><table><tr><td>
            <p class="sp1">发行商</p><p class="sp2">样例网络科技有限公司</p>
          </td></tr></table></li></ul>
        </div>
        """.encode("utf-8")
        parsed = _parse_haoyou_detail_html(html)
        self.assertIn("组队挑战副本", parsed["description"])
        self.assertNotIn("更多", parsed["description"])
        self.assertEqual(parsed["developer"], "样例网络科技有限公司")
        self.assertEqual(parsed["icon_url"], "https://img.71acg.net/kbyx/sample-icon.png")
        self.assertEqual(parsed["developer_role"], "发行商")
        self.assertEqual(parsed["screenshot_urls"], [
            "https://img.71acg.net/game/shot1.jpg",
            "https://img.71acg.net/game/shot2.jpg",
        ])

    def test_huawei_detail_requires_matching_app_card(self):
        payload = {
            "layoutData": [
                {"layoutName": "detailhiddencard", "dataList": [{
                    "appid": "C123", "name": "样例游戏", "package": "com.example.game",
                    "icon": "https://example.com/icon.png", "versionName": "1.2.3",
                }]},
                {"layoutName": "detailappinfocard", "dataList": [{
                    "developer": "样例游戏公司", "releaseDate": "2026/8/20",
                }]},
                {"layoutName": "detailappintrocard", "dataList": [{
                    "appIntro": "第一段完整介绍。\n\n第二段玩法介绍。",
                }]},
            ]
        }
        self.assertIsNone(_parse_huawei_public_detail(payload, "C999"))
        parsed = _parse_huawei_public_detail(payload, "C123")
        self.assertEqual(parsed["developer"], "样例游戏公司")
        self.assertIn("第二段玩法介绍", parsed["description"])

    def test_xiaomi_detail_uses_structured_state(self):
        state = {
            "domain": "https://t1.g.mi.com",
            "game": {"gameInfo": {
                "detail": {"developerCompanyName": "样例开发公司"},
                "gameInfo": {
                    "gameId": 62387499, "displayName": "样例游戏",
                    "packageName": "com.example.game", "publisherName": "样例发行公司",
                    "introduction": "一段完整介绍。\n\n包含核心玩法和世界观。",
                    "versionName": "2.0",
                    "screenShot": [
                        {"url": "AppStore/shot-1"},
                        {"url": "https://img.example/xiaomi-2.jpg"},
                    ],
                },
            }}
        }
        html = (
            "<html><script>window.__INITIAL_STATE__= "
            + json.dumps(state, ensure_ascii=False)
            + ";</script></html>"
        ).encode("utf-8")
        self.assertIsNone(_parse_xiaomi_public_detail(html, "1"))
        parsed = _parse_xiaomi_public_detail(html, "62387499")
        self.assertEqual(parsed["developer"], "样例开发公司")
        self.assertIn("核心玩法", parsed["description"])
        self.assertEqual(parsed["screenshots"], [
            "https://t1.g.mi.com/thumbnail/jpeg/w1200q90/AppStore/shot-1",
            "https://img.example/xiaomi-2.jpg",
        ])

    def test_vivo_detail_requires_matching_package(self):
        payload = {"retcode": 0, "data": {"appointment": {
            "name": "样例游戏", "pkgName": "com.example.vivo",
            "gameDeveloper": "样例开发公司", "gameType": "角色扮演",
            "desc": "世界观介绍<br>玩家可以组队探索并挑战副本。",
            "editorRecommend": "组队探索玩法", "icon": "https://example.com/icon.png",
            "contentTags": [{"name": "RPG"}, {"name": "多人联机"}],
        }}}
        self.assertIsNone(_parse_vivo_appointment_detail(payload, "com.wrong.vivo"))
        parsed = _parse_vivo_appointment_detail(payload, "com.example.vivo")
        self.assertEqual(parsed["developer"], "样例开发公司")
        self.assertIn("组队探索", parsed["description"])

    def test_honor_cache_extracts_metadata_without_creating_events(self):
        payload = {"assList": [{"appList": [{
            "name": "样例游戏", "company": "样例荣耀公司",
            "description": "完整游戏介绍", "brief": "玩法短介绍",
            "pName": "com.example.honor", "verName": "1.0",
            "bannerInfo": {"banner": "https://img.example/honor-banner.webp"},
        }]}]}
        metadata = _honor_cache_metadata(payload)
        parsed = metadata["样例游戏"][0]
        self.assertEqual(parsed["developer"], "样例荣耀公司")
        self.assertEqual(parsed["package_name"], "com.example.honor")
        self.assertEqual(parsed["screenshot_urls"], [
            "https://img.example/honor-banner.webp"
        ])


if __name__ == "__main__":
    unittest.main()
