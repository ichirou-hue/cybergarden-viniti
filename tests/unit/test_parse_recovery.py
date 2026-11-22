"""Прерванный разбор получает терминальный статус и не переигрывается сам.

Сценарий, который здесь закреплён: daemon thread запустил subprocess, сервер
остановили по Ctrl+C, ``job.json`` остался ``running`` без bundle и parse.log.
Новый сервер обязан либо показать понятную ошибку, либо возобновить разбор по
явному правилу. Правило выбрано одно: терминальная ошибка, потому что доказать
завершение старого рабочего процесса нельзя, а выдумывать результат запрещено.
"""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest import mock
from contextlib import redirect_stdout
import io

from viniti import server
from viniti.server import Handler


def write_job(folder, job):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")


def read_job(folder):
    return json.loads((folder / "job.json").read_text(encoding="utf-8"))


class RecoverParseJobsTest(unittest.TestCase):
    """Startup recovery of jobs left by a previous server process."""

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_active_jobs_become_terminal_errors_without_replaying_work(self):
        running = {"id": "a" * 16, "status": "running", "owner_user_id": 7,
                   "sha256": "abc", "started_at": 10.0, "updated_at": 10.0,
                   "document_id": "solar:card", "filename": "D001.pdf"}
        queued = {"id": "b" * 16, "status": "queued", "owner_user_id": 8,
                  "created_at": 5.0, "updated_at": 5.0}
        finished = {"id": "c" * 16, "status": "ok", "owner_user_id": 7,
                    "summary": {"pages": 2}, "updated_at": 1.0}
        for job in (running, queued, finished):
            write_job(self.root / job["id"], job)

        with mock.patch.object(server.subprocess, "run",
                               side_effect=AssertionError("старый job не перезапускается")), \
                mock.patch.object(server.subprocess, "Popen",
                                  side_effect=AssertionError("старый job не перезапускается")):
            recovered = server.recover_parse_jobs(self.root)

        self.assertEqual(recovered, 2)
        first = read_job(self.root / running["id"])
        self.assertEqual(first["status"], "error")
        self.assertEqual(first["error"], server.PARSE_INTERRUPTED_ERROR)
        self.assertGreater(first["updated_at"], 10.0)
        # Ownership and input identity stay: the owner still sees their own job.
        self.assertEqual(first["owner_user_id"], 7)
        self.assertEqual(first["sha256"], "abc")
        self.assertEqual(first["document_id"], "solar:card")
        self.assertEqual(first["started_at"], 10.0)
        second = read_job(self.root / queued["id"])
        self.assertEqual(second["status"], "error")
        self.assertEqual(second["owner_user_id"], 8)
        untouched = read_job(self.root / finished["id"])
        self.assertEqual(untouched, finished)
        # No bundle, parse.log or temp file: recovery only rewrites job.json.
        self.assertEqual(sorted(item.name for item in (self.root / running["id"]).iterdir()),
                         ["job.json"])

    def test_unknown_or_broken_entries_are_left_alone(self):
        foreign = self.root / "not-a-job-id"
        write_job(foreign, {"status": "running"})
        broken = self.root / ("d" * 16)
        broken.mkdir()
        (broken / "job.json").write_text("{не json", encoding="utf-8")
        empty = self.root / ("e" * 16)
        empty.mkdir()
        (self.root / "notes.txt").write_text("x", encoding="utf-8")

        self.assertEqual(server.recover_parse_jobs(self.root), 0)
        self.assertEqual(read_job(foreign)["status"], "running")
        self.assertEqual((broken / "job.json").read_text(encoding="utf-8"), "{не json")
        self.assertFalse((empty / "job.json").exists())

    def test_missing_root_is_not_an_error(self):
        self.assertEqual(server.recover_parse_jobs(self.root / "absent"), 0)


