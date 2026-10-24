"""Small SQLite adapter for STE's Python 3.5 bundle, which omits sqlite3.

Uses Windows' SQLite C API through bundled ctypes. Only the parameterized,
eager-result operations needed by ExperienceStore are provided; no chat data
is converted to another format and no executable or DLL is downloaded.
"""
import ctypes
import os


class DatabaseError(Exception):
    pass


Row = dict
_library = None


def library():
    global _library
    if _library is not None:
        return _library
    if os.name != "nt":
        raise RuntimeError("SQLite requires the sqlite3 module on this platform")
    # Load by an absolute system path, never from the current working directory.
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    directory = ctypes.create_unicode_buffer(32768)
    kernel.GetSystemDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
    kernel.GetSystemDirectoryW.restype = ctypes.c_uint
    length = kernel.GetSystemDirectoryW(directory, len(directory))
    if not length or length >= len(directory):
        raise RuntimeError("Cannot locate Windows SQLite")
    dll = ctypes.CDLL(os.path.join(directory.value, "winsqlite3.dll"))
    pointer, integer, string = ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p
    signatures = {
        "sqlite3_open_v2": (integer, [string, ctypes.POINTER(pointer), integer, string]),
        "sqlite3_close": (integer, [pointer]),
        "sqlite3_busy_timeout": (integer, [pointer, integer]),
        "sqlite3_errmsg": (string, [pointer]),
        "sqlite3_exec": (integer, [pointer, string, pointer, pointer, ctypes.POINTER(pointer)]),
        "sqlite3_free": (None, [pointer]),
        "sqlite3_prepare_v2": (integer, [pointer, string, integer, ctypes.POINTER(pointer), ctypes.POINTER(string)]),
        "sqlite3_bind_parameter_count": (integer, [pointer]),
        "sqlite3_bind_null": (integer, [pointer, integer]),
        "sqlite3_bind_int64": (integer, [pointer, integer, ctypes.c_longlong]),
        "sqlite3_bind_double": (integer, [pointer, integer, ctypes.c_double]),
        "sqlite3_bind_text": (integer, [pointer, integer, string, integer, pointer]),
        "sqlite3_step": (integer, [pointer]),
        "sqlite3_finalize": (integer, [pointer]),
        "sqlite3_column_count": (integer, [pointer]),
        "sqlite3_column_name": (string, [pointer, integer]),
        "sqlite3_column_type": (integer, [pointer, integer]),
        "sqlite3_column_int64": (ctypes.c_longlong, [pointer, integer]),
        "sqlite3_column_double": (ctypes.c_double, [pointer, integer]),
        "sqlite3_column_text": (pointer, [pointer, integer]),
        "sqlite3_column_blob": (pointer, [pointer, integer]),
        "sqlite3_column_bytes": (integer, [pointer, integer])}
    for name, signature in signatures.items():
        function = getattr(dll, name)
        function.restype, function.argtypes = signature
    _library = dll
    return dll


class Cursor:
    def __init__(self, rows):
        self.rows, self.offset = rows, 0

    def fetchone(self):
        if self.offset >= len(self.rows):
            return None
        row = self.rows[self.offset]
        self.offset += 1
        return row

    def fetchall(self):
        rows = self.rows[self.offset:]
        self.offset = len(self.rows)
        return rows

    def __iter__(self):
        return iter(self.fetchall())


class Connection:
    def __init__(self, path, timeout=15):
        self.api, self.handle = library(), ctypes.c_void_p()
        self.row_factory = Row
        result = self.api.sqlite3_open_v2(os.path.abspath(path).encode("utf-8"), ctypes.byref(self.handle), 2 | 4 | 0x10000, None)
        if result:
            try:
                self.check(result)
            finally:
                self.close()
        try:
            self.check(self.api.sqlite3_busy_timeout(self.handle, int(timeout * 1000)))
        except Exception:
            self.close()
            raise

    def check(self, result):
        if result:
            message = self.api.sqlite3_errmsg(self.handle) if self.handle else None
            raise DatabaseError(message.decode("utf-8", "replace") if message else "SQLite error " + str(result))

    def execute(self, sql, parameters=()):
        statement, tail = ctypes.c_void_p(), ctypes.c_char_p()
        encoded_sql = sql.encode("utf-8")
        self.check(self.api.sqlite3_prepare_v2(self.handle, encoded_sql, -1, ctypes.byref(statement), ctypes.byref(tail)))
        if not statement:
            return Cursor([])
        try:
            if tail.value and tail.value.strip():
                raise DatabaseError("execute accepts one SQL statement")
            if self.api.sqlite3_bind_parameter_count(statement) != len(parameters):
                raise DatabaseError("Incorrect number of SQL parameters")
            for index, value in enumerate(parameters, 1):
                if value is None:
                    result = self.api.sqlite3_bind_null(statement, index)
                elif isinstance(value, int):
                    result = self.api.sqlite3_bind_int64(statement, index, value)
                elif isinstance(value, float):
                    result = self.api.sqlite3_bind_double(statement, index, value)
                elif isinstance(value, str):
                    encoded = value.encode("utf-8")
                    # SQLITE_TRANSIENT copies the buffer before Python releases it.
                    result = self.api.sqlite3_bind_text(statement, index, encoded, len(encoded), ctypes.c_void_p(-1))
                else:
                    raise DatabaseError("Unsupported SQL parameter type")
                self.check(result)
            names = [self.api.sqlite3_column_name(statement, i).decode("utf-8") for i in range(self.api.sqlite3_column_count(statement))]
            rows = []
            while True:
                result = self.api.sqlite3_step(statement)
                if result == 101:  # SQLITE_DONE
                    break
                if result != 100:  # SQLITE_ROW
                    self.check(result)
                row = {}
                for i, name in enumerate(names):
                    kind = self.api.sqlite3_column_type(statement, i)
                    if kind == 1:
                        value = self.api.sqlite3_column_int64(statement, i)
                    elif kind == 2:
                        value = self.api.sqlite3_column_double(statement, i)
                    elif kind in (3, 4):
                        address = (self.api.sqlite3_column_text if kind == 3 else self.api.sqlite3_column_blob)(statement, i)
                        value = ctypes.string_at(address, self.api.sqlite3_column_bytes(statement, i)) if address else b""
                        if kind == 3:
                            value = value.decode("utf-8")
                    else:
                        value = None
                    row[name] = value
                rows.append(row)
            return Cursor(rows)
        finally:
            self.api.sqlite3_finalize(statement)

    def executescript(self, sql):
        error = ctypes.c_void_p()
        try:
            self.check(self.api.sqlite3_exec(self.handle, sql.encode("utf-8"), None, None, ctypes.byref(error)))
        finally:
            if error:
                self.api.sqlite3_free(error)

    def __enter__(self):
        self.execute("BEGIN")
        return self

    def __exit__(self, kind, value, traceback):
        self.execute("COMMIT" if kind is None else "ROLLBACK")

    def close(self):
        if self.handle:
            result = self.api.sqlite3_close(self.handle)
            self.check(result)
            self.handle = ctypes.c_void_p()


def connect(path, timeout=15):
    return Connection(path, timeout)
