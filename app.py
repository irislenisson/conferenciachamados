import os
import json
import threading
from dotenv import load_dotenv
load_dotenv()
from functools import wraps
from flask import Flask, render_template, jsonify, request, redirect, url_for, session, Response
from flask_socketio import SocketIO
from scraper import iniciar_automacao, pausar_automacao, cancelar_automacao
import database

# Inicializa as tabelas do banco no arranque do servidor
database.inicializar_db()

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

PROGRESS_FILE = 'progresso.json'

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'dev_troque_no_env')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Flag global para impedir múltiplas execuções concorrentes
_automacao_em_andamento = False

# Decorador para exigir login
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Não autorizado'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def _carregar_progresso():
    """Lê o arquivo de progresso salvo de uma execução interrompida."""
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return None

@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('logged_in'):
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        user = database.obter_usuario(username)
        if user and database.verificar_senha(password, user['password_hash']):
            session['logged_in'] = True
            session['username'] = username
            return redirect(url_for('index'))
        
        return render_template('login.html', error='Usuário ou senha incorretos.')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    return render_template('index.html')

@app.route('/api/progresso')
@login_required
def api_progresso():
    """Endpoint REST: informa se há progresso salvo de uma execução anterior."""
    info = _carregar_progresso()
    if info and info.get('processados'):
        return jsonify({
            'tem_progresso': True,
            'processados': len(info['processados']),
            'total': info.get('total', '?')
        })
    return jsonify({'tem_progresso': False})

@app.route('/api/historico')
@login_required
def api_historico():
    """Endpoint REST: retorna a lista das execuções passadas no SQLite."""
    try:
        dados = database.listar_historico()
        return jsonify(dados)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/relatorio-pendentes', methods=['GET'])
