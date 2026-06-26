import os
import sys
import time
import json
import threading
import queue
from datetime import datetime
from collections import defaultdict
from gspread.cell import Cell

import database
from sheets_service import SheetsService
from ca_session import CASDMSession
from ca_extractor import CASDMScraper
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import WebDriverException, NoSuchWindowException


# Flags globais para Controle de Fluxo
_automacao_pausada = False
_automacao_cancelada = False
_fluxo_lock = threading.Lock()

def pausar_automacao(estado: bool):
    global _automacao_pausada
    with _fluxo_lock:
        _automacao_pausada = estado

def cancelar_automacao():
    global _automacao_cancelada
    with _fluxo_lock:
        _automacao_cancelada = True

def resetar_fluxo():
    global _automacao_pausada, _automacao_cancelada
    with _fluxo_lock:
        _automacao_pausada = False
        _automacao_cancelada = False


class AutomationOrchestrator:
    """Orquestrador principal que gerencia as threads, estatísticas e banco de dados."""
    def __init__(self, socketio_emit_callback, ja_processados, headless, num_threads, timeout_busca, timeout_pagina):
        self.socketio_emit_callback = socketio_emit_callback
        self.ja_processados = ja_processados or set()
        self.headless = headless
        self.num_threads = num_threads
        self.timeout_busca = timeout_busca
        self.timeout_pagina = timeout_pagina
        
        # Locks para sincronização entre threads
        self.sheet_lock = threading.Lock()
        self.progress_lock = threading.Lock()
        self.stats_lock = threading.Lock()
        self.buffer_lock = threading.Lock()
        
        self.grupos_nao_mapeados = set()
        self.duplicados = {}
        self.total_pendentes = 0
        self.mapeamentos_cache = []
        
        # Otimizado: Buffer para atualização em lote no Google Sheets
        self.cells_to_write_buffer = []
        
        # Estatísticas de execução
        self.stats = {
            'sucessos': 0,
            'avisos': 0,
            'erros': 0,
            'col_d': 0,
            'col_e': 0,
            'col_g': 0,
            'plano_a': 0,
            'plano_b': 0
        }
        self.exec_id = None
        self.data_inicio_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        self.initialized_threads_count = 0

    def obter_threads_inicializadas(self):
        with self.stats_lock:
            return self.initialized_threads_count

    def incrementar_threads_inicializadas(self):
        with self.stats_lock:
            self.initialized_threads_count += 1

    def decrementar_threads_inicializadas(self):
        with self.stats_lock:
            self.initialized_threads_count -= 1

    def log(self, mensagem):
        if self.socketio_emit_callback:
            self.socketio_emit_callback('log_message', {'data': mensagem})
        try:
            print(mensagem)
            sys.stdout.flush()
        except Exception:
            try:
                print(mensagem.encode('ascii', errors='replace').decode('ascii'))
                sys.stdout.flush()
            except Exception:
                pass

    def salvar_progresso(self):
        try:
            with open('progresso.json', 'w', encoding='utf-8') as f:
                json.dump({
                    'processados': list(self.ja_processados),
                    'total': self.total_pendentes
                }, f, ensure_ascii=False, indent=4)
        except Exception as e:
            self.log(f"[ERRO] Falha ao salvar progresso temporario: {str(e)}")

    def worker_thread(self, thread_id, queue_indices, dados, sheets_service, ca_email, ca_password):
        # --- CORREÇÃO 1: Registrar o início da thread ativa de forma real ---
        self.incrementar_threads_inicializadas()
        
        try:
            active_count = self.obter_threads_inicializadas()
            if queue_indices.qsize() <= (active_count - 1): # Desconta a si mesma
                self.log(f"[Navegador {thread_id}] Fila com {queue_indices.qsize()} chamados. Ignorando inicializacao.")
                return

            # Inicializa sessão do CA SDM
            session = CASDMSession(ca_email, ca_password, thread_id, self.headless, self.log, orchestrator=self)
            scraper = CASDMScraper(session, self.log, self.mapeamentos_cache)
            
            # Escalonamento por thread: reduzido de 1.0s para 0.5s. Como o _chrome_lock
            # já serializa a criação do Chrome, 0.5s é suficiente e acelera o início.
            if thread_id > 1:
                delay = (thread_id - 1) * 0.5
                self.log(f"[Navegador {thread_id}] Aguardando {delay:.1f}s para inicializacao escalonada...")
                time.sleep(delay)
                
            # Re-verifica após o delay
            if queue_indices.empty():
                self.log(f"[Navegador {thread_id}] Fila esvaziou durante o delay. Cancelando driver.")
                return
                
            try:
                driver = session.inicializar_driver(queue_indices)
                if not driver:
                    return
            except Exception as e:
                self.log(f"[Navegador {thread_id}] [ERRO] Falha critica de inicializacao: {str(e)}")
                with self.stats_lock:
                    self.stats['erros'] += 1
                return

            # Loop de processamento da fila
            while not queue_indices.empty():
                if _automacao_cancelada:
                    break
                while _automacao_pausada:
                    if _automacao_cancelada:
                        break
                    time.sleep(2)
                if _automacao_cancelada:
                    break

                try:
                    idx = queue_indices.get_nowait()
                except Exception:
                    break

                # Dupla checagem
                with self.progress_lock:
                    if idx in self.ja_processados:
                        continue

                linha = dados[idx - 1]
                id_chamado = linha[1].strip()
                status_h = linha[7].strip()
                data_torre_atual = linha[3].strip() if len(linha) > 3 else ""
                data_envio_atual = linha[4].strip() if len(linha) > 4 else ""
                data_resolucao_atual = linha[6].strip() if len(linha) > 6 else ""

                self.log(f"\n{'-'*55}\n[Navegador {thread_id}] [LINHA {idx}] {id_chamado} | Status H: {status_h}")

                id_upper = id_chamado.upper()
                if id_upper.startswith('I'):
                    valor_ticket = "go_in"
                elif id_upper.startswith('R'):
                    valor_ticket = "go_cr"
                elif id_upper.startswith('P'):
                    valor_ticket = "go_pr"
                else:
                    self.log(f"[Navegador {thread_id}] Linha {idx}: [AVISO] Prefixo invalido '{id_chamado[0]}'. Pulando.")
                    with self.stats_lock:
                        self.stats['avisos'] += 1
                    with self.progress_lock:
                        self.ja_processados.add(idx)
                        self.salvar_progresso()
                    continue

                tentativa_processo = 0
                processamento_completo = False

                while tentativa_processo < 2 and not processamento_completo:
                    tentativa_processo += 1
                    try:
                        # Auto-recuperação do WebDriver
                        try:
                            _ = session.driver.current_window_handle
                        except Exception:
                            self.log(f"[Navegador {thread_id}] [AUTORRECUPERAÇÃO] Reiniciando driver...")
                            session.fechar_driver()
                            session.inicializar_driver()

                        # Limpeza de janelas extras — só itera se realmente houver popups abertos
                        try:
                            handles = session.driver.window_handles
                            if len(handles) > 1:
                                for handle in list(handles):
                                    if handle != session.main_window_handle:
                                        session.driver.switch_to.window(handle)
                                        session.driver.close()
                                session.driver.switch_to.window(session.main_window_handle)
                        except Exception:
                            session.fechar_driver()
                            session.inicializar_driver()

                        # Busca
                        busca_ok = False
                        try:
                            busca_ok = scraper.buscar_no_gobtn(id_chamado, valor_ticket, timeout_busca=self.timeout_busca)
                            if busca_ok:
                                WebDriverWait(session.driver, 8.0, poll_frequency=0.1).until(lambda d: len(d.window_handles) > 1)
                                with self.stats_lock:
                                    self.stats['plano_a'] += 1
                        except Exception:
                            busca_ok = False
  
                        if not busca_ok:
                            # Plano B
                            session.fechar_driver()
                            session.inicializar_driver()
                            try:
                                busca_ok = scraper.buscar_no_gobtn(id_chamado, valor_ticket, timeout_busca=self.timeout_busca + 4)
                                if busca_ok:
                                    WebDriverWait(session.driver, 10.0, poll_frequency=0.1).until(lambda d: len(d.window_handles) > 1)
                                    with self.stats_lock:
                                        self.stats['plano_b'] += 1
                            except Exception:
                                busca_ok = False

                        if not busca_ok:
                            self.log(f"[Navegador {thread_id}] Linha {idx}: [AVISO] Nao encontrado.")
                            with self.stats_lock:
                                self.stats['avisos'] += 1
                            processamento_completo = True
                            continue

                        # Foca popup e extrai
                        popup_handle = None
                        for handle in session.driver.window_handles:
                            if handle != session.main_window_handle:
                                popup_handle = handle
                                session.driver.switch_to.window(handle)
                                break

                        if not popup_handle:
                            raise WebDriverException("Popup nao localizado.")

                        resultado = scraper.extrair_dados_popup(
                            id_chamado, idx, data_torre_atual, data_envio_atual,
                            self.grupos_nao_mapeados, data_resolucao_atual=data_resolucao_atual,
                            timeout_pagina=self.timeout_pagina
                        )

                        if resultado:
                            cells_to_update = []
                            val_d = resultado.get('col_d_val')
                            val_e = resultado.get('col_e_val')
                            val_g = resultado.get('col_g_val')

                            if val_d and val_d != data_torre_atual:
                                cells_to_update.append(Cell(row=idx, col=4, value=val_d))
                                with self.stats_lock: self.stats['col_d'] += 1; self.stats['sucessos'] += 1
                            if val_e and not data_envio_atual:
                                cells_to_update.append(Cell(row=idx, col=5, value=val_e))
                                with self.stats_lock: self.stats['col_e'] += 1; self.stats['sucessos'] += 1
                            if val_g:
                                cells_to_update.append(Cell(row=idx, col=7, value=val_g))
                                with self.stats_lock: self.stats['col_g'] += 1; self.stats['sucessos'] += 1

                            if cells_to_update:
                                with self.buffer_lock:
                                    self.cells_to_write_buffer.extend(cells_to_update)
                                    buffer_size = len(self.cells_to_write_buffer)
                                
                                if buffer_size >= 25:
                                    try:
                                        with self.buffer_lock:
                                            cells_to_write = list(self.cells_to_write_buffer)
                                            self.cells_to_write_buffer.clear()
                                        if cells_to_write:
                                            sheets_service.atualizar_celulas(cells_to_write)
                                    except Exception as e_partial:
                                        self.log(f"[ERRO] Parcial: {e_partial}")
                                        with self.buffer_lock:
                                            self.cells_to_write_buffer.extend(cells_to_write)
                            else:
                                with self.stats_lock: self.stats['sucessos'] += 1

                        try:
                            session.driver.close()
                            session.driver.switch_to.window(session.main_window_handle)
                        except Exception:
                            pass
                        
                        processamento_completo = True

                    except (WebDriverException, NoSuchWindowException) as e_drv:
                        if tentativa_processo >= 2:
                            with self.stats_lock: self.stats['erros'] += 1
                            processamento_completo = True
                    except Exception as e_gen:
                        with self.stats_lock: self.stats['erros'] += 1
                        processamento_completo = True

                with self.progress_lock:
                    self.ja_processados.add(idx)
                    # Otimização #5: grava progresso em disco a cada 5 chamados
                    # para reduzir contencao de I/O entre threads.
                    if len(self.ja_processados) % 5 == 0:
                        self.salvar_progresso()
                    if self.socketio_emit_callback:
                        self.socketio_emit_callback('progresso', {'atual': len(self.ja_processados), 'total': self.total_pendentes})

            # Garante gravação final do progresso ao encerrar a thread
            try:
                self.salvar_progresso()
            except Exception:
                pass

            # --- CORREÇÃO 2: Fechar driver de forma segura e garantida ---
            try:
                session.fechar_driver()
            except Exception:
                pass

        finally:
            # --- CORREÇÃO 3: Garantir decremento do contador não importa o erro interno ---
            self.decrementar_threads_inicializadas()
    def orquestrar(self):
        start_time = time.time()
        resetar_fluxo()
        
        database.inicializar_db()
        self.mapeamentos_cache = database.listar_mapeamentos()
        self.exec_id = database.criar_execucao(self.data_inicio_str)

        try:
            # Carrega configurações do banco SQLite
            config = database.obter_configuracoes()
            
            # Se vier de automação manual SocketIO, usa os parâmetros passados.
            # Caso contrário, usa os do banco.
            sheets_url = config.get('sheets_url') or os.getenv("SHEETS_URL")
            ca_email = os.getenv("CA_EMAIL")
            ca_password = os.getenv("CA_PASSWORD")

            sheets_service = SheetsService('credentials.json', sheets_url, self.log)
            sheets_service.conectar()
            dados = sheets_service.obter_dados()

            # Mapeia chamados pendentes
            self.total_pendentes = sum(1 for linha in dados[1:] if len(linha) > 7 and linha[7].strip().upper() == "PENDENTE")
            self.log(f"[OK] Total de chamados PENDENTES na planilha: {self.total_pendentes}")

            # Busca duplicados
            chamados_linhas = defaultdict(list)
            for idx, linha in enumerate(dados[1:], start=2):
                if len(linha) > 7 and linha[7].strip().upper() == "PENDENTE":
                    id_ch = linha[1].strip()
                    if id_ch:
                        chamados_linhas[id_ch].append(idx)
            
            self.duplicados = {id_ch: lst for id_ch, lst in chamados_linhas.items() if len(lst) > 1}
            if self.duplicados:
                self.log("[AVISO] Chamados duplicados pendentes encontrados:")
                for id_ch, lst in self.duplicados.items():
                    self.log(f"   - Chamado {id_ch} nas linhas: {', '.join(map(str, lst))}")

            # Filtra o que realmente falta processar
            indices_para_processar = []
            for idx, linha in enumerate(dados[1:], start=2):
                if len(linha) > 7 and linha[7].strip().upper() != "PENDENTE":
                    continue
                if idx in self.ja_processados:
                    continue
                indices_para_processar.append(idx)

            if not indices_para_processar:
                self.log("[FIM] Nenhum chamado pendente restante para processamento.")
                database.atualizar_execucao(self.exec_id, 0.0, 0, 0, 0, 0, 0, 0, 0)
                return

            if self.socketio_emit_callback:
                self.socketio_emit_callback('progresso', {'atual': len(self.ja_processados), 'total': self.total_pendentes})

            queue_indices = queue.Queue()
            for idx in indices_para_processar:
                queue_indices.put(idx)

            threads = []
            for t_id in range(1, self.num_threads + 1):
                t = threading.Thread(
                    target=self.worker_thread,
                    args=(t_id, queue_indices, dados, sheets_service, ca_email, ca_password),
                    name=f"WorkerThread_{t_id}"
                )
                threads.append(t)
                t.start()

            for t in threads:
                t.join()

            # Gravação final em Lote
            if self.cells_to_write_buffer:
                self.log(f"\n[SHEETS] Sincronizando {len(self.cells_to_write_buffer)} alteracoes finais em Lote no Google Sheets...")
                try:
                    sheets_service.atualizar_celulas(self.cells_to_write_buffer)
                    self.log("[OK] Sincronizacao em lote concluida com sucesso!")
                except Exception as e_batch:
                    self.log(f"[ERRO CRITICO] Falha ao gravar lote final: {str(e_batch)}")
            else:
                self.log("\n[SHEETS] Nenhuma gravacao de celula pendente.")

            # Conclusão
            duration = time.time() - start_time
            database.atualizar_execucao(
                exec_id=self.exec_id,
                tempo_total=duration,
                total_chamados=self.total_pendentes,
                sucessos=self.stats['sucessos'],
                avisos=self.stats['avisos'],
                erros=self.stats['erros'],
                col_d=self.stats['col_d'],
                col_e=self.stats['col_e'],
                col_g=self.stats['col_g']
            )

            status_txt = "Varredura cancelada!" if _automacao_cancelada else "Varredura concluida!"
            self.log(f"\n{'='*55}")
            self.log(f"[FIM] {status_txt}")
            self.log(f"{'='*55}")
            self.log(f"  Chamados Processados : {self.stats['sucessos'] + self.stats['avisos'] + self.stats['erros']}")
            self.log(f"  Sucessos (Validados) : {self.stats['sucessos']}")
            self.log(f"  Avisos               : {self.stats['avisos']}")
            self.log(f"  Erros (Crashes/etc)  : {self.stats['erros']}")
            self.log(f"  Plano A (Sessao OK)  : {self.stats['plano_a']}")
            self.log(f"  Plano B (Novo Login) : {self.stats['plano_b']}")
            self.log(f"{'-'*55}")
            self.log(f"  Col. D (Torre)      : {self.stats['col_d']} atualizacao(oes)")
            self.log(f"  Col. E (Abertura)   : {self.stats['col_e']} preenchimento(s)")
            self.log(f"  Col. G (Resolucao)  : {self.stats['col_g']} preenchimento(s)")
            self.log(f"{'='*55}")

            if not _automacao_pausada and os.path.exists('progresso.json'):
                try:
                    os.remove('progresso.json')
                except Exception:
                    pass

            # Notificação de Telegram
            tg_token = config.get('telegram_token')
            tg_chat_id = config.get('telegram_chat_id')
            if tg_token and tg_chat_id:
                self.log("\n[NOTIFICAÇÃO] Enviando resumo da execucao via Telegram...")
                msg = (
                    f"🤖 *Relatório de Conferência de Chamados*\n"
                    f"📅 Início: {self.data_inicio_str}\n"
                    f"⏱ Tempo total: {duration:.1f}s\n"
                    f"🎫 Total de chamados: {self.total_pendentes}\n"
                    f"✅ Sucessos: {self.stats['sucessos']}\n"
                    f"⚠️ Avisos: {self.stats['avisos']}\n"
                    f"❌ Erros: {self.stats['erros']}\n"
                    f"---------------------------------\n"
                    f"⚙ Atualizações na planilha:\n"
                    f"- Torre (Col D): {self.stats['col_d']}\n"
                    f"- Abertura (Col E): {self.stats['col_e']}\n"
                    f"- Resolução (Col G): {self.stats['col_g']}\n"
                )
                try:
                    import urllib.request
                    import urllib.parse
                    url_api = f"https://api.telegram.org/bot{tg_token}/sendMessage"
                    post_data = urllib.parse.urlencode({
                        'chat_id': tg_chat_id,
                        'text': msg,
                        'parse_mode': 'Markdown'
                    }).encode('utf-8')
                    req = urllib.request.Request(url_api, data=post_data, method='POST')
                    with urllib.request.urlopen(req) as resp:
                        resp.read()
                    self.log("[OK] Notificacao enviada com sucesso!")
                except Exception as e_tg:
                    self.log(f"[AVISO] Falha ao enviar Telegram: {e_tg}")

        except Exception as e:
            import traceback
            err_tb = traceback.format_exc()
            self.log(f"[ERRO CRITICO] {str(e)}\n{err_tb}")
            if self.exec_id:
                database.atualizar_execucao(
                    exec_id=self.exec_id,
                    tempo_total=time.time() - start_time,
                    total_chamados=self.total_pendentes,
                    sucessos=0,
                    avisos=0,
                    erros=1,
                    col_d=0,
                    col_e=0,
                    col_g=0
                )
