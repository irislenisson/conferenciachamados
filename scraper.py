import os
import sys
import time
import json
import threading
from datetime import datetime
import urllib.request
import random

# Garante que o terminal aceita UTF-8 mesmo em Windows (CP1252)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

import gspread
from google.oauth2.service_account import Credentials
from gspread.cell import Cell
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select
from selenium.common.exceptions import UnexpectedAlertPresentException, NoSuchWindowException, WebDriverException
from dotenv import load_dotenv

import database

load_dotenv(override=True)

STATUS_RESOLVIDOS = {
    "RESOLVIDO", "RESOLVIDA",
    "CONCLUÍDO", "CONCLUÍDA",
    "ENCERRADO", "ENCERRADA",
}

# ─── Flags globais para Controle de Fluxo (Pausar / Cancelar) ─────────────────
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


class SheetsService:
    """Serviço para gerenciar conexão e manipulação da planilha Google Sheets."""
    def __init__(self, credentials_path, sheets_url, log_callback):
        self.credentials_path = credentials_path
        self.sheets_url = sheets_url
        self.log_callback = log_callback
        self.client = None
        self.worksheet = None
        self.lock = threading.Lock()

    def conectar(self):
        self.log_callback("[SHEETS] Iniciando autenticacao na API do Google Sheets...")
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        if not os.path.exists(self.credentials_path):
            raise FileNotFoundError("Arquivo credentials.json nao encontrado!")
        
        credenciais = Credentials.from_service_account_file(self.credentials_path, scopes=scopes)
        self.client = gspread.authorize(credenciais)
        self.worksheet = self.client.open_by_url(self.sheets_url).get_worksheet(0)
        self.log_callback("[OK] Planilha conectada com sucesso.")

    def obter_dados(self):
        with self.lock:
            return self.worksheet.get_all_values()

    def atualizar_celulas(self, cells_to_update, max_tentativas=5):
        """Atualiza células no Google Sheets com retentativas e backoff exponencial."""
        for tentativa in range(1, max_tentativas + 1):
            try:
                with self.lock:
                    self.worksheet.update_cells(cells_to_update)
                return True
            except Exception as e:
                if tentativa == max_tentativas:
                    self.log_callback(f"[SHEETS] [ERRO CRITICO] Falha ao gravar dados apos {max_tentativas} tentativas: {str(e)}")
                    raise e
                
                # Backoff exponencial com jitter
                wait_time = (2 ** tentativa) + random.uniform(0.1, 1.0)
                self.log_callback(f"[SHEETS] [AVISO] Limite de cota ou erro de conexao. Re-tentando em {wait_time:.1f} segundos (Tentativa {tentativa}/{max_tentativas})...")
                time.sleep(wait_time)


