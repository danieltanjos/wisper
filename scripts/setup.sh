#!/usr/bin/env bash
# Setup de primeira vez do wisper no Linux (Fedora KDE Plasma 6, Wayland).
# Idempotente: pode rodar quantas vezes quiser. NAO usa sudo: o que precisar de
# root sai impresso como uma linha `sudo dnf install ...` (e esta em
# scripts/setup-linux-root.sh).
#
# O que faz:
#   1. confere sessao (Wayland + KDE), interpretador e pacotes do sistema;
#   2. cria o .venv com --system-site-packages (o `gi` da bandeja vem do Fedora)
#      e instala o requirements-linux.txt;
#   3. COPIA o binario do Python para .venv/bin/wisper. E' esse executavel que
#      o KWin libera para injetar o atalho de colar (org_kde_kwin_fake_input),
#      casando o /proc/<pid>/exe com o Exec= do .desktop. Copia real, nao
#      symlink: com symlink o exe seria o /usr/bin/python3.14 e a liberacao
#      valeria para todo Python da maquina;
#   4. grava ~/.local/share/applications/wisper.desktop com
#      X-KDE-Wayland-Interfaces=org_kde_kwin_fake_input e confere a liberacao
#      SEM mandar tecla nenhuma (so conecta, autentica e desconecta);
#   5. com --download-model, baixa os pesos agora (senao, no primeiro ditado).
#
# Uso:
#   ./scripts/setup.sh [--download-model] [--force] [--python /usr/bin/python3]
#
# Ver docs/LINUX.md.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
PY_BASE="/usr/bin/python3"
DOWNLOAD=0
FORCE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --download-model) DOWNLOAD=1 ;;
        --force) FORCE=1 ;;
        --python) PY_BASE="$2"; shift ;;
        -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
        *) echo "opcao desconhecida: $1" >&2; exit 2 ;;
    esac
    shift
done

step() { printf '\n==> %s\n' "$1"; }
ok()   { printf '    [ok]  %s\n' "$1"; }
warn() { printf '    [!]   %s\n' "$1"; }
info() { printf '          %s\n' "$1"; }
fail() { printf '\nERRO: %s\n\n' "$1" >&2; exit 1; }

# --------------------------------------------------------------------------- #
step "sessao"
if [[ "${XDG_SESSION_TYPE:-}" != "wayland" ]]; then
    warn "XDG_SESSION_TYPE=${XDG_SESSION_TYPE:-?}: o port foi feito para Wayland."
else
    ok "Wayland"
fi
if [[ "${XDG_CURRENT_DESKTOP:-}" != *KDE* ]]; then
    warn "XDG_CURRENT_DESKTOP=${XDG_CURRENT_DESKTOP:-?}: o atalho global usa o kglobalaccel do KDE."
else
    ok "KDE Plasma"
fi

# --------------------------------------------------------------------------- #
step "pacotes do sistema"
# portaudio: o sounddevice carrega libportaudio.so.2 por ctypes.
# wl-clipboard: wl-copy/wl-paste, a entrega do texto.
# libappindicator-gtk3: typelib AppIndicator3 da bandeja (StatusNotifierItem).
# python3-gobject: o `gi` que o pystray usa.
# xprop: area de trabalho para posicionar a pilula acima do painel.
# python3-tkinter: so a pilula; sem ele o app roda sem pilula.
MISSING=()
for pkg in portaudio wl-clipboard libappindicator-gtk3 python3-gobject xprop python3-tkinter; do
    if rpm -q "$pkg" >/dev/null 2>&1; then
        ok "$pkg"
    else
        warn "$pkg ausente"
        MISSING+=("$pkg")
    fi