class StaleRunningJobTest(unittest.TestCase):
    """A running job past its own timeout must not stay active forever."""

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.handler = Handler.__new__(Handler)
        self.handler.server = SimpleNamespace(parse_root=self.root)
        self.handler.send_json = mock.Mock()
        self.session = {"user": {"id": 7}}

    def start_job(self, started_at):
        job = {"id": "f" * 16, "status": "running", "owner_user_id": 7,
               "created_at": 1.0, "updated_at": 1.0, "started_at": started_at}
        write_job(self.root / job["id"], job)
        return job

    def test_running_job_past_the_timeout_is_persisted_as_error(self):
        job = self.start_job(server.time() - server.PARSE_TIMEOUT - 120)

        self.handler.parse_get(f"/api/parse/{job['id']}", self.session)
        payload = self.handler.send_json.call_args.args[0]
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error"], server.PARSE_STALE_ERROR)
        stored = read_job(self.root / job["id"])
        self.assertEqual(stored["status"], "error")
        self.assertEqual(stored["error"], server.PARSE_STALE_ERROR)

        # The active list agrees with the single-job route instead of showing it.
        self.handler.parse_jobs({"active": ["1"]}, self.session)
        listing = self.handler.send_json.call_args.args[0]
        self.assertEqual(listing["total"], 0)

    def test_recent_running_job_is_left_alone(self):
        job = self.start_job(server.time() - 5)

        self.handler.parse_get(f"/api/parse/{job['id']}", self.session)
        payload = self.handler.send_json.call_args.args[0]
        self.assertEqual(payload["status"], "running")
        self.assertEqual(read_job(self.root / job["id"])["status"], "running")

    def test_queued_job_is_not_age_checked(self):
        # A queued job may legitimately wait for a parse slot that long.
        job = {"id": "1" * 16, "status": "queued", "owner_user_id": 7,
               "created_at": server.time() - server.PARSE_TIMEOUT - 600}
        write_job(self.root / job["id"], job)

        self.handler.parse_get(f"/api/parse/{job['id']}", self.session)
        payload = self.handler.send_json.call_args.args[0]
        self.assertEqual(payload["status"], "queued")

    def test_active_list_persists_a_stale_verdict_without_a_prior_read(self):
        job = self.start_job(server.time() - server.PARSE_TIMEOUT - 120)
        job.update(sha256="abc", document_id="solar:card", filename="D001.pdf")
        write_job(self.root / job["id"], job)

        self.handler.parse_jobs({"active": ["1"]}, self.session)
        listing = self.handler.send_json.call_args.args[0]
        self.assertEqual(listing["total"], 0)

        stored = read_job(self.root / job["id"])
        self.assertEqual(stored["status"], "error")
        self.assertEqual(stored["error"], server.PARSE_STALE_ERROR)
        # Ownership and input identity survive the watchdog write.
        self.assertEqual(stored["owner_user_id"], 7)
        self.assertEqual(stored["sha256"], "abc")
        self.assertEqual(stored["document_id"], "solar:card")
        self.assertEqual(stored["started_at"], job["started_at"])

    def test_active_list_leaves_another_owners_stale_job_alone(self):
        job = self.start_job(server.time() - server.PARSE_TIMEOUT - 120)
        job["owner_user_id"] = 8
        write_job(self.root / job["id"], job)

        self.handler.parse_jobs({"active": ["1"]}, self.session)
        listing = self.handler.send_json.call_args.args[0]
        self.assertEqual(listing["total"], 0)
        self.assertEqual(read_job(self.root / job["id"])["status"], "running")

    def test_refresh_does_not_overwrite_a_job_finished_meanwhile(self):
        stale = {"id": "9" * 16, "status": "running", "owner_user_id": 7,
                 "created_at": 1.0, "updated_at": 1.0,
                 "started_at": server.time() - server.PARSE_TIMEOUT - 120}
        folder = self.root / stale["id"]
        write_job(folder, stale)
        snapshot = read_job(folder)
        finished = dict(snapshot, status="ok", summary={"pages": 1},
                        updated_at=server.time())
        write_job(folder, finished)

        result = server.refresh_parse_job(folder, snapshot)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(read_job(folder)["status"], "ok")


