# -*- coding: utf-8 -*-
"""Hotkey global do wisper no KDE Plasma 6 (Wayland): `Meta+A` grava, `Enter`
finaliza, `Esc` cancela.

Carregado como `wispr.hotkey` pelo finder do `wispr/__init__.py`; a API e' a do
docs/CONTRACT.md, a mesma do `wispr/hotkey.py` do Windows.

No Wayland nao existe `WH_KEYBOARD_LL`: so o compositor ve as teclas. Os atalhos
vem do kglobalaccel do KDE por D-Bus (wispr/linux/kde.py):

- `dictate` (Meta+A): registrado no `start()` e mantido a sessao inteira. Sem
  NO_AUTOLOADING, de proposito: se o usuario trocar a tecla em Configuracoes do
  Sistema > Atalhos > wisper, vale a dele.
- `stop` (Return + Enter do numerico) e `cancel` (Escape): **so existem
  enquanto `recording` / `cancellable` estao ligados**. Fora disso o Enter e o
  Esc sao do usuario. E' o mesmo contrato das duas flags do Windows ("o Enter so'
  e' engolido depois que o app CONFIRMOU a gravacao"), so que em vez de o hook
  decidir tecla a tecla, a tecla e' registrada e desregistrada no compositor.

Alternativas descartadas:
- evdev (`/dev/input/event*`): exige o grupo `input` — o usuario nao esta nele,
  e estar nele deixa QUALQUER processo dele ler todas as teclas, inclusive
  senhas. Pior que o problema.
- portal GlobalShortcuts (xdg-desktop-portal): pede confirmacao em dialogo a
  cada BindShortcuts; para Return/Esc temporarios seria um dialogo por ditado.
- atalho personalizado do KDE rodando um CLI: resolve o Meta+A, mas Enter e
  Esc teriam que ficar ligados para sempre.

O perigo de registrar Return/Escape: medido que o kglobalaccel MANTEM o atalho
ativo depois que o processo dono morre. Tres cintos, do mais comum ao mais raro:
1. desligamento normal: `stop()` desarma;
2. crash/kill -9: o guardiao (wispr/linux/keyguard.py, processo filho com pipe)
   ve o EOF e desarma;
3. ate o guardiao morreu junto: o proximo `start()` desarma antes de tudo, e
   `scripts/release-keys.sh` faz o mesmo na mao.

Threads:
  - hk-dbus  : o `DBusRouter` do jeepney (recebe sinais e respostas);
  - hk-signal: le os `globalShortcutPressed` e enfileira eventos;
  - hk-worker: chama os callbacks do app (mesma regra do Windows);
  - hk-arm   : aplica o estado desejado de Return/Escape no kglobalaccel. O app
               liga `recording` dentro do proprio lock: uma chamada D-Bus ali
               (timeout de segundos se o kglobalaccel travar) seguraria o app.
  - hk-watchdog: confere a cada WATCHDOG_PERIOD_S se o Meta+A ainda esta de pe
               (o kglobalaccel reiniciou? o usuario apagou o atalho?).
"""
from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import time

from wispr import config
from wispr.linux import kde

try:
    from wispr import logging_setup as _logging_setup
    log = _logging_setup.get(__name__)
except Exception:  # pragma: no cover
    log = logging.getLogger(__name__)

__all__ = ["HotkeyEngine", "parse_chord"]

WATCHDOG_PERIOD_S = 5.0
WATCHDOG_MAX_PERIOD_S = 60.0
HOOK_FAIL_ALERT = 3                 # falhas seguidas ate avisar o app (uma vez por serie)
ARM_RETRIES = 2

FRIENDLY = {
    "dictate": "Gravar ditado",
    "stop": "Finalizar ditado (so' durante a gravacao)",
    "cancel": "Cancelar ditado (so' durante a gravacao)",
}


def parse_chord(spec: str) -> int:
    """'win+a' -> codigo Qt. Mesmas regras e apelidos do Windows."""
    return kde.parse_chord(spec)


def desired_arms(recording: bool, cancellable: bool) -> dict[str, bool]:
    """Quais teclas temporarias devem estar registradas agora. Funcao pura:
    e' a regra inteira das duas flags, testada sem D-Bus."""
    return {"stop": bool(recording), "cancel": bool(cancellable)}