done
if (( ${#MISSING[@]} )); then
    info "instale com (precisa de root; ver scripts/setup-linux-root.sh):"
    info "    sudo dnf install ${MISSING[*]}"
fi

# --------------------------------------------------------------------------- #
step "interpretador"
[[ -x "$PY_BASE" ]] || fail "$PY_BASE nao existe. Passe --python <caminho>."
PYVER="$("$PY_BASE" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
case "$PYVER" in
    3.11|3.12|3.13|3.14) ok "$PY_BASE ($PYVER)" ;;
    *) fail "Python $PYVER nao validado; use 3.11 a 3.14." ;;
esac

# --------------------------------------------------------------------------- #
step "venv"
if [[ $FORCE == 1 && -d "$VENV" ]]; then
    info "--force: apagando $VENV"
    rm -rf "$VENV"
fi
if [[ ! -x "$VENV/bin/python" ]]; then
    "$PY_BASE" -m venv --system-site-packages "$VENV"
    ok "criado em $VENV"
else
    ok "ja existe"
fi
"$VENV/bin/python" -m pip install -q --upgrade pip
"$VENV/bin/python" -m pip install -q -r "$ROOT/requirements-linux.txt"
ok "requirements-linux.txt instalado"
"$VENV/bin/python" - <<'EOF'
import importlib
for mod in ("faster_whisper", "ctranslate2", "sounddevice", "soxr", "scipy", "jeepney", "pystray"):
    try:
        importlib.import_module(mod)
        print("    [ok]  import %s" % mod)
    except Exception as exc:
        print("    [!]   import %s falhou: %s" % (mod, exc))
try:
    import gi
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3  # noqa: F401
    print("    [ok]  AppIndicator3 (bandeja)")
except Exception as exc:
    print("    [!]   AppIndicator3 indisponivel (%s): a bandeja cai num backend pior" % exc)
try:
    import tkinter  # noqa: F401
    print("    [ok]  tkinter (pilula)")
except Exception:
    print("    [!]   tkinter ausente: sem pilula (sudo dnf install python3-tkinter)")
EOF

# --------------------------------------------------------------------------- #
step "executavel proprio (.venv/bin/wisper)"
REAL_PY="$(readlink -f "$VENV/bin/python3")"
if ! cmp -s "$REAL_PY" "$VENV/bin/wisper" 2>/dev/null; then
    cp -f "$REAL_PY" "$VENV/bin/wisper"
    chmod 755 "$VENV/bin/wisper"
    ok "copiado de $REAL_PY"
else
    ok "ja atualizado ($REAL_PY)"
fi
"$VENV/bin/wisper" -c 'import sys; assert sys.prefix.endswith(".venv"), sys.prefix' \
    || fail ".venv/bin/wisper nao reconhece o venv"

# --------------------------------------------------------------------------- #
step "atalho de aplicativo (~/.local/share/applications/wisper.desktop)"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPS"
cat > "$APPS/wisper.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=wisper
GenericName=Ditado por voz
Comment=Meta+A grava, Enter insere o texto, Esc cancela
Exec=$VENV/bin/wisper $ROOT/main.pyw
Path=$ROOT
Icon=audio-input-microphone
Terminal=false
Categories=Utility;Accessibility;
StartupNotify=false
# O KWin so anuncia o fake_input (colar automatico) para o executavel deste Exec=.
X-KDE-Wayland-Interfaces=org_kde_kwin_fake_input
EOF
ok "gravado"
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true

# A liberacao depende do cache do KService, que leva alguns segundos para ver
# o .desktop novo. O probe nao manda tecla: conecta, autentica, desconecta.
PROBE_OK=0
for _ in $(seq 1 15); do
    if "$VENV/bin/wisper" -c '
import sys
sys.path.insert(0, sys.argv[1])
from wispr.linux import wayland
ok, why = wayland.probe()
print("          " + why)
sys.exit(0 if ok else 1)' "$ROOT" >/dev/null 2>&1; then
        PROBE_OK=1
        break
    fi
    sleep 1
done
if [[ $PROBE_OK == 1 ]]; then
    ok "KWin liberou org_kde_kwin_fake_input para .venv/bin/wisper: o texto e' colado sozinho"
else
    warn "o KWin ainda nao liberou o fake_input. Sem ele o texto fica na area de"
    info "transferencia e voce cola com Ctrl+V. Tente de novo em alguns segundos, ou"
    info "faca logout/login."
fi

# --------------------------------------------------------------------------- #
if [[ $DOWNLOAD == 1 ]]; then
    step "modelo"
    "$VENV/bin/python" - "$ROOT" <<'EOF'
import sys
sys.path.insert(0, sys.argv[1])
from wispr import config
cfg = config.load()
from faster_whisper.utils import download_model
mid = cfg.get("model_id")
print("          baixando %s para %s ..." % (mid, config.MODELS_DIR))
path = download_model(mid)
print("    [ok]  %s" % path)
EOF
fi

step "pronto"
info "rodar:      ./scripts/run.sh --console     (ou pelo menu: wisper)"
info "autostart:  ./scripts/autostart_install.sh"
info "testes:     ./tests/run_all.sh"
