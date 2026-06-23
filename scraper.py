# Fachada (Facade) para manter retrocompatibilidade com app.py e scripts externos
from sheets_service import SheetsService
from ca_session import CASDMSession
from ca_extractor import CASDMScraper, STATUS_RESOLVIDOS
from orchestrator import (
    AutomationOrchestrator,
    pausar_automacao,
    cancelar_automacao,
    resetar_fluxo,
    _automacao_pausada,
    _automacao_cancelada
)

# Wrapper simples para iniciar a automação utilizando o orquestrador
def iniciar_automacao(socketio_emit_callback=None, ja_processados=None, headless=True, num_threads=1, timeout_busca=8, timeout_pagina=15):
    orchestrator = AutomationOrchestrator(
        socketio_emit_callback=socketio_emit_callback,
        ja_processados=ja_processados,
        headless=headless,
        num_threads=num_threads,
        timeout_busca=timeout_busca,
        timeout_pagina=timeout_pagina
    )
    orchestrator.orquestrar()
