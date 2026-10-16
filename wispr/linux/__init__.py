# -*- coding: utf-8 -*-
"""Implementacoes Linux (KDE Plasma 6 / Wayland / PipeWire) do wisper.

No Linux, `import wispr.hotkey` e `import wispr.inject` carregam os arquivos
DESTA pasta, sob os nomes de sempre: o `wispr/__init__.py`
instala um finder que troca so o arquivo-fonte, nunca o nome do modulo. O
`app.py`, a bandeja e a suite continuam escrevendo `from wispr import inject`,
e os logs continuam saindo como `wisper.inject`. Os arquivos Windows
`wispr/hotkey.py` e `wispr/inject.py` ficam intocados.

Nunca importe `wispr.linux.hotkey` / `wispr.linux.inject` pelo nome de dentro
desta pasta: seria uma SEGUNDA copia do modulo, com estado
proprio (o lock de injecao, o engine de atalho). Os modulos auxiliares
(`kde`, `wayland`, `clipboard`, `keyguard`) podem ser importados direto. A
pilula (`wispr/overlay.py`) e a bandeja sao os arquivos de sempre, com ramos
`sys.platform`.

Ver docs/LINUX.md.
"""