@login_required
def api_relatorio_pendentes():
    try:
        from scraper import SheetsService
        from datetime import datetime, timezone, timedelta
        
        config = database.obter_configuracoes()
        sheets_url = config.get('sheets_url') or os.getenv("SHEETS_URL", "https://docs.google.com/spreadsheets/d/1ETTEHL0yJ7Y4qaAHqR7cSktEgmsRH6DkWzVkABMI8fU/edit?pli=1&gid=0#gid=0")
        
        # Conecta e obtém dados reais e atualizados do Sheets
        sheets_service = SheetsService('credentials.json', sheets_url, lambda msg: print(msg))
        sheets_service.conectar()
        dados = sheets_service.obter_dados()
        
        if not dados or len(dados) <= 1:
            return jsonify({'error': 'Nenhum dado encontrado na planilha.'}), 404
        
        # Agrupamento de chamados L por torre D
        chamados_por_torre = {}
        for linha in dados[1:]:
            if len(linha) > 7:
                status = linha[7].strip().upper()
                if status == "PENDENTE":
                    col_l = linha[11].strip() if len(linha) > 11 else ""
                    if col_l:
                        torre = linha[3].strip() if len(linha) > 3 else ""
                        if not torre:
                            torre = "-"
                        if torre not in chamados_por_torre:
                            chamados_por_torre[torre] = []
                        chamados_por_torre[torre].append(col_l)
                        
        if not chamados_por_torre:
            return jsonify({'message': 'Nenhum chamado PENDENTE com conteúdo na coluna L foi encontrado.'}), 200
            
        # Obter data e período do dia no Brasil GMT-3
        tz_gmt3 = timezone(timedelta(hours=-3))
        now_gmt3 = datetime.now(timezone.utc).astimezone(tz_gmt3)
        data_str = now_gmt3.strftime("%d/%m/%Y")
        periodo = "manhã" if now_gmt3.hour < 12 else "tarde"
        
        linhas_relatorio = []
        # Ordenar as torres alfabeticamente
        torres_ordenadas = sorted(chamados_por_torre.keys())
        
        for torre in torres_ordenadas:
            chamados = chamados_por_torre[torre]
            # Ordenar chamados de cada torre alfabeticamente
            chamados_ordenados = sorted(chamados)
            count = len(chamados_ordenados)
            
            # Cabeçalho da Torre
            header = f"*{count}* chamados na Torre {torre} em {data_str} - Período da {periodo}"
            linhas_relatorio.append(header)
            
            # Chamados
            for ch in chamados_ordenados:
                linhas_relatorio.append(ch)
                
            # Separador em branco entre torres
            linhas_relatorio.append("")
            
        # Limpar o último elemento em branco
        if linhas_relatorio and linhas_relatorio[-1] == "":
            linhas_relatorio.pop()
            
        texto_final = "\r\n".join(linhas_relatorio)
        filename = f"relatorio_pendentes_{now_gmt3.strftime('%d_%m_%Y_%H_%M_%S')}.txt"
        
        return Response(
            texto_final,
            mimetype="text/plain;charset=utf-8",
            headers={"Content-disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# ─── Mapeamento CRUD Endpoints ───────────────────────────────────────────

@app.route('/api/mapeamentos', methods=['GET'])
@login_required
def api_get_mapeamentos():
    try:
        dados = database.listar_mapeamentos()
        return jsonify(dados)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/mapeamentos', methods=['POST'])
@login_required
def api_post_mapeamento():
    try:
        req_data = request.json or {}
        grupo_match = req_data.get('grupo_match', '')
        torre = req_data.get('torre', '')
        if not grupo_match or not torre:
            return jsonify({'error': 'grupo_match e torre sao obrigatorios'}), 400
        
        success = database.inserir_mapeamento(grupo_match, torre)
        return jsonify({'success': success})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/mapeamentos/<int:mapping_id>', methods=['DELETE'])
@login_required
def api_delete_mapeamento(mapping_id):
    try:
        database.deletar_mapeamento(mapping_id)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─── Endpoint de Auditoria de Erros (Captura de Tela base64) ──────────────

@app.route('/api/execucoes/<int:exec_id>/erros', methods=['GET'])
@login_required
def api_get_erros_execucao(exec_id):
    try:
        dados = database.listar_erros_execucao(exec_id)
        return jsonify(dados)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─── Endpoints de Configurações Globais ───────────────────────────────────

@app.route('/api/configuracoes', methods=['GET'])
@login_required
def api_get_configuracoes():
    try:
        configs = database.obter_configuracoes()
        return jsonify(configs)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/configuracoes', methods=['POST'])
@login_required
def api_post_configuracoes():
    try:
        req_data = request.json or {}
        
        configs = {}
        if 'sheets_url' in req_data:
            configs['sheets_url'] = str(req_data['sheets_url'])
        if 'num_threads' in req_data:
            configs['num_threads'] = int(req_data['num_threads'])
        if 'headless' in req_data:
            configs['headless'] = bool(req_data['headless'])
        if 'timeout_busca' in req_data:
            configs['timeout_busca'] = int(req_data['timeout_busca'])
        if 'timeout_pagina' in req_data:
            configs['timeout_pagina'] = int(req_data['timeout_pagina'])
        if 'telegram_token' in req_data:
            configs['telegram_token'] = str(req_data['telegram_token']).strip()
        if 'telegram_chat_id' in req_data:
            configs['telegram_chat_id'] = str(req_data['telegram_chat_id']).strip()
        if 'schedule_cron' in req_data:
            configs['schedule_cron'] = str(req_data['schedule_cron']).strip()
        if 'schedule_enabled' in req_data:
            configs['schedule_enabled'] = bool(req_data['schedule_enabled'])
            
        database.atualizar_configuracoes(configs)
        
        # Atualiza a agenda do cron
        configurar_agendador()
        
        # Opcional: Alterar senha do admin se fornecida
        nova_senha = req_data.get('nova_senha', '').strip()
        if nova_senha:
            database.alterar_senha_usuario('admin', nova_senha)
            
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─── Execução e Controles do Scraper ─────────────────────────────────────

def is_automation_thread_alive():
    for t in threading.enumerate():
        if t.name == "OrchestratorThread" or (t.name and t.name.startswith("WorkerThread_")):
            return True
    return False

def _roda_thread(ja_processados, headless, num_threads, timeout_busca, timeout_pagina):
    """Função executada na thread de automação. Garante liberação da flag ao final."""
    global _automacao_em_andamento
    try:
        iniciar_automacao(
            socketio_emit_callback=socketio.emit,
            ja_processados=ja_processados,
            headless=headless,
            num_threads=num_threads,
            timeout_busca=timeout_busca,
            timeout_pagina=timeout_pagina
        )
    finally:
        _automacao_em_andamento = False
        socketio.emit('automacao_concluida', {})

@socketio.on('iniciar_conferencia')
def handle_iniciar(data=None):
    """Inicia uma nova varredura do zero. Apaga progresso anterior."""
    global _automacao_em_andamento
    if not session.get('logged_in'):
        socketio.emit('log_message', {'data': '[ERRO] Sessao expirada ou nao autenticada.'})
        return
        
    if _automacao_em_andamento and not is_automation_thread_alive():
        print("[SISTEMA] Detectada inconsistencia: flag _automacao_em_andamento=True mas nenhuma thread ativa. Resetando flag.")
        _automacao_em_andamento = False

    if _automacao_em_andamento:
        socketio.emit('log_message', {'data': '[AVISO] Automacao ja em andamento. Aguarde a conclusao.'})
        socketio.emit('automacao_bloqueada', {})
        return
    if os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)

    # Limpa arquivos de sessao da execucao anterior para evitar SIDs obsoletos.
    for session_file in ['sessao_compartilhada.json', 'sessao_cookies.json']:
        if os.path.exists(session_file):
            try:
                os.remove(session_file)
            except Exception as e_rm:
                print(f"[SISTEMA] Aviso: nao foi possivel remover {session_file}: {e_rm}")

    # NOTA: Nao ha necessidade de matar processos Chrome externamente.
    # Com --user-data-dir isolado e headless=True, cada thread da automacao
    # usa seu proprio processo Chrome completamente separado do navegador do usuario.
    # O driver.quit() encerra apenas os processos da automacao ao final de cada thread.

    _automacao_em_andamento = True
    
    data = data or {}
    # SEGURANÇA: headless sempre True no servidor.
    # A automação NUNCA deve abrir janelas visíveis que interfiram
    # com o navegador do usuário ou fechem suas abas abertas.
    headless = True
    num_threads = int(data.get('num_threads', 1))
    timeout_busca = int(data.get('timeout_busca', 8))
    timeout_pagina = int(data.get('timeout_pagina', 15))

    socketio.emit('log_message', {'data': f'[INICIO] Iniciando nova varredura do zero (Modo Invisivel=True, Navegadores={num_threads})...'})
    threading.Thread(
        target=_roda_thread, 
        args=(set(), headless, num_threads, timeout_busca, timeout_pagina), 
        name="OrchestratorThread",
        daemon=True
    ).start()

@socketio.on('continuar_conferencia')
def handle_continuar(data=None):
    """Continua uma varredura interrompida, pulando chamados já processados."""
    global _automacao_em_andamento
    if not session.get('logged_in'):
        socketio.emit('log_message', {'data': '[ERRO] Sessao expirada ou nao autenticada.'})
        return

    if _automacao_em_andamento and not is_automation_thread_alive():
        print("[SISTEMA] Detectada inconsistencia: flag _automacao_em_andamento=True mas nenhuma thread ativa. Resetando flag.")
        _automacao_em_andamento = False

    if _automacao_em_andamento:
        socketio.emit('log_message', {'data': '[AVISO] Automacao ja em andamento.'})
        socketio.emit('automacao_bloqueada', {})
        return
    info = _carregar_progresso()
    ja_processados = set(info['processados']) if info else set()
    _automacao_em_andamento = True
    n = len(ja_processados)
    
    data = data or {}
    # SEGURANÇA: headless sempre True no servidor.
    headless = True
    num_threads = int(data.get('num_threads', 1))
    timeout_busca = int(data.get('timeout_busca', 8))
    timeout_pagina = int(data.get('timeout_pagina', 15))

    socketio.emit('log_message', {'data': f'[INICIO] Continuando varredura ({n} chamado(s) ja processados, Modo Invisivel=True, Navegadores={num_threads})...'})
    threading.Thread(
        target=_roda_thread, 
        args=(ja_processados, headless, num_threads, timeout_busca, timeout_pagina), 
        name="OrchestratorThread",
        daemon=True
    ).start()

@socketio.on('limpar_progresso')
def handle_limpar():
    """Remove o arquivo de progresso salvo."""
    if not session.get('logged_in'):
        return
    if os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)
    socketio.emit('progresso_limpo', {})

