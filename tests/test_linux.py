# -*- coding: utf-8 -*-
"""Port Linux: a logica independente de plataforma dos modulos de wispr/linux/.

Roda nas duas plataformas (o que so faz sentido no Linux pula com motivo).
Nada aqui conecta no D-Bus de sessao, no Wayland do usuario ou roda wl-copy:
o kglobalaccel, o fake_input e o clipboard sao dublados, e o compositor dos
testes de protocolo e' um servidor falso num socket em diretorio temporario.
`_safety` estoura se algum caminho escapar.
"""
from __future__ import annotations

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)

import _safety  # noqa: E402  stubs antes de qualquer import de wispr

import ast  # noqa: E402
import fcntl  # noqa: E402
import shutil  # noqa: E402
import socket  # noqa: E402
import struct  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import types  # noqa: E402
import unittest  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest import mock  # noqa: E402

from wispr import config  # noqa: E402

_kde, _why_kde = _safety.load_pure("wispr.linux.kde")
_wl, _why_wl = _safety.load_pure("wispr.linux.wayland")
_lhk, _why_lhk = _safety.load_pure("wispr.linux.hotkey")
_linj, _why_linj = _safety.load_pure("wispr.linux.inject")
_kg, _why_kg = _safety.load_pure("wispr.linux.keyguard")
_audio, _why_audio = _safety.load_pure("wispr.audio")
_stt, _why_stt = _safety.load_pure("wispr.stt")
_app, _why_app = _safety.load_pure("wispr.app")
_stub, _why_stub = _safety.load_pure("wispr.win32stub")
_clip, _why_clip = _safety.load_pure("wispr.linux.clipboard")

_LINUX = _sys.platform.startswith("linux")
_ROOT = Path(_safety.ROOT)


# --------------------------------------------------------------------------- #
# 1. paridade do Delivery / sanitize entre Windows e Linux
# --------------------------------------------------------------------------- #

def _segments(path: Path) -> dict:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in (
                "Delivery", "sanitize", "utf16_units"):
            out[node.name] = ast.get_source_segment(src, node)
    lines = [ln for ln in src.splitlines() if ln.startswith("_CTRL_MAP")]
    out["_CTRL_MAP"] = "\n".join(lines)
    return out


class DeliveryParityTest(unittest.TestCase):
    """wispr/delivery.py e' uma COPIA das pecas puras do wispr/inject.py (que nao
    importa fora do Windows). Copia que diverge quebra o contrato numa
    plataforma so'; este teste e' o alarme."""

    def test_the_four_pieces_are_identical(self):
        win = _segments(_ROOT / "wispr" / "inject.py")
        lin = _segments(_ROOT / "wispr" / "delivery.py")
        for name in ("Delivery", "sanitize", "utf16_units", "_CTRL_MAP"):
            with self.subTest(piece=name):
                self.assertTrue(win.get(name), "%s sumiu de wispr/inject.py" % name)
                self.assertEqual(lin.get(name), win.get(name),
                                 "wispr/delivery.py divergiu de wispr/inject.py em %s" % name)


# --------------------------------------------------------------------------- #
# 2. atalhos: codigos Qt do kglobalaccel
# --------------------------------------------------------------------------- #

@unittest.skipUnless(_kde, _why_kde or "wispr.linux.kde indisponivel")
class QtChordTest(unittest.TestCase):

    def test_win_a_is_meta_a(self):
        self.assertEqual(_kde.parse_chord("win+a"), _kde.QT_META | 0x41)

    def test_the_windows_aliases_still_parse(self):
        for spec in ("super+a", "meta+a", "Win-A", " WIN+a "):
            with self.subTest(spec=spec):
                self.assertEqual(_kde.parse_chord(spec), _kde.QT_META | 0x41)
        self.assertEqual(_kde.parse_chord("ctrl+alt+a"), _kde.QT_CTRL | _kde.QT_ALT | 0x41)

    def test_the_default_hotkey_parses(self):
        self.assertEqual(_kde.parse_chord(config.DEFAULTS["hotkey"]), _kde.QT_META | 0x41)

    def test_a_chord_needs_a_modifier(self):
        with self.assertRaises(ValueError):
            _kde.parse_chord("a")
        with self.assertRaises(ValueError):
            _kde.parse_chord("hyper+a")
        with self.assertRaises(ValueError):
            _kde.parse_chord("win+naoexiste")

    def test_enter_covers_the_keypad_enter_too(self):
        # No Windows os dois sao VK_RETURN; no Qt sao teclas diferentes.
        self.assertEqual(_kde.single_key_codes("enter"),
                         [0x01000004, _kde.QT_KEYPAD | 0x01000005])
        self.assertEqual(_kde.single_key_codes("esc"), [0x01000000])

    def test_f_keys_follow_qt(self):
        self.assertEqual(_kde.qt_key("f1"), 0x01000030)
        self.assertEqual(_kde.qt_key("f24"), 0x01000047)   # o F24 medido no probe

    def test_describe_is_readable(self):
        self.assertEqual(_kde.describe(_kde.QT_META | 0x41), "Meta+A")
        self.assertEqual(_kde.describe(_kde.QT_KEYPAD | 0x01000005), "Num+Enter")


