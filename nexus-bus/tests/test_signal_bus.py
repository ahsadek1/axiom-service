"""test_signal_bus.py — NNS SIGNAL Bus v3 test suite (TC-1 through TC-7)"""
import json, os, sys, tempfile, threading, time, unittest, urllib.request, urllib.error
from datetime import datetime, timezone
from http.server import HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import main as bus


def _tmp():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return f.name


def _post(url, body):
    data = json.dumps(body).encode()
    req  = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _server(db_path):
    bus.DB_PATH = db_path
    bus.init_db()
    srv = HTTPServer(("127.0.0.1", 0), bus.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class TestSignalBus(unittest.TestCase):

    def setUp(self):
        self.db = _tmp()
        self.srv, self.base = _server(self.db)

    def tearDown(self):
        self.srv.shutdown()
        try: os.unlink(self.db)
        except: pass

    # TC-1: Happy path — send, recv, ack, confirm cleared
    def test_tc1_happy_path(self):
        code, r = _post(f"{self.base}/signal", {
            "from": "genesis", "to": "sovereign", "priority": "P0",
            "subject": "alpha-exec-down", "message": "Port 8005 unreachable",
            "ttl_minutes": 5, "ack_required": True
        })
        self.assertEqual(code, 200)
        self.assertTrue(r["ok"])
        sig_id = r["signal_id"]
        self.assertTrue(sig_id.startswith("sig_"))

        # recv — signal present
        code, r = _get(f"{self.base}/recv?agent=sovereign")
        self.assertEqual(code, 200)
        self.assertEqual(len(r["signals"]), 1)
        self.assertEqual(r["signals"][0]["signal_id"], sig_id)

        # ack received
        code, r = _post(f"{self.base}/ack", {
            "signal_id": sig_id, "agent": "sovereign",
            "status": "received", "note": "Investigating"
        })
        self.assertEqual(code, 200)
        self.assertTrue(r["ok"])

        # ack resolved
        code, r = _post(f"{self.base}/ack", {
            "signal_id": sig_id, "agent": "sovereign",
            "status": "resolved", "note": "Fixed"
        })
        self.assertEqual(code, 200)

        # recv — no longer in pending
        code, r = _get(f"{self.base}/recv?agent=sovereign")
        self.assertEqual(len(r["signals"]), 0)

        # history — shows up
        code, r = _get(f"{self.base}/history?agent=sovereign&limit=10")
        self.assertEqual(code, 200)
        self.assertEqual(r["signals"][0]["ack_status"], "resolved")

    # TC-2: TTL expiration triggers escalation
    def test_tc2_escalation(self):
        code, r = _post(f"{self.base}/signal", {
            "from": "watchdog", "to": "genesis", "priority": "P1",
            "subject": "service-down", "message": "OMNI unreachable",
            "ttl_minutes": 0, "ack_required": True, "escalate_to": "sovereign"
        })
        sig_id = r["signal_id"]
        bus._escalation_daemon.__code__  # confirm exists
        bus._run_escalation_once()       # call directly

        code, r = _get(f"{self.base}/recv?agent=sovereign")
        self.assertEqual(code, 200)
        escalated = [s for s in r["signals"] if "ESCALATED" in s["subject"]]
        self.assertEqual(len(escalated), 1)

        # Original marked expired
        code, r = _get(f"{self.base}/history?agent=genesis&limit=5")
        original = next(s for s in r["signals"] if s["signal_id"] == sig_id)
        self.assertEqual(original["ack_status"], "expired")

    # TC-3: /recv is non-destructive
    def test_tc3_recv_nondestructive(self):
        _post(f"{self.base}/signal", {
            "from": "a", "to": "b", "priority": "P2",
            "subject": "test", "message": "hello", "ttl_minutes": 60
        })
        _, r1 = _get(f"{self.base}/recv?agent=b")
        _, r2 = _get(f"{self.base}/recv?agent=b")
        self.assertEqual(len(r1["signals"]), 1)
        self.assertEqual(len(r2["signals"]), 1)
        self.assertEqual(r1["signals"][0]["signal_id"], r2["signals"][0]["signal_id"])

    # TC-4: Priority ordering — P0 returned before P2
    def test_tc4_priority_order(self):
        _post(f"{self.base}/signal", {"from":"a","to":"x","priority":"P2","subject":"low","message":"low","ttl_minutes":60})
        _post(f"{self.base}/signal", {"from":"a","to":"x","priority":"P0","subject":"high","message":"high","ttl_minutes":60})
        _, r = _get(f"{self.base}/recv?agent=x")
        self.assertEqual(r["signals"][0]["priority"], "P0")
        self.assertEqual(r["signals"][1]["priority"], "P2")

    # TC-5: Malformed requests rejected cleanly
    def test_tc5_malformed(self):
        # Missing subject
        code, r = _post(f"{self.base}/signal", {"from":"a","to":"b","priority":"P1","message":"x","ttl_minutes":5})
        self.assertEqual(code, 400)
        # Invalid priority
        code, r = _post(f"{self.base}/signal", {"from":"a","to":"b","priority":"P9","subject":"x","message":"x"})
        self.assertEqual(code, 400)
        # ACK unknown signal_id
        code, r = _post(f"{self.base}/ack", {"signal_id":"sig_fake","agent":"x","status":"received"})
        self.assertEqual(code, 404)
        # ACK invalid status
        code, r = _post(f"{self.base}/ack", {"signal_id":"sig_fake","agent":"x","status":"maybe"})
        self.assertEqual(code, 400)

    # TC-6: /pending shows all in-flight signals
    def test_tc6_pending_oversight(self):
        _post(f"{self.base}/signal", {"from":"a","to":"b","priority":"P0","subject":"s1","message":"m","ttl_minutes":60})
        _post(f"{self.base}/signal", {"from":"a","to":"c","priority":"P1","subject":"s2","message":"m","ttl_minutes":60})
        _, r = _get(f"{self.base}/pending")
        self.assertEqual(r["total"], 2)
        self.assertEqual(r["signals"][0]["priority"], "P0")

    # TC-7: Legacy /send and /inbox still work
    def test_tc7_legacy_compat(self):
        code, r = _post(f"{self.base}/send", {"from":"agent-a","to":"agent-b","message":"hello legacy"})
        self.assertEqual(code, 200)
        self.assertTrue(r["ok"])
        code, r = _get(f"{self.base}/inbox/agent-b")
        self.assertEqual(code, 200)
        self.assertEqual(len(r["messages"]), 1)
        self.assertEqual(r["messages"][0]["message"], "hello legacy")
        # consume-on-read: second read empty
        _, r2 = _get(f"{self.base}/inbox/agent-b")
        self.assertEqual(len(r2["messages"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
