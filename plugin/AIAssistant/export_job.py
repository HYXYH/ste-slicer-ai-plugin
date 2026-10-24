"""Native WriteFileJob orchestration; the GUI must never wait for a writer."""
import logging
import os
import tempfile
import threading
import zipfile

from .session import ApiError
from .agent.contracts import fail

log = logging.getLogger(__name__)
EXPORT_TOOLS = ("save_project", "export_gcode")


class NativeExport:
    def __init__(self, api, command):
        self.api, self.bridge, self.command = api, api.bridge, command
        self.stream, self.temporary, self.job = None, None, None
        self.verify_lock, self.verifying = threading.Lock(), False
        self.cleanup_lock, self.verified = threading.Lock(), threading.Event()
        self.error, self.size = None, 0

    def start(self):
        # Preparation and freshness checks belong to the Qt queue. Native writers
        # marshal their own Qt-only work (workspace serialization / OpenGL preview).
        from UM.FileHandler.WriteFileJob import WriteFileJob
        arguments = self.command["arguments"]
        target = self.api.exports.get(arguments["export_target_id"])
        if not target or target["session_id"] != arguments["session_id"]:
            fail("Выберите папку экспорта", "EXPORT_FAILED", 409)
        self.binary = self.command["name"] == "save_project"
        self.filename = "project.3mf" if self.binary else "result.gcode"
        self.destination = os.path.join(target["path"], self.filename)
        self.overwrite = target["overwrite"]
        if os.path.lexists(self.destination) and not self.overwrite:
            fail("Файл уже существует. Подтвердите замену в интерфейсе.", "EXPORT_EXISTS", 409)
        self.reference = self.api.get_scene_snapshot()["snapshot_ref"]
        if self.command["expected"] and self.reference != self.command["expected"]:
            fail("Сцена изменилась перед сохранением", "STALE_INPUT", 409)
        handler = self.api.app.getWorkspaceFileHandler() if self.binary else self.api.app.getMeshFileHandler()
        writer = handler.getWriter("3MFWriter" if self.binary else "GCodeWriter")
        if writer is None:
            fail("Штатный экспортёр недоступен", "EXPORT_FAILED", 409)
        nodes = [self.api.app.getController().getScene().getRoot()] if self.binary else list(self.api.nodes().values())
        mode = writer.OutputMode.BinaryMode if self.binary else writer.OutputMode.TextMode
        try:
            descriptor, self.temporary = tempfile.mkstemp(prefix=".assistant-", suffix=".tmp", dir=target["path"])
            self.stream = os.fdopen(descriptor, "wb") if self.binary else os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
            # This is the same job contract used by LocalFileOutputDevice. Calling
            # writer.write on Qt deadlocks STE 7.10's call_on_qt_thread decorator.
            self.job = WriteFileJob(writer, self.stream, nodes, mode)
            self.job.setFileName(self.destination)
            self.job.setAddToRecentFiles(False)
            self.job.finished.connect(self._written)
            self.job.start()
            log.info("Assistant native export started: %s", self.filename)
        except OSError:
            self._cleanup()
            fail("Не удалось создать файл. Проверьте права и свободное место.", "EXPORT_FAILED", 409)
        except Exception:
            self._cleanup()
            raise

    def _written(self, job):
        with self.verify_lock:
            if self.verifying:
                return
            self.verifying = True
        # A finished signal may arrive on Qt. Flush and ZIP verification must not
        # occupy that thread; only publication and the final scene check return there.
        worker = threading.Thread(target=self._verify, args=(job,), name="STE export verification")
        worker.daemon = True
        worker.start()

    def _verify(self, job):
        try:
            self.stream.close()
            if not job.getResult() or not os.path.getsize(self.temporary):
                fail("Экспортёр не записал результат", "EXPORT_FAILED", 409)
            if self.binary:
                with zipfile.ZipFile(self.temporary) as archive:
                    if archive.testzip() is not None or not any(n.endswith(".model") for n in archive.namelist()):
                        fail("Записанный 3MF не читается", "EXPORT_FAILED", 409)
            self.size = os.path.getsize(self.temporary)
        except ApiError as error:
            self.error = error
        except Exception:
            log.exception("Assistant native export verification failed")
            self.error = ApiError("Не удалось записать файл. Проверьте права и свободное место.", 409, "EXPORT_FAILED")
        self.verified.set()
        if not self.bridge.post(self._complete):
            # Shutdown already released the waiter. A late writer must never
            # publish a file or leave its own temporary file behind.
            self._cleanup()

    def _complete(self):
        # Completion and abandonment share the lock: a timed-out request cannot
        # publish a final file later. No writer wait or ZIP scan happens here.
        with self.bridge.lock:
            try:
                if self.command["abandoned"] or self.bridge.closed:
                    return
                if self.error:
                    raise self.error
                actual = self.api.get_scene_snapshot()["snapshot_ref"]
                if actual != self.reference:
                    fail("Сцена изменилась при сохранении. Повторите расчёт.", "STALE_INPUT", 409)
                if self.overwrite:
                    os.replace(self.temporary, self.destination)
                else:
                    # On Windows rename refuses to replace an existing destination.
                    if os.name == "nt":
                        os.rename(self.temporary, self.destination)
                    else:
                        os.link(self.temporary, self.destination)
                        os.unlink(self.temporary)
                result = {"filename": self.filename, "size": self.size, "written": True}
                self.command["result"] = {"result": result, "snapshot_ref": actual} if self.command["expected"] else result
                log.info("Assistant native export completed: %s (%d bytes)", self.filename, self.size)
            except ApiError as error:
                self.command["error"] = error
            except FileExistsError:
                self.command["error"] = ApiError("Файл уже существует. Подтвердите замену в интерфейсе.", 409, "EXPORT_EXISTS")
            except Exception:
                log.exception("Assistant native export publication failed")
                self.command["error"] = ApiError("Не удалось сохранить файл. Проверьте журнал STE Slicer.", 409, "EXPORT_FAILED")
            finally:
                self._cleanup()
                self.api.export_active = None
                self.command["done"].set()

    def _cleanup(self):
        with self.cleanup_lock:
            try:
                if self.stream is not None and not self.stream.closed:
                    self.stream.close()
                if self.temporary and os.path.exists(self.temporary):
                    os.unlink(self.temporary)
            except OSError:
                log.exception("Assistant could not remove its export temporary file")

    def interrupt(self):
        # Do not close a stream still used by a native writer, or join its thread.
        self.command["abandoned"] = True
        self.command["error"] = ApiError("Приложение закрыто", 409, "INTERRUPTED")
        self.command["done"].set()
        if self.verified.is_set():
            self._cleanup()