class HotkeyEngine:
    """Atalho global via kglobalaccel, com a API do `wispr.hotkey` do Windows.

    `recording` e `cancellable` tem os MESMOS donos do Windows (ver a docstring
    de la): o app liga `recording` depois do microfone de pe e desliga ao
    terminar; o engine liga `cancellable` ao entregar o "start", e quem desliga
    e' o app. Aqui elas sao properties: escrever nelas pede ao hk-arm para
    registrar ou soltar Return/Escape no compositor.
    """

    def __init__(self, on_start, on_stop, on_cancel, cfg=None, *, on_error=None):
        self.cfg = cfg or config.load()
        self._recording = False
        self._cancellable = False
        self.installs = 0
        self.failed_installs = 0

        self.q: queue.Queue = queue.Queue()
        self._cbs = {"start": on_start, "stop": on_stop, "cancel": on_cancel}
        self._on_error = on_error

        spec = str(self.cfg.get("hotkey", config.DEFAULTS["hotkey"]))
        try:
            self._key = parse_chord(spec)
        except ValueError as exc:
            log.error("invalid hotkey %r (%s); falling back to %r",
                      spec, exc, config.DEFAULTS["hotkey"])
            spec = config.DEFAULTS["hotkey"]
            self._key = parse_chord(spec)
        self.chord = spec
        self._stop_keys = self._resolve_key("stop_key", "enter")
        self._cancel_keys = self._resolve_key("cancel_key", "esc")

        self._aids = {name: kde.action_id(name, FRIENDLY[name]) for name in FRIENDLY}
        self._conn = None
        self._router = None
        self._kga: kde.KGlobalAccel | None = None
        self._live_keys: list[int] = []     # o que o kglobalaccel devolveu para o Meta+A
        self._armed = {"stop": False, "cancel": False}
        self._arm_ok = {"stop": True, "cancel": True}   # o ultimo arm pegou a tecla?
        self._arm_evt = threading.Event()
        self._pending = False
        self._consec_fails = 0
        self._error_sent = False
        self._last_error = ""
        self._guard: subprocess.Popen | None = None
        self._stopping = threading.Event()
        self._started = False
        self._threads: list[threading.Thread] = []
        self._lk = threading.Lock()

    def _resolve_key(self, key: str, default_name: str) -> list[int]:
        raw = str(self.cfg.get(key, default_name))
        try:
            return kde.single_key_codes(raw)
        except ValueError:
            log.error("invalid %s %r; falling back to %r", key, raw, default_name)
            return kde.single_key_codes(default_name)

    # ------------------------------------------------------------------ flags
    @property
    def recording(self) -> bool:
        return self._recording

    @recording.setter
    def recording(self, value) -> None:
        self._recording = bool(value)
        self._arm_evt.set()

    @property
    def cancellable(self) -> bool:
        return self._cancellable

    @cancellable.setter
    def cancellable(self, value) -> None:
        self._cancellable = bool(value)
        self._arm_evt.set()

    @property
    def alive(self) -> bool:
        """True enquanto o Meta+A (ou o que o usuario configurou) esta registrado
        no kglobalaccel e a conexao D-Bus esta de pe."""
        return bool(self._router is not None and self._live_keys)

    # ------------------------------------------------------------------ callbacks
    def _fire(self, name: str) -> None:
        try:
            self._cbs[name]()
        except Exception:
            log.exception("hotkey callback %r raised", name)

    def _fire_error(self, reason: str) -> None:
        cb = self._on_error
        if cb is None:
            return
        try:
            cb(reason)
        except Exception:
            log.exception("hotkey on_error callback raised")

    def _dispatch(self, ev: str) -> None:
        """A mesma maquina do `_worker` do Windows, um evento por vez."""
        if ev == "start":
            if self.recording or self._pending:
                if self.recording and not self._arm_ok.get("stop", True):
                    # O Return nao pegou (outro componente ja usa a tecla): sem
                    # este desvio a gravacao so terminaria pelo teto de tempo.
                    log.info("dictate pressed while recording and the stop key is not "
                             "registered: treating it as stop")
                    self._fire("stop")
                return                      # duplo toque colapsa num inicio so
            self._pending = True
            self.cancellable = True         # Esc ja cancela, antes mesmo do mic
            try:
                self._fire("start")
            finally:
                self._pending = False
        elif ev == "stop" and self.recording:
            self._fire("stop")
        elif ev == "cancel" and self.cancellable:
            self._fire("cancel")

    def _worker(self, stopping: threading.Event | None = None) -> None:
        # `stopping` opcional: a suite roda o worker na thread dela, como no
        # Windows (`eng._worker()` com um None no fim da fila).
        while True:
            try:
                ev = self.q.get()
            except Exception:
                log.exception("hotkey queue failed")
                return
            if ev is None or (stopping is not None and stopping.is_set()):
                return
            if type(ev) is tuple:
                self._fire_error(ev[1])
                continue
            self._dispatch(ev)

    # ------------------------------------------------------------------ D-Bus
    def _open(self) -> None:
        # DBusRouter direto (o `open_dbus_router` do jeepney e' context manager):
        # a conexao vive do start() ao stop(), nao dentro de um `with`.
        from jeepney.io.threading import DBusRouter, open_dbus_connection
        self._conn = open_dbus_connection(bus="SESSION")
        self._router = DBusRouter(self._conn)
        self._kga = kde.KGlobalAccel(self._router)

    def _register_dictate(self) -> bool:
        """Registra o Meta+A. True se o kglobalaccel ficou com alguma tecla."""
        kga, aid = self._kga, self._aids["dictate"]
        try:
            kga.register(aid)
            kga.set_keys(aid, [self._key], kde.IS_DEFAULT)
            keys = kga.set_keys(aid, [self._key], kde.SET_PRESENT)
        except Exception as exc:
            self._install_failed("kglobalaccel refused the registration: %r" % (exc,))
            return False
        self._live_keys = keys
        if not keys:
            owners = []
            try:
                owners = kga.owners_of(self._key)
            except Exception:
                pass
            self._install_failed("kglobalaccel kept no key for %r (in use by %s, or the user "
                                 "cleared it in System Settings > Shortcuts > wisper)"
                                 % (kde.describe(self._key), owners or "nobody"))
            return False
        self.installs += 1
        self._consec_fails = 0
        self._error_sent = False
        if keys != [self._key]:
            log.info("dictate shortcut is %s (the user's choice in System Settings), not %s",
                     ", ".join(kde.describe(k) for k in keys), kde.describe(self._key))
        else:
            log.info("dictate shortcut registered: %s", kde.describe(self._key))
        return True

    def _install_failed(self, why: str) -> None:
        self.failed_installs += 1
        self._consec_fails += 1
        self._last_error = why
        log.error("global shortcut not installed: %s", why)
        if self._consec_fails >= HOOK_FAIL_ALERT and not self._error_sent:
            self._error_sent = True
            self.q.put(("error", why))

    def _signal_loop(self, stopping: threading.Event) -> None:
        from jeepney import MatchRule
        from jeepney.bus_messages import message_bus

        router = self._router
        rule = MatchRule(type="signal", interface=kde.COMPONENT_IFACE,
                         member="globalShortcutPressed")
        try:
            router.send_and_get_reply(message_bus.AddMatch(rule), timeout=3)
        except Exception:
            log.exception("AddMatch for globalShortcutPressed failed")
        names = {"dictate": "start", "stop": "stop", "cancel": "cancel"}
        try:
            with router.filter(rule, bufsize=64) as sigq:
                while not stopping.is_set():
                    try:
                        msg = sigq.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    try:
                        component, action = str(msg.body[0]), str(msg.body[1])
                    except Exception:
                        continue
                    if component != kde.COMPONENT:
                        continue
                    ev = names.get(action)
                    if ev is not None:
                        log.debug("shortcut %s -> %s", action, ev)
                        self.q.put(ev)
        except Exception:
            if not stopping.is_set():
                log.exception("shortcut signal loop died")

    def _apply_arms(self, force: bool = False) -> None:
        want = desired_arms(self._recording, self._cancellable)
        for name, keys in (("stop", self._stop_keys), ("cancel", self._cancel_keys)):
            if not force and want[name] == self._armed[name]:
                continue
            aid = self._aids[name]
            for attempt in range(ARM_RETRIES + 1):
                try:
                    if want[name]:
                        got = self._kga.arm(aid, keys)
                        self._arm_ok[name] = bool(got)
                        if not got:
                            log.warning("%s key %s not registered (in use by another "
                                        "global shortcut?)", name,
                                        "/".join(kde.describe(k) for k in keys))
                    else:
                        self._kga.disarm(aid)
                    self._armed[name] = want[name]
                    log.debug("%s %s", name, "armed" if want[name] else "released")
                    break
                except Exception:
                    if attempt == ARM_RETRIES:
                        log.exception("could not %s the %s key",
                                      "arm" if want[name] else "release", name)
                    else:
                        time.sleep(0.05)

    def _arm_loop(self, stopping: threading.Event) -> None:
        while not stopping.is_set():
            self._arm_evt.wait(1.0)
            self._arm_evt.clear()
            if stopping.is_set():
                break
            try:
                self._apply_arms()
            except Exception:
                log.exception("arm loop iteration failed")

    def _watchdog(self, stopping: threading.Event) -> None:
        period = WATCHDOG_PERIOD_S
        while not stopping.wait(period):
            try:
                keys = self._kga.keys(self._aids["dictate"])
                active = self._kga.is_active()
            except Exception as exc:
                log.warning("kglobalaccel health check failed: %r", exc)
                keys, active = [], False
            if keys and active:
                if keys != self._live_keys:
                    log.info("dictate shortcut changed to %s",
                             ", ".join(kde.describe(k) for k in keys))
                    self._live_keys = keys
                period = WATCHDOG_PERIOD_S
                continue
            log.warning("dictate shortcut is gone (keys=%s active=%s); re-registering",
                        keys, active)
            self._live_keys = []
            if self._register_dictate():
                self._apply_arms(force=True)
                period = WATCHDOG_PERIOD_S
            else:
                period = min(WATCHDOG_MAX_PERIOD_S, period * 2)

    # ------------------------------------------------------------------ guardiao
    def _spawn_guard(self) -> None:
        try:
            env = dict(os.environ)
            root = str(config.ROOT)
            env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"]
                                        if env.get("PYTHONPATH") else "")
            self._guard = subprocess.Popen(
                [sys.executable, "-m", "wispr.linux.keyguard"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=root, env=env, start_new_session=True, close_fds=True)
            log.info("key guardian up (pid %d)", self._guard.pid)
        except Exception:
            self._guard = None
            log.exception("key guardian did not start: a crash while recording would "
                          "leave Return/Escape grabbed until the next start "
                          "(scripts/release-keys.sh releases them by hand)")

    def _stop_guard(self) -> None:
        g, self._guard = self._guard, None
        if g is None:
            return
        try:
            if g.stdin:
                g.stdin.close()
            g.wait(3.0)
        except Exception:
            log.debug("key guardian did not exit cleanly", exc_info=True)

    # ------------------------------------------------------------------ API
    def start(self) -> "HotkeyEngine":
        """Conecta no D-Bus, desarma sobras de um crash, registra o Meta+A e sobe
        as threads. Nunca levanta: sem kglobalaccel o app fica sabendo por
        `alive` False / `installs == 0`, como no Windows."""
        if self._started:
            return self
        self._started = True
        self._stopping = threading.Event()
        stopping = self._stopping
        log.info("hotkey engine starting (kglobalaccel): chord=%r -> %s stop=%s cancel=%s",
                 self.chord, kde.describe(self._key),
                 "/".join(kde.describe(k) for k in self._stop_keys),
                 "/".join(kde.describe(k) for k in self._cancel_keys))
        try:
            self._open()
        except Exception as exc:
            self._router = None
            self._install_failed("no D-Bus session / kglobalaccel: %r" % (exc,))
            self.q.put(("error", self._last_error))
            self._error_sent = True
        worker = threading.Thread(target=self._worker, args=(stopping,), name="hk-worker",
                                  daemon=True)
        worker.start()
        self._threads = [worker]
        if self._router is None:
            return self

        # Sobra de um crash em que ate o guardiao morreu: Return/Escape ainda
        # presos. Soltar ANTES de qualquer coisa.
        self._armed = {"stop": True, "cancel": True}
        self._apply_arms()
        self._spawn_guard()
        self._register_dictate()
        for target, name in ((self._signal_loop, "hk-signal"), (self._arm_loop, "hk-arm"),
                             (self._watchdog, "hk-watchdog")):
            t = threading.Thread(target=target, args=(stopping,), name=name, daemon=True)
            t.start()
            self._threads.append(t)
        if not self.alive:
            log.error("global shortcut is NOT registered; %r will not work until the "
                      "watchdog manages to register it", self.chord)
        return self

    def stop(self) -> None:
        """Desliga tudo e SOLTA as teclas. Idempotente; nao mexe em `cancellable`
        (o dono e' o app), mas desarma o Esc no compositor mesmo assim: sem
        engine nao ha quem ouca o sinal, e o Esc engolido seria do usuario."""
        self._stopping.set()
        self._started = False
        self._arm_evt.set()
        kga = self._kga
        if kga is not None:
            for name in ("stop", "cancel"):
                try:
                    kga.disarm(self._aids[name])
                    self._armed[name] = False
                except Exception:
                    log.exception("could not release the %s key on stop", name)
            try:
                kga.set_inactive(self._aids["dictate"])
            except Exception:
                log.exception("could not deactivate the dictate shortcut")
        self._stop_guard()
        try:
            self.q.put(None)
        except Exception:
            pass
        for t in list(self._threads):
            try:
                if t.is_alive() and t is not threading.current_thread():
                    t.join(2.0)
            except Exception:
                log.exception("joining %s failed", t.name)
        self._threads = []
        router, self._router = self._router, None
        conn, self._conn = self._conn, None
        self._live_keys = []
        for what in (router, conn):
            if what is not None:
                try:
                    what.close()
                except Exception:
                    log.debug("closing the D-Bus connection failed", exc_info=True)