# --------------------------------------------------------------------------- #
# 3. HotkeyEngine do Linux, com o kglobalaccel dublado
# --------------------------------------------------------------------------- #

class FakeKGA:
    """Duble do kde.KGlobalAccel. Registra cada chamada; nunca fala D-Bus."""

    def __init__(self, dictate_keys=None, arm_keys=True):
        self.calls = []
        self.dictate_keys = dictate_keys
        self.arm_keys = arm_keys

    def register(self, aid):
        self.calls.append(("register", aid[1]))

    def set_keys(self, aid, keys, flags):
        self.calls.append(("set_keys", aid[1], list(keys), flags))
        if self.dictate_keys is not None and aid[1] == "dictate":
            return list(self.dictate_keys)
        return list(keys)

    def arm(self, aid, keys):
        self.calls.append(("arm", aid[1]))
        return list(keys) if self.arm_keys else []

    def disarm(self, aid):
        self.calls.append(("disarm", aid[1]))

    def set_inactive(self, aid):
        self.calls.append(("inactive", aid[1]))

    def owners_of(self, key):
        return [("outro", "acao")]

    def keys(self, aid):
        return [_kde.QT_META | 0x41]

    def is_active(self, path=None):
        return True


@unittest.skipUnless(_lhk and _kde, _why_lhk or _why_kde or "wispr.linux.hotkey indisponivel")
class LinuxHotkeyEngineTest(unittest.TestCase):

    def engine(self, kga=None):
        fired = []
        eng = _lhk.HotkeyEngine(lambda: fired.append("start"),
                                lambda: fired.append("stop"),
                                lambda: fired.append("cancel"),
                                cfg=config.Config(dict(config.DEFAULTS)))
        eng._kga = kga or FakeKGA()
        return eng, fired

    def drain(self, eng, events):
        for ev in events:
            eng.q.put(ev)
        eng.q.put(None)
        eng._worker()

    # -- a regra das duas flags, sem D-Bus
    def test_desired_arms_follow_the_two_flags(self):
        self.assertEqual(_lhk.desired_arms(False, False), {"stop": False, "cancel": False})
        self.assertEqual(_lhk.desired_arms(True, True), {"stop": True, "cancel": True})
        # transcrevendo: Enter ja e' do usuario, Esc ainda cancela
        self.assertEqual(_lhk.desired_arms(False, True), {"stop": False, "cancel": True})

    def test_return_is_only_registered_while_recording(self):
        eng, _ = self.engine()
        kga = eng._kga
        eng.recording = True
        eng._apply_arms()
        self.assertIn(("arm", "stop"), kga.calls)
        self.assertNotIn(("arm", "cancel"), kga.calls)
        kga.calls.clear()
        eng.recording = False
        eng._apply_arms()
        self.assertEqual(kga.calls, [("disarm", "stop")])

    def test_esc_survives_the_enter(self):
        eng, _ = self.engine()
        kga = eng._kga
        eng.cancellable = True
        eng.recording = True
        eng._apply_arms()
        kga.calls.clear()
        eng.recording = False          # Enter: a transcricao comeca
        eng._apply_arms()
        self.assertEqual(kga.calls, [("disarm", "stop")])   # Esc continua armado
        kga.calls.clear()
        eng.cancellable = False        # o app terminou o ditado
        eng._apply_arms()
        self.assertEqual(kga.calls, [("disarm", "cancel")])

    def test_unchanged_flags_make_no_dbus_call(self):
        eng, _ = self.engine()
        eng._apply_arms()
        eng._apply_arms()
        self.assertEqual(eng._kga.calls, [])

    def test_writing_a_flag_wakes_the_arm_thread(self):
        eng, _ = self.engine()
        eng._arm_evt.clear()
        eng.recording = True
        self.assertTrue(eng._arm_evt.is_set())

    def test_start_arms_esc_before_the_app_runs(self):
        eng, fired = self.engine()
        seen = []
        eng._cbs["start"] = lambda: seen.append(eng.cancellable)
        self.drain(eng, ["start"])
        self.assertEqual(seen, [True])

    def test_double_tap_collapses(self):
        eng, fired = self.engine()
        eng._cbs["start"] = lambda: (fired.append("start"), setattr(eng, "recording", True))
        self.drain(eng, ["start", "start"])
        self.assertEqual(fired, ["start"])

    def test_dictate_stops_when_return_could_not_be_registered(self):
        eng, fired = self.engine()
        eng.recording = True
        eng._arm_ok["stop"] = False
        self.drain(eng, ["start"])
        self.assertEqual(fired, ["stop"])

    def test_dictate_does_not_stop_when_return_is_registered(self):
        eng, fired = self.engine()
        eng.recording = True
        self.drain(eng, ["start"])
        self.assertEqual(fired, [])

    def test_stop_and_cancel_gating_matches_windows(self):
        eng, fired = self.engine()
        self.drain(eng, ["stop", "cancel"])
        self.assertEqual(fired, [])
        eng, fired = self.engine()
        eng.cancellable = True
        self.drain(eng, ["stop", "cancel"])
        self.assertEqual(fired, ["cancel"])

    def test_register_reports_the_users_own_key(self):
        eng, _ = self.engine(FakeKGA(dictate_keys=[_kde.QT_CTRL | _kde.QT_ALT | 0x44]))
        eng._router = object()
        self.assertTrue(eng._register_dictate())
        self.assertTrue(eng.alive)
        self.assertEqual(eng.installs, 1)

    def test_a_shortcut_kglobalaccel_refused_alerts_once_after_three_failures(self):
        eng, _ = self.engine(FakeKGA(dictate_keys=[]))
        eng._router = object()
        for _ in range(5):
            self.assertFalse(eng._register_dictate())
        self.assertFalse(eng.alive)
        self.assertEqual(eng.failed_installs, 5)
        errors = []
        while not eng.q.empty():
            ev = eng.q.get()
            if type(ev) is tuple:
                errors.append(ev)
        self.assertEqual(len(errors), 1)

    def test_stop_releases_return_and_esc_even_with_the_flags_on(self):
        eng, _ = self.engine()
        kga = eng._kga
        eng.recording = True
        eng.cancellable = True
        eng.stop()
        self.assertIn(("disarm", "stop"), kga.calls)
        self.assertIn(("disarm", "cancel"), kga.calls)
        self.assertIn(("inactive", "dictate"), kga.calls)
        self.assertTrue(eng.cancellable, "stop() nao e' dono de `cancellable`")

    def test_start_without_a_session_bus_never_raises(self):
        eng, _ = self.engine()

        def no_bus():
            raise OSError("sem barramento")

        with mock.patch.object(eng, "_open", no_bus):
            self.assertIs(eng.start(), eng)
        try:
            self.assertFalse(eng.alive)
            self.assertEqual(eng.installs, 0)
        finally:
            eng._kga = None
            eng.stop()

    def test_the_guardian_runs_as_its_own_session(self):
        eng, _ = self.engine()
        with mock.patch.object(_lhk.subprocess, "Popen") as popen:
            eng._spawn_guard()
        args, kwargs = popen.call_args
        self.assertIn("wispr.linux.keyguard", args[0])
        self.assertTrue(kwargs.get("start_new_session"))
        self.assertEqual(kwargs.get("stdin"), _lhk.subprocess.PIPE)


