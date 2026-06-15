import os
import sys
import time
import json
import threading
from datetime import datetime

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
from selenium.common.exceptions import UnexpectedAlertPresentException, NoSuchWindowException
from dotenv import load_dotenv

load_dotenv(override=True)

STATUS_RESOLVIDOS = {
    "RESOLVIDO", "RESOLVIDA",
    "CONCLUÍDO", "CONCLUÍDA",
    "ENCERRADO", "ENCERRADA",
}


def iniciar_automacao(socketio_emit_callback=None, ja_processados=None, headless=True, num_threads=1):
    # Inicializa conjunto de chamados processados
    ja_processados = ja_processados or set()

    # Locks para concorrência
    sheet_lock = threading.Lock()
    progress_lock = threading.Lock()
    cookies_lock = threading.Lock()

    # ─── helper de log ──────────────
    def emitir_log(mensagem):
        if socketio_emit_callback:
            socketio_emit_callback('log_message', {'data': mensagem})
        try:
            print(mensagem)
        except Exception:
            try:
                print(mensagem.encode('ascii', errors='replace').decode('ascii'))
            except Exception:
                pass

    def salvar_progresso(total_pendentes):
        try:
            with open('progresso.json', 'w', encoding='utf-8') as f:
                json.dump({
                    'processados': list(ja_processados),
                    'total': total_pendentes
                }, f, ensure_ascii=False, indent=4)
        except Exception as e_save:
            emitir_log(f"[ERRO] Falha ao salvar progresso: {str(e_save)}")

    def fechar_alertas(driver, thread_id, contexto=""):
        """Descarta qualquer alerta aberto. Retorna True se havia alerta."""
        try:
            alert = driver.switch_to.alert
            texto = alert.text
            alert.accept()
            emitir_log(f"[Navegador {thread_id}] [AVISO] Alerta fechado [{contexto}]: {texto}")
            time.sleep(0.5)
            return True
        except Exception:
            return False

    # ─── helper: navegar com retry ────────────
    def navegar(driver, url, thread_id, tentativas=3):
        for t in range(tentativas):
            try:
                fechar_alertas(driver, thread_id, "pré-navegação")
                driver.get(url)
                return True
            except UnexpectedAlertPresentException:
                fechar_alertas(driver, thread_id, f"navegação tentativa {t+1}")
                time.sleep(1)
            except Exception as e:
                emitir_log(f"[Navegador {thread_id}] Erro ao navegar (tentativa {t+1}): {str(e)}")
                time.sleep(1)
        return False

    # ─── helper: fazer login ──────────────────
    def fazer_login(driver, wait, ca_email, ca_password, thread_id):
        username_field = None
        for tentativa in range(5):
            fechar_alertas(driver, thread_id, f"login tentativa {tentativa+1}")
            try:
                username_field = WebDriverWait(driver, 3).until(
                    EC.presence_of_element_located((By.NAME, "USERNAME"))
                )
                break
            except UnexpectedAlertPresentException:
                fechar_alertas(driver, thread_id, f"login alerta tentativa {tentativa+1}")
                time.sleep(1)
            except Exception:
                time.sleep(1)

        if username_field:
            try:
                password_field = driver.find_element(By.NAME, "PIN")
                emitir_log(f"[Navegador {thread_id}] [LOGIN] Tela de login detectada. Realizando login...")
                username_field.clear()
                username_field.send_keys(ca_email)
                password_field.clear()
                password_field.send_keys(ca_password)

                logon_clicado = False
                for seletor in [(By.ID, "imgBtn0"), (By.CLASS_NAME, "loginbtn"), (By.NAME, "HardcodedSub")]:
                    try:
                        driver.find_element(*seletor).click()
                        logon_clicado = True
                        break
                    except Exception:
                        continue

                if not logon_clicado:
                    password_field.submit()

                time.sleep(3)
                return True
            except UnexpectedAlertPresentException:
                fechar_alertas(driver, thread_id, "durante preenchimento do login")
                return False
            except Exception as e:
                emitir_log(f"[Navegador {thread_id}] Erro ao preencher login: {str(e)}")
                return False
        else:
            emitir_log(f"[Navegador {thread_id}] Sessao ativa detectada (sem tela de login).")
            return True

    # --- helper: buscar chamado no gobtn ------
    def buscar_no_gobtn(driver, wait, id_chamado, valor_ticket, thread_id, timeout_gobtn=8):
        fechar_alertas(driver, thread_id, "pre-gobtn")
        try:
            driver.switch_to.default_content()
            WebDriverWait(driver, timeout_gobtn).until(
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
        except UnexpectedAlertPresentException:
            fechar_alertas(driver, thread_id, "dentro do gobtn")
            driver.switch_to.default_content()
            return False
        except Exception as e:
            emitir_log(f"[Navegador {thread_id}] buscar_no_gobtn falhou: {str(e)[:80]}")
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
            return False

    def mapear_grupo_para_torre(grupo_raw):
        if not grupo_raw:
            return ""
        g = grupo_raw.strip().upper()
        if "SERVICE DESK" in g and "NIVEL" in g:
            return "N1"
        if "TORRE A" in g:
            return "A"
        if "TORRE B" in g:
            return "B"
        if "TORRE C" in g:
            return "C"
        if "COEIN" in g:
            return "COEIN"
        if "GESTAO DE DADOS" in g or "GEST\u00c3O DE DADOS" in g or "BI" in g:
            return "BI"
        return ""

    # --- helper: extrair dados do popup -------
    def extrair_dados_popup(driver, wait, id_chamado, index,
                            data_torre_atual, data_envio_atual,
                            thread_id, grupos_nao_mapeados):
        chamado_carregado = False
        chamado_nao_encontrado = False

        for _ in range(30):
            try:
                fechar_alertas(driver, thread_id, "aguardando cai_main")
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
                fechar_alertas(driver, thread_id, "cai_main loop")
            except Exception:
                pass
            time.sleep(0.5)

        if chamado_nao_encontrado:
            emitir_log(f"[Navegador {thread_id}] Linha {index}: [NAO LOCALIZADO] Chamado {id_chamado} nao localizado.")
            return None

        if not chamado_carregado:
            emitir_log(f"[Navegador {thread_id}] Linha {index}: [TIMEOUT] Popup do chamado {id_chamado} nao carregou.")
            return None

        try:
            driver.switch_to.default_content()
            driver.switch_to.frame("cai_main")
        except Exception:
            pass

        valores_retornados = {
            'col_d_val': None,
            'col_e_val': None,
            'col_g_val': None
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
            codigo_torre = mapear_grupo_para_torre(grupo_raw)
            if codigo_torre:
                valores_retornados['col_d_val'] = codigo_torre
            else:
                emitir_log(f"[Navegador {thread_id}] Linha {index}: [AVISO] Grupo '{grupo_raw}' nao mapeado para Torre.")
                with progress_lock:
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
                    emitir_log(f"[Navegador {thread_id}] Linha {index}: [ERRO] Formatacao data abertura: {str(e)}")

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
                    emitir_log(f"[Navegador {thread_id}] Linha {index}: [ERRO] Formatacao resolucao '{campo_data_hora}': {str(e)}")

        return valores_retornados

    # ═══════════════════════════════════════════
    # EXECUÇÃO PRINCIPAL
    # ═══════════════════════════════════════════
    try:
        ca_email = os.getenv("CA_EMAIL")
        ca_password = os.getenv("CA_PASSWORD")

        emitir_log("[SHEETS] Iniciando autenticacao na API do Google Sheets...")
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        if not os.path.exists('credentials.json'):
            emitir_log("[ERRO] Arquivo credentials.json nao encontrado!")
            return

        credenciais = Credentials.from_service_account_file('credentials.json', scopes=scopes)
        client = gspread.authorize(credenciais)
        
        url_planilha = os.getenv("SHEETS_URL", "https://docs.google.com/spreadsheets/d/1ETTEHL0yJ7Y4qaAHqR7cSktEgmsRH6DkWzVkABMI8fU/edit?pli=1&gid=0#gid=0")
        sh = client.open_by_url(url_planilha)
        worksheet = sh.get_worksheet(0)
        emitir_log("[OK] Planilha conectada. Lendo dados...")
        dados = worksheet.get_all_values()

        # Conta total de chamados pendentes
        total_pendentes = sum(1 for linha in dados[1:] if len(linha) > 7 and linha[7].strip().upper() == "PENDENTE")
        emitir_log(f"[OK] Total de chamados PENDENTES na planilha: {total_pendentes}")

        # Identifica chamados duplicados entre os pendentes
        from collections import defaultdict
        chamados_linhas = defaultdict(list)
        for idx, linha in enumerate(dados[1:], start=2):
            if len(linha) > 7 and linha[7].strip().upper() == "PENDENTE":
                id_ch = linha[1].strip()
                if id_ch:
                    chamados_linhas[id_ch].append(idx)
        
        duplicados = {id_ch: lst for id_ch, lst in chamados_linhas.items() if len(lst) > 1}
        if duplicados:
            emitir_log("[AVISO] Chamados duplicados pendentes detectados na planilha:")
            for id_ch, lst in duplicados.items():
                emitir_log(f"   - Chamado {id_ch} encontrado nas linhas: {', '.join(map(str, lst))}")

        # Envia progresso inicial
        if socketio_emit_callback:
            socketio_emit_callback('progresso', {'atual': len(ja_processados), 'total': total_pendentes})

        # Coleta índices a serem processados
        indices_para_processar = []
        for index, linha in enumerate(dados[1:], start=2):
            status_h = linha[7].strip()
            if status_h.upper() != "PENDENTE":
                continue
            if index in ja_processados:
                continue
            indices_para_processar.append(index)

        if not indices_para_processar:
            emitir_log("[FIM] Nao ha chamados pendentes para processar.")
            return

        # Estatísticas finais compartilhadas
        stats = {
            'plano_a': 0, 'plano_b': 0,
            'col_d': 0, 'col_e': 0, 'col_g': 0
        }
        stats_lock = threading.Lock()
        grupos_nao_mapeados = set()

        # Função de execução de cada worker
        def worker_thread(thread_id, chunk_indices):
            emitir_log(f"[Navegador {thread_id}] Inicializando navegador (Headless={headless})...")
            options = webdriver.ChromeOptions()
            if headless:
                options.add_argument("--headless=new")
                options.add_argument("--window-size=1920,1080")
            
            driver = webdriver.Chrome(options=options)
            wait = WebDriverWait(driver, 10)

            # Autenticação e Cookies
            cookies_restaurados = False
            if os.path.exists('sessao_cookies.json'):
                try:
                    driver.get("http://vms-ca-sdm:8080/")
                    time.sleep(1)
                    with cookies_lock:
                        with open('sessao_cookies.json', 'r', encoding='utf-8') as f:
                            cookies = json.load(f)
                    for cookie in cookies:
                        try:
                            driver.add_cookie(cookie)
                        except Exception:
                            pass
                    driver.get("http://vms-ca-sdm:8080/CAisd/pdmweb.exe")
                    time.sleep(2)
                    try:
                        driver.find_element(By.NAME, "USERNAME")
                        emitir_log(f"[Navegador {thread_id}] Cookies expirados. Login necessario.")
                    except Exception:
                        emitir_log(f"[Navegador {thread_id}] [OK] Sessao restaurada com sucesso via cookies.")
                        cookies_restaurados = True
                except Exception as e_cook:
                    emitir_log(f"[Navegador {thread_id}] [AVISO] Falha ao carregar cookies: {str(e_cook)[:60]}")

            if not cookies_restaurados:
                driver.get("http://vms-ca-sdm:8080/CAisd/pdmweb.exe")
                fazer_login(driver, wait, ca_email, ca_password, thread_id)
                try:
                    with cookies_lock:
                        with open('sessao_cookies.json', 'w', encoding='utf-8') as f:
                            json.dump(driver.get_cookies(), f)
                    emitir_log(f"[Navegador {thread_id}] [OK] Cookies de sessao salvos.")
                except Exception as e_save:
                    emitir_log(f"[Navegador {thread_id}] Falha ao salvar cookies: {str(e_save)}")

            janela_principal = driver.current_window_handle
            
            # Executa os chamados do chunk
            for idx in chunk_indices:
                # Dupla checagem sob Lock para evitar processamentos redundantes
                with progress_lock:
                    if idx in ja_processados:
                        continue
                
                linha = dados[idx - 1]
                id_chamado       = linha[1].strip()
                status_h         = linha[7].strip()
                data_torre_atual = linha[3].strip() if len(linha) > 3 else ""
                data_envio_atual = linha[4].strip() if len(linha) > 4 else ""

                if id_chamado in duplicados:
                    outras_linhas = [l for l in duplicados[id_chamado] if l != idx]
                    emitir_log(f"[Navegador {thread_id}] Linha {idx}: [AVISO] Chamado duplicado {id_chamado}. Presente tambem na(s) linha(s): {', '.join(map(str, outras_linhas))}")

                emitir_log(f"\n{'-'*55}")
                emitir_log(f"[Navegador {thread_id}] [LINHA {idx}] {id_chamado} | Status H: {status_h}")

                # Determina tipo pelo prefixo do ID
                id_upper = id_chamado.upper()
                if id_upper.startswith('I'):
                    valor_ticket = "go_in"
                elif id_upper.startswith('R'):
                    valor_ticket = "go_cr"
                elif id_upper.startswith('P'):
                    valor_ticket = "go_pr"
                else:
                    emitir_log(f"[Navegador {thread_id}] Linha {idx}: [AVISO] Prefixo invalido '{id_chamado[0]}'. Pulando.")
                    with progress_lock:
                        ja_processados.add(idx)
                        salvar_progresso(total_pendentes)
                        if socketio_emit_callback:
                            socketio_emit_callback('progresso', {'atual': len(ja_processados), 'total': total_pendentes})
                    continue

                try:
                    driver.switch_to.window(janela_principal)
                except NoSuchWindowException:
                    emitir_log(f"[Navegador {thread_id}] Janela perdida. Re-login...")
                    driver.get("http://vms-ca-sdm:8080/CAisd/pdmweb.exe")
                    fazer_login(driver, wait, ca_email, ca_password, thread_id)
                    janela_principal = driver.current_window_handle

                busca_ok = False
                
                # PLANO A
                emitir_log(f"[Navegador {thread_id}] [PLANO A] Tentando busca na sessao ativa...")
                try:
                    busca_ok = buscar_no_gobtn(driver, wait, id_chamado, valor_ticket, thread_id, timeout_gobtn=8)
                    if busca_ok:
                        WebDriverWait(driver, 12).until(lambda d: len(d.window_handles) > 1)
                        with stats_lock:
                            stats['plano_a'] += 1
                        emitir_log(f"[Navegador {thread_id}] [PLANO A] OK - Popup aberto.")
                except Exception as e_a:
                    emitir_log(f"[Navegador {thread_id}] [PLANO A] FALHOU: {str(e_a)[:70]}")
                    busca_ok = False

                # PLANO B
                if not busca_ok:
                    emitir_log(f"[Navegador {thread_id}] [PLANO B] Navegando e realizando novo login...")
                    try:
                        for handle in driver.window_handles:
                            if handle != janela_principal:
                                try:
                                    driver.switch_to.window(handle)
                                    driver.close()
                                except Exception:
                                    pass
                        driver.switch_to.window(janela_principal)

                        driver.get("http://vms-ca-sdm:8080/CAisd/pdmweb.exe")
                        fazer_login(driver, wait, ca_email, ca_password, thread_id)
                        janela_principal = driver.current_window_handle

                        fechar_alertas(driver, thread_id, "plano B pré-busca")
                        busca_ok = buscar_no_gobtn(driver, wait, id_chamado, valor_ticket, thread_id, timeout_gobtn=12)

                        if busca_ok:
                            WebDriverWait(driver, 15).until(lambda d: len(d.window_handles) > 1)
                            with stats_lock:
                                stats['plano_b'] += 1
                            emitir_log(f"[Navegador {thread_id}] [PLANO B] OK - Popup aberto.")
                        else:
                            emitir_log(f"[Navegador {thread_id}] [PLANO B] FALHOU: Busca falhou. Pulando.")
                            with progress_lock:
                                ja_processados.add(idx)
                                salvar_progresso(total_pendentes)
                                if socketio_emit_callback:
                                    socketio_emit_callback('progresso', {'atual': len(ja_processados), 'total': total_pendentes})
                            continue
                    except Exception as e_b:
                        emitir_log(f"[Navegador {thread_id}] [PLANO B] FALHA CRITICA: {str(e_b)[:100]}. Pulando.")
                        with progress_lock:
                            ja_processados.add(idx)
                            salvar_progresso(total_pendentes)
                            if socketio_emit_callback:
                                socketio_emit_callback('progresso', {'atual': len(ja_processados), 'total': total_pendentes})
                        continue

                # Switch para o popup
                try:
                    for handle in driver.window_handles:
                        if handle != janela_principal:
                            driver.switch_to.window(handle)
                            break
                except Exception as e:
                    emitir_log(f"[Navegador {thread_id}] Linha {idx}: Erro ao mudar para popup: {str(e)}")
                    with progress_lock:
                        ja_processados.add(idx)
                        salvar_progresso(total_pendentes)
                        if socketio_emit_callback:
                            socketio_emit_callback('progresso', {'atual': len(ja_processados), 'total': total_pendentes})
                    continue

                # Extrai dados do popup
                resultado = None
                try:
                    resultado = extrair_dados_popup(
                        driver, wait, id_chamado, idx,
                        data_torre_atual, data_envio_atual,
                        thread_id, grupos_nao_mapeados
                    )
                    if isinstance(resultado, dict):
                        cells_to_update = []
                        val_d = resultado.get('col_d_val')
                        val_e = resultado.get('col_e_val')
                        val_g = resultado.get('col_g_val')

                        if val_d:
                            if val_d != data_torre_atual:
                                cells_to_update.append(Cell(row=idx, col=4, value=val_d))
                                with stats_lock:
                                    stats['col_d'] += 1
                                if data_torre_atual:
                                    emitir_log(f"[Navegador {thread_id}] Linha {idx}: [OK] Coluna D: '{data_torre_atual}' -> '{val_d}'")
                                else:
                                    emitir_log(f"[Navegador {thread_id}] Linha {idx}: [OK] Coluna D preenchida -> '{val_d}'")
                            else:
                                emitir_log(f"[Navegador {thread_id}] Linha {idx}: Coluna D ja correta ('{data_torre_atual}').")

                        if val_e:
                            if not data_envio_atual:
                                cells_to_update.append(Cell(row=idx, col=5, value=val_e))
                                with stats_lock:
                                    stats['col_e'] += 1
                                emitir_log(f"[Navegador {thread_id}] Linha {idx}: [OK] Coluna E atualizada -> {val_e}")
                            else:
                                emitir_log(f"[Navegador {thread_id}] Linha {idx}: Coluna E ja preenchida ('{data_envio_atual}').")

                        if val_g:
                            cells_to_update.append(Cell(row=idx, col=7, value=val_g))
                            with stats_lock:
                                stats['col_g'] += 1
                            emitir_log(f"[Navegador {thread_id}] Linha {idx}: [OK] Coluna G atualizada -> {val_g}")

                        if cells_to_update:
                            # Gravação thread-safe no Google Sheets
                            with sheet_lock:
                                worksheet.update_cells(cells_to_update)
                            emitir_log(f"[Navegador {thread_id}] Linha {idx}: [OK] Planilha atualizada com sucesso.")
                except UnexpectedAlertPresentException:
                    fechar_alertas(driver, thread_id, "extracao de dados")
                    emitir_log(f"[Navegador {thread_id}] Linha {idx}: Alerta durante extracao. Pulando chamado.")
                except Exception as e_ext:
                    import traceback
                    emitir_log(f"[Navegador {thread_id}] Linha {idx}: Erro na extracao: {str(e_ext)}")
                    emitir_log(traceback.format_exc())

                # Atualiza progresso local
                with progress_lock:
                    ja_processados.add(idx)
                    salvar_progresso(total_pendentes)
                    if socketio_emit_callback:
                        socketio_emit_callback('progresso', {'atual': len(ja_processados), 'total': total_pendentes})

                # Fecha o popup
                try:
                    driver.close()
                    driver.switch_to.window(janela_principal)
                except Exception:
                    try:
                        remaining = driver.window_handles
                        if remaining:
                            driver.switch_to.window(remaining[0])
                            janela_principal = driver.current_window_handle
                    except Exception:
                        pass

            try:
                driver.quit()
            except Exception:
                pass

        # Divide os índices entre as threads
        chunks = [[] for _ in range(num_threads)]
        for i, idx in enumerate(indices_para_processar):
            chunks[i % num_threads].append(idx)

        # Dispara as threads
        threads = []
        for thread_id in range(1, num_threads + 1):
            chunk = chunks[thread_id - 1]
            if not chunk:
                continue
            t = threading.Thread(target=worker_thread, args=(thread_id, chunk))
            threads.append(t)
            t.start()

        # Aguarda todas as threads
        for t in threads:
            t.join()

        # ── Relatorio final ───────────────────────────────────────────────
        total_chamados = stats['plano_a'] + stats['plano_b']
        emitir_log(f"\n{'='*55}")
        emitir_log(f"[FIM] Varredura concluida!")
        emitir_log(f"{'='*55}")
        emitir_log(f"  Chamados processados : {total_chamados}")
        emitir_log(f"  Plano A (sessao ok)  : {stats['plano_a']}")
        emitir_log(f"  Plano B (novo login) : {stats['plano_b']}")
        emitir_log(f"{'-'*55}")
        emitir_log(f"  Atualizacoes na planilha:")
        emitir_log(f"    Col. D (Torre)        : {stats['col_d']} linha(s) atualizada(s)")
        emitir_log(f"    Col. E (Data Abertura): {stats['col_e']} linha(s) preenchida(s)")
        emitir_log(f"    Col. G (Data Resoluc.): {stats['col_g']} linha(s) preenchida(s)")
        emitir_log(f"{'-'*55}")
        emitir_log(f"  Quantidade de atualizações feitas na Torre G (Feito em): {stats['col_g']}")
        emitir_log(f"{'-'*55}")
        if grupos_nao_mapeados:
            emitir_log(f"  Grupos nao mapeados detectados:")
            for grp in sorted(grupos_nao_mapeados):
                emitir_log(f"    - {grp}")
            emitir_log(f"{'-'*55}")
        emitir_log(f"{'='*55}")

        # Se concluiu tudo sem falhas, apaga o progresso salvo
        if os.path.exists('progresso.json'):
            try:
                os.remove('progresso.json')
            except Exception:
                pass

    except Exception as e:
        import traceback
        err_tb = traceback.format_exc()
        emitir_log(f"[ERRO CRITICO] {str(e)}\nTraceback:\n{err_tb}")