class CASDMSession:
    """Gerencia a sessão do WebDriver no CA SDM, autenticação e cookies."""
    def __init__(self, ca_email, ca_password, thread_id, headless, log_callback):
        self.ca_email = ca_email
        self.ca_password = ca_password
        self.thread_id = thread_id
        self.headless = headless
        self.log_callback = log_callback
        self.driver = None
        self.wait = None
        self.cookie_file = f'sessao_cookies_{thread_id}.json'
        self.cookies_lock = threading.Lock()
        self.main_window_handle = None

    def fechar_alertas(self, contexto=""):
        if not self.driver:
            return False
        try:
            alert = self.driver.switch_to.alert
            texto = alert.text
            alert.accept()
            self.log_callback(f"[Navegador {self.thread_id}] [AVISO] Alerta fechado [{contexto}]: {texto}")
            time.sleep(0.5)
            return True
        except Exception:
            return False

    def inicializar_driver(self):
        self.log_callback(f"[Navegador {self.thread_id}] Inicializando navegador (Headless={self.headless})...")
        options = webdriver.ChromeOptions()
        if self.headless:
            options.add_argument("--headless=new")
            options.add_argument("--window-size=1920,1080")
            options.add_argument("--disable-gpu")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
        
        # Desabilitar imagens e notificações para economizar banda, CPU e acelerar renderização
        chrome_prefs = {
            "profile.managed_default_content_settings.images": 2,
            "profile.default_content_setting_values.notifications": 2
        }
        options.add_experimental_option("prefs", chrome_prefs)
        
        self.driver = webdriver.Chrome(options=options)
        self.wait = WebDriverWait(self.driver, 10)

        cookies_restaurados = False
        if os.path.exists(self.cookie_file):
            try:
                self.driver.get("http://vms-ca-sdm:8080/")
                with self.cookies_lock:
                    with open(self.cookie_file, 'r', encoding='utf-8') as f:
                        cookies = json.load(f)
                for cookie in cookies:
                    try:
                        self.driver.add_cookie(cookie)
                    except Exception:
                        pass
                self.driver.get("http://vms-ca-sdm:8080/CAisd/pdmweb.exe")
                try:
                    # Espera curta de até 1.5s para ver se o campo de login carrega.
                    # Se NÃO carregar (TimeoutException), fomos redirecionados direto para o sistema.
                    WebDriverWait(self.driver, 1.5).until(
                        EC.presence_of_element_located((By.NAME, "USERNAME"))
                    )
                    self.log_callback(f"[Navegador {self.thread_id}] Cookies expirados. Login necessario.")
                except Exception:
                    self.log_callback(f"[Navegador {self.thread_id}] [OK] Sessao restaurada com sucesso via cookies.")
                    cookies_restaurados = True
            except Exception as e:
                self.log_callback(f"[Navegador {self.thread_id}] [AVISO] Falha ao carregar cookies: {str(e)[:60]}")

        if not cookies_restaurados:
            self.driver.get("http://vms-ca-sdm:8080/CAisd/pdmweb.exe")
            self.fazer_login()
            try:
                with self.cookies_lock:
                    with open(self.cookie_file, 'w', encoding='utf-8') as f:
                        json.dump(self.driver.get_cookies(), f)
                self.log_callback(f"[Navegador {self.thread_id}] [OK] Cookies de sessao salvos.")
            except Exception as e:
                self.log_callback(f"[Navegador {self.thread_id}] Falha ao salvar cookies: {str(e)}")

        self.main_window_handle = self.driver.current_window_handle
        return self.driver

    def fazer_login(self):
        username_field = None
        for tentativa in range(5):
            self.fechar_alertas(f"login tentativa {tentativa+1}")
            try:
                username_field = WebDriverWait(self.driver, 3).until(
                    EC.presence_of_element_located((By.NAME, "USERNAME"))
                )
                break
            except UnexpectedAlertPresentException:
                self.fechar_alertas(f"login alerta tentativa {tentativa+1}")
                time.sleep(1)
            except Exception:
                time.sleep(1)

        if username_field:
            try:
                password_field = self.driver.find_element(By.NAME, "PIN")
                self.log_callback(f"[Navegador {self.thread_id}] [LOGIN] Tela de login detectada. Realizando login...")
                username_field.clear()
                username_field.send_keys(self.ca_email)
                password_field.clear()
                password_field.send_keys(self.ca_password)

                logon_clicado = False
                for seletor in [(By.ID, "imgBtn0"), (By.CLASS_NAME, "loginbtn"), (By.NAME, "HardcodedSub")]:
                    try:
                        self.driver.find_element(*seletor).click()
                        logon_clicado = True
                        break
                    except Exception:
                        continue

                if not logon_clicado:
                    password_field.submit()

                time.sleep(3)
            except Exception as e:
                self.log_callback(f"[Navegador {self.thread_id}] Erro ao preencher login: {str(e)}")
                raise e
        else:
            self.log_callback(f"[Navegador {self.thread_id}] Sessao ativa detectada (sem tela de login).")

    def fechar_driver(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None


class CASDMScraper:
    """Executa buscas e extrai detalhes dos chamados no CA SDM."""
    def __init__(self, session: CASDMSession, log_callback, mapeamentos_cache):
        self.session = session
        self.log_callback = log_callback
        self.mapeamentos_cache = mapeamentos_cache

    def buscar_no_gobtn(self, id_chamado, valor_ticket, timeout_busca=8):
        driver = self.session.driver
        self.session.fechar_alertas("pre-gobtn")
        try:
            driver.switch_to.default_content()
            WebDriverWait(driver, timeout_busca).until(
                EC.frame_to_be_available_and_switch_to_it("gobtn")
            )
            select_el = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.NAME, "ticket_type"))
            )
            Select(select_el).select_by_value(valor_ticket)

            campo = driver.find_element(By.NAME, "searchKey")
            campo.clear()
            campo.send_keys(id_chamado)

            driver.find_element(By.ID, "imgBtn0").click()
            driver.switch_to.default_content()
            return True
        except Exception as e:
            self.log_callback(f"[Navegador {self.session.thread_id}] buscar_no_gobtn falhou: {str(e)[:80]}")
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
            return False

    def mapear_grupo_para_torre(self, grupo_raw):
        if not grupo_raw:
            return ""
        g = grupo_raw.strip().upper()
        # Busca dinamicamente nos mapeamentos cadastrados no banco de dados SQLite
        for mapping in self.mapeamentos_cache:
            match_str = mapping['grupo_match'].strip().upper()
            if match_str in g:
                return mapping['torre']
        return ""

    def extrair_dados_popup(self, id_chamado, index, data_torre_atual, data_envio_atual, grupos_nao_mapeados, timeout_pagina=15):
        driver = self.session.driver
        chamado_carregado = False
        chamado_nao_encontrado = False

        # Otimizado: polling de 0.2s em vez de 0.5s para carregamentos mais rápidos
        limite_loops = int(timeout_pagina * 5)
        for _ in range(limite_loops):
            try:
                self.session.fechar_alertas("aguardando cai_main")
                driver.switch_to.default_content()
                driver.switch_to.frame("cai_main")
                
                titulo = driver.title.strip()
                id_upper = id_chamado.upper()
                if titulo:
                    if id_upper in titulo.upper():
                        chamado_carregado = True
                        break
                    elif ("n\u00e3o localizado" in titulo.lower()
                            or "n\u00e3o localizada" in titulo.lower()
                            or "n\u00e3o existe" in titulo.lower()
                            or "erro" in titulo.lower()
                            or "error" in titulo.lower()):
                        chamado_nao_encontrado = True
                        break

                page_content = driver.page_source.lower()
                if ("n\u00e3o localizado" in page_content
                        or "n\u00e3o localizada" in page_content
                        or "n\u00e3o existe" in page_content):
                    chamado_nao_encontrado = True
                    break

                try:
                    el_status = driver.find_element(By.XPATH, "//*[@pdmqa='status'] | //*[@id='df_0_2_status']")
                    if el_status:
                        chamado_carregado = True
                        break
                except Exception:
                    pass

            except UnexpectedAlertPresentException:
                self.session.fechar_alertas("cai_main loop")
            except Exception:
                pass
            time.sleep(0.2)

        if chamado_nao_encontrado:
            self.log_callback(f"[Navegador {self.session.thread_id}] Linha {index}: [NAO LOCALIZADO] Chamado {id_chamado} nao localizado.")
            return None

        if not chamado_carregado:
            self.log_callback(f"[Navegador {self.session.thread_id}] Linha {index}: [TIMEOUT] Popup do chamado {id_chamado} nao carregou.")
            return None

        try:
            driver.switch_to.default_content()
            driver.switch_to.frame("cai_main")
        except Exception:
            pass

        valores_retornados = {
            'col_d_val': None,
            'col_e_val': None,
            'col_g_val': None,
            'grupo_raw': None
        }

        # -- Coluna D: Torre -----------
        grupo_raw = ""
        for seletor in [(By.XPATH, "//*[@pdmqa='group']"), (By.ID, "df_5_2")]:
            try:
                el = driver.find_element(*seletor)
                txt = el.text.strip()
                if txt:
                    grupo_raw = txt
                    break
            except Exception:
                continue

        if grupo_raw:
            valores_retornados['grupo_raw'] = grupo_raw
            codigo_torre = self.mapear_grupo_para_torre(grupo_raw)
            if codigo_torre:
                valores_retornados['col_d_val'] = codigo_torre
            else:
                self.log_callback(f"[Navegador {self.session.thread_id}] Linha {index}: [AVISO] Grupo '{grupo_raw}' nao mapeado para Torre.")
                grupos_nao_mapeados.add(grupo_raw)

        # -- Coluna E: Data Abertura -----------
        if not data_envio_atual:
            campo_data_abertura = ""
            for seletor in [(By.XPATH, "//*[@pdmqa='open_date']"), (By.ID, "df_8_0")]:
                try:
                    el = driver.find_element(*seletor)
                    txt = el.text.strip()
                    if txt:
                        campo_data_abertura = txt
                        break
                except Exception:
                    continue

            if campo_data_abertura:
                try:
                    data_pura = campo_data_abertura.split()[0]
                    data_obj = datetime.strptime(data_pura, "%d/%m/%Y")
                    valores_retornados['col_e_val'] = data_obj.strftime("%d/%m/%Y")
                except Exception as e:
                    self.log_callback(f"[Navegador {self.session.thread_id}] Linha {index}: [ERRO] Formatacao data abertura: {str(e)}")

        # -- Status Real -----------
        status_real_ca = ""
        for seletor in [(By.XPATH, "//*[@pdmqa='status']"), (By.ID, "df_0_2_status")]:
            try:
                el = driver.find_element(*seletor)
                txt = el.text.strip()
                if txt:
                    status_real_ca = txt
                    break
            except Exception:
                continue

        # -- Coluna G: Data Resolucao -----------
        if status_real_ca.upper() in STATUS_RESOLVIDOS:
            campo_data_hora = ""
            for seletor in [
                (By.XPATH, "//*[@pdmqa='resolve_date']"), (By.ID, "df_8_2"),
                (By.XPATH, "//*[@pdmqa='close_date']"), (By.ID, "df_8_3"),
            ]:
                try:
                    el = driver.find_element(*seletor)
                    txt = el.text.strip()
                    if txt:
                        campo_data_hora = txt
                        break
                except Exception:
                    continue

            if campo_data_hora:
                try:
                    data_pura = campo_data_hora.split()[0]
                    data_obj = datetime.strptime(data_pura, "%d/%m/%Y")
                    valores_retornados['col_g_val'] = data_obj.strftime("%d/%m/%Y")
                except Exception as e:
                    self.log_callback(f"[Navegador {self.session.thread_id}] Linha {index}: [ERRO] Formatacao resolucao '{campo_data_hora}': {str(e)}")

        return valores_retornados


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

    def log(self, mensagem):
        if self.socketio_emit_callback:
            self.socketio_emit_callback('log_message', {'data': mensagem})
        try:
            print(mensagem)
        except Exception:
            try:
                print(mensagem.encode('ascii', errors='replace').decode('ascii'))
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

    def worker_thread(self, thread_id, chunk_indices, dados, sheets_service, ca_email, ca_password):
        # Inicializa sessão do CA SDM
        session = CASDMSession(ca_email, ca_password, thread_id, self.headless, self.log)
        scraper = CASDMScraper(session, self.log, self.mapeamentos_cache)
        
        try:
            session.inicializar_driver()
        except Exception as e:
            self.log(f"[Navegador {thread_id}] [ERRO] Falha critica de inicializacao: {str(e)}")
            with self.stats_lock:
                self.stats['erros'] += 1
            return

        for idx in chunk_indices:
            # ─── Verificações de Controle de Fluxo ────────────────────────────
            if _automacao_cancelada:
                self.log(f"[Navegador {thread_id}] Execucao cancelada. Parando thread...")
                break
            
            while _automacao_pausada:
                if _automacao_cancelada:
                    break
                self.log(f"[Navegador {thread_id}] Em pausa. Aguardando liberacao...")
                time.sleep(2)
                
            if _automacao_cancelada:
                self.log(f"[Navegador {thread_id}] Execucao cancelada. Parando thread...")
                break

            # Dupla checagem para evitar concorrência redundante
            with self.progress_lock:
                if idx in self.ja_processados:
                    continue

            linha = dados[idx - 1]
            id_chamado       = linha[1].strip()
            status_h         = linha[7].strip()
            data_torre_atual = linha[3].strip() if len(linha) > 3 else ""
            data_envio_atual = linha[4].strip() if len(linha) > 4 else ""

            if id_chamado in self.duplicados:
                outras_linhas = [l for l in self.duplicados[id_chamado] if l != idx]
                self.log(f"[Navegador {thread_id}] Linha {idx}: [AVISO] Chamado duplicado {id_chamado}. Também na(s) linha(s): {', '.join(map(str, outras_linhas))}")

            self.log(f"\n{'-'*55}")
            self.log(f"[Navegador {thread_id}] [LINHA {idx}] {id_chamado} | Status H: {status_h}")

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
                    if self.socketio_emit_callback:
                        self.socketio_emit_callback('progresso', {'atual': len(self.ja_processados), 'total': self.total_pendentes})
                continue

            # --- PROCESSAMENTO DO CHAMADO ---
            tentativa_processo = 0
            processamento_completo = False

            while tentativa_processo < 2 and not processamento_completo:
                tentativa_processo += 1
                try:
                    # Verifica se o driver está ativo
                    try:
                        _ = session.driver.current_window_handle
                    except Exception:
                        self.log(f"[Navegador {thread_id}] [AUTORRECUPERAÇÃO] WebDriver inativo. Reiniciando driver...")
                        session.fechar_driver()
                        session.inicializar_driver()

                    # Garante janela principal ativa
                    try:
                        for handle in list(session.driver.window_handles):
                            if handle != session.main_window_handle:
                                session.driver.switch_to.window(handle)
                                session.driver.close()
                        session.driver.switch_to.window(session.main_window_handle)
                    except Exception:
                        session.fechar_driver()
                        session.inicializar_driver()

                    # Executa busca no botão Go
                    busca_ok = False
                    self.log(f"[Navegador {thread_id}] [PLANO A] Buscando chamado {id_chamado}...")
                    try:
                        busca_ok = scraper.buscar_no_gobtn(id_chamado, valor_ticket, timeout_busca=self.timeout_busca)
                        if busca_ok:
                            # Se o chamado não existe, o CA SDM exibe um alerta de erro em vez de abrir popup.
                            # Reduzir este tempo para 4.5 segundos evita esperas desnecessárias.
                            WebDriverWait(session.driver, 4.5).until(lambda d: len(d.window_handles) > 1)
                            with self.stats_lock:
                                self.stats['plano_a'] += 1
                            self.log(f"[Navegador {thread_id}] [PLANO A] OK - Popup detectado.")
                        else:
                            busca_ok = False
                    except Exception:
                        busca_ok = False

                    if not busca_ok:
                        # PLANO B
                        self.log(f"[Navegador {thread_id}] [PLANO B] Recriando sessao...")
                        session.fechar_driver()
                        session.inicializar_driver()
                        session.fechar_alertas("plano B pré-busca")
                        try:
                            busca_ok = scraper.buscar_no_gobtn(id_chamado, valor_ticket, timeout_busca=self.timeout_busca + 4)
                            if busca_ok:
                                WebDriverWait(session.driver, 6).until(lambda d: len(d.window_handles) > 1)
                                with self.stats_lock:
                                    self.stats['plano_b'] += 1
                                self.log(f"[Navegador {thread_id}] [PLANO B] OK - Popup detectado.")
                        except Exception:
                            busca_ok = False

                    if not busca_ok:
                        self.log(f"[Navegador {thread_id}] Linha {idx}: [AVISO] Chamado nao encontrado no CA SDM.")
                        with self.stats_lock:
                            self.stats['avisos'] += 1
                        
                        scr_b64 = ""
                        try: scr_b64 = session.driver.get_screenshot_as_base64()
                        except Exception: pass
                        database.registrar_erro_detalhado(
                            self.exec_id, idx, id_chamado, 
                            "Chamado nao encontrado no CA SDM (Busca falhou)", scr_b64
                        )
                        processamento_completo = True
                        continue

                    # Alterna para a janela do popup
                    popup_handle = None
                    for handle in session.driver.window_handles:
                        if handle != session.main_window_handle:
                            popup_handle = handle
                            session.driver.switch_to.window(handle)
                            break

                    if not popup_handle:
                        raise WebDriverException("Falha ao focar janela do popup do chamado.")

                    # Extrai dados do popup
                    resultado = scraper.extrair_dados_popup(
                        id_chamado, idx, data_torre_atual, data_envio_atual, 
                        self.grupos_nao_mapeados, timeout_pagina=self.timeout_pagina
                    )

                    if resultado:
                        cells_to_update = []
                        val_d = resultado.get('col_d_val')
                        val_e = resultado.get('col_e_val')
                        val_g = resultado.get('col_g_val')
                        grupo_raw = resultado.get('grupo_raw')

                        if grupo_raw and not val_d:
                            database.registrar_grupo_desconhecido(self.exec_id, grupo_raw)

                        if val_d:
                            if val_d != data_torre_atual:
                                cells_to_update.append(Cell(row=idx, col=4, value=val_d))
                                with self.stats_lock:
                                    self.stats['col_d'] += 1
                                    self.stats['sucessos'] += 1
                                self.log(f"[Navegador {thread_id}] Linha {idx}: [OK] Coluna D identificada -> '{val_d}'")
                            else:
                                self.log(f"[Navegador {thread_id}] Linha {idx}: Coluna D ja esta correta ('{data_torre_atual}').")

                        if val_e:
                            if not data_envio_atual:
                                cells_to_update.append(Cell(row=idx, col=5, value=val_e))
                                with self.stats_lock:
                                    self.stats['col_e'] += 1
                                    self.stats['sucessos'] += 1
                                self.log(f"[Navegador {thread_id}] Linha {idx}: [OK] Coluna E identificada -> {val_e}")

                        if val_g:
                            cells_to_update.append(Cell(row=idx, col=7, value=val_g))
                            with self.stats_lock:
                                self.stats['col_g'] += 1
                                self.stats['sucessos'] += 1
                            self.log(f"[Navegador {thread_id}] Linha {idx}: [OK] Coluna G identificada -> {val_g}")

                        if cells_to_update:
                            # Otimizado: Bufferiza as gravações em vez de chamar HTTP sincronamente agora
                            with self.buffer_lock:
                                self.cells_to_write_buffer.extend(cells_to_update)
                                buffer_size = len(self.cells_to_write_buffer)
                            
                            self.log(f"[Navegador {thread_id}] Linha {idx}: [OK] Alteracoes adicionadas ao buffer.")
                            
                            # Para segurança (proteção de memória e quedas repentinas), se o buffer passar de 15 células, descarrega parcial
                            if buffer_size >= 15:
                                self.log(f"[Navegador {thread_id}] [SHEETS] Buffer cheio ({buffer_size} celulas). Gravando parcial...")
                                try:
                                    with self.buffer_lock:
                                        cells_to_write = list(self.cells_to_write_buffer)
                                        self.cells_to_write_buffer.clear()
                                    if cells_to_write:
                                        sheets_service.atualizar_celulas(cells_to_write)
                                        self.log(f"[Navegador {thread_id}] [OK] Gravacao parcial concluida.")
                                except Exception as e_partial:
                                    self.log(f"[Navegador {thread_id}] [ERRO] Falha ao gravar parcial: {str(e_partial)}")
                                    # Devolve ao buffer em caso de erro para tentar novamente mais tarde
                                    with self.buffer_lock:
                                        self.cells_to_write_buffer.extend(cells_to_write)
                        else:
                            with self.stats_lock:
                                self.stats['sucessos'] += 1
                            self.log(f"[Navegador {thread_id}] Linha {idx}: [OK] Verificado sem pendências de atualização.")
                    else:
                        with self.stats_lock:
                            self.stats['avisos'] += 1

                    # Fecha o popup
                    try:
                        session.driver.close()
                        session.driver.switch_to.window(session.main_window_handle)
                    except Exception:
                        pass
                    
                    processamento_completo = True

                except (WebDriverException, NoSuchWindowException) as e_drv:
                    self.log(f"[Navegador {thread_id}] [AVISO] Falha de WebDriver na tentativa {tentativa_processo}: {str(e_drv)[:80]}")
                    
                    scr_b64 = ""
                    try: scr_b64 = session.driver.get_screenshot_as_base64()
                    except Exception: pass
                    database.registrar_erro_detalhado(
                        self.exec_id, idx, id_chamado, 
                        f"Falha de WebDriver: {str(e_drv)}", scr_b64
                    )
                    
                    if tentativa_processo >= 2:
                        self.log(f"[Navegador {thread_id}] [ERRO] Desistindo do chamado {id_chamado} apos falhas repetidas.")
                        with self.stats_lock:
                            self.stats['erros'] += 1
                        processamento_completo = True
                except Exception as e_gen:
                    self.log(f"[Navegador {thread_id}] [ERRO] Falha geral no chamado {id_chamado}: {str(e_gen)}")
                    
                    scr_b64 = ""
                    try: scr_b64 = session.driver.get_screenshot_as_base64()
                    except Exception: pass
                    database.registrar_erro_detalhado(
                        self.exec_id, idx, id_chamado, 
                        f"Erro Geral: {str(e_gen)}", scr_b64
                    )
                    
                    with self.stats_lock:
                        self.stats['erros'] += 1
                    processamento_completo = True

            # Registra progresso e notifica frontend
            with self.progress_lock:
                self.ja_processados.add(idx)
                self.salvar_progresso()
                if self.socketio_emit_callback:
                    self.socketio_emit_callback('progresso', {'atual': len(self.ja_processados), 'total': self.total_pendentes})

        # Finalização da thread
        session.fechar_driver()

    def orquestrar(self):
        start_time = time.time()
        resetar_fluxo()
        
        # Inicializa banco de dados e carrega cache de mapeamentos
        database.inicializar_db()
        self.mapeamentos_cache = database.listar_mapeamentos()
        self.exec_id = database.criar_execucao(self.data_inicio_str)

        try:
            ca_email = os.getenv("CA_EMAIL")
            ca_password = os.getenv("CA_PASSWORD")
            sheets_url = os.getenv("SHEETS_URL", "https://docs.google.com/spreadsheets/d/1ETTEHL0yJ7Y4qaAHqR7cSktEgmsRH6DkWzVkABMI8fU/edit?pli=1&gid=0#gid=0")

            sheets_service = SheetsService('credentials.json', sheets_url, self.log)
            sheets_service.conectar()
            dados = sheets_service.obter_dados()

            # Mapeia chamados pendentes
            self.total_pendentes = sum(1 for linha in dados[1:] if len(linha) > 7 and linha[7].strip().upper() == "PENDENTE")
            self.log(f"[OK] Total de chamados PENDENTES na planilha: {self.total_pendentes}")

            # Busca duplicados
            from collections import defaultdict
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

            # Filtra o que realmente falta processar nesta rodada
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

            # Divisão dos índices entre os navegadores
            chunks = [[] for _ in range(self.num_threads)]
            for i, idx in enumerate(indices_para_processar):
                chunks[i % self.num_threads].append(idx)

            threads = []
            for t_id in range(1, self.num_threads + 1):
                chunk = chunks[t_id - 1]
                if not chunk:
                    continue
                t = threading.Thread(
                    target=self.worker_thread,
                    args=(t_id, chunk, dados, sheets_service, ca_email, ca_password)
                )
                threads.append(t)
                t.start()

            for t in threads:
                t.join()

            # Otimizado: Gravação final de todas as células restantes no buffer (em Lote)
            if self.cells_to_write_buffer:
                self.log(f"\n[SHEETS] Sincronizando {len(self.cells_to_write_buffer)} alteracoes finais em Lote no Google Sheets...")
                try:
                    sheets_service.atualizar_celulas(self.cells_to_write_buffer)
                    self.log("[OK] Sincronizacao em lote concluida com sucesso!")
                except Exception as e_batch:
                    self.log(f"[ERRO CRITICO] Falha ao gravar lote final: {str(e_batch)}")
            else:
                self.log("\n[SHEETS] Nenhuma gravacao de celula pendente.")

            # Conclusão e Relatório Final
            duration = time.time() - start_time
            
            # Atualiza banco SQLite
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

            # Limpa progresso salvo ao concluir tudo com sucesso (ou cancelamento explícito)
            if not _automacao_pausada and os.path.exists('progresso.json'):
                try:
                    os.remove('progresso.json')
                except Exception:
                    pass

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