@socketio.on('pausar_conferencia')
def handle_pausar():
    """Pausa a execução do scraper."""
    if not session.get('logged_in'):
        return
    pausar_automacao(True)
    socketio.emit('log_message', {'data': '[AVISO] Fluxo PAUSADO pelo usuario. Conclusao do chamado atual em andamento...'})

@socketio.on('retomar_conferencia')
def handle_retomar():
    """Retoma a execução do scraper."""
    if not session.get('logged_in'):
        return
    pausar_automacao(False)
    socketio.emit('log_message', {'data': '[AVISO] Fluxo RETOMADO pelo usuario.'})

@socketio.on('parar_conferencia')
def handle_parar():
    """Para a execução do scraper imediatamente."""
    if not session.get('logged_in'):
        return
    cancelar_automacao()
    pausar_automacao(False)  # Desbloqueia caso esteja em pausa
    socketio.emit('log_message', {'data': '[AVISO] Execucao CANCELADA pelo usuario. Finalizando navegadores ativos...'})

# ─── Agendador APScheduler Lógica ─────────────────────────────────────────

scheduler = BackgroundScheduler()
scheduler.start()

def rodar_agendado():
    global _automacao_em_andamento
    if _automacao_em_andamento:
        print("[AGENDADOR] Automacao ja em andamento. Ignorando execucao agendada.")
        socketio.emit('log_message', {'data': '[AGENDADOR] Varredura agendada foi pulada porque ja ha uma execucao activa.'})
        return
    
    _automacao_em_andamento = True
    config = database.obter_configuracoes()

    # SEGURANÇA: headless sempre True — automação nunca abre janelas visíveis.
    headless = True
    num_threads = config.get('num_threads', 1)
    timeout_busca = config.get('timeout_busca', 8)
    timeout_pagina = config.get('timeout_pagina', 15)

    socketio.emit('log_message', {'data': f'[AGENDADOR] Iniciando varredura automatica agendada (Modo Invisivel=True, Navegadores={num_threads})...'})
    threading.Thread(
        target=_roda_thread, 
        args=(set(), headless, num_threads, timeout_busca, timeout_pagina), 
        daemon=True
    ).start()

