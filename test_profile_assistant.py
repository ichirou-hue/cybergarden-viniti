"""The assistant may propose fields, but only sourced values pass validation."""
import os
import json
from threading import Thread
from unittest import TestCase, mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from profile_assistant import suggest
from server import Handler, ThreadingHTTPServer


class ProfileAssistantTest(TestCase):
    def test_suggestions_keep_only_verifiable_values(self):
        payload = {"question": "Какие поля нужны?", "profile": {"types": []},
                   "sources": [{"page": 2, "quote": "PCE reached 21.5% in the device."}]}
        response = {"suggestions": [
            {"entity": "Устройство", "field": "КПД", "type": "number",
             "value": "21.5%", "page": 2, "quote": "PCE reached 21.5%"},
            {"entity": "Устройство", "field": "Материал", "type": "text",
             "value": "perovskite", "page": 2, "quote": "invented quote"},
        ]}
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test", "OPENROUTER_MODEL": "test"}):
            result = suggest(payload, complete_fn=lambda *args, **kwargs: response)
        self.assertEqual(result["suggestions"][0]["value"], "21.5%")
        self.assertEqual(result["suggestions"][1]["value"], "")
        self.assertEqual(result["suggestions"][1]["quote"], "")

    def test_missing_configuration_is_explicit(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "", "OPENROUTER_MODEL": ""}):
            with self.assertRaisesRegex(RuntimeError, "не настроен"):
                suggest({"question": "Нужны поля", "sources": [], "profile": {"types": []}})

    def test_http_requires_server_configuration(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            request = Request(f"http://127.0.0.1:{server.server_port}/api/profile/assist",
                              data=json.dumps({"question": "Какие поля?", "sources": [],
                                               "profile": {"types": []}}).encode(),
                              headers={"Content-Type": "application/json"}, method="POST")
            with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "", "OPENROUTER_MODEL": ""}):
                with self.assertRaises(HTTPError) as caught:
                    urlopen(request, timeout=5)
            self.assertEqual(caught.exception.code, 503)
            caught.exception.close()
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
