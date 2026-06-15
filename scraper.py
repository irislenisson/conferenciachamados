import os
import sys
import time
import json
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

# --------------------------------------------------
# STATUS que disparam gravacao na Coluna G
# --------------------------------------------------
STATUS_RESOLVIDOS = {
    "RESOLVIDO", "RESOLVIDA",
    "CONCLUÍDO", "CONCLUÍDA",
    "ENCERRADO", "ENCERRADA",
}


def iniciar_automacao(socketio_emit_callback=None, ja_processados=None):
    # Inicializa conjunto de chamados processados
    ja_processados = ja_processados or set()

    # ─── helpers de log e alerta ──────────────
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

    def fechar_alertas(driver, contexto=""):
        """Descarta qualquer alerta aberto. Retorna True se havia alerta."""
        try:
            alert = driver.switch_to.alert
            texto = alert.text
            alert.accept()
            emitir_log(f"[AVISO] Alerta fechado [{contexto}]: {texto}")
            time.sleep(0.5)
            return True
        except Exception:
            return False

    # ─── helper: navegar com retry ────────────
    def navegar(driver, url, tentativas=3):
        for t in range(tentativas):
            try:
                fechar_alertas(driver, "pré-navegação")
                driver.get(url)
                return True
            except UnexpectedAlertPresentException:
                fechar_alertas(driver, f"navegação tentativa {t+1}")
                time.sleep(1)
            except Exception as e:
                emitir_log(f"Erro ao navegar (tentativa {t+1}): {str(e)}")
                time.sleep(1)
        return False

    # ─── helper: fazer login ──────────────────
    def fazer_login(driver, wait, ca_email, ca_password):
        """Tenta fazer login. Retorna True se logou ou se sessão já estava ativa."""
        username_field = None
        for tentativa in range(5):
            fechar_alertas(driver, f"login tentativa {tentativa+1}")
            try:
                username_field = WebDriverWait(driver, 3).until(
                    EC.presence_of_element_located((By.NAME, "USERNAME"))
                )
                break
            except UnexpectedAlertPresentException:
                fechar_alertas(driver, f"login alerta tentativa {tentativa+1}")
                time.sleep(1)
            except Exception:
                time.sleep(1)

        if username_field:
            try:
                password_field = driver.find_element(By.NAME, "PIN")
                emitir_log("[LOGIN] Tela de login detectada. Realizando login no CA SDM...")
                username_field.clear()
                username_field.send_keys(ca_email)
                password_field.clear()
                password_field.send_keys(ca_password)

                logon_clicado = False
                for seletor in [(By.ID, "imgBtn0"), (By.CLASS_NAME, "loginbtn"), (By.NAME, "HardcodedSub")]:
                    try:
                        driver.find_element(*seletor).click()
                        logon_clicado = True
                        emitir_log(f"   Botão de Logon clicado via seletor {seletor[1]}")
                        break
                    except Exception:
                        continue

                if not logon_clicado:
                    password_field.submit()

                time.sleep(3)
                return True
            except UnexpectedAlertPresentException:
                fechar_alertas(driver, "durante preenchimento do login")
                return False
            except Exception as e:
                emitir_log(f"Erro ao preencher login: {str(e)}")
                return False
        else:
            emitir_log("   Sessao ativa detectada (sem tela de login).")
            return True

    # --- helper: buscar chamado no gobtn ------
    def buscar_no_gobtn(driver, wait, id_chamado, valor_ticket, timeout_gobtn=8):
        """
        Entra no frame gobtn da janela atual, digita o ID e clica em Ir.
        Retorna True se conseguiu disparar a busca.
        """
        fechar_alertas(driver, "pre-gobtn")
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
            fechar_alertas(driver, "dentro do gobtn")
            driver.switch_to.default_content()
            return False
        except Exception as e:
            emitir_log(f"   buscar_no_gobtn falhou: {str(e)[:80]}")
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
            return False

    # --------------------------------------------------
    # Mapeamento Grupo CA SDM -> codigo Torre (Coluna D)
    # --------------------------------------------------
    def mapear_grupo_para_torre(grupo_raw):
        """Converte o nome do Grupo do CA SDM para o codigo da Torre."""
        if not grupo_raw:
            return ""
        g = grupo_raw.strip().upper()
        if "SERVICE DESK" in g and "NIVEL" in g:
            return "N1"
        if g.startswith("TORRE A"):
            return "A"
        if g.startswith("TORRE B"):
            return "B"
        if g.startswith("TORRE C"):
            return "C"
        if g.startswith("COEIN"):
            return "COEIN"
        if "GESTAO DE DADOS" in g or "GEST\u00c3O DE DADOS" in g:
            return "BI"
        return ""

    # --- helper: extrair dados do popup -------
    def extrair_dados_popup(driver, wait, id_chamado, index,
                            data_torre_atual, data_envio_atual,
                            emitir_log, grupos_nao_mapeados):
        """
        Inspeciona o popup do chamado (focado em cai_main).
        Retorna dicionario com valores encontrados para gravacao posterior:
        { 'col_d_val': str_ou_None, 'col_e_val': str_ou_None, 'col_g_val': str_ou_None }
        """
        chamado_carregado = False
        chamado_nao_encontrado = False

        for _ in range(30):
            try:
                fechar_alertas(driver, "aguardando cai_main")
                driver.switch_to.default_content()
                driver.switch_to.frame("cai_main")
                
                # Checa se o titulo carregou e contem o ID do chamado
                titulo = driver.title.strip()
                id_upper = id_chamado.upper()
                if titulo:
                    if id_upper in titulo.upper():
                        chamado_carregado = True
                        emitir_log(f"   Titulo do popup carregado: '{titulo}'")
                        break
                    elif ("n\u00e3o localizado" in titulo.lower()
                            or "n\u00e3o localizada" in titulo.lower()
                            or "n\u00e3o existe" in titulo.lower()
                            or "erro" in titulo.lower()
                            or "error" in titulo.lower()):
                        chamado_nao_encontrado = True
                        emitir_log(f"   Titulo de erro detectado: '{titulo}'")
                        break

                # Checa por sinal de nao localizado no page_source
                page_content = driver.page_source.lower()
                if ("n\u00e3o localizado" in page_content
                        or "n\u00e3o localizada" in page_content
                        or "n\u00e3o existe" in page_content):
                    chamado_nao_encontrado = True
                    emitir_log(f"   Mensagem de nao localizado no conteudo da pagina.")
                    break

                # Fallback: Checa se elemento de status ja existe na tela
                try:
                    el_status = driver.find_element(By.XPATH, "//*[@pdmqa='status'] | //*[@id='df_0_2_status']")
                    if el_status:
                        chamado_carregado = True
                        emitir_log(f"   Conteudo carregado no frame cai_main (fallback de status).")
                        break
                except Exception:
                    pass

            except UnexpectedAlertPresentException:
                fechar_alertas(driver, "cai_main loop")
            except Exception:
                pass
            time.sleep(0.5)

        if chamado_nao_encontrado:
            emitir_log(f"Linha {index}: [NAO LOCALIZADO] Chamado {id_chamado} nao localizado no CA SDM.")
            return None

        if not chamado_carregado:
            emitir_log(f"Linha {index}: [TIMEOUT] Timeout ao carregar popup do chamado {id_chamado}.")
            return None

        # Re-confirma foco no frame cai_main
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

        # -- Coluna D: Grupo/Torre -----------
        grupo_raw = ""
        for seletor in [
            (By.XPATH, "//*[@pdmqa='group']"),
            (By.ID, "df_5_2"),
        ]:
            try:
                el = driver.find_element(*seletor)
                txt = el.text.strip()
                if txt:
                    grupo_raw = txt
                    break
            except Exception:
                continue

        if grupo_raw:
            emitir_log(f"Linha {index}: Grupo no CA SDM -> '{grupo_raw}'")
            codigo_torre = mapear_grupo_para_torre(grupo_raw)
            if codigo_torre:
                valores_retornados['col_d_val'] = codigo_torre
            else:
                emitir_log(f"Linha {index}: [AVISO] Grupo '{grupo_raw}' nao mapeado para Torre.")
                grupos_nao_mapeados.add(grupo_raw)
        else:
            emitir_log(f"Linha {index}: Campo Grupo nao encontrado no popup.")

        # -- Coluna E: Data de Abertura (somente se E vazia na planilha) -----
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
                    data_fmt = data_obj.strftime("%d/%m/%Y")
                    valores_retornados['col_e_val'] = data_fmt
                except Exception as e:
                    emitir_log(f"Linha {index}: [ERRO] Erro ao formatar data abertura: {str(e)}")
            else:
                emitir_log(f"Linha {index}: Data de abertura nao encontrada no popup.")

        # -- Status real no CA SDM -----------
        status_real_ca = ""
        for seletor in [
            (By.XPATH, "//*[@pdmqa='status']"),
            (By.ID, "df_0_2_status"),
            (By.XPATH, "//*[contains(@id,'status') or contains(@name,'status')]"),
        ]:
            try:
                el = driver.find_element(*seletor)
                txt = el.text.strip()
                if txt:
                    status_real_ca = txt
                    break
            except Exception:
                continue

        emitir_log(f"Linha {index}: Status no CA SDM -> '{status_real_ca}'")

        # -- Coluna G: Data de Resolucao -----
        if status_real_ca.upper() in STATUS_RESOLVIDOS:
            campo_data_hora = ""
            for seletor in [
                (By.XPATH, "//*[@pdmqa='resolve_date']"),
                (By.ID, "df_8_2"),
                (By.XPATH, "//*[@pdmqa='close_date']"),
                (By.ID, "df_8_3"),
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
                    data_fmt = data_obj.strftime("%d/%m/%Y")
                    valores_retornados['col_g_val'] = data_fmt
                except Exception as e:
                    emitir_log(f"Linha {index}: Erro ao formatar data resolucao '{campo_data_hora}': {str(e)}")
            else:
                emitir_log(f"Linha {index}: Campo data de resolucao nao encontrado.")
        else:
            emitir_log(f"Linha {index}: Status '{status_real_ca}' nao e resolvido/encerrado. Coluna G inalterada.")

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

        # Envia progresso inicial
        if socketio_emit_callback:
            socketio_emit_callback('progresso', {'atual': len(ja_processados), 'total': total_pendentes})

        options = webdriver.ChromeOptions()
        driver = webdriver.Chrome(options=options)
        wait = WebDriverWait(driver, 10)

        # ── LOGIN ÚNICO (antes do loop) ───────────────────────────────────
        emitir_log("[CA SDM] Acessando CA SDM para login inicial...")
        navegar(driver, "http://vms-ca-sdm:8080/CAisd/pdmweb.exe")
        fazer_login(driver, wait, ca_email, ca_password)
        janela_principal = driver.current_window_handle
        emitir_log("[INICIO] Login inicial concluido. Iniciando varredura da planilha...")

        plano_a_count = 0
        plano_b_count = 0
        col_d_count   = 0  # Torre preenchida
        col_e_count   = 0  # Data abertura preenchida
        col_g_count   = 0  # Data resolucao preenchida
        
        grupos_nao_mapeados = set()

        # ── LOOP PRINCIPAL ────────────────────────────────────────────────
        for index, linha in enumerate(dados[1:], start=2):
            id_chamado       = linha[1].strip()
            status_h         = linha[7].strip()
            data_torre_atual = linha[3].strip() if len(linha) > 3 else ""
            data_envio_atual = linha[4].strip() if len(linha) > 4 else ""

            if status_h.upper() != "PENDENTE":
                continue

            # Se ja foi processado em execucao anterior, pula
            if id_chamado in ja_processados:
                emitir_log(f"Linha {index}: [OK] Chamado {id_chamado} ja processado em execucao anterior. Pulando.")
                if socketio_emit_callback:
                    socketio_emit_callback('progresso', {'atual': len(ja_processados), 'total': total_pendentes})
                continue

            emitir_log(f"\n{'-'*55}")
            emitir_log(f"[LINHA {index}] {id_chamado} | Status H: {status_h}")

            # Determina tipo pelo prefixo do ID
            id_upper = id_chamado.upper()
            if id_upper.startswith('I'):
                valor_ticket = "go_in"
            elif id_upper.startswith('R'):
                valor_ticket = "go_cr"
            elif id_upper.startswith('P'):
                valor_ticket = "go_pr"
            else:
                emitir_log(f"Linha {index}: [AVISO] Prefixo invalido '{id_chamado[0]}'. Pulando.")
                ja_processados.add(id_chamado)
                salvar_progresso(total_pendentes)
                if socketio_emit_callback:
                    socketio_emit_callback('progresso', {'atual': len(ja_processados), 'total': total_pendentes})
                continue

            # Garante que estamos na janela principal antes de cada busca
            try:
                driver.switch_to.window(janela_principal)
            except NoSuchWindowException:
                emitir_log("Janela principal perdida. Reabrindo sessão...")
                navegar(driver, "http://vms-ca-sdm:8080/CAisd/pdmweb.exe")
                fazer_login(driver, wait, ca_email, ca_password)
                janela_principal = driver.current_window_handle

            busca_ok = False

            # ════════════════════════════════════
            # PLANO A: Reutilizar sessão atual
            # ════════════════════════════════════
            emitir_log(f"   [PLANO A] Tentando busca na sessão ativa...")
            try:
                busca_ok = buscar_no_gobtn(driver, wait, id_chamado, valor_ticket, timeout_gobtn=8)
                if busca_ok:
                    # Aguarda popup abrir
                    WebDriverWait(driver, 12).until(lambda d: len(d.window_handles) > 1)
                    plano_a_count += 1
                    emitir_log(f"   [PLANO A] OK - Popup aberto com sessao reutilizada.")
            except Exception as e_a:
                emitir_log(f"   [PLANO A] FALHOU: {str(e_a)[:70]}")
                busca_ok = False

            # ════════════════════════════════════
            # PLANO B: Navegar + re-login
            # ════════════════════════════════════
            if not busca_ok:
                emitir_log(f"   [PLANO B] Navegando e realizando novo login...")
                try:
                    for handle in driver.window_handles:
                        if handle != janela_principal:
                            try:
                                driver.switch_to.window(handle)
                                driver.close()
                            except Exception:
                                pass
                    driver.switch_to.window(janela_principal)

                    navegar(driver, "http://vms-ca-sdm:8080/CAisd/pdmweb.exe")
                    fazer_login(driver, wait, ca_email, ca_password)
                    janela_principal = driver.current_window_handle

                    fechar_alertas(driver, "plano B pré-busca")
                    busca_ok = buscar_no_gobtn(driver, wait, id_chamado, valor_ticket, timeout_gobtn=12)

                    if busca_ok:
                        WebDriverWait(driver, 15).until(lambda d: len(d.window_handles) > 1)
                        plano_b_count += 1
                        emitir_log(f"   [PLANO B] OK - Popup aberto apos novo login.")
                    else:
                        emitir_log(f"   [PLANO B] FALHOU: Busca no gobtn falhou. Pulando chamado.")
                        ja_processados.add(id_chamado)
                        salvar_progresso(total_pendentes)
                        if socketio_emit_callback:
                            socketio_emit_callback('progresso', {'atual': len(ja_processados), 'total': total_pendentes})
                        continue
                except Exception as e_b:
                    emitir_log(f"   [PLANO B] FALHA CRITICA: {str(e_b)[:100]}. Pulando.")
                    ja_processados.add(id_chamado)
                    salvar_progresso(total_pendentes)
                    if socketio_emit_callback:
                        socketio_emit_callback('progresso', {'atual': len(ja_processados), 'total': total_pendentes})
                    continue

            if not busca_ok:
                ja_processados.add(id_chamado)
                salvar_progresso(total_pendentes)
                if socketio_emit_callback:
                    socketio_emit_callback('progresso', {'atual': len(ja_processados), 'total': total_pendentes})
                continue

            # ── Muda para o popup recém-aberto ───────────────────────────
            try:
                for handle in driver.window_handles:
                    if handle != janela_principal:
                        driver.switch_to.window(handle)
                        break
            except Exception as e:
                emitir_log(f"Linha {index}: Erro ao mudar para popup: {str(e)}")
                ja_processados.add(id_chamado)
                salvar_progresso(total_pendentes)
                if socketio_emit_callback:
                    socketio_emit_callback('progresso', {'atual': len(ja_processados), 'total': total_pendentes})
                continue

            emitir_log(f"   Popup detectado. Carregando dados de {id_chamado}...")

            # ── Extrai dados do popup ─────────────────────────────────────
            resultado = None
            try:
                resultado = extrair_dados_popup(
                    driver, wait, id_chamado, index,
                    data_torre_atual, data_envio_atual,
                    emitir_log, grupos_nao_mapeados
                )
                if isinstance(resultado, dict):
                    cells_to_update = []
                    
                    val_d = resultado.get('col_d_val')
                    val_e = resultado.get('col_e_val')
                    val_g = resultado.get('col_g_val')
                    
                    # Coluna D: Torre (atualiza se for diferente da atual)
                    if val_d:
                        if val_d != data_torre_atual:
                            cells_to_update.append(Cell(row=index, col=4, value=val_d))
                            col_d_count += 1
                            if data_torre_atual:
                                emitir_log(f"Linha {index}: [OK] Coluna D: '{data_torre_atual}' -> '{val_d}'")
                            else:
                                emitir_log(f"Linha {index}: [OK] Coluna D preenchida -> '{val_d}'")
                        else:
                            emitir_log(f"Linha {index}: Coluna D ja correta ('{data_torre_atual}'). Sem alteracao.")
                            
                    # Coluna E: Data Envio (somente se vazia)
                    if val_e:
                        if not data_envio_atual:
                            cells_to_update.append(Cell(row=index, col=5, value=val_e))
                            col_e_count += 1
                            emitir_log(f"Linha {index}: [OK] Coluna E atualizada -> {val_e}")
                        else:
                            emitir_log(f"Linha {index}: Coluna E ja preenchida ('{data_envio_atual}'). Ignorando.")
                            
                    # Coluna G: Data Resolucao (se houver)
                    if val_g:
                        cells_to_update.append(Cell(row=index, col=7, value=val_g))
                        col_g_count += 1
                        emitir_log(f"Linha {index}: [OK] Coluna G atualizada -> {val_g}")
                        
                    if cells_to_update:
                        worksheet.update_cells(cells_to_update)
                        emitir_log(f"Linha {index}: [OK] Planilha atualizada em lote.")
            except UnexpectedAlertPresentException:
                fechar_alertas(driver, "extracao de dados")
                emitir_log(f"Linha {index}: Alerta durante extracao. Pulando.")
            except Exception as e_ext:
                import traceback
                emitir_log(f"Linha {index}: Erro na extracao: {str(e_ext)}")
                emitir_log(traceback.format_exc())

            # Adiciona aos processados e salva progresso
            ja_processados.add(id_chamado)
            salvar_progresso(total_pendentes)
            if socketio_emit_callback:
                socketio_emit_callback('progresso', {'atual': len(ja_processados), 'total': total_pendentes})

            # ── Fecha popup e volta para janela principal ─────────────────
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

        # ── Relatorio final ───────────────────────────────────────────────
        total_chamados = plano_a_count + plano_b_count
        emitir_log(f"\n{'='*55}")
        emitir_log(f"[FIM] Varredura concluida!")
        emitir_log(f"{'='*55}")
        emitir_log(f"  Chamados processados : {total_chamados}")
        emitir_log(f"  Plano A (sessao ok)  : {plano_a_count}")
        emitir_log(f"  Plano B (novo login) : {plano_b_count}")
        emitir_log(f"{'-'*55}")
        emitir_log(f"  Atualizacoes na planilha:")
        emitir_log(f"    Col. D (Torre)        : {col_d_count} linha(s) atualizada(s)")
        emitir_log(f"    Col. E (Data Abertura): {col_e_count} linha(s) preenchida(s)")
        emitir_log(f"    Col. G (Data Resoluc.): {col_g_count} linha(s) preenchida(s)")
        emitir_log(f"{'-'*55}")
        # Explicitamente atendendo a solicitacao #6 do usuario:
        emitir_log(f"  Quantidade de atualizações feitas na Torre G (Feito em): {col_g_count}")
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
    finally:
        if 'driver' in locals():
            try:
                driver.quit()
            except Exception:
                pass