class JobFileTest(unittest.TestCase):
    """The atomic ``job.json`` write must survive concurrent readers."""

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_transient_sharing_error_on_replace_is_retried(self):
        folder = self.root / "job"
        folder.mkdir()
        real_replace = server.os.replace
        calls = []

        def flaky(source, target):
            calls.append(target)
            if len(calls) == 1:
                raise PermissionError(13, "sharing violation")
            return real_replace(source, target)

        with mock.patch.object(server.os, "replace", side_effect=flaky), \
                mock.patch.object(server, "PARSE_JOB_REPLACE_DELAY", 0):
            server.write_parse_job(folder, {"status": "ok"})

        self.assertEqual(len(calls), 2)
        self.assertEqual(read_job(folder), {"status": "ok"})
        self.assertEqual([item.name for item in folder.iterdir()], ["job.json"])

    def test_persistent_replace_error_is_raised_and_temp_removed(self):
        folder = self.root / "job"
        folder.mkdir()
        with mock.patch.object(server.os, "replace",
                               side_effect=PermissionError(13, "denied")), \
                mock.patch.object(server, "PARSE_JOB_REPLACE_ATTEMPTS", 3), \
                mock.patch.object(server, "PARSE_JOB_REPLACE_DELAY", 0):
            with self.assertRaises(PermissionError):
                server.write_parse_job(folder, {"status": "ok"})

        self.assertEqual(list(folder.iterdir()), [])

    def test_reader_never_sees_a_missing_or_partial_job(self):
        folder = self.root / "job"
        folder.mkdir()
        server.write_parse_job(folder, {"status": "running", "n": -1})
        stop = Event()
        broken = []

        def reader():
            while not stop.is_set():
                job = server.read_parse_job(folder)
                if not job or job.get("status") != "running":
                    broken.append(job)
                    return

        thread = Thread(target=reader)
        thread.start()
        try:
            for index in range(200):
                server.write_parse_job(folder, {"status": "running", "n": index})
        finally:
            stop.set()
            thread.join()

        self.assertEqual(broken, [])
        self.assertEqual(server.read_parse_job(folder)["n"], 199)


class ParseChildrenTest(unittest.TestCase):
    """Server stop must stop the parser child, not orphan it."""

    def test_shutdown_refuses_new_work(self):
        children = server.ParseChildren()
        self.assertEqual(children.terminate_all(), 0)
        self.assertTrue(children.closing)
        with self.assertRaises(server.ParseClosed):
            children.spawn(["python"], env={}, timeout=1)

    def test_spawn_puts_child_in_its_own_group(self):
        process = mock.Mock()
        process.communicate.return_value = ("{}", "")
        process.returncode = 0
        with mock.patch.object(server.subprocess, "Popen", return_value=process) as popen:
            children = server.ParseChildren()
            children.spawn(["python"], env={}, timeout=5)

        options = popen.call_args.kwargs
        if os.name == "nt":
            self.assertEqual(options["creationflags"],
                             server.subprocess.CREATE_NEW_PROCESS_GROUP)
            self.assertNotIn("start_new_session", options)
        else:
            self.assertTrue(options["start_new_session"])
            self.assertNotIn("creationflags", options)

    def test_terminate_all_stops_registered_child_tree_and_reaps_it(self):
        children = server.ParseChildren()
        process = mock.Mock(pid=4242, returncode=None)
        with children._lock:
            children._processes.add(process)

        if os.name == "nt":
            taskkill = mock.Mock(return_value=SimpleNamespace(returncode=0))
            with mock.patch.object(server.subprocess, "run", taskkill):
                self.assertEqual(children.terminate_all(), 1)
            self.assertEqual(taskkill.call_args.args[0],
                             ["taskkill", "/T", "/F", "/PID", "4242"])
        else:
            with mock.patch.object(server.os, "killpg") as killpg:
                self.assertEqual(children.terminate_all(), 1)
            killpg.assert_called_once_with(4242, server.signal.SIGTERM)
        process.wait.assert_called_once()

    def test_terminate_all_kills_a_child_that_ignores_the_first_stop(self):
        children = server.ParseChildren()
        process = mock.Mock(pid=4243, returncode=None)
        process.wait.side_effect = [server.subprocess.TimeoutExpired("parse", 1), None]
        with children._lock:
            children._processes.add(process)

        if os.name == "nt":
            taskkill = mock.Mock(return_value=SimpleNamespace(returncode=0))
            with mock.patch.object(server.subprocess, "run", taskkill):
                children.terminate_all(grace=0)
            self.assertEqual(taskkill.call_count, 2)
        else:
            with mock.patch.object(server.os, "killpg") as killpg:
                children.terminate_all(grace=0)
            self.assertEqual([call.args[1] for call in killpg.call_args_list],
                             [server.signal.SIGTERM, server.signal.SIGKILL])
        self.assertEqual(process.wait.call_count, 2)

    def test_failed_tree_stop_falls_back_to_the_direct_process(self):
        process = mock.Mock(pid=4244, returncode=None)
        if os.name == "nt":
            taskkill = mock.Mock(return_value=SimpleNamespace(returncode=1))
            with mock.patch.object(server.subprocess, "run", taskkill):
                server.stop_parse_tree(process, force=False)
        else:
            with mock.patch.object(server.os, "killpg", side_effect=OSError("gone")):
                server.stop_parse_tree(process, force=False)
        process.terminate.assert_called_once_with()

    def test_timeout_kills_child_and_propagates(self):
        children = server.ParseChildren()
        process = mock.Mock()
        process.communicate.side_effect = [
            server.subprocess.TimeoutExpired("parse", 1),
            ("", ""),
        ]
        with mock.patch.object(server.subprocess, "Popen", return_value=process), \
                mock.patch.object(server, "stop_parse_tree") as stopper:
            with self.assertRaises(server.subprocess.TimeoutExpired):
                children.spawn(["python"], env={}, timeout=1)
        stopper.assert_called_once_with(process, force=True)
        self.assertEqual(process.communicate.call_count, 2)
        self.assertEqual(len(children._processes), 0)

    def test_run_parse_child_without_registry_keeps_subprocess_run(self):
        completed = SimpleNamespace(returncode=0, stdout="{}", stderr="")
        with mock.patch.object(server.subprocess, "run", return_value=completed) as runner:
            result = server.run_parse_child(None, ["python", "x"], env={}, timeout=5)
        self.assertIs(result, completed)
        self.assertEqual(runner.call_args.args[0], ["python", "x"])
        self.assertEqual(runner.call_args.kwargs["timeout"], 5)


