"""
tests/test_push_and_seo_growth.py - 0円グロース3大施策（SEO 15クエリ集中・一次統計引用キット・Web Push & LINE自動応答）単体テスト
"""

import base64
import datetime
import hashlib
import hmac
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("DISABLE_SCHEDULER", "1")
os.environ.setdefault("DISABLE_SHEETS", "1")
os.environ.setdefault("SECRET_KEY", "test-secret-key-12345")

import analytics  # noqa: E402
import app as app_module  # noqa: E402
import local_db  # noqa: E402
import pdca_engine  # noqa: E402
import push_watch  # noqa: E402


def _make_client_ecdh_keys():
    priv = ec.generate_private_key(ec.SECP256R1())
    pub_bytes = priv.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    auth_bytes = os.urandom(16)
    p256dh_b64 = base64.urlsafe_b64encode(pub_bytes).rstrip(b"=").decode("ascii")
    auth_b64 = base64.urlsafe_b64encode(auth_bytes).rstrip(b"=").decode("ascii")
    return priv, pub_bytes, auth_bytes, p256dh_b64, auth_b64


class TestPushLineAndSeoGrowth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp_dir = tempfile.mkdtemp(prefix="aiseki_push_test_")
        cls._prev_db_path = local_db.DB_PATH
        local_db.DB_PATH = os.path.join(cls._tmp_dir, "push_test.db")
        local_db.init_db()
        app_module.app.config["TESTING"] = True
        cls.client = app_module.app.test_client()

    @classmethod
    def tearDownClass(cls):
        local_db.DB_PATH = cls._prev_db_path
        shutil.rmtree(cls._tmp_dir, ignore_errors=True)

    def setUp(self):
        app_module.clear_fast_http_caches()
        analytics.reset_for_tests()
        conn = local_db.get_connection()
        try:
            local_db._ensure_moat_rollup_schema()
            conn.execute("DELETE FROM web_push_subscriptions")
            conn.commit()
        finally:
            conn.close()
        with app_module.data_lock:
            self._saved_latest = dict(app_module.latest_data)
            app_module.latest_data["full_data"] = [
                {"name": "OLG 新宿", "women": 24, "men": 14, "region": "Kanto", "is_open_now": True},
                {"name": "JIS SHINJUKU", "women": 18, "men": 12, "region": "Kanto", "is_open_now": True},
                {"name": "JIS NISHISHINJUKU", "women": 8, "men": 15, "region": "Kanto", "is_open_now": True},
                {"name": "OLG ag渋谷", "women": 10, "men": 18, "region": "Kanto", "is_open_now": True},
                {"name": "OLG 名古屋 錦", "women": 16, "men": 11, "region": "Chubu", "is_open_now": True},
                {"name": "OLG 錦糸町", "women": 19, "men": 9, "region": "Kanto", "is_open_now": True},
            ]
            app_module.latest_data["last_updated"] = "21:30"

    def tearDown(self):
        with app_module.data_lock:
            app_module.latest_data.clear()
            app_module.latest_data.update(self._saved_latest)
        app_module.clear_fast_http_caches()

    def test_vapid_jwt_and_rfc8291_aes128gcm_roundtrip_decryption(self):
        """VAPID ES256署名とRFC 8291 aes128gcm暗号化がブラウザ復号仕様と完全一致すること"""
        vapid = push_watch.get_or_create_vapid_keys(allow_blocking=False)
        auth_hdr = push_watch.build_vapid_authorization_header(
            endpoint="https://fcm.googleapis.com:443/fcm/send/token123",
            private_pem=vapid["private_pem"],
            public_key_b64=vapid["public_key"],
        )
        self.assertTrue(auth_hdr.startswith("vapid t="))
        jwt_part = auth_hdr.split("vapid t=", 1)[1].split(", k=", 1)[0]
        h_b64, c_b64, s_b64 = jwt_part.split(".")
        claims = json.loads(push_watch._b64url_decode(c_b64).decode("utf-8"))
        self.assertEqual(claims["aud"], "https://fcm.googleapis.com")
        raw_sig = push_watch._b64url_decode(s_b64)
        self.assertEqual(len(raw_sig), 64)
        r_int = int.from_bytes(raw_sig[:32], "big")
        s_int = int.from_bytes(raw_sig[32:], "big")
        pub_point = push_watch._b64url_decode(vapid["public_key"])
        vapid_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), pub_point)
        vapid_pub.verify(
            encode_dss_signature(r_int, s_int),
            f"{h_b64}.{c_b64}".encode("ascii"),
            ec.ECDSA(hashes.SHA256()),
        )

        # RFC 8291 aes128gcm roundtrip test with real client ECDH private key
        ua_priv, ua_pub_bytes, auth_secret, p256dh_b64, auth_b64 = _make_client_ecdh_keys()
        original_payload = json.dumps(
            {"title": "オリエンタルラウンジ 新宿｜女性比55%以上に到達", "url": "/store/oriental-lounge-shinjuku?s=push_watch"},
            ensure_ascii=False,
        ).encode("utf-8")
        encrypted = push_watch.encrypt_rfc8291_aes128gcm(original_payload, p256dh_b64, auth_b64)

        salt16 = encrypted[:16]
        rs = int.from_bytes(encrypted[16:20], "big")
        idlen = encrypted[20]
        self.assertEqual(rs, 4096)
        self.assertEqual(idlen, 65)
        as_pub_bytes = encrypted[21 : 21 + idlen]
        ciphertext = encrypted[21 + idlen :]

        as_pub_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub_bytes)
        shared = ua_priv.exchange(ec.ECDH(), as_pub_key)
        key_info = b"WebPush: info\x00" + ua_pub_bytes + as_pub_bytes
        ikm = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth_secret, info=key_info).derive(shared)
        cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt16, info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
        nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt16, info=b"Content-Encoding: nonce\x00").derive(ikm)
        decrypted_padded = AESGCM(cek).decrypt(nonce, ciphertext, None)
        self.assertTrue(decrypted_padded.endswith(b"\x02"))
        self.assertEqual(decrypted_padded[:-1], original_payload)

    def test_push_subscribe_ssrf_protection_and_lifecycle(self):
        """SSRF危険URLがすべてHTTP 400で遮断され、公式Pushホストのみ保存・解除できること"""
        _, _, _, p256dh_b64, auth_b64 = _make_client_ecdh_keys()

        key_res = self.client.get("/api/push/vapid-public-key")
        self.assertEqual(key_res.status_code, 200)
        key_data = key_res.get_json()
        self.assertTrue(key_data["ok"])
        self.assertEqual(key_data["public_key"], key_data["publicKey"])

        bad_endpoints = [
            "http://fcm.googleapis.com/fcm/send/insecure",
            "https://127.0.0.1/push/ssrf",
            "https://169.254.169.254/latest/meta-data",
            "https://evil.example.com/fcm.googleapis.com",
            "https://fcm.googleapis.com:8443/fcm/send/badport",
            "https://fcm.googleapis.com:abc/fcm/send/nonnumericport",
            "https://user:pass@fcm.googleapis.com/fcm/send/cred",
        ]
        for bad_ep in bad_endpoints:
            r = self.client.post(
                "/api/push/subscribe",
                json={
                    "store_slug": "oriental-lounge-shinjuku",
                    "target_cond": "female_55",
                    "subscription": {"endpoint": bad_ep, "keys": {"p256dh": p256dh_b64, "auth": auth_b64}},
                },
            )
            self.assertEqual(r.status_code, 400, f"Expected 400 for SSRF endpoint: {bad_ep}")

        good_ep = "https://web.push.apple.com/QD99_valid_apns_webpush_endpoint"
        ok_res = self.client.post(
            "/api/push/subscribe",
            json={
                "store_slug": "oriental-lounge-shinjuku",
                "target_cond": "female_55",
                "subscription": {"endpoint": good_ep, "keys": {"p256dh": p256dh_b64, "auth": auth_b64}},
            },
        )
        self.assertEqual(ok_res.status_code, 200)
        self.assertTrue(ok_res.get_json()["ok"])
        self.assertEqual(len(local_db.get_active_web_push_subscriptions()), 1)

        unsub_res = self.client.post(
            "/api/push/unsubscribe",
            json={"endpoint": good_ep, "store_slug": "oriental-lounge-shinjuku"},
        )
        self.assertEqual(unsub_res.status_code, 200)
        self.assertEqual(unsub_res.get_json()["removed"], 1)
        self.assertEqual(len(local_db.get_active_web_push_subscriptions()), 0)

    def test_dispatch_cooldown_410_purge_and_14day_ip_anonymization(self):
        """条件一致時の送信・180分クールダウン抑制・410自動削除・14日IPハッシュ匿名化が機能すること"""
        _, _, _, p256dh_b64, auth_b64 = _make_client_ecdh_keys()
        ep_ok = "https://fcm.googleapis.com/fcm/send/sub_alive_1"
        ep_gone = "https://updates.push.services.mozilla.com/wpush/v2/sub_gone_2"

        local_db.upsert_web_push_subscription(
            endpoint=ep_ok, p256dh=p256dh_b64, auth=auth_b64, store_name="OLG 新宿", target_cond="female_55", ip_hash="abcd1234ef567890"
        )
        local_db.upsert_web_push_subscription(
            endpoint=ep_gone, p256dh=p256dh_b64, auth=auth_b64, store_name="OLG 新宿", target_cond="evening_peak", ip_hash="abcd1234ef567890"
        )

        def _fake_post(url, data=None, headers=None, timeout=None, allow_redirects=False):
            m = mock.MagicMock()
            m.status_code = 410 if "sub_gone_2" in url else 201
            return m

        now_t0 = datetime.datetime(2026, 11, 7, 21, 30, 0)
        with mock.patch("push_watch.requests.post", side_effect=_fake_post):
            res1 = push_watch.dispatch_matching_watch_pushes(
                [{"name": "OLG 新宿", "women": 22, "men": 14, "is_open_now": True}],
                cooldown_minutes=180,
                now_jst=now_t0,
            )
            self.assertEqual(res1["sent"], 1)
            self.assertEqual(res1["purged"], 1)

            # Within 180m cooldown -> should not re-send
            res2 = push_watch.dispatch_matching_watch_pushes(
                [{"name": "OLG 新宿", "women": 25, "men": 12, "is_open_now": True}],
                cooldown_minutes=180,
                now_jst=now_t0 + datetime.timedelta(minutes=60),
            )
            self.assertEqual(res2["sent"], 0)

            # After 181m -> cooldown elapsed, sends again
            res3 = push_watch.dispatch_matching_watch_pushes(
                [{"name": "OLG 新宿", "women": 25, "men": 12, "is_open_now": True}],
                cooldown_minutes=180,
                now_jst=now_t0 + datetime.timedelta(minutes=181),
            )
            self.assertEqual(res3["sent"], 1)

        # Backdate `created_at` > 14 days and run `prune_old_logs` -> `ip_hash` must be anonymized to ''
        old_created = (datetime.datetime.now() - datetime.timedelta(days=16)).strftime("%Y-%m-%d %H:%M:%S")
        conn = local_db.get_connection()
        try:
            conn.execute("UPDATE web_push_subscriptions SET created_at = ?", (old_created,))
            conn.commit()
        finally:
            conn.close()
        local_db.prune_old_logs(monitor_days=90, vote_days=14)
        conn = local_db.get_connection()
        try:
            row = conn.execute("SELECT ip_hash FROM web_push_subscriptions WHERE endpoint = ?", (ep_ok,)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "")
        finally:
            conn.close()

    def test_line_webhook_signature_and_formal_brand_replies(self):
        """LINE Webhookが署名検証を行い、エリア別・女子待ちゼロ別に正式ブランド名で無料Reply応答すること"""
        # Unconfigured -> 503
        with mock.patch.dict(os.environ, {"LINE_CHANNEL_SECRET": "", "LINE_CHANNEL_ACCESS_TOKEN": ""}, clear=False):
            r503 = self.client.post("/api/line/webhook", data=b"{}")
            self.assertEqual(r503.status_code, 503)

        secret = "line_test_channel_secret_xyz"
        token = "line_test_access_token_xyz"
        body_dict = {
            "events": [
                {"type": "message", "replyToken": "rt_shinjuku_1", "message": {"type": "text", "text": "新宿"}},
                {"type": "message", "replyToken": "rt_ladies_2", "message": {"type": "text", "text": "女子待ちゼロ"}},
            ]
        }
        raw_body = json.dumps(body_dict, ensure_ascii=False).encode("utf-8")
        valid_sig = base64.b64encode(hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()).decode("ascii")

        with mock.patch.dict(
            os.environ,
            {"LINE_CHANNEL_SECRET": secret, "LINE_CHANNEL_ACCESS_TOKEN": token},
            clear=False,
        ):
            # Bad signature (including non-ASCII safety) -> 403
            r403 = self.client.post(
                "/api/line/webhook",
                data=raw_body,
                headers={"X-Line-Signature": "invalid_sig"},
            )
            self.assertEqual(r403.status_code, 403)
            self.assertFalse(push_watch.verify_line_signature(raw_body, "不正署名テスト", secret))

            # Area keyword disambiguation: 名古屋 must NOT match 錦糸町
            nagoya_reply = push_watch.build_line_auto_reply_text("名古屋", app_module.latest_data["full_data"], last_updated="21:30")
            self.assertIn("オリエンタルラウンジ 名古屋 錦", nagoya_reply)
            self.assertNotIn("錦糸町", nagoya_reply)

            sent_payloads = []

            def _fake_line_post(url, headers=None, json=None, timeout=None):
                sent_payloads.append((url, headers, json))
                m = mock.MagicMock()
                m.status_code = 200
                return m

            with mock.patch("push_watch.requests.post", side_effect=_fake_line_post):
                r200 = self.client.post(
                    "/api/line/webhook",
                    data=raw_body,
                    headers={"X-Line-Signature": valid_sig},
                )
                self.assertEqual(r200.status_code, 200)
                self.assertEqual(r200.get_json()["replied"], 2)
                self.assertEqual(len(sent_payloads), 2)
                shinjuku_text = sent_payloads[0][2]["messages"][0]["text"]
                ladies_text = sent_payloads[1][2]["messages"][0]["text"]
                self.assertIn("オリエンタルラウンジ 新宿", shinjuku_text)
                self.assertIn("JIS（ジス）新宿", shinjuku_text)
                self.assertIn("?s=line_bot", shinjuku_text)
                self.assertIn("ag（アグ）渋谷", ladies_text)
                self.assertIn("/for-women?s=line_bot", ladies_text)

    def test_seo_pillars_stats_citation_kit_and_confirmed_epc(self):
        """4大ブランドページ早見表・FAQ、/stats引用スニペット（直/link/ゼロ）、店舗内部リンク、確定EPC併記の検証"""
        for slug in ("oriental-lounge", "ag", "jis", "asobibar"):
            res = self.client.get(f"/{slug}")
            self.assertEqual(res.status_code, 200)
            html = res.data.decode("utf-8")
            self.assertIn("料金システム・対象年齢・1人入店ルール早見表", html)
            self.assertIn("ASOBIBAR（アソビバー）全店", html)
            ld_match = re.search(r'<script type="application/ld\+json">\s*(\[.*?\])\s*</script>', html, re.S)
            self.assertIsNotNone(ld_match, f"Missing JSON-LD in /{slug}")
            ld_arr = json.loads(ld_match.group(1))
            faq_obj = next((x for x in ld_arr if x.get("@type") == "FAQPage"), None)
            self.assertIsNotNone(faq_obj)
            q_names = [q.get("name", "") for q in faq_obj.get("mainEntity", [])]
            self.assertTrue(any("料金システム・男性1人（ソロ）入店可否・女性料金は？" in qn for qn in q_names))

        st_res = self.client.get("/stats")
        self.assertEqual(st_res.status_code, 200)
        st_html = st_res.data.decode("utf-8")
        self.assertIn("cite-snippet-plain", st_html)
        self.assertIn("cite-snippet-md", st_html)
        self.assertIn("cite-snippet-html", st_html)
        with open(os.path.join(ROOT, "templates", "stats_report.html"), "r", encoding="utf-8") as fp:
            self.assertNotIn("/link/", fp.read())
        csv_txt = self.client.get("/stats/monthly.csv").data.decode("utf-8")
        self.assertNotIn("/link/", csv_txt)

        sd_res = self.client.get("/store/oriental-lounge-shinjuku")
        self.assertEqual(sd_res.status_code, 200)
        sd_html = sd_res.data.decode("utf-8")
        self.assertIn("detail-bg-push-btn", sd_html)
        self.assertIn("/guide/shinjuku", sd_html)
        self.assertIn("/guide/brand-comparison", sd_html)
        self.assertIn("/area/umeda", sd_html)

        for area_path in (
            "/area/shinjuku", "/area/shibuya", "/area/umeda", "/area/namba",
            "/area/nagoya", "/area/fukuoka", "/area/omiya", "/area/yokohama",
            "/area/sapporo", "/area/sendai", "/area/hiroshima",
            "/guide/solo-vs-pair", "/guide/dress-code-cleanliness", "/guide/lounge-vs-screened-date",
        ):
            r_nav = self.client.get(area_path)
            self.assertEqual(r_nav.status_code, 200, f"Expected 200 on {area_path}")

        robots_txt = self.client.get("/robots.txt").data.decode("utf-8")
        self.assertIn("Disallow: /api/push/", robots_txt)
        self.assertIn("Disallow: /api/line/", robots_txt)

        summ = analytics.summary(days=7)
        self.assertEqual(summ["revenue"]["est_confirmed_epc_yen"], 30)
        self.assertEqual(summ["revenue"]["epc_basis"], "assumed_fixed")
        self.assertIn(summ["revenue"]["epc_sample_status"], ("accumulating", "sample_sufficient"))
        offers = pdca_engine._build_offer_breakdown(summ, summ, 7)
        self.assertTrue(all(
            "confirmed_epc_yen" in r and r.get("epc_basis") == "assumed_fixed" and r.get("epc_sample_status") in ("accumulating", "sample_sufficient")
            for r in offers
        ))


if __name__ == "__main__":
    unittest.main()
