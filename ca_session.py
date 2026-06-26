import os
import sys
import time
import json
import threading
import tempfile
import shutil
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import UnexpectedAlertPresentException, WebDriverException

_login_lock = threading.Lock()
_cookies_file_lock = threading.Lock()
# Serializa a criação de processos Chrome: ChromeDriver trava quando muitos
# são criados simultaneamente por competir por resources do SO.
_chrome_lock = threading.Lock()

class CASDMSession:
    """Gerencia a sessão do WebDriver no CA SDM, autenticação e cookies."""
    def __init__(self, ca_email, ca_password, thread_id, headless, log_callback, orchestrator=None):
        self.ca_email = ca_email
        self.ca_password = ca_password
        self.thread_id = thread_id
        self.headless = headless
        self.log_callback = log_callback
        self.orchestrator = orchestrator
        self.driver = None
        self.wait = None
        # Sessão compartilhada unificada: todos os navegadores leem/escrevem o mesmo arquivo.
        # Isso garante que a "dupla checagem sob lock" realmente aproveite o login de outra thread.
        self.cookie_file = 'sessao_cookies.json'
        self.shared_session_file = 'sessao_compartilhada.json'
        self.main_window_handle = None
        self.is_initialized = False
        self._temp_profile_dir = None  # Diretório temporário exclusivo deste navegador

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

    def inicializar_driver(self, queue_indices=None):
        # 1. Checkpoint antes de iniciar o Chrome
        if queue_indices:
            active_count = self.orchestrator.obter_threads_inicializadas() if self.orchestrator else 0
            if queue_indices.qsize() <= active_count:
                self.log_callback(f"[Navegador {self.thread_id}] Fila com {queue_indices.qsize()} chamados e {active_count} navegadores ativos. Abortando inicializacao.")
                return None

        self.log_callback(f"[Navegador {self.thread_id}] Inicializando navegador (Headless={self.headless})...")
        options = webdriver.ChromeOptions()
        if self.headless:
            options.add_argument("--headless=new")
            options.add_argument("--window-size=1920,1080")
            options.add_argument("--disable-gpu")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
        
        # Desativar extensões, infobars e habilitar automação controlada desligada para velocidade
        options.page_load_strategy = 'eager'
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-infobars")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--disable-default-apps")
        options.add_argument("--disable-background-networking")
        options.add_argument("--disable-sync")
        options.add_argument("--disable-translate")
        options.add_argument("--mute-audio")
        # Reduzido de 128→64MB: evita OOM quando muitos Chrome abrem simultaneamente
        options.add_argument("--js-flags=--max-old-space-size=64")
        # Evita throttling de timers em background que causa renderer timeouts
        options.add_argument("--disable-background-timer-throttling")
        options.add_argument("--disable-renderer-backgrounding")
        
        # ── Perfil persistente exclusivo por thread ──────────────────────────────
        # CRÍTICO: --user-data-dir garante isolamento total do Chrome do usuário.
        # Sem isso, o ChromeDriver pode compartilhar sessão com janelas abertas
        # no desktop e fechá-las ao fazer driver.quit().
        # Usamos uma pasta fixa (perfis_chrome) para reter os cookies e login entre varreduras.
        perfis_dir = os.path.abspath("perfis_chrome")
        os.makedirs(perfis_dir, exist_ok=True)
        self._temp_profile_dir = os.path.join(perfis_dir, f"perfil_nav{self.thread_id}")
        options.add_argument(f"--user-data-dir={self._temp_profile_dir}")
        options.add_argument("--profile-directory=Default")

        chrome_prefs = {
            "profile.managed_default_content_settings.images": 2,
            "profile.default_content_setting_values.notifications": 2
        }
        options.add_experimental_option("prefs", chrome_prefs)

        self.log_callback(f"[Navegador {self.thread_id}] Aguardando slot para abrir o Chrome...")
        with _chrome_lock:
            self.log_callback(f"[Navegador {self.thread_id}] Abrindo Chrome (perfil isolado)...")
            self.driver = webdriver.Chrome(options=options)
            self.driver.set_page_load_timeout(30)
            self.driver.set_script_timeout(30)
            self.wait = WebDriverWait(self.driver, 10)
            # Pequena pausa para o ChromeDriver estabilizar antes do proximo browser (reduzido de 1.0s para 0.5s)
            time.sleep(0.5)

        # 2. Checkpoint após iniciar o Chrome
        if queue_indices:
            active_count = self.orchestrator.obter_threads_inicializadas() if self.orchestrator else 0
            if queue_indices.qsize() <= active_count:
                self.log_callback(f"[Navegador {self.thread_id}] Fila com {queue_indices.qsize()} chamados e {active_count} navegadores ativos. Fechando navegador.")
                self.fechar_driver()
                return None

        # ── Navegação inicial: about:blank (local, sem tocar no servidor CA SDM) ──
        # O servidor CA SDM nao suporta N conexoes simultaneas.
        # Todo contato com o servidor fica serializado dentro do _login_lock abaixo.
        self.driver.get("about:blank")

        # 3. Checkpoint antes de aguardar o lock de autenticacao
        if queue_indices:
            active_count = self.orchestrator.obter_threads_inicializadas() if self.orchestrator else 0
            if queue_indices.qsize() <= active_count:
                self.log_callback(f"[Navegador {self.thread_id}] Fila com {queue_indices.qsize()} chamados e {active_count} navegadores ativos. Pulando login.")
                self.fechar_driver()
                return None

        self.log_callback(f"[Navegador {self.thread_id}] Aguardando lock para autenticacao no CA SDM...")
        lock_adquirido = _login_lock.acquire(timeout=90)
        if not lock_adquirido:
            self.log_callback(f"[Navegador {self.thread_id}] [ERRO] Timeout ao aguardar lock de autenticacao (90s). Abortando.")
            self.fechar_driver()
            return None
        try:
            # 4. Checkpoint sob lock
            if queue_indices:
                active_count = self.orchestrator.obter_threads_inicializadas() if self.orchestrator else 0
                if queue_indices.qsize() <= active_count:
                    self.log_callback(f"[Navegador {self.thread_id}] Fila com {queue_indices.qsize()} chamados e {active_count} navegadores ativos sob lock. Abortando.")
                    self.fechar_driver()
                    return None

            cookies_restaurados = False

            # O CA SDM (servidor intranet) não tolera o compartilhamento de um mesmo SID/sessão 
            # por múltiplos navegadores headless paralelos rodando simultaneamente. Isso invalidava o SID
            # e causava expiração e loops constantes de reautenticação.
            # Cada thread agora realiza o seu próprio login isolado de forma sequencial sob o lock de login.

            # ── Login completo isolado ──
            self.fechar_alertas("pre-login")
            try:
                # Navega para a pagina de login se ainda nao estiver la
                if "USERNAME" not in self.driver.page_source:
                    self.driver.get("http://vms-ca-sdm:8080/CAisd/pdmweb.exe")
                    WebDriverWait(self.driver, 15, poll_frequency=0.1).until(
                        lambda d: len(d.find_elements(By.NAME, "USERNAME")) > 0 or
                                  len(d.find_elements(By.NAME, "gobtn")) > 0
                    )
            except Exception:
                pass

            self.fazer_login()

            new_sid = self.extrair_sid_da_pagina()
            self.log_callback(f"[Navegador {self.thread_id}] [OK] Login concluido. Novo SID: {new_sid}")

            # Não salvamos cookies globais compartilhados para evitar que threads concorrentes
            # tentem restaurar a mesma sessão. O isolamento de sessão garante o correto funcionamento e estabilidade.

            # Pausa apos login: evita que o proximo browser conecte imediatamente
            self.log_callback(f"[Navegador {self.thread_id}] Aguardando 2s antes de liberar lock de login...")
            time.sleep(2.0)

        finally:
            _login_lock.release()

        self.main_window_handle = self.driver.current_window_handle
        self.is_initialized = True
        return self.driver


    def extrair_sid_da_pagina(self):
        # 1. Tentar obter a variável javascript cfgSID do window principal
        try:
            sid = self.driver.execute_script("return (typeof cfgSID !== 'undefined' && cfgSID) ? cfgSID.toString() : null;")
            if sid:
                return sid
        except Exception:
            pass

        # 2. Tentar obter do window principal via URL
        try:
            url_atual = self.driver.current_url
            import re
            match = re.search(r'SID=([^+\s&]+)', url_atual)
            if match:
                return match.group(1)
        except Exception:
            pass

        # 3. Tentar inspecionar os frames
        try:
            # Alterna para os frames conhecidos e tenta obter cfgSID
            for frame_name in ["gobtn", "cai_main"]:
                try:
                    self.driver.switch_to.default_content()
                    self.driver.switch_to.frame(frame_name)
                    sid = self.driver.execute_script("return (typeof cfgSID !== 'undefined' && cfgSID) ? cfgSID.toString() : null;")
                    if sid:
                        self.driver.switch_to.default_content()
                        return sid
                except Exception:
                    continue
            self.driver.switch_to.default_content()
        except Exception:
            try: self.driver.switch_to.default_content()
            except Exception: pass

        # 4. Tentar obter de qualquer frame por URL
        try:
            self.driver.switch_to.default_content()
            frames = self.driver.find_elements(By.TAG_NAME, "frame") or self.driver.find_elements(By.TAG_NAME, "iframe")
            for f in frames:
                src = f.get_attribute("src")
                if src:
                    import re
                    match = re.search(r'SID=([^+\s&]+)', src)
                    if match:
                        return match.group(1)
        except Exception:
            pass

        return None

    def fazer_login(self):
        """Realiza login no CA SDM com até 3 tentativas, tratando alertas de timeout de logon."""
        MAX_TENTATIVAS_LOGIN = 3
        for tentativa_login in range(1, MAX_TENTATIVAS_LOGIN + 1):
            if tentativa_login > 1:
                self.log_callback(f"[Navegador {self.thread_id}] [LOGIN] Retentativa {tentativa_login}/{MAX_TENTATIVAS_LOGIN}...")
                # Volta para a tela de login antes de tentar novamente
                try:
                    self.driver.get("http://vms-ca-sdm:8080/CAisd/pdmweb.exe")
                except Exception:
                    pass
                time.sleep(2)

            # Fecha qualquer alerta pendente antes de procurar o campo USERNAME
            self.fechar_alertas(f"pre-preenchimento tentativa {tentativa_login}")

            username_field = None
            for tentativa_campo in range(5):
                try:
                    username_field = WebDriverWait(self.driver, 4, poll_frequency=0.1).until(
                        EC.presence_of_element_located((By.NAME, "USERNAME"))
                    )
                    break
                except UnexpectedAlertPresentException:
                    self.fechar_alertas(f"aguardando USERNAME tentativa {tentativa_campo+1}")
                    time.sleep(1)
                except Exception:
                    time.sleep(1)

            if not username_field:
                self.log_callback(f"[Navegador {self.thread_id}] Sessao ativa detectada (sem tela de login).")
                return

            try:
                password_field = self.driver.find_element(By.NAME, "PIN")
                self.log_callback(f"[Navegador {self.thread_id}] [LOGIN] Tela de login detectada. Realizando login (tentativa {tentativa_login})...")
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

            except Exception as e:
                self.log_callback(f"[Navegador {self.thread_id}] Erro ao preencher login: {str(e)}")
                raise e

            # Aguarda resultado do login: gobtn (sucesso) ou alerta de timeout
            login_bem_sucedido = False
            try:
                # Tenta aguardar o frame gobtn por até 20s
                # Pode lançar UnexpectedAlertPresentException se o CA SDM exibir alerta
                WebDriverWait(self.driver, 20, poll_frequency=0.1).until(
                    EC.frame_to_be_available_and_switch_to_it("gobtn")
                )
                self.driver.switch_to.default_content()
                login_bem_sucedido = True
                self.log_callback(f"[Navegador {self.thread_id}] [LOGIN] Login confirmado (gobtn carregado).")
            except UnexpectedAlertPresentException:
                # CA SDM mostrou alerta (ex: AHD04042 - tempo de logon expirou)
                texto_alerta = "desconhecido"
                try:
                    texto_alerta = self.driver.switch_to.alert.text
                    self.driver.switch_to.alert.accept()
                except Exception:
                    pass
                self.log_callback(
                    f"[Navegador {self.thread_id}] [AVISO] Alerta de login na tentativa {tentativa_login}: "
                    f"{texto_alerta[:120]}. Retentando..."
                )
                time.sleep(2)
                # Continua para a próxima iteração do loop
            except Exception as e:
                # Outro erro (timeout de WebDriver etc.) — loga e tenta novamente
                self.log_callback(
                    f"[Navegador {self.thread_id}] [AVISO] Frame gobtn nao carregou na tentativa {tentativa_login}: "
                    f"{str(e)[:120]}"
                )
                # Fecha qualquer alerta residual
                self.fechar_alertas(f"pos-login tentativa {tentativa_login}")
                time.sleep(2)

            if login_bem_sucedido:
                return

        # Esgotou todas as tentativas
        raise RuntimeError(
            f"[Navegador {self.thread_id}] Falha ao realizar login apos {MAX_TENTATIVAS_LOGIN} tentativas. "
            "Verifique credenciais ou disponibilidade do CA SDM."
        )

    def fechar_driver(self):
        """Encerra o WebDriver e remove o perfil temporário isolado.
        NÃO decrementa o contador de threads — isso é feito pelo bloco
        finally do worker_thread no orchestrator para evitar duplo-decremento."""
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
            self.is_initialized = False

        # Como o perfil agora é persistente para reter login, não removemos mais o diretório.
        self._temp_profile_dir = None