class ShutdownWorkerTest(unittest.TestCase):
    """Worker and shutdown agree on the terminal status."""

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def handler(self, children):
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(parse_root=self.root, parse_children=children)
        return handler

    def job_folder(self, job):
        folder = self.root / job["id"]
        write_job(folder, job)
        return folder

    def test_child_stopped_by_shutdown_is_reported_as_interrupted(self):
        children = server.ParseChildren()
        children.terminate_all()
        folder = self.job_folder({"id": "2" * 16, "status": "queued",
                                  "filename": "D001.pdf", "profile": "solar"})
        completed = SimpleNamespace(returncode=-15, stdout="", stderr="")

        with mock.patch.object(server, "run_parse_child", return_value=completed):
            self.handler(children).run_parse(folder, Path("solar.json"), "pdfplumber", "auto", False)

        job = read_job(folder)
        self.assertEqual(job["status"], "error")
        self.assertEqual(job["error"], server.PARSE_INTERRUPTED_ERROR)
        self.assertEqual(job["returncode"], -15)

    def test_job_that_never_started_is_interrupted_not_internal_error(self):
        children = server.ParseChildren()
        children.terminate_all()
        folder = self.job_folder({"id": "3" * 16, "status": "queued",
                                  "filename": "D001.pdf", "profile": "solar"})

        self.handler(children).run_parse(folder, Path("solar.json"), "pdfplumber", "auto", False)

        job = read_job(folder)
        self.assertEqual(job["status"], "error")
        self.assertEqual(job["error"], server.PARSE_INTERRUPTED_ERROR)

    def test_shutdown_helper_marks_remaining_active_jobs(self):
        folder = self.job_folder({"id": "4" * 16, "status": "running", "owner_user_id": 4})
        instance = SimpleNamespace(parse_root=self.root,
                                   parse_children=server.ParseChildren())

        server.shutdown_parse_workers(instance)

        job = read_job(folder)
        self.assertEqual(job["status"], "error")
        self.assertEqual(job["error"], server.PARSE_INTERRUPTED_ERROR)
        self.assertEqual(job["owner_user_id"], 4)


class StartupWiringTest(unittest.TestCase):
    """``main`` must recover before the first request is served."""

    def test_main_recovers_stale_jobs_before_serving(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stale = root / "parses" / ("5" * 16)
            write_job(stale, {"id": "5" * 16, "status": "running", "owner_user_id": 3})
            served = []

            class FakeServer:
                server_port = 1

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                def serve_forever(self):
                    served.append(read_job(stale)["status"])

            with mock.patch.object(server, "ThreadingHTTPServer", return_value=FakeServer()), \
                    mock.patch.object(server, "Library"), \
                    mock.patch.object(server, "RagStore"), \
                    mock.patch.object(server.paths, "DATA_DIR", root), \
                    mock.patch.object(server.sys, "argv", [
                        "server.py", "--port", "0",
                        "--db", str(root / "papers.sqlite3"),
                        "--data", str(root / "auth.sqlite3")]):
                with redirect_stdout(io.StringIO()):
                    server.main()

            # Recovery ran before serve_forever: the stale job is already terminal.
            self.assertEqual(served, ["error"])
            self.assertEqual(read_job(stale)["status"], "error")
            self.assertEqual(read_job(stale)["error"], server.PARSE_INTERRUPTED_ERROR)


if __name__ == "__main__":
    unittest.main()
