import time
from datetime import datetime
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select
from selenium.common.exceptions import UnexpectedAlertPresentException, WebDriverException

STATUS_RESOLVIDOS = {
    "RESOLVIDO", "RESOLVIDA",
    "CONCLUÍDO", "CONCLUÍDA",
    "ENCERRADO", "ENCERRADA",
    "CORRIGIDO", "CORRIGIDA"
}

class CASDMScraper:
    """Executa buscas e extrai detalhes dos chamados no CA SDM."""
    def __init__(self, session, log_callback, mapeamentos_cache):
        self.session = session
        self.log_callback = log_callback
        self.mapeamentos_cache = mapeamentos_cache

    def buscar_no_gobtn(self, id_chamado, valor_ticket, timeout_busca=8):
        driver = self.session.driver
        self.session.fechar_alertas("pre-gobtn")
        try:
            driver.switch_to.default_content()
            WebDriverWait(driver, timeout_busca, poll_frequency=0.1).until(
                EC.frame_to_be_available_and_switch_to_it("gobtn")
            )
            select_el = WebDriverWait(driver, timeout_busca, poll_frequency=0.1).until(
                EC.presence_of_element_located((By.NAME, "ticket_type"))
            )

            # #4: Select é síncrono — sem loop de verificação
            Select(select_el).select_by_value(valor_ticket)

            campo = driver.find_element(By.NAME, "searchKey")
            campo.clear()
            campo.send_keys(id_chamado)

            # #3: send_keys é síncrono — verifica 1× apenas como sanidade
            valor_atual = campo.get_attribute("value")
            if valor_atual != id_chamado:
                campo.clear()
                campo.send_keys(id_chamado)

            driver.find_element(By.ID, "imgBtn0").click()
            driver.switch_to.default_content()
            return True
        except Exception as e:
            self.log_callback(f"[Navegador {self.session.thread_id}] buscar_no_gobtn falhou: {str(e)[:200]}")
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

    def extrair_dados_popup(self, id_chamado, index, data_torre_atual, data_envio_atual, grupos_nao_mapeados, data_resolucao_atual="", timeout_pagina=15):
        driver = self.session.driver

        chamado_carregado = False
        chamado_nao_encontrado = False

        # Contador no escopo da closure para limitar a leitura pesada de page_source a cada 1.0s (10 checagens de 100ms)
        iteracoes = [0]

        def _popup_pronto(d):
            """Condição composta: retorna True assim que o popup estiver pronto."""
            try:
                self.session.fechar_alertas("aguardando popup")
                d.switch_to.default_content()

                top_title = d.title.lower()
                if any(k in top_title for k in ("logon", "login", "expirou", "erro", "error")):
                    raise WebDriverException(f"Erro de sessao detectado: {d.title}")

                if d.find_elements(By.NAME, "USERNAME"):
                    raise WebDriverException("Sessao expirada (login detectado no popup).")

                d.switch_to.frame("cai_main")
                titulo = d.title.strip()
                id_upper = id_chamado.upper()

                if titulo:
                    if id_upper in titulo.upper():
                        return "carregado"
                    if any(k in titulo.lower() for k in ("não localizado", "não localizada", "não existe")):
                        return "nao_encontrado"

                # Limita a leitura pesada do DOM inteiro via page_source
                iteracoes[0] += 1
                if iteracoes[0] % 10 == 0:
                    page = d.page_source.lower()
                    if any(k in page for k in ("não localizado", "não localizada", "não existe")):
                        return "nao_encontrado"

                # Busca rápida de carregado usando seletores CSS ou ID
                if d.find_elements(By.CSS_SELECTOR, "[pdmqa='status']") or d.find_elements(By.ID, "df_0_2_status"):
                    return "carregado"

                return False  # ainda carregando
            except UnexpectedAlertPresentException:
                self.session.fechar_alertas("popup pronto check")
                return False
            except WebDriverException:
                raise
            except Exception:
                return False

        try:
            resultado_wait = WebDriverWait(driver, timeout_pagina, poll_frequency=0.1).until(_popup_pronto)
            if resultado_wait == "carregado":
                chamado_carregado = True
            elif resultado_wait == "nao_encontrado":
                chamado_nao_encontrado = True
        except WebDriverException as e_web:
            raise e_web
        except Exception:
            pass

        if chamado_nao_encontrado:
            self.log_callback(f"[Navegador {self.session.thread_id}] Linha {index}: [NAO LOCALIZADO] Chamado {id_chamado} nao localizado.")
            return {"nao_localizado": True}

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
        for seletor in [(By.CSS_SELECTOR, "[pdmqa='group']"), (By.ID, "df_5_2")]:
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
            for seletor in [(By.CSS_SELECTOR, "[pdmqa='open_date']"), (By.ID, "df_8_0")]:
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
        for seletor in [(By.CSS_SELECTOR, "[pdmqa='status']"), (By.ID, "df_0_2_status")]:
            try:
                el = driver.find_element(*seletor)
                txt = el.text.strip()
                if txt:
                    status_real_ca = txt
                    break
            except Exception:
                continue

        # -- Coluna G: Data Resolucao -----------
        if status_real_ca.upper() in STATUS_RESOLVIDOS and not data_resolucao_atual:
            campo_data_hora = ""
            for seletor in [
                (By.CSS_SELECTOR, "[pdmqa='resolve_date']"),
                (By.ID,    "df_8_2"),
                (By.CSS_SELECTOR, "[pdmqa='close_date']"),
                (By.ID,    "df_8_3"),
                (By.CSS_SELECTOR, "[pdmqa='last_mod_dt']"),
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
                    self.log_callback(f"[Navegador {self.session.thread_id}] Linha {index}: [ERRO] Formatacao data resolucao '{campo_data_hora}': {str(e)}")
            else:
                self.log_callback(f"[Navegador {self.session.thread_id}] Linha {index}: [AVISO] Status '{status_real_ca}' reconhecido mas data de resolucao nao encontrada no popup.")

        return valores_retornados