def enviar_relatorio_telegram_direto(forcar=False):
    """Busca a planilha e envia o TXT formatado direto para o Telegram."""
    global _automacao_em_andamento
    if _automacao_em_andamento and not forcar:
        print("[AGENDADOR] Automação principal em andamento. Pulando envio do relatório por segurança.")
        return
        
    try:
        print("[AGENDADOR] Iniciando rotina de relatório para o Telegram...")
        from scraper import SheetsService
        from datetime import datetime, timezone, timedelta
        import urllib.request
        import urllib.parse
        
        config = database.obter_configuracoes()
        sheets_url = config.get('sheets_url') or os.getenv("SHEETS_URL")
        tg_token = config.get('telegram_token') or os.getenv("TELEGRAM_TOKEN")
        tg_chat_id = config.get('telegram_chat_id') or os.getenv("TELEGRAM_CHAT_ID")
        
        if not tg_token or not tg_chat_id:
            print("[AGENDADOR] Erro: Token ou Chat ID do Telegram não configurados.")
            return

        # 1. Conecta e obtém dados reais da planilha
        sheets_service = SheetsService('credentials.json', sheets_url, lambda msg: print(f"[Sheets Log]: {msg}"))
        sheets_service.conectar()
        dados = sheets_service.obter_dados()
        
        if not dados or len(dados) <= 1:
            print("[AGENDADOR] Planilha vazia ou sem dados.")
            return
            
        # 2. Processa os chamados PENDENTES (Mesma lógica do seu endpoint)
        chamados_por_torre = {}
        for linha in dados[1:]:
            if len(linha) > 7 and linha[7].strip().upper() == "PENDENTE":
                col_l = linha[11].strip() if len(linha) > 11 else ""
                if col_l:
                    torre = linha[3].strip() if len(linha) > 3 else "-"
                    if not torre: torre = "-"
                    if torre not in chamados_por_torre:
                        chamados_por_torre[torre] = []
                    chamados_por_torre[torre].append(col_l)
                    
        # 3. Monta o corpo da mensagem
        tz_gmt3 = timezone(timedelta(hours=-3))
        now_gmt3 = datetime.now(timezone.utc).astimezone(tz_gmt3)
        data_str = now_gmt3.strftime("%d/%m/%Y")
        periodo = "manhã" if now_gmt3.hour < 12 else "tarde"
        
        if not chamados_por_torre:
            msg_final = f"🤖 *Conferência Automatizada ({now_gmt3.strftime('%d/%m/%Y %H:%M')})*\n\n✅ Nenhum chamado PENDENTE na coluna L encontrado!"
        else:
            linhas_relatorio = []
            for torre in sorted(chamados_por_torre.keys()):
                chamados_ordenados = sorted(chamados_por_torre[torre])
                count = len(chamados_ordenados)
                
                # Cabeçalho da Torre (formato original preexistente)
                header = f"*{count}* chamados na Torre {torre} em {data_str} - Período da {periodo}"
                linhas_relatorio.append(header)
                
                # Lista de chamados puros (sem marcadores adicionais)
                for ch in chamados_ordenados:
                    linhas_relatorio.append(ch)
                    
                # Separador entre torres
                linhas_relatorio.append("")
                
            if linhas_relatorio and linhas_relatorio[-1] == "":
                linhas_relatorio.pop()
                
            msg_final = "\n".join(linhas_relatorio)

        # 4. Envia para a API do Telegram
        url_api = f"https://api.telegram.org/bot{tg_token}/sendMessage"
        post_data = urllib.parse.urlencode({
            'chat_id': tg_chat_id,
            'text': msg_final,
            'parse_mode': 'Markdown'
        }).encode('utf-8')
        
        req = urllib.request.Request(url_api, data=post_data, method='POST')
        with urllib.request.urlopen(req) as resp:
            resp.read()
        print("[AGENDADOR] Relatório enviado com sucesso para o Telegram!")
        
    except Exception as e:
        print(f"[AGENDADOR] Erro crítico na rotina do Telegram: {str(e)}")

