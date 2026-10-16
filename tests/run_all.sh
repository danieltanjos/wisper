#!/usr/bin/env bash
# Roda a suite offline do wisper no Linux (equivalente do tests/run_all.ps1).
#
# Todos os testes sao offline e passivos: nao abrem microfone, nao carregam
# modelo, nao conectam no D-Bus de sessao (nada de atalho global), nao
# conectam no Wayland (nada de tecla injetada), nao rodam wl-copy/wl-paste
# (nada de clipboard) e nao abrem janela. tests/_safety.py transforma tudo isso
# em RuntimeError. Pode rodar com a maquina em uso.
#
# Uso:
#   ./tests/run_all.sh                    # tudo
#   ./tests/run_all.sh -k Linux           # filtra (qualquer argumento do pytest)
#   ./tests/run_all.sh tests/test_dsp.py  # um arquivo
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || { echo "ERRO: $PY nao existe. Rode ./scripts/setup.sh" >&2; exit 2; }

cd "$ROOT"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUTF8=1
export PYTHONDONTWRITEBYTECODE=1
# -p no:cacheprovider: nada de .pytest_cache dentro do projeto.
if [[ $# -eq 0 ]]; then
    set -- tests
fi
exec "$PY" -m pytest -q -p no:cacheprovider "$@"
