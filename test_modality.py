"""Local HTTP contract checks; never contacts a deployment node."""
import contextlib
import io
import json
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import api_test
from modality_client import ModalityClient, TransportFailure

ROOT = Path(__file__).resolve().parent


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, body):
        payload = {"code": 0, "msg": "ok", "data": {"request_id": body.get("request_id")}}
        data = payload["data"]
        if self.path == "/modality/deploy":
            self.server.modality = body.get("modality")
            data.update(modality_ip="127.0.0.1", modality_port="9000")
        elif self.path == "/modality/delete":
            self.server.modality = None
        elif self.path.startswith("/resource/status"):
            usage = dict(compute_usage_percent=0, storage_usage_mb=0, forwarding_usage_mbps=0)
            data.update(timestamp_ms=1, node_id="node", node_resource=usage,
                        modalities_resource=([dict(modality=self.server.modality, **usage)]
                                             if self.server.modality else []))
        status, override = self.server.reply
        raw = override if override is not None else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.requests.append((self.command, self.path, raw, self.headers.get("Content-Type")))
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
        self.respond(body)

    def do_GET(self):
        self.server.requests.append((self.command, self.path, b"", None))
        body = {key: values[0] for key, values in parse_qs(urlsplit(self.path).query).items()}
        body["request_id"] = int(body["request_id"])
        self.respond(body)


class ModalityTests(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.server.requests = []
        self.server.modality = None
        self.server.reply = (200, None)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.args = ["--ip", "127.0.0.1", "--port", str(self.server.server_port),
                     "--node-id", "node", "--modality", " Mixed-Mode "]
        self.client = ModalityClient(f"http://127.0.0.1:{self.server.server_port}")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def cli(self, action, *extra):
        return subprocess.run([sys.executable, str(ROOT / "modality_cli.py"), *self.args,
                               "--action", action, *extra], capture_output=True, text=True)

    def test_cli_deploy_and_delete(self):
        result = self.cli("deploy", "--compute-config-percent", "25")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["code"], 0)
        self.assertEqual(len(self.server.requests), 1)
        method, path, raw, content_type = self.server.requests[0]
        body = json.loads(raw)
        self.assertEqual((method, path, content_type), ("POST", "/modality/deploy", "application/json"))
        self.assertEqual(body["modality"], "Mixed-Mode")
        self.assertEqual(body["compute_config_percent"], 25)
        self.assertEqual(body["storage_config_mb"], 128)
        self.assertEqual(body["forwarding_config_mbps"], 10)
        self.assertEqual(body["node_id"], "node")
        self.assertIs(type(body["timestamp_ms"]), int)
        self.assertIs(type(body["request_id"]), int)
        self.assertEqual(self.server.modality, "Mixed-Mode")
        result = self.cli("delete")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.server.requests), 2)
        self.assertEqual(self.server.requests[-1][1], "/modality/delete")
        self.assertEqual(set(json.loads(self.server.requests[-1][2])),
                         {"node_id", "modality", "timestamp_ms", "request_id"})

    def test_response_failures(self):
        for status, body in [(200, b'{"code": 9}'), (500, b'{"code": 0}'),
                             (200, b'broken'), (200, b'{"code": false}'),
                             (200, b'[]')]:
            with self.subTest(status=status, body=body):
                self.server.reply = status, body
                before = len(self.server.requests)
                result = self.cli("delete")
                self.assertEqual(result.returncode, 1)
                self.assertTrue(result.stderr)
                self.assertEqual(len(self.server.requests), before + 1)
                if body != b'broken':
                    self.assertEqual(json.loads(result.stdout), json.loads(body))

    def test_invalid_arguments_do_not_send(self):
        for action, extra in [("other", []), ("deploy", ["--modality", " "]),
                              ("deploy", ["--compute-config-percent", "101"]),
                              ("deploy", ["--storage-config-mb", "-1"]),
                              ("deploy", ["--forwarding-config-mbps", "nan"]),
                              ("delete", ["--storage-config-mb", "0"])]:
            with self.subTest(action=action, extra=extra):
                self.assertEqual(self.cli(action, *extra).returncode, 2)
        self.assertEqual(self.server.requests, [])

    def test_library_request_ids_and_raw_errors(self):
        first = self.client.deploy("node", "mode", request_id=123)
        self.assertEqual(first.payload["data"]["request_id"], 123)
        second = self.client.delete("node", "mode")
        third = self.client.delete("node", "mode")
        self.assertNotEqual(second.payload["data"]["request_id"], third.payload["data"]["request_id"])
        self.server.reply = 400, b'{"code": 1}'
        result = self.client.delete("node", "mode")
        self.assertEqual(result.status, 400)
        self.assertEqual(result.body, '{"code": 1}')
        self.assertEqual(result.payload, {"code": 1})

    def test_transport_failure(self):
        with patch.object(self.client.opener, "open", side_effect=TimeoutError("timeout")):
            with self.assertRaises(TransportFailure):
                self.client.delete("node", "mode")
        # Reserve a local port without listening, so connection refusal is deterministic.
        import socket
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            result = self.cli("delete", "--port", str(reserved.getsockname()[1]))
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stderr)

    def test_existing_suite_and_cleanup(self):
        args = api_test.parse_args(self.args)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(api_test.run_suite(args), 0)
        self.assertEqual([urlsplit(r[1]).path for r in self.server.requests],
                         ["/resource/status", "/modality/deploy", "/resource/config",
                          "/resource/status", "/modality/delete"])
        self.server.requests.clear()
        with patch.object(api_test, "validate_deploy_data", side_effect=api_test.TestFailure("bad data")):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(api_test.run_suite(args), 1)
        self.assertEqual([urlsplit(r[1]).path for r in self.server.requests],
                         ["/resource/status", "/modality/deploy", "/modality/delete"])

    def test_negative_deploy_cases_remain_sendable(self):
        cases = api_test.build_negative_cases(api_test.RequestIds(), node_id="node",
                                             modality="mode", compute=10, storage=128, forwarding=10)
        cases = [case for case in cases if case.path == "/modality/deploy"]
        self.assertEqual(len(cases), 15)
        for case in cases:
            self.client.request(case.method, case.path, json_body=case.json_body, raw_body=case.raw_body)
        bodies = [raw for _, _, raw, _ in self.server.requests]
        self.assertIn(b"{", bodies)
        parsed = [json.loads(raw) for raw in bodies if raw != b"{"]
        self.assertTrue(any("modality" not in body for body in parsed))
        self.assertTrue(any(body.get("modality") == 123 for body in parsed))


if __name__ == "__main__":
    unittest.main()