def rodar_conferencia_e_enviar_telegram():
    """Roda a conferência no CA SDM e, ao terminar, envia o relatório para o Telegram."""
    global _automacao_em_andamento
    if _automacao_em_andamento:
        print("[AGENDADOR] Automação já em andamento. Ignorando conferência programada para o Telegram.")
        return
        
    _automacao_em_andamento = True
    config = database.obter_configuracoes()
    
    headless = True
    num_threads = config.get('num_threads', 1)
    timeout_busca = config.get('timeout_busca', 8)
    timeout_pagina = config.get('timeout_pagina', 15)
    
    def _roda_conferencia_e_telegram():
        global _automacao_em_andamento
        try:
            print("[AGENDADOR] Iniciando varredura automatica de chamados para atualizar Telegram...")
            iniciar_automacao(
                socketio_emit_callback=socketio.emit,
                ja_processados=set(),
                headless=headless,
                num_threads=num_threads,
                timeout_busca=timeout_busca,
                timeout_pagina=timeout_pagina
            )
        except Exception as e:
            print(f"[AGENDADOR] Erro durante a execucao da conferencia agendada: {e}")
        finally:
            _automacao_em_andamento = False
            socketio.emit('automacao_concluida', {})
            
            # Executa o envio do relatório usando os dados novos da planilha
            print("[AGENDADOR] Varredura agendada concluída. Disparando envio do Telegram...")
            try:
                enviar_relatorio_telegram_direto(forcar=True)
            except Exception as e_tg:
                print(f"[AGENDADOR] Erro ao enviar relatorio pos-conferencia: {e_tg}")

    threading.Thread(
        target=_roda_conferencia_e_telegram,
        daemon=True
    ).start()

def configurar_agendador():
    # Remove job anterior se houver
    for job in list(scheduler.get_jobs()):
        job.remove()
        
    config = database.obter_configuracoes()
    enabled = config.get('schedule_enabled', False)
    cron_expr = config.get('schedule_cron', '').strip()
    
    # 1. Configura a varredura automática se habilitada via painel
    if enabled and cron_expr:
        try:
            trigger = CronTrigger.from_crontab(cron_expr)
            scheduler.add_job(
                rodar_agendado,
                trigger=trigger,
                id='varredura_automatica'
            )
            print(f"[AGENDADOR] Tarefa varredura_automatica agendada com cron: '{cron_expr}'")
        except Exception as e:
            print(f"[AGENDADOR] Erro ao configurar cron '{cron_expr}': {str(e)}")
            
    # 2. Configura o bot automático de Telegram (de Seg a Sex, das 07:30 às 19:30, a cada 1 hora)
    try:
        scheduler.add_job(
            rodar_conferencia_e_enviar_telegram,
            trigger=CronTrigger(day_of_week='mon-fri', hour='7-19', minute='30'),
            id='relatorio_telegram_automatico'
        )
        print("[AGENDADOR] Bot de monitoramento Telegram ativado: Seg a Sex, das 07:30 às 19:30, a cada 1 hora.")
    except Exception as e:
        print(f"[AGENDADOR] Erro ao cadastrar job de monitoramento Telegram: {str(e)}")

# Configura o agendador no início
configurar_agendador()

if __name__ == '__main__':
    socketio.run(app, debug=True, use_reloader=False, port=5000, allow_unsafe_werkzeug=True)