# --------------------------------------------------------------------------- #
# 4. guardiao
# --------------------------------------------------------------------------- #

@unittest.skipUnless(_kg, _why_kg or "wispr.linux.keyguard indisponivel")
class KeyGuardTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wisper_kg_")
        self.lock = _os.path.join(self.tmp, "wisper.lock")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_free_lock_is_free(self):
        self.assertTrue(_kg._lock_is_free(self.lock))

    def test_a_held_lock_is_not_free(self):
        fd = _os.open(self.lock, _os.O_RDWR | _os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertFalse(_kg._lock_is_free(self.lock))
        finally:
            _os.close(fd)

    def _main_after_eof(self, lock_free: bool):
        released = []
        stdin = types.SimpleNamespace(buffer=types.SimpleNamespace(read=lambda n: b""))
        with mock.patch.object(_kg.sys, "stdin", stdin), \
                mock.patch.object(_kg, "_lock_is_free", lambda p: lock_free), \
                mock.patch.object(_kg, "release_all", lambda: released.append(1) or []):
            code = _kg.main([])
        return code, released

    def test_parent_death_releases_the_keys(self):
        code, released = self._main_after_eof(lock_free=True)
        self.assertEqual((code, released), (0, [1]))

    def test_a_new_instance_keeps_its_keys(self):
        # O lock preso no EOF = um wisper NOVO ja subiu; os atalhos sao dele.
        code, released = self._main_after_eof(lock_free=False)
        self.assertEqual((code, released), (0, []))


# --------------------------------------------------------------------------- #
# 5. protocolo Wayland do fake_input
# --------------------------------------------------------------------------- #

class FakeCompositor(threading.Thread):
    """Servidor Wayland de mentira: anuncia (ou nao) o fake_input, responde aos
    syncs e grava tudo que o cliente pede. Socket num diretorio temporario."""

    def __init__(self, path, advertise=True, error_on_bind=False):
        super().__init__(daemon=True)
        self.path = path
        self.advertise = advertise
        self.error_on_bind = error_on_bind
        self.requests = []
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(path)
        self.srv.listen(1)

    def send(self, conn, obj, opcode, payload=b""):
        conn.sendall(_wl.encode_message(obj, opcode, payload))

    def run(self):
        conn, _ = self.srv.accept()
        buf = b""
        with conn:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                msgs, buf = _wl.split_messages(buf + chunk)
                for obj, op, payload in msgs:
                    self.requests.append((obj, op, payload))
                    if obj == 1 and op == 1:        # get_registry
                        if self.advertise:
                            self.send(conn, 2, 0, struct.pack("=I", 56)
                                      + _wl.encode_string(_wl.FAKE_INPUT)
                                      + struct.pack("=I", 6))
                        self.send(conn, 2, 0, struct.pack("=I", 1)
                                  + _wl.encode_string("wl_compositor")
                                  + struct.pack("=I", 6))
                    elif obj == 2 and op == 0 and self.error_on_bind:
                        new_id = struct.unpack_from("=I", payload, len(payload) - 4)[0]
                        self.send(conn, 1, 0, struct.pack("=II", new_id, 0)
                                  + _wl.encode_string("permission denied"))
                    elif obj == 1 and op == 0:      # sync
                        (cb,) = struct.unpack("=I", payload)
                        self.send(conn, cb, 0, struct.pack("=I", 7))


@unittest.skipUnless(_wl, _why_wl or "wispr.linux.wayland indisponivel")
class WaylandWireTest(unittest.TestCase):

    def test_strings_are_nul_terminated_and_padded(self):
        self.assertEqual(_wl.encode_string("abc"), struct.pack("=I", 4) + b"abc\0")
        self.assertEqual(_wl.encode_string("abcd"), struct.pack("=I", 5) + b"abcd\0\0\0\0")
        s, off = _wl.decode_string(_wl.encode_string("wisper") + b"rest", 0)
        self.assertEqual((s, off), ("wisper", 12))

    def test_the_header_carries_size_and_opcode(self):
        msg = _wl.encode_message(5, 10, struct.pack("=II", 42, 1))
        obj, word = struct.unpack_from("=II", msg)
        self.assertEqual((obj, word >> 16, word & 0xFFFF), (5, 16, 10))

    def test_split_keeps_the_incomplete_tail(self):
        a = _wl.encode_message(1, 0, b"\0\0\0\0")
        b = _wl.encode_message(2, 3, b"\1\0\0\0\2\0\0\0")
        msgs, rest = _wl.split_messages(a + b[:5])
        self.assertEqual(len(msgs), 1)
        self.assertEqual(rest, b[:5])

    def test_paste_chords(self):
        self.assertEqual(_wl.parse_chord("shift+insert"), [42, 110])
        self.assertEqual(_wl.parse_chord("ctrl+v"), [29, 47])
        self.assertEqual(_wl.parse_chord("ctrl+shift+v"), [29, 42, 47])
        for bad in ("", "shift", "ctrl+x", "shift+insert+ctrl"):
            with self.subTest(spec=bad), self.assertRaises(ValueError):
                _wl.parse_chord(bad)


@unittest.skipUnless(_wl and hasattr(socket, "AF_UNIX"), _why_wl or "sem AF_UNIX")
class FakeInputSessionTest(unittest.TestCase):
    """O cliente inteiro contra um compositor falso: bind, authenticate, teclas."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wisper_wl_")
        self.path = _os.path.join(self.tmp, "wayland-test")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_client(self, fn, **server_kw):
        srv = FakeCompositor(self.path, **server_kw)
        srv.start()
        try:
            with mock.patch.object(_wl, "socket_path", lambda: self.path):
                return fn(), srv
        finally:
            srv.join(2.0)
            srv.srv.close()

    def test_a_chord_presses_in_order_and_releases_in_reverse(self):
        def go():
            with _wl.FakeInput(timeout=2.0) as fi:
                fi.chord([42, 110], hold=0)
                return fi.fake
        fake, srv = self.run_client(go)
        keys = [struct.unpack("=II", p) for o, op, p in srv.requests
                if o == fake and op == 10]
        self.assertEqual(keys, [(42, 1), (110, 1), (110, 0), (42, 0)])
        ops = [op for o, op, _ in srv.requests if o == fake]
        self.assertEqual(ops[0], 0, "authenticate tem que vir antes de qualquer tecla")
        self.assertEqual(ops[-1], 11, "destroy no fim (v5+)")

    def test_the_bind_names_the_interface_and_version(self):
        def go():
            with _wl.FakeInput(timeout=2.0) as fi:
                return fi.version
        version, srv = self.run_client(go)
        self.assertEqual(version, 6)
        bind = [p for o, op, p in srv.requests if o == 2 and op == 0][0]
        name = struct.unpack_from("=I", bind)[0]
        iface, off = _wl.decode_string(bind, 4)
        self.assertEqual((name, iface), (56, _wl.FAKE_INPUT))

    def test_no_grant_means_a_clear_error_and_no_key(self):
        def go():
            return _wl.probe(timeout=2.0)
        (ok, why), srv = self.run_client(go, advertise=False)
        self.assertFalse(ok)
        self.assertIn("setup.sh", why)
        self.assertFalse([1 for o, op, _ in srv.requests if op == 10])

    def test_a_protocol_error_is_an_oserror(self):
        def go():
            with self.assertRaises(OSError):
                with _wl.FakeInput(timeout=2.0):
                    pass
            return True
        self.run_client(go, error_on_bind=True)

    def test_a_silent_compositor_times_out(self):
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.path)
        srv.listen(1)
        try:
            with mock.patch.object(_wl, "socket_path", lambda: self.path):
                with self.assertRaises(OSError):
                    _wl.FakeInput(timeout=0.2).connect()
        finally:
            srv.close()


# --------------------------------------------------------------------------- #
# 6. entrega no Linux: clipboard + atalho de colar
# --------------------------------------------------------------------------- #

class FakeClipboard:
    def __init__(self, text="antes", types_=("text/plain;charset=utf-8",), fail_set=False):
        self.text = {False: text, True: "primaria"}
        self.types = list(types_)
        self.fail_set = fail_set
        self.writes = []

    def set_text(self, text, primary=False):
        if self.fail_set:
            raise OSError("wl-copy quebrado")
        self.writes.append((primary, text))
        self.text[primary] = text

    def get_text(self, primary=False):
        return self.text[primary]

    def list_types(self, primary=False):
        return list(self.types)

    has_text_type = staticmethod(lambda types: any(t.startswith("text/") for t in types))


class FakeFI:
    chords = []
    fail = False

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        if FakeFI.fail:
            raise OSError("fake_input nao liberado")
        return self

    def __exit__(self, *exc):
        return False

    def chord(self, keys, hold=0.012):
        FakeFI.chords.append(list(keys))


@unittest.skipUnless(_linj, _why_linj or "wispr.linux.inject indisponivel")
class LinuxDeliverTest(unittest.TestCase):
    TEXT = "ação não é coração"

    def setUp(self):
        FakeFI.chords = []
        FakeFI.fail = False
        self.cfg = config.Config(dict(config.DEFAULTS))
        self.cfg["clipboard_restore_delay"] = 0.0

    def deliver(self, clip, text=None, **cfg):
        self.cfg.update(cfg)
        with mock.patch.object(_linj, "clipboard", clip), \
                mock.patch.object(_linj.wayland, "FakeInput", FakeFI), \
                mock.patch.object(_linj.time, "sleep", lambda s: None):
            return _linj.deliver(self.TEXT if text is None else text, self.cfg)

    def test_a_paste_reports_every_code_unit(self):
        clip = FakeClipboard()
        res = self.deliver(clip)
        self.assertEqual(res, "paste")
        self.assertEqual(res.chars, len(self.TEXT.encode("utf-16-le")) // 2)
        self.assertEqual(FakeFI.chords, [[42, 110]])          # Shift+Insert
        self.assertIn((False, self.TEXT), clip.writes)
        self.assertIn((True, self.TEXT), clip.writes)         # primary tambem
        self.assertEqual(clip.text[False], "antes", "o clipboard do usuario volta")
        self.assertEqual(clip.text[True], "primaria")

    def test_the_six_attributes_exist_on_every_return(self):
        for clip, text in ((FakeClipboard(), self.TEXT), (FakeClipboard(), "​"),
                           (FakeClipboard(fail_set=True), self.TEXT)):
            res = self.deliver(clip, text)
            for attr in ("mode", "chars", "target_elevated", "blocked",
                         "lost_nontext_formats", "empty"):
                self.assertTrue(hasattr(res, attr), attr)

    def test_an_empty_transcript_never_touches_the_clipboard(self):
        clip = FakeClipboard()
        res = self.deliver(clip, "​")
        self.assertEqual((res, res.empty, res.chars), ("empty", True, 0))
        self.assertEqual(clip.writes, [])
        self.assertEqual(FakeFI.chords, [])

    def test_without_fake_input_nothing_is_delivered_and_the_clipboard_is_restored(self):
        FakeFI.fail = True
        clip = FakeClipboard()
        res = self.deliver(clip)
        self.assertEqual(res.chars, 0)          # o app cai no "copiei, use Ctrl+V"
        self.assertEqual(clip.text[False], "antes")

    def test_a_broken_wl_copy_never_raises(self):
        res = self.deliver(FakeClipboard(fail_set=True))
        self.assertEqual(res.chars, 0)
        self.assertEqual(FakeFI.chords, [])

    def test_an_image_on_the_clipboard_is_reported_lost(self):
        res = self.deliver(FakeClipboard(text=None, types_=("image/png",)))
        self.assertTrue(res.lost_nontext_formats)

    def test_files_copied_in_dolphin_count_as_nontext(self):
        self.assertTrue(_linj.is_nontext(["text/uri-list", "text/plain"]))
        self.assertFalse(_linj.is_nontext(["text/plain;charset=utf-8", "TEXT"]))

    def test_the_paste_keys_are_configurable_and_validated(self):
        self.deliver(FakeClipboard(), linux_paste_keys="ctrl+v")
        self.deliver(FakeClipboard(), linux_paste_keys="ctrl+nada")
        self.assertEqual(FakeFI.chords, [[29, 47], [42, 110]])

    def test_type_mode_still_pastes(self):
        res = self.deliver(FakeClipboard(), inject_mode="type")
        self.assertEqual(res, "paste")
        self.assertEqual(len(FakeFI.chords), 1)

    def test_no_primary_when_disabled(self):
        clip = FakeClipboard()
        self.deliver(clip, linux_paste_primary=False)
        self.assertFalse([w for w in clip.writes if w[0]])

    def test_type_unicode_says_nothing_was_sent(self):
        with self.assertRaises(OSError) as cm:
            _linj.type_unicode("oi")
        self.assertEqual(cm.exception.sent, 0)

    def test_there_is_no_uipi_on_wayland(self):
        self.assertIs(_linj.injection_blocked(), False)
        self.assertIs(_linj.target_is_elevated(), False)


@unittest.skipUnless(_clip and _LINUX, _why_clip or "so no Linux")
class ClipboardSetTextTest(unittest.TestCase):
    """O wl-copy de verdade deixa um filho servindo a selecao, herdando o stderr.
    Ao vivo (2026-10-15) o stderr=PIPE fazia TODO ditado estourar o timeout de 3 s
    e entregar 0 chars. O dublê abaixo faz o mesmo: sai na hora, filho fica."""

    def fake_wl_copy(self, body):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        exe = _os.path.join(d, "fake-wl-copy")
        with open(exe, "w") as f:
            f.write("#!/bin/sh\ncat >/dev/null\n" + body)
        _os.chmod(exe, 0o755)
        return exe

    def set_text(self, exe):
        with mock.patch.object(_clip.shutil, "which", return_value=exe), \
                mock.patch.object(_clip, "TIMEOUT_S", 0.5):
            _clip.set_text("oi")

    def test_a_lingering_child_holding_stderr_does_not_time_out(self):
        self.set_text(self.fake_wl_copy("(sleep 2) &\nexit 0\n"))

    def test_a_failure_still_reports_wl_copy_stderr(self):
        with self.assertRaises(OSError) as cm:
            self.set_text(self.fake_wl_copy("echo sem compositor >&2\nexit 1\n"))
        self.assertIn("sem compositor", str(cm.exception))


# --------------------------------------------------------------------------- #
# 7. microfone, config, stt, app, stub Win32
# --------------------------------------------------------------------------- #

def _dev(name, ins=2):
    return {"name": name, "max_input_channels": ins, "hostapi": 0}


@unittest.skipUnless(_audio, _why_audio or "wispr.audio indisponivel")
class PickLinuxDeviceTest(unittest.TestCase):
    DEVS = [_dev("sof-hda-dsp: - (hw:0,0)"), _dev("HDMI", 0), _dev("sysdefault"),
            _dev("pipewire"), _dev("default"), _dev("HyperX Cloud", 1)]

    def test_pipewire_wins_by_default(self):
        self.assertEqual(_audio.pick_linux_device(self.DEVS), (3, "pipewire", False))

    def test_a_configured_name_wins(self):
        self.assertEqual(_audio.pick_linux_device(self.DEVS, "hyperx"), (5, "HyperX Cloud", False))

    def test_an_unknown_name_falls_back_and_says_so(self):
        self.assertEqual(_audio.pick_linux_device(self.DEVS, "jbl"), (3, "pipewire", True))

    def test_output_only_devices_are_never_picked(self):
        self.assertEqual(_audio.pick_linux_device([_dev("pipewire", 0), _dev("x")]), (1, "x", True))

    def test_no_input_at_all_raises(self):
        with self.assertRaises(RuntimeError):
            _audio.pick_linux_device([_dev("pipewire", 0)])


class LinuxConfigTest(unittest.TestCase):

    def test_linux_defaults_are_known_keys(self):
        for key in config.LINUX_DEFAULTS:
            self.assertIn(key, config.DEFAULTS)

    @unittest.skipUnless(_LINUX, "so no Linux")
    def test_linux_runs_small_on_cpu_int8(self):
        self.assertEqual(config.DEFAULTS["device"], "cpu")
        self.assertEqual(config.DEFAULTS["cpu_compute_type"], "int8")
        self.assertEqual(config.DEFAULTS["model_id"], "Systran/faster-whisper-small")
        self.assertNotIn("distil", config.DEFAULTS["model_id"])

    @unittest.skipUnless(_sys.platform == "win32", "so no Windows")
    def test_windows_keeps_the_turbo_on_the_gpu(self):
        self.assertEqual(config.DEFAULTS["device"], "cuda")
        self.assertIn("turbo", config.DEFAULTS["model_id"])


@unittest.skipUnless(_stt, _why_stt or "wispr.stt indisponivel")
class CpuLoadFailureTest(unittest.TestCase):
    """device=cpu (o padrao do Linux) que nao carrega: com `auto` e chave, Groq."""

    def _engine(self, engine, key):
        cfg = config.load()
        cfg.update(engine=engine, groq_api_key=key, device="cpu")
        fake_fw = types.SimpleNamespace(WhisperModel=object, BatchedInferencePipeline=object)

        def load(_self, _wm, _bp, device, _ctype):
            raise RuntimeError("modelo nao baixou")

        with mock.patch.dict(_sys.modules, {"faster_whisper": fake_fw}), \
                mock.patch.object(_stt, "_add_cuda_dll_dirs", lambda: []), \
                mock.patch.object(_stt, "resolve_model_source", lambda mid: (mid, False)), \
                mock.patch.object(_stt.Engine, "_load", load), \
                mock.patch.dict(_os.environ, {"GROQ_API_KEY": ""}):
            return _stt.Engine(cfg)

    def test_auto_with_a_key_goes_to_groq(self):
        self.assertEqual(self._engine("auto", "gsk_teste").backend, "groq")

    def test_auto_without_a_key_raises(self):
        with self.assertRaises(_stt.TranscriptionError):
            self._engine("auto", "")

    def test_local_never_goes_to_the_cloud(self):
        with self.assertRaises(_stt.TranscriptionError):
            self._engine("local", "gsk_teste")


@unittest.skipUnless(_LINUX and _app, _why_app or "so no Linux")
class LinuxAppTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wisper_app_")
        self.lock = Path(self.tmp) / "wisper.lock"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def app(self):
        with mock.patch.object(config, "ensure_dirs", lambda: None):
            a = _app.App(config.Config(dict(config.DEFAULTS)))
        a.log = _safety.null_logger()
        return a

    def test_the_second_instance_exits(self):
        with mock.patch.object(config, "LOCK_PATH", self.lock):
            first, second = self.app(), self.app()
            self.assertTrue(first._acquire_mutex())
            try:
                self.assertFalse(second._acquire_mutex())
            finally:
                first._mutex.close()
            self.assertTrue(second._acquire_mutex(), "o lock tem que soltar no close")
            second._mutex.close()

    def test_no_uipi_on_linux(self):
        self.assertFalse(_app.foreground_is_elevated())
        self.assertFalse(_app.process_is_elevated())

    def test_open_logs_uses_xdg_open(self):
        with mock.patch("subprocess.Popen") as popen:
            _app._open_path("/tmp/x")
        self.assertEqual(popen.call_args[0][0], ["xdg-open", "/tmp/x"])


@unittest.skipUnless(_LINUX, "so no Linux")
class PlatformFinderTest(unittest.TestCase):

    def test_hotkey_and_inject_load_the_linux_files_under_the_usual_names(self):
        for name in ("wispr.hotkey", "wispr.inject"):
            mod, why = _safety.load_pure(name)
            self.assertIsNotNone(mod, why)
            self.assertEqual(mod.__name__, name)
            self.assertIn(_os.sep + "linux" + _os.sep, mod.__file__)

    def test_the_contract_api_is_there(self):
        hk, _ = _safety.load_pure("wispr.hotkey")
        inj, _ = _safety.load_pure("wispr.inject")
        for attr in ("HotkeyEngine",):
            self.assertTrue(hasattr(hk, attr))
        for attr in ("INJECT_TAG", "MSG_ELEVATED", "Delivery", "deliver", "type_unicode",
                     "paste_text", "tap", "clip_get_text", "clip_set_text", "sanitize",
                     "target_is_elevated", "injection_blocked"):
            self.assertTrue(hasattr(inj, attr), attr)
        eng = hk.HotkeyEngine(lambda: None, lambda: None, lambda: None,
                              cfg=config.Config(dict(config.DEFAULTS)))
        for attr in ("recording", "cancellable", "installs", "failed_installs", "alive",
                     "start", "stop"):
            self.assertTrue(hasattr(eng, attr), attr)


@unittest.skipUnless(_stub, _why_stub or "wispr.win32stub indisponivel")
class Win32StubTest(unittest.TestCase):

    def test_declarations_pass_and_calls_raise(self):
        dll = _stub.NoWin32("user32")
        dll.GetForegroundWindow.restype = int
        dll.GetForegroundWindow.argtypes = ()
        self.assertIs(dll.GetForegroundWindow.restype, int)
        with self.assertRaises(OSError):
            dll.GetForegroundWindow()


@unittest.skipUnless(_LINUX, "so no Linux")
class OverlayWorkareaTest(unittest.TestCase):

    def setUp(self):
        self.ov, why = _safety.load_pure("wispr.overlay")
        if self.ov is None:
            self.skipTest(why)
        self.ov._X11_WORKAREA.update(t=0.0, bottom=None)

    def test_the_pill_sits_above_the_panel(self):
        out = types.SimpleNamespace(stdout="_NET_WORKAREA(CARDINAL) = 0, 0, 1920, 1034\n")
        with mock.patch.object(self.ov.subprocess, "run", lambda *a, **k: out):
            self.assertEqual(self.ov._workarea_bottom(1080), 1034)

    def test_no_xprop_falls_back_to_the_screen(self):
        def boom(*a, **k):
            raise FileNotFoundError("xprop")
        with mock.patch.object(self.ov.subprocess, "run", boom):
            self.assertEqual(self.ov._workarea_bottom(1080), 1080)


if __name__ == "__main__":
    unittest.main()
